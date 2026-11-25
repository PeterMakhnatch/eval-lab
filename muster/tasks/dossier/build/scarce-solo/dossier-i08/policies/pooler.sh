#!/bin/bash
set -u
exec python3 - pooler <<'PYTHON'
from collections.abc import Generator
from functools import partial

Request = tuple[str, str, dict | None]
Reply = tuple[int, dict]
Play = Generator[Request, Reply, None]
MAX_POLLS = 60
MAX_REPAIR = 40
ALL_SKILLS = ("pool", "select", "tombstone", "cascade", "dispute")


def status_of(site, zone, entries):
    relevant = [entry for entry in entries if entry["key"] == site or (
        entry["key"] == zone and entry["status"] == "closed"
    )]
    if not relevant:
        return "unknown"
    newest = max(entry["at"] for entry in relevant)
    statuses = {entry["status"] for entry in relevant if entry["at"] == newest}
    return next(iter(statuses)) if len(statuses) == 1 else "disputed"


def get(path):
    status, body = yield "GET", path, None
    assert status == 200, (path, status, body)
    return body


def put(entry):
    return (yield "POST", "/put", entry)


def zones_of(rules):
    return {site: zone for zone, sites in rules["zones"].items() for site in sites}


def entries(register):
    return [{"key": key, "status": line["status"], "at": line["at"]}
            for key, line in register.items()]


def next_put(own, lines, watch, zones):
    zone_of = {site: zone for zone, sites in zones.items() for site in sites}
    relevant = set(watch) | {zone_of[site] for site in watch}
    known = [*own, *({"key": key, "status": line["status"], "at": line["at"]}
                     for key, line in lines.items())]
    for report in own:
        key, at, status = (report[k] for k in ("key", "at", "status"))
        if key not in relevant:
            continue
        targets = [site for site in watch if key in (site, zone_of[site])]
        if not any(all(other["at"] <= at for other in known
                       if other["key"] in (site, zone_of[site])) for site in targets):
            continue
        old = lines.get(key)
        if old is None or old["at"] < at:
            return dict(report)
        if old["at"] == at and old["status"] not in (status, "disputed"):
            assert key in zone_of
            return {**report, "status": "disputed"}
    return None

def understood(entry, skills, zones):
    if entry["key"] in zones:
        return "cascade" in skills
    if entry["status"] == "withdrawn":
        return "tombstone" in skills
    if entry["status"] == "disputed":
        return "dispute" in skills
    return True


def relevant_keys(rules):
    return set(rules["watch"]) | {zone for zone, sites in rules["zones"].items()
                                  if set(sites) & set(rules["watch"])}


def publish(rules, own, skills, select):
    relevant = relevant_keys(rules)
    if "pool" not in skills:
        return
    for report in own:
        key = report["key"]
        if not understood(report, skills, rules["zones"]):
            continue
        if (select or key in rules["zones"]) and key not in relevant:
            continue
        body = yield from get("/register")
        if body["phase"] == "answering":
            return
        old = body["lines"].get(key)
        if old is not None and old["at"] >= report["at"]:
            if (old["at"] != report["at"] or old["status"] in (report["status"], "disputed")
                    or "dispute" not in skills):
                continue
            report = {**report, "status": "disputed"}
        status, reply = yield from put(report)
        assert status in (200, 409, 429), reply
        if status == 429:
            return


def remove_decoys(seat, body, rules):
    relevant = relevant_keys(rules)
    for key, line in body["lines"].items():
        if key not in relevant and line["by"] == seat:
            status, reply = yield "POST", "/drop", {"key": key}
            assert status in (200, 403, 409), reply


def repair(seat, rules, own, cleaner, remaining):
    assert 0 <= remaining <= MAX_REPAIR
    for _ in range(remaining):
        body = yield from get("/register")
        if body["phase"] == "answering":
            return body, remaining
        report = next_put(own, body["lines"], rules["watch"], rules["zones"])
        if report is None:
            return body, remaining
        status, reply = yield from put(report)
        remaining -= 1
        assert status in (200, 409, 429), reply
        if status == 429:
            return body, 0
        if status == 409 and reply["error"] == "register full":
            if not cleaner or not any(line["by"] == seat and key not in relevant_keys(rules)
                                      for key, line in body["lines"].items()):
                return body, remaining
            yield from remove_decoys(seat, body, rules)
    return (yield from get("/register")), remaining


def writing(seat, rules, own, skills, mode):
    correct = skills == ALL_SKILLS
    cleaner = mode == "cleaner"
    if not correct or cleaner:
        yield from publish(rules, own, skills, "select" in skills and not cleaner)
    remaining = MAX_REPAIR
    for _ in range(MAX_POLLS):
        if correct:
            body, remaining = yield from repair(seat, rules, own, cleaner, remaining)
        else:
            body = yield from get("/register")
        if body["phase"] == "answering":
            return True
        status, reply = yield "POST", "/ready", {"version": body["version"]}
        assert status in (200, 409), reply
        if status == 200 and reply["closed"]:
            return True
        if cleaner and status == 200:
            yield from remove_decoys(seat, body, rules)
    asks = yield from get("/asks")
    return asks["phase"] == "answering"


def answer_value(site, zone, data, skills):
    if "dispute" in skills:
        return status_of(site, zone, data)
    candidates = [entry for entry in data if entry["key"] == site or (
        entry["key"] == zone and entry["status"] == "closed"
    )]
    return max(candidates, key=lambda entry: entry["at"])["status"] if candidates else "unknown"


def answer(rules, own, skills, mode):
    asks = yield from get("/asks")
    if asks["phase"] != "answering":
        return
    zones = zones_of(rules)
    code, register = yield "GET", "/register", None
    assert code in (200, 404), register
    lines = entries(register["lines"]) if code == 200 else []
    local = [entry for entry in own if understood(entry, skills, rules["zones"])]
    shared = [entry for entry in lines if understood(entry, skills, rules["zones"])]
    for site in asks["asks"]:
        zone = zones[site]
        if mode == "hoarder":
            status = status_of(site, zone, own)
        elif mode == "own_first" and any(entry["key"] == site for entry in own):
            status = status_of(site, zone, [entry for entry in own if entry["key"] == site])
        else:
            status = answer_value(site, zone, local + shared, skills)
        result, reply = yield "POST", "/answer", {"site": site, "status": status}
        assert result == 200, reply


def clerk(seat, skills, mode="normal") -> Play:
    rules = yield from get("/rules")
    own = (yield from get("/feed"))["reports"]
    if rules["phase"] == "writing" and not (yield from writing(seat, rules, own, skills, mode)):
        return
    yield from answer(rules, own, skills, mode)


def idle(seat) -> Play:
    yield "GET", "/rules", None


def spam(seat) -> Play:
    own = (yield from get("/feed"))["reports"]
    for report in reversed(own):
        yield from put(report)


def fuzz(seat) -> Play:
    rules = yield from get("/rules")
    site = next(iter(zones_of(rules)))
    valid = {"key": site, "status": rules["statuses"][0], "at": 1}
    for path, body in (
        ("/put", None), ("/put", {}), ("/put", {**valid, "at": True}),
        ("/put", {**valid, "at": 1.5}), ("/put", {**valid, "at": []}),
        ("/put", {**valid, "key": {}}), ("/put", {**valid, "key": "missing"}),
        ("/put", {**valid, "status": []}), ("/put", {**valid, "status": "fake"}),
        ("/put", {**valid, "extra": 1}), ("/drop", {"key": []}),
        ("/drop", {"key": site, "extra": 1}), ("/answer", {"site": [], "status": "open"}),
        ("/answer", {"site": site, "status": False}), ("/answer", {"site": site, "status": "fake"}),
        ("/answer", {"site": site, "status": "open", "at": 1}), ("/ready", {"version": []}),
        ("/ready", {"version": True}), ("/ready", {"version": -1}), ("/missing", {}),
    ):
        yield "POST", path, body


POLICIES = {
    "hoarder": partial(clerk, skills=(), mode="hoarder"),
    "pooler": partial(clerk, skills=("pool",)),
    "selector": partial(clerk, skills=("pool", "select")),
    "burier": partial(clerk, skills=("pool", "tombstone")),
    "closer": partial(clerk, skills=("pool", "cascade")),
    "umpire": partial(clerk, skills=("pool", "dispute")),
    "oracle": partial(clerk, skills=ALL_SKILLS),
    "own_first": partial(clerk, skills=ALL_SKILLS, mode="own_first"),
    "cleaner": partial(clerk, skills=ALL_SKILLS, mode="cleaner"),
    "idle": idle,
    "spam": spam,
    "fuzz": fuzz,
}

import json
import os
import sys
import time
import urllib.error
import urllib.request

MAX_REQUESTS = 2000
TRANSPORT_RETRIES = 3
REQUEST_TIMEOUT_SEC = 30
RETRY_PAUSE_SEC = 1
READY_PAUSE_STEP_SEC = 0.3
READY_PAUSE_MAX_SEC = 2.0


def exchange(opener, world, token, action):
    method, path, body = action
    assert method in ("GET", "POST") and path.startswith("/") and not path.startswith("//")
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        world + path,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method=method,
    )
    for attempt in range(TRANSPORT_RETRIES + 1):
        try:
            try:
                response = opener.open(request, timeout=REQUEST_TIMEOUT_SEC)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = response.code
                reply = json.load(response)
            print(method, path, status, flush=True)
            assert isinstance(reply, dict)
            return status, reply
        except (OSError, urllib.error.URLError) as error:
            print(f"transport error: {method} {path}: {error}", file=sys.stderr)
            if attempt == TRANSPORT_RETRIES:
                raise SystemExit(3) from error
            time.sleep(RETRY_PAUSE_SEC)
    raise AssertionError("unreachable")


def main(policies):
    world = os.environ["WORLD"].rstrip("/")
    seat = os.environ["SEAT"]
    token = os.environ["SEAT_TOKEN"]
    assert len(sys.argv) == 2 and sys.argv[1] in policies
    player = policies[sys.argv[1]](seat)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    reply = None
    ready_calls = 0
    for index in range(MAX_REQUESTS + 1):
        try:
            action = next(player) if index == 0 else player.send(reply)
        except StopIteration:
            return
        if index == MAX_REQUESTS:
            raise SystemExit("request limit exceeded")
        if action[:2] == ("POST", "/ready"):
            if ready_calls:
                time.sleep(min(READY_PAUSE_MAX_SEC, READY_PAUSE_STEP_SEC * ready_calls))
            ready_calls += 1
        reply = exchange(opener, world, token, action)
    raise AssertionError("unreachable")

main(POLICIES)
PYTHON
