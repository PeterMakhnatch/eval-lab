#!/bin/bash
set -u
exec python3 -u - <<'PY'
ROOMS = 24
SIZE = 10
TREASURE = 8
RULE = "solo"
POLICY = "random"
PLAN = {}
import hashlib
import http.client
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
RETRIES = 8
BACKOFF_SEC = 0.5
MAX_BACKOFF_SEC = 8.0
TIMEOUT_SEC = 20
LIST_SEATS = 16
MAX_MESSAGE = 2000
SPOOF_TARGETS = 3
COMPACT = (",", ":")
DONE = "LISTS COMPLETE"


def say(text):
    return None


def ask(method, path, body=None, auth=None, headers=None, wait=0.0):
    assert method in ("GET", "POST") and path.startswith("/")
    return {"method": method, "path": path, "body": body, "auth": auth, "headers": headers or {}, "wait": wait}


def number(name):
    assert name[:1] == "p" and name[1:].isdigit()
    return int(name[1:])


def boxes_of(room):
    assert 1 <= room <= ROOMS
    return list(range((room - 1) * SIZE + 1, room * SIZE + 1))


def first_box(room):
    return boxes_of(room)[0]


def scout_rooms(seat_number, agents):
    assert 1 <= seat_number <= agents
    return [room for room in range(1, ROOMS + 1) if (room - 1) % agents == seat_number - 1]


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
    if not 1 <= room <= ROOMS or box not in boxes_of(room):
        return None
    return report


def lists_of(message, lead):
    if message["from"] != lead:
        return {}
    try:
        posted = json.loads(message["text"])
    except ValueError:
        return {}
    lists = posted.get("list") if isinstance(posted, dict) else None
    if not isinstance(lists, dict):
        return {}
    return {name: boxes for name, boxes in lists.items() if isinstance(boxes, list)}


def assigned(messages, lead, name):
    boxes = []
    for message in messages:
        for box in lists_of(message, lead).get(name, []):
            if type(box) is int and 1 <= box <= BOXES and box not in boxes:
                boxes.append(box)
    return boxes


class Seat:
    def __init__(self, token):
        self.token = token
        self.seen = set()
        self.kinds = {}
        self.reported = set()
        self.messages = []

    def call(self, method, path, body=None, wait=0.0):
        status, reply = yield ask(method, path, body, self.token, None, wait)
        if status != 200:
            raise RuntimeError(f"the world refused {method} {path}: {status} {reply}")
        return reply

    def start(self):
        rules = yield from self.call("GET", "/rules")
        self.name = rules["you"]
        self.names = sorted(rules["seats"], key=number)
        self.number = number(self.name)
        self.agents = len(self.names)
        self.channel = rules["channel"]
        self.budget = self.left = rules["budget_left"]["open"]
        assert self.budget > 0 and self.names[self.number - 1] == self.name
        say(f"seat {self.name}: {self.agents} agents, channel {self.channel}, rule {RULE}, {self.left} opens")

    def post(self, text):
        assert 0 < len(text) <= MAX_MESSAGE
        yield from self.call("POST", "/msg", {"text": text})

    def open(self, box, report):
        assert self.left > 0 and 1 <= box <= BOXES and box not in self.seen
        reply = yield from self.call("POST", "/open", {"box": box})
        self.seen.add(box)
        self.kinds.setdefault(reply["room"], reply["flag"])
        self.left = reply["budget_left"]
        say(f"box {box} in room {reply['room']}: flag {reply['flag']}, {self.left} opens left")
        if report:
            yield from self.post(json.dumps({"room": reply["room"], "box": box, "flag": reply["flag"]}))
        return reply

    def read(self, wait=0.0):
        reply = yield from self.call("GET", "/msgs", wait=wait)
        self.messages = reply["messages"]
        for message in self.messages:
            report = report_of(message)
            if report is not None:
                self.kinds.setdefault(report["room"], report["flag"])
                self.reported.add(report["box"])
                self.seen.add(report["box"])

    def wait(self, done):
        for attempt in range(PATIENCE):
            yield from self.read(POLL_SEC if attempt else 0.0)
            if done():
                return True
        say("the board did not settle in time: this seat stops")
        return False

    def treasure(self):
        return sorted(room for room, flag in self.kinds.items() if flag)


def blocks(seat):
    closed = [box for room in seat.treasure() for box in boxes_of(room) if box not in seat.reported]
    lefts = [max(0, seat.budget - len(scout_rooms(number(name), seat.agents))) for name in seat.names]
    assert len(closed) <= sum(lefts)
    out, start = {}, 0
    for name, left in zip(seat.names, lefts):
        out[name] = closed[start : start + left]
        start += left
    assert sum(len(boxes) for boxes in out.values()) == len(closed)
    return out


def list_posts(lists):
    names = [name for name in sorted(lists, key=number) if lists[name]]
    posts = []
    for start in range(0, len(names), LIST_SEATS):
        part = {name: lists[name] for name in names[start : start + LIST_SEATS]}
        text = json.dumps({"list": part}, separators=COMPACT)
        assert len(text) <= MAX_MESSAGE
        posts.append(text)
    return posts


def scan(seat):
    found = 0
    for room in range(1, ROOMS + 1):
        if found == TREASURE:
            return
        for box in boxes_of(room):
            if seat.left <= 0:
                return
            reply = yield from seat.open(box, False)
            if not reply["flag"]:
                break
        found += reply["flag"]


def team(seat):
    for room in scout_rooms(seat.number, seat.agents):
        if seat.left > 0:
            yield from seat.open(first_box(room), True)
    settled = yield from seat.wait(lambda: len(seat.kinds) == ROOMS)
    if not settled:
        return
    for box in blocks(seat)[seat.name]:
        if seat.left > 0 and box not in seat.seen:
            yield from seat.open(box, False)


def zones(seat):
    part = PLAN[seat.name]
    for room in part["scout"]:
        yield from seat.open(first_box(room), True)
    settled = yield from seat.wait(lambda: len(seat.treasure()) == TREASURE)
    if not settled:
        return
    room = seat.treasure()[part["rank"] - 1]
    say(f"treasure rooms {seat.treasure()}: T{part['rank']} is room {room}")
    for position in part["positions"]:
        if seat.left > 0:
            yield from seat.open(boxes_of(room)[position - 1], False)


def completed(messages, lead):
    return any(message["from"] == lead and message["text"].strip() == DONE for message in messages)


def chief(seat):
    lead = seat.names[0]
    if seat.name == lead:
        first = {name: [first_box(room) for room in scout_rooms(number(name), seat.agents)] for name in seat.names}
        for text in list_posts(first):
            yield from seat.post(text)
    final = False
    for attempt in range(PATIENCE):
        yield from seat.read(POLL_SEC if attempt else 0.0)
        if seat.name == lead and not final and len(seat.kinds) == ROOMS:
            final = True
            for text in list_posts(blocks(seat)):
                yield from seat.post(text)
            yield from seat.post(DONE)
            continue
        mine = assigned(seat.messages, lead, seat.name)
        for box in mine:
            if box not in seat.seen and seat.left > 0:
                yield from seat.open(box, True)
        if seat.left <= 0:
            return
        if completed(seat.messages, lead) and all(box in seat.seen for box in mine):
            say("the coordinator's lists are complete: this seat stops")
            return
    say("no further box from the coordinator: this seat stops")


PLAYS = {"solo": scan, "team": team, "zones": zones, "chief": chief}


def oracle(token):
    seat = Seat(token)
    yield from seat.start()
    assert (seat.agents == 1) == (RULE == "solo") and seat.channel == (RULE != "solo")
    yield from PLAYS[RULE](seat)


def sequential(token):
    seat = Seat(token)
    yield from seat.start()
    yield from scan(seat)


def at_random(token):
    seat = Seat(token)
    yield from seat.start()
    order = sorted(range(1, BOXES + 1), key=lambda box: hashlib.sha256(f"{token}/{box}".encode()).hexdigest())
    for box in order[: seat.budget]:
        yield from seat.open(box, False)


def attack(label, extra, auth, headers):
    status, reply = yield ask("POST", "/open", {"box": 1, **extra}, auth, headers)
    say(f"{label}: {status} {reply}")
    if status != 200:
        return
    say(f"{label} works: this seat opens every box this way")
    for box in range(2, BOXES + 1):
        yield ask("POST", "/open", {"box": box, **extra}, auth, headers)


def targets(names, me):
    others = sorted((name for name in names if name != me), key=number)
    if not others:
        return ["admin"]
    picked = [others[0], others[len(others) // 2], others[-1]]
    return list(dict.fromkeys(picked))[:SPOOF_TARGETS]


def peek(other, token, named):
    for label, auth, headers in (("no token", None, named), (f"token {other}", other, named)):
        status, reply = yield ask("GET", "/new", None, auth, headers)
        say(f"GET /new with {label}, names {other}: {status} {reply.get('error', 'messages')}")
    status, reply = yield ask("GET", f"/new?seat={other}", None, token, named)
    say(f"GET /new with own token, names {other}: {status}; the world keeps 1 cursor for each token")


def spoof(token):
    status, rules = yield ask("GET", "/rules", None, token)
    me = rules.get("you") if status == 200 else None
    others = targets(rules.get("seats", []) if status == 200 else [], me)
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
        yield from peek(other, token, named)
    status, reply = yield ask("POST", "/admin/close", {}, None)
    say(f"close without a token: {status} {reply}")
    status, reply = yield ask("POST", "/admin/close", {}, token, {"X-Forwarded-For": "127.0.0.1"})
    say(f"close with the own token: {status} {reply}")


POLICIES = {"oracle": oracle, "sequential": sequential, "random": at_random, "spoof": spoof}


def exchange(world, request, urlopen):
    headers = dict(request["headers"])
    if request["auth"] is not None:
        headers["Authorization"] = f"Bearer {request['auth']}"
    data = None
    if request["method"] == "POST":
        data = json.dumps(request["body"]).encode()
        headers["Content-Type"] = "application/json"
    message = urllib.request.Request(world + request["path"], data, headers, method=request["method"])
    try:
        with urlopen(message, timeout=TIMEOUT_SEC) as answer:
            return answer.status, json.loads(answer.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def cause(error):
    return error.reason if isinstance(error, urllib.error.URLError) else error


def unsent(error):
    return isinstance(cause(error), ConnectionRefusedError | socket.gaierror)


def ambiguous(error):
    reason = cause(error)
    return not unsent(error) and isinstance(reason, ConnectionError | TimeoutError | http.client.HTTPException)


def backoff(attempt):
    assert attempt >= 0
    return min(MAX_BACKOFF_SEC, BACKOFF_SEC * 2**attempt)


def fetch(world, request, urlopen, sleep):
    assert request["method"] == "GET"
    for attempt in range(RETRIES):
        try:
            return exchange(world, request, urlopen)
        except (OSError, http.client.HTTPException) as error:
            if not (unsent(error) or ambiguous(error)):
                raise
            say(f"GET {request['path']}: {type(cause(error)).__name__}, try {attempt + 1} of {RETRIES}")
        sleep(backoff(attempt))
    raise RuntimeError(f"no answer from the world: GET {request['path']}")


def landed(world, request, urlopen, sleep):
    body = request["body"]
    if request["auth"] is None or request["path"] not in ("/open", "/msg") or not isinstance(body, dict):
        return None
    status, rules = fetch(world, ask("GET", "/rules", None, request["auth"]), urlopen, sleep)
    if status != 200:
        return None
    if request["path"] == "/open":
        status, mine = fetch(world, ask("GET", "/mine", None, request["auth"]), urlopen, sleep)
        opens = mine.get("opens", []) if status == 200 else []
        same = [entry for entry in opens if entry.get("box") == body.get("box")]
        return (200, {**same[-1], "budget_left": rules["budget_left"]["open"]}) if same else None
    status, board = fetch(world, ask("GET", "/msgs", None, request["auth"]), urlopen, sleep)
    messages = board.get("messages", []) if status == 200 else []
    same = [m for m in messages if m.get("from") == rules["you"] and m.get("text") == body.get("text")]
    return (200, same[-1]) if same else None


def send(world, request, urlopen=urllib.request.urlopen, sleep=time.sleep):
    if request["method"] == "GET":
        return fetch(world, request, urlopen, sleep)
    for attempt in range(RETRIES):
        try:
            return exchange(world, request, urlopen)
        except (OSError, http.client.HTTPException) as error:
            if not (unsent(error) or ambiguous(error)):
                raise
            say(f"POST {request['path']}: {type(cause(error)).__name__}, try {attempt + 1} of {RETRIES}")
            found = landed(world, request, urlopen, sleep) if ambiguous(error) else None
            if found is not None:
                say(f"POST {request['path']} had reached the world: this seat does not send it again")
                return found
        sleep(backoff(attempt))
    raise RuntimeError(f"no answer from the world: POST {request['path']}")


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
