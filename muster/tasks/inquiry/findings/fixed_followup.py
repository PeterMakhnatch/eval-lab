from muster.tasks.inquiry.zoo import parse_reply

MAX_CASES = 8


def policy(seat):
    status, rules = yield 'GET', '/rules', None
    assert status == 200
    status, body = yield 'GET', '/cases', None
    assert status == 200
    cases = body['cases']
    assert len(cases) == MAX_CASES
    for case in cases:
        status, body = yield 'POST', '/ask', {
            'to': rules['sites'][case['site']]['holder'], 'site': case['site'], 'month': case['month']}
        assert status == 200
        reply = body['reply']
        first = parse_reply(reply)
        plan = []
        if first['month'] != case['month']:
            plan.extend(('month', 'parts'))
        elif len(first['parts']) == 1:
            plan.append('parts')
        elif first['value'] is None:
            plan.append('value')
        for field in plan:
            status, body = yield 'POST', '/correct', {'reply': reply['id'], 'fields': [field]}
            assert status == 200
            reply = body['reply']
        final = parse_reply(reply)
        value = final['value'] if final['value'] is not None else final['low']
        if first['unit'] == 'crates':
            value *= rules['kg_per_crate']
        status, _ = yield 'POST', '/file', {'case': case['id'], 'value': value}
        assert status == 200
