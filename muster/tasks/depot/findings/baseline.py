from functools import partial

from muster.tasks.depot.zoo import apply_leg, legs, setup


def settle(seat, *, mode, prior_fault=False, prior_takeover=False, draw_choice=None):
    _, queue, pairs = yield from setup()
    draws = [t for t in queue if t['kind'] == 'draw']
    own_adjusts = [t for t in queue if t['kind'] == 'adjust' and 'from' not in t]
    inherited = [t for t in queue if 'from' in t]
    if draws and draw_choice == 'slack':
        status, result = yield 'GET', '/book', None
        assert status == 200
        draw_choice = min(range(len(draws)), key=lambda i: sum(
            result['items'][s]['qty'] for s in pairs[draws[i]['item']]['items']
        ) - pairs[draws[i]['item']]['floor'])
    for t in queue:
        if t in draws and draw_choice is not None:
            if draws.index(t) == draw_choice:
                yield 'POST', '/decline', {'ticket': t['id']}
            else:
                item, delta = legs(t)[0]
                yield from apply_leg('reread', t, item, delta, pairs[item])
            continue
        if prior_takeover and t in inherited and inherited.index(t) in (0, 1, 2):
            continue
        for i, (item, delta) in enumerate(legs(t)):
            if prior_takeover and t in inherited and t['kind'] == 'move' and i == 0:
                continue
            if prior_fault and t in own_adjusts:
                adjust_index = own_adjusts.index(t)
                if adjust_index in (1, 2, len(own_adjusts) - 1):
                    yield from fault_prior(t, item, delta, mode, adjust_index == 2)
                    continue
            yield from apply_leg(mode, t, item, delta, pairs.get(item))


def fault_prior(ticket, item, delta, mode, retry):
    for _ in range(8):
        status, result = yield 'GET', '/book', None
        assert status == 200
        row = result['items'][item]
        body = {'ticket': ticket['id'], 'item': item, 'qty': row['qty'] + delta}
        if mode == 'cas':
            body['expect'] = {item: row['version']}
        status, reply = yield 'POST', '/write', body
        if status == 200 or status == 504 and not retry:
            return
        if status != 504 and reply.get('error') != 'stale':
            return


fault_no_skill = partial(settle, mode='reread', prior_fault=True)
fault_cas = partial(settle, mode='cas', prior_fault=True)
takeover_no_skill = partial(settle, mode='reread', prior_takeover=True)
takeover_cas = partial(settle, mode='cas', prior_takeover=True)
first_no_skill = partial(settle, mode='reread', draw_choice=0)
second_no_skill = partial(settle, mode='reread', draw_choice=1)
slack_no_skill = partial(settle, mode='reread', draw_choice='slack')
first_cas = partial(settle, mode='cas', draw_choice=0)
second_cas = partial(settle, mode='cas', draw_choice=1)
slack_cas = partial(settle, mode='cas', draw_choice='slack')
combined_no_skill = partial(settle, mode='reread', prior_fault=True, prior_takeover=True, draw_choice='slack')
combined_cas = partial(settle, mode='cas', prior_fault=True, prior_takeover=True, draw_choice='slack')
