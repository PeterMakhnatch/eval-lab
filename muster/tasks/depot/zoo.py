from functools import partial

MAX_TICKETS = 128
MAX_TRIES = 8
MAX_SPAM = 256
CAS = {"cas", "cas_version", "cas_value", "cas_check_once", "cas_dedupe_first", "cas_dedupe_ticket",
       "cas_readset", "cas_dedupe", "oracle"}
READSET = {"cas_readset", "oracle"}
EVERY_LOG = {"cas_dedupe", "oracle", "cas_dedupe_ticket"}
FAULT_LOG = EVERY_LOG | {"cas_check_once", "cas_dedupe_first"}


def legs(ticket):
    if ticket["kind"] == "move":
        return [(ticket["item"], -ticket["n"]), (ticket["to"], ticket["n"])]
    return [(ticket["item"], ticket["delta"] if ticket["kind"] == "adjust" else -ticket["take"])]


def permitted(book, pair, delta):
    return pair is None or sum(book[item]["qty"] for item in pair["items"]) + delta >= pair["floor"]


def get(path):
    status, result = yield "GET", path, None
    assert status == 200
    return result


def setup():
    rules = yield from get("/rules")
    queue = (yield from get("/tickets"))["tickets"]
    assert len(queue) <= MAX_TICKETS
    pairs = {item: pair for pair in rules["pairs"] for item in pair["items"]}
    return rules, queue, pairs


def blind(seat):
    _, queue, pairs = yield from setup()
    book = (yield from get("/book"))["items"]
    for ticket in queue:
        for item, delta in legs(ticket):
            if not permitted(book, pairs.get(item), delta):
                yield "POST", "/decline", {"ticket": ticket["id"]}
                continue
            qty = book[item]["qty"] + delta
            body = {"ticket": ticket["id"], "item": item, "qty": qty}
            status, _ = yield "POST", "/write", body
            if status == 504:
                yield "POST", "/write", body
            book[item]["qty"] = qty


def done(log, mode, tid, item):
    return any(row["ticket"] == tid and (mode == "cas_dedupe_ticket" or row["item"] == item)
               for row in log["writes"]) or any(row["ticket"] == tid for row in log["declined"])


def apply_leg(mode, ticket, item, delta, pair, initial=None):
    conditional = mode in CAS
    tid = ticket["id"]
    book = dict(initial[0]) if initial else None
    unknown_body = None
    for attempt in range(MAX_TRIES):
        if book is None:
            book = (yield from get("/book"))["items"]
        if unknown_body is not None:
            old, sent = unknown_body
            if mode == "cas_version" and book[item]["version"] != old:
                return
            if mode == "cas_value" and book[item]["qty"] == sent:
                return
        check = mode in EVERY_LOG or (unknown_body is not None and mode in FAULT_LOG)
        if initial and attempt == 0 and done(initial[1], mode, tid, item):
            return
        if check and done((yield from get("/log")), mode, tid, item):
            return
        unknown_body = None
        if not permitted(book, pair, delta):
            yield "POST", "/decline", {"ticket": tid}
            return
        qty = book[item]["qty"] + delta
        body = {"ticket": tid, "item": item, "qty": qty}
        if conditional:
            keys = pair["items"] if pair and mode in READSET else [item]
            body["expect"] = {key: book[key]["version"] for key in keys}
        status, reply = yield "POST", "/write", body
        if status == 200 or reply.get("error") == "ticket already written for this item":
            return
        if status == 504:
            unknown_body = (book[item]["version"], qty)
            book = None
        elif status == 428:
            conditional = True
            book = None
        elif reply.get("error") == "pair rule":
            yield "POST", "/decline", {"ticket": tid}
            return
        elif reply.get("error") == "stale":
            if pair and mode not in READSET:
                book = None
            else:
                book.update(reply["current"])
        else:
            return


def repair_leg(ticket, item, delta, pair):
    book = (yield from get("/book"))["items"]
    if not permitted(book, pair, delta):
        yield "POST", "/decline", {"ticket": ticket["id"]}
        return
    previous = book[item]
    target = previous["qty"] + delta
    for _ in range(MAX_TRIES):
        body = {"ticket": ticket["id"], "item": item, "qty": target}
        status, reply = yield "POST", "/write", body
        log = yield from get("/log")
        if status != 200:
            return
        missed = 0
        qty = previous["qty"]
        for row in log["writes"]:
            if row["item"] != item or not previous["version"] < row["version"] < reply["version"]:
                continue
            missed += row["qty"] - qty
            qty = row["qty"]
        if not missed:
            return
        target += missed
        previous = reply


def repair(seat):
    _, queue, pairs = yield from setup()
    for ticket in queue:
        for item, delta in legs(ticket):
            yield from repair_leg(ticket, item, delta, pairs.get(item))


def careful(seat, *, mode):
    _, queue, pairs = yield from setup()
    for ticket in queue:
        initial = None
        if mode == "cas_dedupe_first":
            book = (yield from get("/book"))["items"]
            log = yield from get("/log")
            initial = (book, log)
        for item, delta in legs(ticket):
            yield from apply_leg(mode, ticket, item, delta, pairs.get(item), initial)


def slack_guess(seat):
    _, queue, pairs = yield from setup()
    initial = (yield from get("/book"))["items"]
    draws = [ticket for ticket in queue if ticket["kind"] == "draw"]
    guess = min(draws, key=lambda t: sum(initial[k]["qty"] for k in pairs[t["item"]]["items"])
                - pairs[t["item"]]["floor"], default=None)
    for ticket in queue:
        for item, change in legs(ticket):
            if ticket["kind"] != "draw":
                yield from apply_leg("oracle", ticket, item, change, None)
            elif ticket is guess:
                yield "POST", "/decline", {"ticket": ticket["id"]}
            else:
                yield from apply_leg("cas", ticket, item, change, None)


def idle(seat):
    yield "GET", "/rules", None


def decliner(seat):
    _, queue, _ = yield from setup()
    for ticket in queue:
        if ticket["kind"] == "draw":
            yield "POST", "/decline", {"ticket": ticket["id"]}


def spam(seat):
    _, queue, _ = yield from setup()
    if not queue:
        return
    ticket = queue[0]
    item = ticket["item"]
    version = (yield from get("/book"))["items"][item]["version"]
    for _ in range(MAX_SPAM):
        body = {"ticket": ticket["id"], "item": item, "qty": 0, "expect": {item: version}}
        status, reply = yield "POST", "/write", body
        if status == 200:
            version = reply["version"]
        elif reply.get("error") == "stale":
            version = reply["current"][item]["version"]
        elif status == 504:
            version = (yield from get("/book"))["items"][item]["version"]
        else:
            return


def fuzz(seat):
    _, queue, _ = yield from setup()
    if not queue:
        return
    tid, item = queue[0]["id"], queue[0]["item"]
    valid = {"ticket": tid, "item": item, "qty": 0}
    book = (yield from get("/book"))["items"]
    oversized = {name: row["version"] for name, row in list(book.items())[:9]}
    if len(oversized) > 8:
        status, _ = yield "POST", "/write", {**valid, "expect": oversized}
        assert status == 400
    for body in ({}, {"ticket": []}, {**valid, "item": {}}, {**valid, "qty": True},
                 {**valid, "qty": -1}, {**valid, "qty": "5"}, {**valid, "expect": []},
                 {**valid, "expect": {}},
                 {**valid, "expect": {item: False}}, {**valid, "expect": {"unknown": 1}},
                 {**valid, "expect": {item: 0}}, {**valid, "ticket": {}}):
        status, _ = yield "POST", "/write", body
        assert status == 400
    yield "POST", "/decline", {"ticket": []}
    yield "POST", "/msg", {"text": []}


POLICIES = {"blind": blind, "repair": repair, "idle": idle, "decliner": decliner, "spam": spam, "fuzz": fuzz,
            "slack_guess": slack_guess,
            **{mode: partial(careful, mode=mode) for mode in sorted(CAS | {"reread", "follower"})}}
