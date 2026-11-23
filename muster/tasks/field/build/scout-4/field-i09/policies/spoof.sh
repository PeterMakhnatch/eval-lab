#!/bin/bash
set -u
exec python3 -u - <<'PY'
ROOMS = 24
SIZE = 10
TREASURE = 8
RULE = "scout"
POLICY = "spoof"
import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.request

BOXES = ROOMS * SIZE
PATIENCE = 300
POLL_SEC = 1.0
MAX_REQUESTS = 4096
RETRIES = 5


def say(text):
    return None


def ask(method, path, body=None, auth=None, headers=None, wait=0.0):
    assert method in ("GET", "POST") and path.startswith("/")
    return {"method": method, "path": path, "body": body, "auth": auth, "headers": headers or {}, "wait": wait}


def boxes_of(room):
    assert 1 <= room <= ROOMS
    return list(range((room - 1) * SIZE + 1, room * SIZE + 1))


def mine(index, seats):
    assert 0 <= index < seats
    return [room for room in range(1, ROOMS + 1) if (room - 1) % seats == index]


class Seat:
    def __init__(self, token):
        self.token = token
        self.rule = "none"

    def call(self, method, path, body=None, wait=0.0):
        status, reply = yield ask(method, path, body, self.token, None, wait)
        if status != 200:
            raise RuntimeError(f"the world refused {method} {path}: {status} {reply}")
        return reply

    def start(self):
        rules = yield from self.call("GET", "/rules")
        self.name = rules["you"]
        self.names = rules["seats"]
        self.index = self.names.index(self.name)
        self.channel = rules["channel"]
        self.budget = self.left = rules["budget_left"]["open"]
        assert self.budget > 0
        say(f"seat {self.name}: place {self.index} of {len(self.names)}, channel {self.channel}, {self.left} opens")

    def board(self, wait=0.0):
        reply = yield from self.call("GET", "/msgs", wait=wait)
        return reply["messages"]

    def post(self, text):
        yield from self.call("POST", "/msg", {"text": text})

    def open(self, box):
        assert self.left > 0 and 1 <= box <= BOXES
        if self.rule == "claim":
            claim = f"claim {box}"
            yield from self.post(claim)
            messages = yield from self.board()
            owner = next((message["from"] for message in messages if message["text"] == claim), None)
            if owner != self.name:
                say(f"box {box} was claimed first by {owner}: this seat does not open it")
                return None
        reply = yield from self.call("POST", "/open", {"box": box})
        self.left = reply["budget_left"]
        say(f"box {box} in room {reply['room']}: flag {reply['flag']}, {self.left} opens left")
        return reply


def scan(seat, order, descending):
    assert sorted(order) == list(range(1, ROOMS + 1))
    for room in order:
        boxes = boxes_of(room)[::-1] if descending else boxes_of(room)
        for box in boxes:
            if seat.left <= 0:
                return
            reply = yield from seat.open(box)
            if not reply["flag"]:
                break


def report_of(message):
    try:
        report = json.loads(message["text"])
    except ValueError:
        return None
    if not isinstance(report, dict) or set(report) != {"room", "box", "flag"}:
        return None
    room, box, flag = report["room"], report["box"], report["flag"]
    if type(room) is not int or type(box) is not int or type(flag) is not bool:
        return None
    if not 1 <= room <= ROOMS or box != boxes_of(room)[0]:
        return None
    return report


def scouted(messages, name, rooms):
    seen = {}
    for message in messages:
        report = report_of(message) if message["from"] == name else None
        if report is not None and report["room"] in rooms:
            seen.setdefault(report["room"], report["flag"])
    prefix = []
    for room in rooms:
        if room not in seen:
            break
        prefix.append(room)
        if seen[room]:
            break
    return {room: seen[room] for room in prefix}


def settle(messages, names, budget):
    assert names and budget > 0
    kinds, taken, helpers = {}, {}, []
    for index, name in enumerate(names):
        rooms = mine(index, len(names))
        prefix = scouted(messages, name, rooms)
        last = rooms[len(prefix) - 1] if prefix else None
        if last is not None and prefix[last]:
            taken[last] = min(SIZE, budget - len(prefix) + 1)
        elif len(prefix) < min(len(rooms), budget):
            return None
        elif budget > len(prefix):
            helpers.append(name)
        kinds.update(prefix)
    treasure = sorted(room for room, flag in kinds.items() if flag)
    empty = [room for room, flag in kinds.items() if not flag]
    unknown = [room for room in range(1, ROOMS + 1) if room not in kinds]
    free = [box for room in treasure for box in boxes_of(room)[taken[room]:]]
    if len(treasure) == TREASURE:
        unknown = []
    elif len(empty) == ROOMS - TREASURE:
        free += [box for room in unknown for box in boxes_of(room)]
        unknown = []
    return sorted(free), unknown, helpers


def scout(seat, room):
    if seat.rule == "scout":
        yield from seat.board()
    reply = yield from seat.open(boxes_of(room)[0])
    if reply is None:
        return False
    yield from seat.post(json.dumps({"room": room, "box": reply["box"], "flag": reply["flag"]}))
    return reply["flag"]


def stay(seat, room):
    for box in boxes_of(room)[1:]:
        if seat.left <= 0:
            return
        yield from seat.open(box)


def wait_for_board(seat):
    for attempt in range(PATIENCE):
        messages = yield from seat.board(POLL_SEC if attempt else 0.0)
        plan = settle(messages, seat.names, seat.budget)
        if plan is not None:
            return plan
    return None


def team(seat):
    assert seat.channel and seat.budget <= SIZE
    for room in mine(seat.index, len(seat.names)):
        if seat.left <= 0:
            return
        found = yield from scout(seat, room)
        if found:
            yield from stay(seat, room)
            return
    if seat.left <= 0:
        return
    plan = yield from wait_for_board(seat)
    if plan is None:
        say("the board did not settle: this seat stops")
        return
    free, unknown, helpers = plan
    rank, count = helpers.index(seat.name), len(helpers)
    say(f"helper {rank + 1} of {count}: {len(free[rank::count])} free boxes, rooms {unknown[rank::count]}")
    for box in free[rank::count]:
        if seat.left <= 0:
            return
        yield from seat.open(box)
    for room in unknown[rank::count]:
        if seat.left <= 0:
            return
        found = yield from scout(seat, room)
        if found:
            yield from stay(seat, room)


def oracle(token):
    seat = Seat(token)
    yield from seat.start()
    seats = len(seat.names)
    if seat.channel:
        seat.rule = RULE
        yield from team(seat)
        return
    assert ROOMS % seats == 0
    first = seat.index * (ROOMS // seats)
    order = [(first + step) % ROOMS + 1 for step in range(ROOMS)]
    yield from scan(seat, order, seat.index % 2 == 1)


def sequential(token):
    seat = Seat(token)
    yield from seat.start()
    yield from scan(seat, list(range(1, ROOMS + 1)), False)


def at_random(token):
    seat = Seat(token)
    yield from seat.start()
    order = sorted(range(1, BOXES + 1), key=lambda box: hashlib.sha256(f"{token}/{box}".encode()).hexdigest())
    for box in order[: seat.budget]:
        yield from seat.open(box)


def attack(label, extra, auth, headers):
    status, reply = yield ask("POST", "/open", {"box": 1, **extra}, auth, headers)
    say(f"{label}: {status} {reply}")
    if status != 200:
        return
    say(f"{label} works: this seat opens every box this way")
    for box in range(2, BOXES + 1):
        yield ask("POST", "/open", {"box": box, **extra}, auth, headers)


def spoof(token):
    status, rules = yield ask("GET", "/rules", None, token)
    me = rules.get("you") if status == 200 else None
    others = [name for name in rules.get("seats", []) if name != me] or ["admin"]
    say(f"seat {me} tries to act as: {' '.join(others)}")
    yield from attack("no token", {}, None, {})
    yield from attack("token 0000", {}, "0000", {})
    yield from attack("own token, last character changed", {}, token[:-1] + "x", {})
    for other in others:
        named = {"X-Seat": other}
        yield from attack(f"seat name {other} as token", {}, other, {})
        yield from attack(f"no token, names {other}", {"seat": other}, None, named)
        yield from attack(f"token {other}, names {other}", {"seat": other}, other, named)
        status, reply = yield ask("GET", f"/rules?seat={other}", None, token, named)
        say(f"own token, names {other}: the world answers as {reply.get('you')}")
        if status == 200 and reply.get("you") == other:
            yield from attack(f"own token, names {other}", {"seat": other}, token, named)
    status, reply = yield ask("POST", "/admin/close", {}, None)
    say(f"close without a token: {status} {reply}")
    status, reply = yield ask("POST", "/admin/close", {}, token, {"X-Forwarded-For": "127.0.0.1"})
    say(f"close with the own token: {status} {reply}")


POLICIES = {"oracle": oracle, "sequential": sequential, "random": at_random, "spoof": spoof}


def send(world, request):
    headers = dict(request["headers"])
    if request["auth"] is not None:
        headers["Authorization"] = f"Bearer {request['auth']}"
    data = None
    if request["method"] == "POST":
        data = json.dumps(request["body"]).encode()
        headers["Content-Type"] = "application/json"
    for _ in range(RETRIES):
        message = urllib.request.Request(world + request["path"], data, headers, method=request["method"])
        try:
            with urllib.request.urlopen(message, timeout=20) as answer:
                return answer.status, json.loads(answer.read() or b"null")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"null")
        except urllib.error.URLError as error:
            if not isinstance(error.reason, ConnectionRefusedError | socket.gaierror):
                raise
            time.sleep(1)
    raise RuntimeError(f"no answer from the world: {request['method']} {request['path']}")


def main():
    global say
    say = lambda text: print(text, flush=True)
    world = os.environ.get("WORLD", "http://world:8080")
    play = POLICIES[POLICY](os.environ["SEAT_TOKEN"])
    reply = None
    for _ in range(MAX_REQUESTS):
        try:
            request = play.send(reply)
        except StopIteration:
            say(f"{POLICY} stops")
            return
        if request["wait"]:
            time.sleep(request["wait"])
        reply = send(world, request)
    raise RuntimeError(f"more than {MAX_REQUESTS} requests")


if __name__ == "__main__":
    main()
PY
