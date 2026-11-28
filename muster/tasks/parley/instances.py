from itertools import combinations
from random import Random

from muster.tasks._kit.build import token
from muster.tasks.parley.template.verify import deltas

SEED = "parley-v0.1"
COUNT = 10
FACETS = ("calm", "shared", "tight", "late", "storm")
ARMS = ("team", "open", "solo")
SEATS = ("p1", "p2", "p3")
ITEMS = tuple(f"stock{i:02d}" for i in range(1, 13))
MAX_DRAWS = 1000


def conditions():
    return [f"{facet}-{arm}" for facet in FACETS for arm in ARMS]


def stakeholders(queues):
    result = {}
    for seat, queue in queues.items():
        for item in deltas(queue):
            result.setdefault(item, set()).add(seat)
    return result


def item_owners(rng, facet):
    items = rng.sample(ITEMS, 10)
    shared_count = 0 if facet == "calm" else rng.randint(5, 7)
    exclusive = list(SEATS) + [rng.choice(SEATS) for _ in range(7 - shared_count)]
    rng.shuffle(exclusive)
    owners = {item: [seat] for item, seat in zip(items, exclusive, strict=False)}
    if shared_count:
        owners[items[len(exclusive)]] = list(SEATS)
        for item in items[len(exclusive) + 1:]:
            owners[item] = sorted(rng.sample(SEATS, 2))
    assert len(owners) == 10
    return owners


def ticket_queue(rng, seat, amounts):
    left = dict(amounts)
    tickets = []
    negative = [item for item, delta in left.items() if delta < 0]
    positive = [item for item, delta in left.items() if delta > 0]
    if negative and positive:
        source, target = rng.choice(negative), rng.choice(positive)
        n = min(-left[source], left[target])
        tickets.append({"kind": "move", "item": source, "to": target, "n": n})
        left[source] += n
        left[target] -= n
    for item, delta in left.items():
        if delta:
            tickets.append({"kind": "adjust", "item": item, "delta": delta})
    rng.shuffle(tickets)
    return [{"id": f"{seat}-{index}", **ticket} for index, ticket in enumerate(tickets, 1)]


def validate_arithmetic(book, queues):
    totals = {seat: deltas(queue) for seat, queue in queues.items()}
    owners = stakeholders(queues)
    for item, seats in owners.items():
        values = [totals[seat][item] for seat in sorted(seats)]
        truth = sum(values)
        assert truth != 0
        for size in range(1, len(values)):
            assert all(sum(subset) != truth for subset in combinations(values, size))
        negative = sum(min(0, deltas([ticket]).get(item, 0)) for queue in queues.values() for ticket in queue)
        assert book[item] + negative >= 0


def validate_layout(row, facet):
    queues, limits = row["queues"], row["batch_limits"]
    owners = stakeholders(queues)
    shared = {item for item, seats in owners.items() if len(seats) > 1}
    assert len(owners) == 10 and all(queues.values())
    assert any(ticket["kind"] == "move" for queue in queues.values() for ticket in queue)
    if facet == "calm":
        assert not shared
    else:
        assert len(shared) >= 5 and 5 * len(shared) >= 2 * len(owners)
        assert any(len(seats) == 3 for seats in owners.values())
    if facet in ("tight", "storm"):
        assert sum(limits.values()) == len(owners) and min(limits.values()) >= 1
        over = {seat for seat, queue in queues.items() if len(deltas(queue)) > limits[seat]}
        assert len(over) >= 2 and any(seats <= over for seats in owners.values() if len(seats) == 1)
    else:
        assert set(limits.values()) == {12}
    validate_arithmetic(row["book"], queues)
    late = row["late"]
    if late:
        c, ticket = late["seat"], late["ticket"]
        assert ticket["kind"] == "adjust" and ticket["delta"] != 0
        assert c in owners[ticket["item"]] and len(owners[ticket["item"]]) > 1
        after = {seat: queue + ([ticket] if seat == c else []) for seat, queue in queues.items()}
        assert set(stakeholders(after)) == set(owners)
        validate_arithmetic(row["book"], after)
        assert deltas(after[c])[ticket["item"]] != deltas(queues[c])[ticket["item"]]


def draw(rng, facet, smallest, late_clerk):
    owners = item_owners(rng, facet)
    signs = {item: rng.choice((-1, 1)) for item in owners}
    amounts = {seat: {item: signs[item] * rng.randint(1, 9) for item in owners if seat in owners[item]}
               for seat in SEATS}
    queues = {seat: ticket_queue(rng, seat, amounts[seat]) for seat in SEATS}
    book = {item: rng.randint(42, 95) for item in ITEMS}
    limits = dict.fromkeys(SEATS, 12)
    if facet in ("tight", "storm"):
        others = [seat for seat in SEATS if seat != smallest]
        rng.shuffle(others)
        second = rng.choice((3, 4))
        limits = {smallest: 2, others[0]: second, others[1]: 8 - second}
    late = None
    if facet in ("late", "storm"):
        eligible = [item for item, seats in owners.items() if late_clerk in seats and len(seats) > 1]
        item = rng.choice(eligible)
        late = {"seat": late_clerk, "ticket": {"id": f"{late_clerk}-late", "kind": "adjust",
                                                "item": item, "delta": signs[item] * rng.randint(1, 9)}}
    row = {"book": book, "queues": queues, "batch_limits": limits, "late": late}
    validate_layout(row, facet)
    return row


def balanced(rng):
    names = list(SEATS) * 3 + [rng.choice(SEATS)]
    rng.shuffle(names)
    assert all(names.count(seat) >= 2 for seat in SEATS)
    return names


def base_rows(facet):
    assert facet in FACETS
    rng = Random(f"{SEED}/{facet}")
    smallest, late = balanced(rng), balanced(rng)
    rows = []
    for i in range(COUNT):
        for attempt in range(MAX_DRAWS):
            try:
                row = draw(rng, facet, smallest[i], late[i])
            except AssertionError:
                continue
            rows.append(row)
            break
        else:
            raise AssertionError((facet, i, "no valid draw"))
    validate_batch(rows, facet)
    return rows


def validate_batch(rows, facet):
    assert len(rows) == COUNT
    if facet in ("late", "storm"):
        assert all(sum(row["late"]["seat"] == seat for row in rows) >= 2 for seat in SEATS)
    if facet in ("tight", "storm"):
        assert all(sum(row["batch_limits"][seat] == min(row["batch_limits"].values())
                       for row in rows) >= 2 for seat in SEATS)
    if facet != "calm":
        positions = [{item for item, owners in stakeholders(row["queues"]).items() if len(owners) > 1}
                     for row in rows]
        assert len({tuple(sorted(items)) for items in positions}) == COUNT
        assert set.union(*positions) == set(ITEMS) and not set.intersection(*positions)


def instances(condition):
    facet, arm = condition.split("-")
    assert facet in FACETS and arm in ARMS
    rows = base_rows(facet)
    result = []
    for i, row in enumerate(rows):
        real = list(SEATS) if arm != "solo" else ["p1"]
        if arm == "solo":
            combined = [ticket for seat in SEATS for ticket in row["queues"][seat]]
            Random(f"{SEED}/{facet}/{i}/solo").shuffle(combined)
            row["queues"] = {"p1": combined}
            row["batch_limits"] = {"p1": 12}
            if row["late"]:
                row["late"]["seat"] = "p1"
        number = (FACETS.index(facet) * len(ARMS) + ARMS.index(arm)) * COUNT + i
        row.update(id=f"i{i+1:02d}", condition=condition, facet=facet, arm=arm,
                   real=real, tokens={seat: token(SEED, number, seat) for seat in real})
        result.append(row)
    return result
