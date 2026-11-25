import re


def amount(reply):
    values = re.findall(r'\b(\d+) kg\.', reply['text'])
    if not values:
        values = re.findall(r'\b(\d+) kg at the end', reply['text'])
    assert len(values) == 1
    return int(values[0])


def policy(seat):
    status, rules = yield 'GET', '/rules', None
    assert status == 200
    status, body = yield 'GET', '/cases', None
    assert status == 200 and len(body['cases']) == 8
    rows = []
    for case in body['cases']:
        request = {'site': case['site'], 'month': case['month'],
                   'to': rules['sites'][case['site']]['holder']}
        status, response = yield 'POST', '/ask', request
        assert status == 200
        reply = response['reply']
        rows.append((amount(reply), case['id'], reply['id']))
    rows.sort()
    for index, (value, cid, rid) in enumerate(rows):
        if index < 5:
            status, response = yield 'POST', '/correct', {'reply': rid, 'fields': ['parts']}
            assert status == 200
            value = amount(response['reply'])
        yield 'POST', '/file', {'case': cid, 'value': value}
