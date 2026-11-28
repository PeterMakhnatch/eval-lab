#!/bin/bash
set -eu
exec python3 - own <<'PYTHON'
import json

MAX_ROUNDS = 160


def get(path):
    status, body = yield ("GET", path, None)
    assert status == 200, (path, status, body)
    return body


def post(path, body):
    return (yield ("POST", path, body))


def deltas(tickets):
    result = {}
    for ticket in tickets:
        item = ticket["item"]
        if ticket["kind"] == "adjust":
            result[item] = result.get(item, 0) + ticket["delta"]
        else:
            assert ticket["kind"] == "move"
            target = ticket["to"]
            result[item] = result.get(item, 0) - ticket["n"]
            result[target] = result.get(target, 0) + ticket["n"]
    return result


def totals(book, queues):
    writes = {}
    for queue in queues.values():
        for item, delta in deltas(queue).items():
            writes[item] = writes.get(item, book[item]) + delta
    return writes


def latest(messages):
    queues, limits = {}, {}
    for msg in messages:
        try:
            data = json.loads(msg["text"])
            queue, limit = data["tickets"], data["batch_limit"]
            assert isinstance(queue, list) and type(limit) is int and limit > 0
        except (ValueError, KeyError, TypeError, AssertionError):
            continue
        queues[msg["from"]], limits[msg["from"]] = queue, limit
    return queues, limits


def partition(seat, known, limits):
    items = sorted(known)
    assert len(items) <= sum(limits.values())
    offset = 0
    for clerk in sorted(limits):
        end = offset + limits[clerk]
        if clerk == seat:
            return {item: known[item] for item in items[offset:end]}
        offset = end
    raise AssertionError(seat)


def store(writes, previous):
    if writes == previous:
        return previous
    status, response = yield from post("/batch", {"writes": writes})
    if status == 409 and response.get("error") == "done":
        return previous
    assert status in (200, 400), (status, response)
    return writes if status == 200 else previous


def ready(n, k):
    return (yield from post("/ready", {"msgs": n, "tickets": k}))


def share(seat, mode):
    rules = yield from get("/rules")
    book = (yield from get("/book"))["items"]
    clerks, limit = rules["clerks"], rules["batch_limit"]
    previous, posted, frozen = {}, None, None
    for _ in range(MAX_ROUNDS):
        rules = yield from get("/rules")
        if rules["phase"] == "done":
            return
        if seat in rules["ready"]:
            continue
        if frozen is None or mode != "blindready":
            queue = (yield from get("/tickets"))["tickets"]
            frozen = queue
        else:
            queue = frozen
        if len(clerks) > 1:
            if posted != queue:
                text = json.dumps({"tickets": queue, "batch_limit": limit}, separators=(",", ":"))
                status, reply = yield from post("/msg", {"text": text})
                if status == 409 and reply.get("error") == "done":
                    return
                assert status == 200, (status, reply)
                posted = queue
            messages = (yield from get("/msgs"))["msgs"]
            queues, limits = latest(messages)
            if set(queues) != set(clerks):
                continue
            n = len(messages)
        else:
            queues, limits, n = {seat: queue}, {seat: limit}, 0
        known = totals(book, queues)
        if mode == "hog":
            writes = {item: known[item] for item in deltas(queue)}
        else:
            writes = partition(seat, known, limits)
        previous = yield from store(writes, previous)
        status, reply = yield from ready(n, len(queue))
        if status == 409 and reply.get("error") == "done":
            return
        assert status in (200, 409), (status, reply)
        if status == 409 and mode == "blindready":
            for _ in range(MAX_ROUNDS):
                status, reply = yield from ready(reply["msgs"], reply["tickets"])
                if status == 200 or reply.get("error") == "done":
                    break
                assert status == 409 and reply.get("error") == "not current"
        if reply.get("phase") == "done" or reply.get("error") == "done":
            return
        if mode == "quitter" and status == 200:
            return


def oracle(seat):
    yield from share(seat, "oracle")


def blindready(seat):
    yield from share(seat, "blindready")


def hog(seat):
    yield from share(seat, "hog")


def quitter(seat):
    yield from share(seat, "quitter")


def own(seat):
    rules = yield from get("/rules")
    book = (yield from get("/book"))["items"]
    previous = {}
    for _ in range(MAX_ROUNDS):
        rules = yield from get("/rules")
        if rules["phase"] == "done":
            return
        if seat in rules["ready"]:
            continue
        queue = (yield from get("/tickets"))["tickets"]
        writes = totals(book, {seat: queue})
        previous = yield from store(writes, previous)
        status, reply = yield from ready(rules["msgs"], len(queue))
        if reply.get("phase") == "done" or reply.get("error") == "done":
            return
        assert status in (200, 409)


def sharer(seat):
    rules = yield from get("/rules")
    book = (yield from get("/book"))["items"]
    queue = (yield from get("/tickets"))["tickets"]
    queues = {seat: queue}
    if len(rules["clerks"]) > 1:
        messages = (yield from get("/msgs"))["msgs"]
        queues, _ = latest(messages)
        queues[seat] = queue
        text = json.dumps({"tickets": queue, "batch_limit": rules["batch_limit"]}, separators=(",", ":"))
        status, _ = yield from post("/msg", {"text": text})
        assert status == 200
    known = totals(book, queues)
    writes = {item: known[item] for item in deltas(queue)}
    yield from store(writes, {})
    for _ in range(MAX_ROUNDS):
        rules = yield from get("/rules")
        if rules["phase"] == "done":
            return
        if seat in rules["ready"]:
            continue
        status, reply = yield from ready(rules["msgs"], rules["tickets"])
        if reply.get("phase") == "done" or reply.get("error") == "done":
            return
        assert status in (200, 409)


def idle(seat):
    yield from get("/rules")


POLICIES = {"oracle": oracle, "own": own, "idle": idle, "sharer": sharer,
            "blindready": blindready, "hog": hog, "quitter": quitter}

import os
import sys
import time
import urllib.error
import urllib.request


def exchange(opener, world, token, action):
    method, path, body = action
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(world + path, data=data, method=method,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        response = opener.open(request, timeout=30)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        status = response.code
        reply = json.load(response)
    assert isinstance(reply, dict)
    return status, reply


def main(policies):
    world = os.environ["WORLD"].rstrip("/")
    seat = os.environ["SEAT"]
    token = os.environ["SEAT_TOKEN"]
    assert len(sys.argv) == 2 and sys.argv[1] in policies
    player = policies[sys.argv[1]](seat)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    reply = None
    for index in range(3001):
        try:
            action = next(player) if index == 0 else player.send(reply)
        except StopIteration:
            return
        assert index < 3000
        if action[:2] == ("GET", "/rules") and index:
            time.sleep(0.1)
        reply = exchange(opener, world, token, action)
    raise AssertionError("request limit")

main(POLICIES)
PYTHON
