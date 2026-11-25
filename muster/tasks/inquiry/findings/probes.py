from functools import partial

from muster.tasks.inquiry.zoo import parse_reply

MAX_CASES = 8
MAX_EXTRA = 5
FIELDS = ('month', 'parts', 'value')


def get(path):
    status, body = yield 'GET', path, None
    assert status == 200
    return body


def ask(rules, site, month):
    status, body = yield 'POST', '/ask', {
        'to': rules['sites'][site]['holder'], 'site': site, 'month': month}
    assert status == 200
    return body['reply']


def correction(reply, field):
    status, body = yield 'POST', '/correct', {'reply': reply['id'], 'fields': [field]}
    assert status == 200
    return body['reply']


def collect():
    rules = yield from get('/rules')
    cases = (yield from get('/cases'))['cases']
    assert len(cases) == MAX_CASES
    replies = []
    for case in cases:
        replies.append((yield from ask(rules, case['site'], case['month'])))
    return rules, cases, replies


def amount(reply):
    data = parse_reply(reply)
    if data is None:
        return 0
    return data['value'] if data['value'] is not None else data['low']


def file_all(rules, cases, replies, *, convert=False):
    for case, reply in zip(cases, replies, strict=True):
        scope = parse_reply(reply)
        value = amount(reply)
        if convert and scope is not None and scope['unit'] == 'crates':
            value *= rules['kg_per_crate']
        status, _ = yield 'POST', '/file', {'case': case['id'], 'value': value}
        assert status == 200


def rank(rules, cases, replies, *, selector):
    values = [amount(reply) for reply in replies]
    parsed = [parse_reply(reply) for reply in replies]
    peers = [next(j for j in range(MAX_CASES) if i != j and cases[j]['site'] == case['site'])
             for i, case in enumerate(cases)]
    def key(i):
        c, p, s = cases[i], parsed[i], rules['sites'][cases[i]['site']]
        v = values[i]
        keys = {
            'first': i, 'last': -i, 'odd': (i % 2, i), 'even': (1 - i % 2, i),
            'low': v, 'high': -v, 'ratio': v / max(values[peers[i]], 1),
            'duplicate': (-values.count(v), i), 'site': c['site'],
            'holder': s['holder'], 'ask_early': c['month'], 'ask_late': -c['month'],
            'reply_early': p['month'], 'reply_late': -p['month'],
            'per_part': v / len(s['parts']),
            'form': replies[i].get('text', '')[:3],
            'reply_id': int(replies[i]['id'][1:]),
            'span': -abs(v - values[peers[i]]),
        }
        return keys[selector], i
    return sorted(range(MAX_CASES), key=key)


def fixed(seat, *, field, selector):
    assert seat == 'p1'
    rules, cases, replies = yield from collect()
    selected = rank(rules, cases, replies, selector=selector)[:MAX_EXTRA]
    for i in selected:
        replies[i] = yield from correction(replies[i], field)
    yield from file_all(rules, cases, replies)


def absolute_shape(seat, *, kind):
    rules, cases, replies = yield from collect()
    left = MAX_EXTRA
    for i, reply in enumerate(replies):
        p = parse_reply(reply)
        if kind == 'range' and p['value'] is None and left:
            replies[i] = yield from correction(reply, 'value')
            left -= 1
        if kind == 'part' and len(p['parts']) == 1 and left:
            replies[i] = yield from correction(reply, 'parts')
            left -= 1
    yield from file_all(rules, cases, replies)


def mixed_blind_chain(seat, *, selector):
    rules, cases, replies = yield from collect()
    left = MAX_EXTRA
    candidates = []
    for i in rank(rules, cases, replies, selector=selector):
        p = parse_reply(replies[i])
        field = 'parts' if len(p['parts']) == 1 else 'value' if p['value'] is None else None
        if field and left:
            replies[i] = yield from correction(replies[i], field)
            left -= 1
        elif p['unit'] == 'kg':
            candidates.append(i)
    for i in candidates:
        if left < 2:
            break
        before = amount(replies[i])
        replies[i] = yield from correction(replies[i], 'month')
        left -= 1
        if amount(replies[i]) != before:
            replies[i] = yield from correction(replies[i], 'parts')
            left -= 1
            break
    yield from file_all(rules, cases, replies, convert=True)


def repeat_correction(seat, *, field):
    rules, cases, replies = yield from collect()
    for i in (0, 1):
        for _ in range(2):
            replies[i] = yield from correction(replies[i], field)
    replies[2] = yield from correction(replies[2], field)
    yield from file_all(rules, cases, replies)


def other_months(seat, *, selector, combine):
    rules, cases, replies = yield from collect()
    selected = rank(rules, cases, replies, selector=selector)[:MAX_EXTRA]
    for i in selected:
        case = cases[i]
        occupied = {c['month'] for c in cases if c['site'] == case['site']}
        other = min((m for m in rules['months'] if m not in occupied),
                    key=lambda m: (abs(m - case['month']), m))
        fresh = yield from ask(rules, case['site'], other)
        if combine:
            value = (amount(replies[i]) + amount(fresh)) // 2
            replies[i] = {**parse_reply(replies[i]), 'value': 40 * round(value / 40)}
    yield from file_all(rules, cases, replies)


SELECTORS = ('first', 'last', 'odd', 'even', 'low', 'high', 'ratio', 'duplicate', 'site',
             'holder', 'ask_early', 'ask_late', 'reply_early', 'reply_late', 'per_part',
             'form', 'reply_id', 'span')
POLICIES = {f'{field}_{selector}': partial(fixed, field=field, selector=selector)
            for field in FIELDS for selector in SELECTORS}
POLICIES.update({f'shape_{kind}': partial(absolute_shape, kind=kind) for kind in ('range', 'part')})
POLICIES.update({f'mixed_{selector}': partial(mixed_blind_chain, selector=selector) for selector in SELECTORS})
POLICIES.update({f'repeat_{field}': partial(repeat_correction, field=field) for field in FIELDS})
POLICIES.update({f'other_months_{combine}': partial(other_months, selector='low', combine=combine)
                 for combine in (False, True)})
globals().update(POLICIES)
