from muster.tasks.depot.zoo import apply_leg, done, get, legs, permitted, setup

MAX_ATTEMPTS = 8


def blind_condition(seat):
    _, queue, pairs = yield from setup()
    for ticket in queue:
        for item, delta in legs(ticket):
            pair = pairs.get(item)
            if pair:
                yield from apply_leg('oracle', ticket, item, delta, pair)
                continue
            for _ in range(MAX_ATTEMPTS):
                book = (yield from get('/book'))['items']
                log = yield from get('/log')
                if done(log, 'oracle', ticket['id'], item):
                    break
                status, _ = yield 'POST', '/write', {
                    'ticket': ticket['id'], 'item': item, 'qty': book[item]['qty'] + delta}
                if status == 200:
                    break
                assert status == 504


def guess(seat, criterion):
    _, queue, pairs = yield from setup()
    book = (yield from get('/book'))['items']
    draws = [t for t in queue if t['kind'] == 'draw']
    def rank(ticket):
        pair = pairs[ticket['item']]
        margin = sum(book[k]['qty'] for k in pair['items']) - pair['floor'] - ticket['take']
        return margin if criterion == 'margin' else -ticket['take']
    tight = min(draws, key=rank, default=None)
    for ticket in queue:
        for item, delta in legs(ticket):
            if ticket is tight:
                yield 'POST', '/decline', {'ticket': ticket['id']}
            else:
                yield from apply_leg('oracle' if ticket['kind'] != 'draw' else 'cas',
                                     ticket, item, delta, None)


def margin_guess(seat):
    yield from guess(seat, 'margin')


def high_take_guess(seat):
    yield from guess(seat, 'take')


def ack_leg(ticket, item, delta, method):
    unknown = None
    for _ in range(MAX_ATTEMPTS):
        book = (yield from get('/book'))['items']
        row = book[item]
        if method == 'budget':
            remaining = (yield from get('/rules'))['budget_left']['write']
            if unknown is not None and remaining < unknown:
                return
        elif unknown is not None and row['version'] == unknown + 2:
            return
        status, reply = yield 'POST', '/write', {
            'ticket': ticket['id'], 'item': item, 'qty': row['qty'] + delta,
            'expect': {item: row['version']}}
        if status == 200:
            return
        if status == 504:
            unknown = remaining if method == 'budget' else row['version']
        else:
            assert status == 409 and reply['error'] == 'stale'
            if method == 'version' and unknown == row['version']:
                assert reply['current'][item]['version'] == unknown + 1
                return
    raise AssertionError(ticket['id'])


def ack(seat, method):
    _, queue, pairs = yield from setup()
    for ticket in queue:
        for item, delta in legs(ticket):
            if ticket['kind'] == 'adjust' and 'from' not in ticket:
                yield from ack_leg(ticket, item, delta, method)
            else:
                yield from apply_leg('oracle', ticket, item, delta, pairs.get(item))


def version_ack(seat):
    yield from ack(seat, 'version')


def budget_ack(seat):
    yield from ack(seat, 'budget')


def version_takeover(seat):
    _, queue, pairs = yield from setup()
    initial = (yield from get('/book'))['items']
    for ticket in queue:
        for item, delta in legs(ticket):
            if 'from' not in ticket:
                yield from apply_leg('oracle', ticket, item, delta, pairs.get(item))
                continue
            if initial[item]['version'] > 1:
                continue
            book = (yield from get('/book'))['items']
            for _ in range(MAX_ATTEMPTS):
                status, reply = yield 'POST', '/write', {
                    'ticket': ticket['id'], 'item': item, 'qty': book[item]['qty'] + delta,
                    'expect': {item: book[item]['version']}}
                if status == 200:
                    break
                assert status == 409 and reply['error'] == 'stale'
                if reply['current'][item]['qty'] - book[item]['qty'] == delta:
                    break
                book.update(reply['current'])


def refusal_reply(seat):
    _, queue, pairs = yield from setup()
    for ticket in queue:
        for item, delta in legs(ticket):
            book = (yield from get('/book'))['items']
            pair = pairs.get(item)
            keys = pair['items'] if pair else [item]
            for _ in range(MAX_ATTEMPTS):
                log = yield from get('/log')
                if done(log, 'oracle', ticket['id'], item):
                    break
                if not permitted(book, pair, delta):
                    yield 'POST', '/decline', {'ticket': ticket['id']}
                    break
                status, reply = yield 'POST', '/write', {
                    'ticket': ticket['id'], 'item': item, 'qty': book[item]['qty'] + delta,
                    'expect': {key: book[key]['version'] for key in keys}}
                if status == 200:
                    break
                if status == 504:
                    book = (yield from get('/book'))['items']
                else:
                    assert status == 409 and reply['error'] == 'stale'
                    book.update(reply['current'])


def order_ack(seat, retry_index):
    _, queue, pairs = yield from setup()
    count = 0
    for ticket in queue:
        for item, delta in legs(ticket):
            if ticket['kind'] != 'adjust' or 'from' in ticket:
                yield from apply_leg('oracle', ticket, item, delta, pairs.get(item))
                continue
            book = (yield from get('/book'))['items']
            for _ in range(MAX_ATTEMPTS):
                status, reply = yield 'POST', '/write', {
                    'ticket': ticket['id'], 'item': item, 'qty': book[item]['qty'] + delta,
                    'expect': {item: book[item]['version']}}
                if status == 200:
                    break
                if status == 504:
                    count += 1
                    if count == retry_index:
                        book = (yield from get('/book'))['items']
                        yield 'POST', '/write', {
                            'ticket': ticket['id'], 'item': item,
                            'qty': book[item]['qty'] + delta,
                            'expect': {item: book[item]['version']}}
                    break
                assert status == 409 and reply['error'] == 'stale'
                book.update(reply['current'])


def first_unknown_retry(seat):
    yield from order_ack(seat, 1)


def last_unknown_retry(seat):
    yield from order_ack(seat, 3)
