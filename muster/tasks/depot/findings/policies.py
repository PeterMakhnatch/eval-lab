MAX_TICKETS = 128
MAX_ATTEMPTS = 8


def get(path):
    status, data = yield "GET", path, None
    assert status == 200
    return data


def legs(ticket):
    if ticket["kind"] == "move":
        return [(ticket["item"], -ticket["n"]), (ticket["to"], ticket["n"])]
    if ticket["kind"] == "draw":
        return [(ticket["item"], -ticket["take"])]
    return [(ticket["item"], ticket["delta"])]


def completed(log, tid, item):
    return any(row["ticket"] == tid and row["item"] == item for row in log["writes"]) or any(
        row["ticket"] == tid for row in log["declined"]
    )


def remaining():
    return (yield from get("/rules"))["budget_left"]["write"]


def settle(ticket, item, delta, pair, *, partner_only, selective, use_budget):
    tid = ticket["id"]
    uncertain = False
    prior_budget = None
    for _ in range(MAX_ATTEMPTS):
        book = (yield from get("/book"))["items"]
        if use_budget:
            left = yield from remaining()
            if uncertain and left < prior_budget:
                return
        if not selective or "from" in ticket or (uncertain and not use_budget):
            log = yield from get("/log")
            if completed(log, tid, item):
                return
        if pair and sum(book[key]["qty"] for key in pair["items"]) + delta < pair["floor"]:
            status, _ = yield "POST", "/decline", {"ticket": tid}
            assert status == 200
            return
        keys = pair["items"] if pair else [item]
        if pair and partner_only:
            keys = [key for key in keys if key != item]
        body = {"ticket": tid, "item": item, "qty": book[item]["qty"] + delta,
                "expect": {key: book[key]["version"] for key in keys}}
        status, reply = yield "POST", "/write", body
        if status == 200:
            return
        if status == 504:
            uncertain = True
            if use_budget:
                prior_budget = left
        else:
            assert status == 409 and reply["error"] == "stale", (status, reply)
    raise AssertionError((tid, item))


def run(seat, *, partner_only=False, selective=False, use_budget=False):
    rules = yield from get("/rules")
    queue = (yield from get("/tickets"))["tickets"]
    assert len(queue) <= MAX_TICKETS
    pairs = {item: pair for pair in rules["pairs"] for item in pair["items"]}
    for ticket in queue:
        for item, delta in legs(ticket):
            yield from settle(ticket, item, delta, pairs.get(item), partner_only=partner_only,
                              selective=selective, use_budget=use_budget)


def peer_only(seat):
    yield from run(seat, partner_only=True)


def selective_log(seat):
    yield from run(seat, selective=True)


def budget_check(seat):
    yield from run(seat, selective=True, use_budget=True)


def combined(seat):
    yield from run(seat, partner_only=True, selective=True, use_budget=True)


def reconcile_leg(ticket, item, delta):
    tid = ticket["id"]
    base = (yield from get("/book"))["items"][item]
    log = yield from get("/log")
    if completed(log, tid, item):
        return
    target = base["qty"] + delta
    for _ in range(MAX_ATTEMPTS):
        status, _ = yield "POST", "/write", {"ticket": tid, "item": item, "qty": target}
        assert status in (200, 504)
        log = yield from get("/log")
        previous = base["qty"]
        target = base["qty"] + delta
        applied = False
        for row in log["writes"]:
            if row["item"] != item or row["version"] <= base["version"]:
                continue
            if row["ticket"] == tid:
                applied = True
            else:
                target += row["qty"] - previous
            previous = row["qty"]
        if applied and previous == target:
            return
    raise AssertionError((tid, item))


def reconcile(seat):
    rules = yield from get("/rules")
    queue = (yield from get("/tickets"))["tickets"]
    assert len(queue) <= MAX_TICKETS
    pairs = {item: pair for pair in rules["pairs"] for item in pair["items"]}
    for ticket in queue:
        for item, delta in legs(ticket):
            if item in pairs:
                yield from settle(ticket, item, delta, pairs[item], partner_only=True,
                                  selective=True, use_budget=False)
            else:
                yield from reconcile_leg(ticket, item, delta)


def conservative_draw(seat):
    rules = yield from get("/rules")
    queue = (yield from get("/tickets"))["tickets"]
    assert len(queue) <= MAX_TICKETS
    pairs = {item: pair for pair in rules["pairs"] for item in pair["items"]}
    for ticket in queue:
        for item, delta in legs(ticket):
            pair = pairs.get(item)
            if pair is None:
                yield from settle(ticket, item, delta, None, partner_only=False,
                                  selective=False, use_budget=False)
                continue
            book = (yield from get("/book"))["items"]
            slack = sum(book[key]["qty"] for key in pair["items"]) - pair["floor"]
            if slack < -2 * delta:
                status, _ = yield "POST", "/decline", {"ticket": ticket["id"]}
            else:
                status, _ = yield "POST", "/write", {
                    "ticket": ticket["id"], "item": item, "qty": book[item]["qty"] + delta}
            assert status == 200
