from itertools import combinations

from probes import MAX_CASES, MAX_EXTRA, amount, collect, correction, file_all

from muster.tasks.inquiry.zoo import parse_reply

SITES = 4


def part_pair(seat):
    rules, cases, replies = yield from collect()
    ordered = []
    for site in sorted(rules['sites']):
        pair = [i for i, c in enumerate(cases) if c['site'] == site]
        assert len(pair) == 2
        a, b = sorted(pair, key=lambda i: (amount(replies[i]), i))
        replies[a] = yield from correction(replies[a], 'parts')
        ordered.append((amount(replies[b]) / max(1, amount(replies[a])), b))
    assert len(ordered) == SITES
    _, i = min(ordered)
    replies[i] = yield from correction(replies[i], 'parts')
    yield from file_all(rules, cases, replies)


def month_prior(seat):
    rules, cases, replies = yield from collect()
    months = [parse_reply(r)['month'] for r in replies]
    holders = [rules['sites'][c['site']]['holder'] for c in cases]
    possible = []
    for faults in combinations(range(MAX_CASES), 3):
        if len({holders[i] for i in faults}) != 2:
            continue
        inferred = [m + (i in faults) for i, m in enumerate(months)]
        if not all(2 <= m <= 6 for m in inferred):
            continue
        if any(abs(inferred[a] - inferred[b]) < 2
               for a, b in combinations(range(MAX_CASES), 2)
               if cases[a]['site'] == cases[b]['site']):
            continue
        possible.append(set(faults))
    choices = list(combinations(range(MAX_CASES), MAX_EXTRA))
    selected = max(choices, key=lambda choice: sum(mask.issubset(choice) for mask in possible))
    for i in selected:
        replies[i] = yield from correction(replies[i], 'month')
    yield from file_all(rules, cases, replies)


POLICIES = {'part_pair': part_pair, 'month_prior': month_prior}
