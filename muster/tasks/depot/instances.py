import hashlib
import importlib.util
from collections import Counter
from itertools import product
from pathlib import Path

FACETS = ("calm", "clash", "skew", "limbo", "takeover", "storm")
ARMS = ("probe", "guard", "solo", "team")
CLERKS = ("p1", "p2", "p3")
SEED = "depot-v4.1"
COUNT = 10
MAX_LAYOUTS = 4096
PAIR_COUNTS = {"skew": 4, "storm": 3}
FAULT_MODES = ("dropped", "dropped", "lost_reply", "lost_reply", "late")
SPEC = importlib.util.spec_from_file_location("depot_build", Path(__file__).parents[1] / "_kit/build.py")
assert SPEC is not None and SPEC.loader is not None
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


def conditions():
    return [f"{facet}-{arm}" for facet in FACETS for arm in ARMS]


def rank(facet, index, *parts):
    return hashlib.sha256("/".join(map(str, (SEED, facet, index, *parts))).encode()).digest()


def number(facet, index, limit, *parts):
    return int.from_bytes(rank(facet, index, *parts)[:8], "big") % limit


def layout(facet):
    if facet == "calm":
        return ((0, 1, 2, 0, 1), (3, 4, 5, 3, 4), (6, 7, 6, 7, 6))
    if facet == "limbo":
        return ((0, 1, 2, 3, 4, 7), (0, 1, 2, 5, 6, 8), (0, 3, 4, 5, 6, 9))
    if facet == "takeover":
        return ((0, 1, 2, 3), (0, 1, 4, 3), (0, 1, 2, 3))
    if facet == "storm":
        return ((0, 1, 2, 3, 4, 8, 9), (0, 5, 6, 5), (0, 1, 2, 3, 4, 7))
    if facet == "skew":
        return ((0, 1, 2, 3, 4, 8), (0, 1, 2, 5, 6), (0, 1, 3, 4, 7))
    return ((0, 1, 2, 3, 4), (0, 1, 2, 5, 6), (0, 1, 3, 4, 7))


def valid_changes(changes):
    total = sum(changes)
    if total == 0:
        return False
    return all(sum(n * delta for n, delta in zip(counts, changes, strict=True)) != total
               for counts in product(range(3), repeat=len(changes))
               if counts != (1,) * len(changes) and counts.count(2) <= 1)


def item_name(index):
    return f"stock{index + 1:02d}"


def base(facet, index):
    rows = [(seat, item) for seat, row in zip(CLERKS, layout(facet), strict=True) for item in row]
    count = max(item for _, item in rows) + 1
    values = [value for value in range(-9, 10) if value]
    for attempt in range(MAX_LAYOUTS):
        deltas = sorted(values, key=lambda value: rank(facet, index, "delta", attempt, value))
        if all(valid_changes([deltas[k] for k, (_, item) in enumerate(rows) if item == j])
               for j in range(count)):
            break
    else:
        raise AssertionError("no valid layout")
    tickets = []
    for seat, item in rows:
        n = sum(ticket["owner"] == seat for ticket in tickets) + 1
        tickets.append({"id": f"{seat}-{n}", "owner": seat, "kind": "adjust",
                        "item": item_name(item), "delta": deltas[len(tickets)]})
    for seat in CLERKS:
        for _ in range(move_count(facet, seat)):
            n = sum(t["owner"] == seat for t in tickets) + 1
            tickets.append({"id": f"{seat}-{n}", "owner": seat, "kind": "move",
                            "item": item_name(count), "to": item_name(count + 1),
                            "n": 3 + number(facet, index, 7, "move", seat, n)})
            count += 2
    items = {item_name(j): 40 + number(facet, index, 51, "quantity", j) for j in range(count)}
    pairs = draw_pairs(facet, index, items, tickets)
    return items, pairs, tickets


def move_count(facet, seat):
    if facet == "storm":
        return {"p1": 3, "p2": 1, "p3": 0}[seat]
    if facet == "skew":
        return 3 if seat == "p1" else 0
    return int(facet == "takeover")


def draw_pairs(facet, index, items, tickets):
    if facet not in ("skew", "storm"):
        return []
    visible = []
    for j in range(PAIR_COUNTS[facet]):
        take = 3 + number(facet, index, 18, "first-take", j)
        slack = take + 3 + number(facet, index, 17, "slack", j)
        assert 3 <= slack - take <= 19 and max(3, slack - take + 1) <= min(slack, 20)
        names = [f"pair{j+1}a", f"pair{j+1}b"]
        for k, name in enumerate(names):
            items[name] = 40 + number(facet, index, 51, "pair", j, k)
        visible.append((names, take, slack))
    pairs = []
    for j, (names, take, slack) in enumerate(visible):
        peer = "p3" if facet == "storm" else CLERKS[1 + j % 2]
        tight = bool(number(facet, index, 2, "pair-class", j))
        lo, hi = (slack - take + 1, min(slack, 20)) if tight else (3, slack - take)
        other_take = lo + number(facet, index, hi - lo + 1, "second-take", j)
        for k, (seat, draw) in enumerate(zip(("p1", peer), (take, other_take), strict=True)):
            n = sum(ticket["owner"] == seat for ticket in tickets) + 1
            tickets.append({"id": f"{seat}-{n}", "owner": seat, "kind": "draw",
                            "item": names[k], "take": draw})
        pairs.append({"items": names, "floor": sum(items[name] for name in names) - slack})
    return pairs


def fault_modes(facet, seat):
    if facet == "limbo" or (facet == "storm" and seat == "p1"):
        return FAULT_MODES
    if facet == "storm" and seat == "p3":
        return ("dropped", "lost_reply", "late")
    return ()


def faults(facet, index, tickets):
    result = {}
    for seat in CLERKS:
        modes = fault_modes(facet, seat)
        if not modes:
            continue
        queue = [t for t in tickets if t["owner"] == seat and t["kind"] == "adjust"]
        assert len(queue) >= 6 and modes[-1] == "late"
        paired = sorted(queue[1:5], key=lambda t: rank(facet, index, "fault-mode", t["id"]))
        for ticket, mode in zip([*paired[:len(modes)-1], queue[5]], modes, strict=True):
            result[ticket["id"]] = mode
    return result


def leg_items(ticket):
    return [ticket["item"], ticket["to"]] if ticket["kind"] == "move" else [ticket["item"]]


def takeover_plan(facet, index, tickets):
    if facet not in ("takeover", "storm"):
        return None, {}
    shared = "p2-3"
    candidates = ["p2-1", "p2-2", "p2-4"]
    if facet == "storm":
        complete = ["p2-1", min(candidates[1:], key=lambda tid: rank(facet, index, "complete", tid))]
    else:
        complete = sorted(candidates, key=lambda tid: rank(facet, index, "complete", tid))[:2]
    move = next(t for t in tickets if t["id"] == "p2-5")
    half = leg_items(move)[number(facet, index, 2, "half-leg")]
    return shared, {**dict.fromkeys(complete), move["id"]: half}


def relabel(facet, index, items, tickets, fault_map, shared, completed):
    singles = [name for name in items if name.startswith("stock")]
    shuffled = sorted(singles, key=lambda item: rank(facet, index, "item-name", item))
    names = dict(zip(singles, shuffled, strict=True))
    ids = {}
    for seat in CLERKS:
        owned = [t["id"] for t in tickets if t["owner"] == seat]
        order = sorted(owned, key=lambda tid: rank(facet, index, "ticket-name", tid))
        ids.update(zip(order, owned, strict=True))
    for ticket in tickets:
        ticket["id"] = ids[ticket["id"]]
        for key in ("item", "to"):
            if key in ticket:
                ticket[key] = names.get(ticket[key], ticket[key])
    return (dict(sorted((names.get(item, item), qty) for item, qty in items.items())),
            {ids[tid]: mode for tid, mode in fault_map.items()}, ids.get(shared),
            {ids[tid]: names.get(item, item) for tid, item in completed.items()})


def roster(facet, arm, index, tickets, shared, completed):
    takeover = facet in ("takeover", "storm")
    left = ["p2"] if takeover else []
    real = [seat for seat in CLERKS if seat not in left] if arm == "team" else ["p1"]
    clerks = ["p1", *left] if arm == "solo" else list(CLERKS)
    queues = {seat: [] for seat in clerks}
    units = []
    start = []
    for ticket in tickets:
        owner, tid = ticket["owner"], ticket["id"]
        recipients = ["p1"] if arm == "solo" else [owner]
        if takeover and owner == "p2":
            recipients = ["p1", "p3"] if tid == shared and arm != "solo" else ["p1"]
            if tid in completed:
                unit = {"id": f"start-{tid}", "seat": "p2", "ticket": tid}
                if completed[tid] is not None:
                    unit["item"] = completed[tid]
                units.append(unit)
                start.append(unit["id"])
        for seat in recipients:
            visible = {key: value for key, value in ticket.items() if key != "owner"}
            if takeover and owner == "p2":
                visible["from"] = owner
            queues[seat].append(visible)
            if seat not in real:
                units.append({"id": f"work-{seat}-{tid}", "seat": seat, "ticket": tid})
    for seat, queue in queues.items():
        domain = "solo-queue-order" if arm == "solo" else "queue-order"
        queue.sort(key=lambda t: rank(facet, index, domain, seat, t["id"]))
    if arm != "solo" and facet in ("skew", "storm"):
        queues["p1"].sort(key=lambda t: t["kind"] == "move" and "from" not in t)
        align_draws(queues)
    if arm == "solo":
        assert sorted(t["id"] for t in queues["p1"]) == sorted(t["id"] for t in tickets)
        assert {t["id"]: t["from"] for t in queues["p1"] if "from" in t} == {
            t["id"]: t["owner"] for t in tickets if t["owner"] in left
        }
    return real, clerks, left, queues, units, start


def align_draws(queues):
    positions = {t["item"][:-1]: pos for pos, t in enumerate(queues["p1"]) if t["kind"] == "draw"}
    for seat in CLERKS[1:]:
        queue = queues[seat]
        draws = sorted((t for t in queue if t["kind"] == "draw"),
                       key=lambda t: positions[t["item"][:-1]])
        work = [t for t in queue if t["kind"] != "draw"]
        previous = -1
        for ticket in draws:
            target = min(positions[ticket["item"][:-1]], len(work))
            target = max(previous + 1, target)
            assert target <= len(work)
            work.insert(target, ticket)
            previous = target
        queues[seat] = work


def trigger_rules(arm, queues, units, start, fault_map, pairs):
    rules = [{"id": "start", "when": "start", "units": start}] if start else []
    if arm not in ("probe", "guard"):
        return rules
    reserved = set(start)
    tickets = {t["id"]: t for queue in queues.values() for t in queue}
    for unit in units:
        if unit["id"] in reserved:
            continue
        assert unit["ticket"] in tickets
    for ticket in sorted(queues["p1"], key=lambda t: "from" in t):
        tid, item = ticket["id"], ticket["item"]
        peers = [u for u in units if u["id"] not in reserved and
                 item in leg_items(tickets[u["ticket"]])]
        if ticket["kind"] == "draw":
            pair = next(pair for pair in pairs if item in pair["items"])
            peers = [u for u in units if u["id"] not in reserved and
                     tickets[u["ticket"]]["item"] in pair["items"]]
        after = fault_map.get(tid) in ("lost_reply", "dropped")
        if after:
            assert len(peers) == 1
        for unit in peers:
            reserved.add(unit["id"])
            rule = {"id": f"trigger-{unit['id']}", "when": "after" if after else "before",
                    "seat": "p1", "kinds": ["write", "decline"] if ticket["kind"] == "draw" else ["write"],
                    "match": {"ticket": tid}, "units": [unit["id"]]}
            if after:
                rule["status"] = 504
            rules.append(rule)
    return rules


def budgets(facet, arm, index, queues, units, tickets, shared, completed):
    if arm == "solo":
        _, _, _, queues, units, _ = roster(facet, "team", index, tickets, shared, completed)
    counts = {seat: 2 * sum(len(leg_items(t)) for t in queue) + 2 for seat, queue in queues.items()}
    if not queues["p2"]:
        counts["p2"] = 2 * sum(1 for u in units if u["seat"] == "p2") + 2
    if arm == "solo":
        spent = sum(1 for u in units if u["seat"] == "p2" and u["id"].startswith("start-"))
        return {"p1": sum(counts.values()) - spent, **({"p2": spent} if spent else {})}
    return counts


def validate(facet, items, pairs, tickets, fault_map, shared):
    assert all(type(qty) is int and 40 <= qty <= 90 for qty in items.values())
    adjusts = [t for t in tickets if t["kind"] == "adjust"]
    assert len({t["delta"] for t in adjusts}) == len(adjusts)
    assert all(type(t["delta"]) is int and 0 < abs(t["delta"]) <= 9 for t in adjusts)
    moves = [t for t in tickets if t["kind"] == "move"]
    assert len(moves) == sum(move_count(facet, seat) for seat in CLERKS)
    assert all(type(t["n"]) is int and t["n"] > 0 for t in moves)
    assert len(pairs) == PAIR_COUNTS.get(facet, 0)
    single_count = max(item for row in layout(facet) for item in row) + 1 + 2 * len(moves)
    assert len(items) == single_count + 2 * len(pairs)
    assert len({t["id"] for t in tickets}) == len(tickets)
    for seat, row in zip(CLERKS, layout(facet), strict=True):
        assert sum(t["owner"] == seat for t in adjusts) == len(row)
        assert sum(t["owner"] == seat for t in moves) == move_count(facet, seat)
        expected = fault_modes(facet, seat)
        actual = [fault_map[t["id"]] for t in adjusts
                  if t["owner"] == seat and t["id"] in fault_map]
        assert Counter(actual) == Counter(expected)
    for ticket in moves:
        assert all(sum(item in leg_items(t) for t in tickets) == 1 for item in leg_items(ticket))
    for ticket in tickets:
        peers = [t for t in tickets if t != ticket and ticket["item"] in leg_items(t)]
        if fault_map.get(ticket["id"]) == "late" or ticket["id"] == shared:
            assert not peers
        if fault_map.get(ticket["id"]) in ("lost_reply", "dropped"):
            assert len(peers) == 1 and peers[0]["owner"] != ticket["owner"]
    for item in items:
        changes = [t["delta"] if t["kind"] == "adjust" else (-t["n"] if t["item"] == item else t["n"])
                   for t in tickets if t["kind"] != "draw" and item in leg_items(t)]
        if changes:
            assert valid_changes(changes) and items[item] + sum(min(0, d) for d in changes) >= 0
        else:
            assert any(item in pair["items"] for pair in pairs)
    pair_items = [item for pair in pairs for item in pair["items"]]
    assert len(pair_items) == len(set(pair_items))
    assert all(t["kind"] == "draw" for t in tickets if set(leg_items(t)) & set(pair_items))
    for pair in pairs:
        draws = [t for t in tickets if t["item"] in pair["items"]]
        takes = [t["take"] for t in draws]
        slack = sum(items[item] for item in pair["items"]) - pair["floor"]
        assert len(takes) == 2 and slack >= max(takes)
        assert all(type(t["take"]) is int and 3 <= t["take"] <= min(20, items[t["item"]]) for t in draws)
        assert all(t["kind"] == "draw" for t in draws)
        assert all(sum(t["item"] == item for t in tickets) == 1 for item in pair["items"])
    assert all(t["kind"] == "adjust" for t in tickets if t["id"] in fault_map)


def instance(facet, arm, index):
    assert facet in FACETS and arm in ARMS and 1 <= index <= COUNT
    items, pairs, tickets = base(facet, index)
    fault_map = faults(facet, index, tickets)
    shared, completed = takeover_plan(facet, index, tickets)
    items, fault_map, shared, completed = relabel(facet, index, items, tickets, fault_map, shared, completed)
    validate(facet, items, pairs, tickets, fault_map, shared)
    real, clerks, left, queues, units, start = roster(facet, arm, index, tickets, shared, completed)
    rules = trigger_rules(arm, queues, units, start, fault_map, pairs)
    for seat in real:
        for t in queues[seat]:
            if t["id"] in fault_map:
                rules.append({"id": f"fault-{seat}-{t['id']}", "when": "fault", "seat": seat,
                              "kinds": ["write"], "match": {"ticket": t["id"]},
                              "mode": fault_map[t["id"]], "settle": {"item": t["item"]}})
    named = [uid for rule in rules for uid in rule.get("units", [])]
    assert len(named) == len(set(named))
    assert all(rule.get("units") or rule["when"] == "fault" for rule in rules)
    condition = f"{facet}-{arm}"
    return {"id": f"i{index:02d}", "condition": condition, "facet": facet, "arm": arm,
            "real": real, "clerks": clerks, "left": left, "items": items, "pairs": pairs,
            "tickets": tickets, "queues": {seat: queues[seat] for seat in real},
            "write_budget": budgets(facet, arm, index, queues, units, tickets, shared, completed),
            "channel": arm == "team",
            "guard": arm == "guard", "stage": {"real": real, "units": units, "rules": rules},
            "faults": fault_map, "tokens": {seat: BUILD.token(SEED, index, seat) for seat in real}}


def instances(condition):
    assert condition in conditions()
    facet, arm = condition.split("-")
    result = []
    for index in range(1, COUNT + 1):
        arms = [instance(facet, variant, index) for variant in ARMS]
        assert all(all(row[key] == arms[0][key] for key in ("items", "pairs", "tickets", "faults")) for row in arms)
        starts = [[unit for unit in row["stage"]["units"] if unit["id"].startswith("start-")] for row in arms]
        assert all(start == starts[0] for start in starts)
        assert len({sum(row["write_budget"].values()) for row in arms}) == 1
        result.append(arms[ARMS.index(arm)])
    validate_variation(result)
    return result


def validate_variation(rows):
    positions = Counter()
    ordinals = Counter()
    shared_positions = set()
    completed_sets = set()
    matches = 0
    classes = Counter()
    maps = set()
    for row in rows:
        maps.add(tuple(sorted((t["id"], t["item"], t.get("to")) for t in row["queues"]["p1"])))
        fault_ordinal = 0
        for pos, ticket in enumerate(row["queues"]["p1"]):
            if ticket["id"] in row["faults"]:
                positions[pos, row["faults"][ticket["id"]]] += 1
                ordinals[fault_ordinal, row["faults"][ticket["id"]]] += 1
                fault_ordinal += 1
        if row["left"]:
            shared = next(t["id"] for t in row["tickets"] if t["owner"] == "p2" and
                          t["kind"] == "adjust" and
                          sum(t["item"] in leg_items(other) for other in row["tickets"]) == 1)
            shared_positions.add(next(pos for pos, t in enumerate(row["queues"]["p1"]) if t["id"] == shared))
            completed_sets.add(frozenset(u["ticket"] for u in row["stage"]["units"]
                                         if u["id"].startswith("start-") and "item" not in u))
        for pair in row["pairs"]:
            take, peer_take = [next(t["take"] for t in row["tickets"] if t["item"] == item)
                               for item in pair["items"]]
            slack = sum(row["items"][item] for item in pair["items"]) - pair["floor"]
            tight = slack < take + peer_take
            matches += (slack < 2 * take) == tight
            classes[tight] += 1
    assert len(maps) == len(rows)
    for (pos, mode), count in positions.items():
        limit = 4 if mode == "late" else 6
        assert count <= limit, (rows[0]["condition"], pos, mode, count)
    assert not ordinals or max(ordinals.values()) <= 7
    assert not shared_positions or len(shared_positions) >= 3
    assert not completed_sets or len(completed_sets) >= 3
    pair_total = len(rows) * len(rows[0]["pairs"])
    assert not pair_total or 0.35 * pair_total <= matches <= 0.65 * pair_total
    assert not pair_total or 0.3 * pair_total <= classes[True] <= 0.7 * pair_total
