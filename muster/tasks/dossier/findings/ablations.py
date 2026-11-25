from collections import defaultdict
from functools import partial

MAX_READY = 60


def get(path):
    code, body = yield "GET", path, None
    assert code == 200, (path, code, body)
    return body


def put(line):
    code, reply = yield "POST", "/put", line
    assert code in (200, 409, 429), reply
    return code


def group(own, dispute):
    groups = defaultdict(list)
    for line in own:
        groups[line["key"]].append(line)
    result = []
    for reports in groups.values():
        at = max(line["at"] for line in reports)
        latest = [line for line in reports if line["at"] == at]
        line = dict(latest[0])
        if dispute and len({r["status"] for r in latest}) > 1:
            line["status"] = "disputed"
        result.append(line)
    return result


def ordering(rules, strategy, line):
    key, status, at = line["key"], line["status"], line["at"]
    special = status in ("withdrawn", "closed", "disputed")
    layout = [site for row in rules["zones"].values() for site in row]
    rank = layout.index(key) if key in layout else -1
    if strategy == "key":
        return (key,)
    if strategy == "zone":
        return (rank,)
    if strategy == "status":
        return (status, at)
    if strategy == "special_early":
        return (not special, at)
    if strategy == "special_late":
        return (not special, -at)
    if strategy == "special_zone":
        return (not special, rank)
    if strategy == "special_feed":
        return (not special,)
    if strategy == "special_key":
        return (not special, key)
    raise AssertionError(strategy)


def permitted(line, missing, zones):
    return not ((missing == "tombstone" and line["status"] == "withdrawn")
                or (missing == "cascade" and line["key"] in zones)
                or (missing == "dispute" and line["status"] == "disputed"))


def publish(rules, own, missing, strategy, dominance):
    if missing == "pool":
        return
    zone_of = {site: zone for zone, sites in rules["zones"].items() for site in sites}
    group_lines = group(own, missing != "dispute")
    group_lines.sort(key=lambda line: ordering(rules, strategy, line))
    relevant = None if missing == "select" else set(rules["watch"]) | {
        zone_of[site] for site in rules["watch"]}
    for line in group_lines:
        key = line["key"]
        if relevant is not None and key not in relevant:
            continue
        if not permitted(line, missing, rules["zones"]):
            continue
        register = (yield from get("/register"))["lines"]
        current = register.get(key)
        if dominance and key in zone_of:
            zone = zone_of[key]
            closures = [entry["at"] for entry in own if entry["key"] == zone]
            if zone in register:
                closures.append(register[zone]["at"])
            if any(at > line["at"] for at in closures):
                continue
        if current and current["at"] > line["at"]:
            continue
        if current and current["at"] == line["at"]:
            if current["status"] in (line["status"], "disputed"):
                continue
            if missing != "dispute":
                line = {**line, "status": "disputed"}
        if (yield from put(line)) == 429:
            return


def close(seat, evict):
    for _ in range(MAX_READY):
        register = yield from get("/register")
        if register["phase"] == "answering":
            return True
        code, reply = yield "POST", "/ready", {"version": register["version"]}
        assert code in (200, 409), reply
        if code == 200 and reply["closed"]:
            return True
        if evict and code == 200 and reply["waiting_for"]:
            own = [(line["at"], key) for key, line in register["lines"].items()
                   if line["by"] == seat]
            if own:
                _, key = min(own)
                code, reply = yield "POST", "/drop", {"key": key}
                assert code in (200, 409), reply
    return False


def answer(rules, own, missing):
    asks = yield from get("/asks")
    if asks["phase"] != "answering":
        return
    shared = (yield from get("/register"))["lines"]
    data = own + [{"key": key, "status": line["status"], "at": line["at"]}
                  for key, line in shared.items()]
    data = [line for line in data if permitted(line, missing, rules["zones"])]
    zone_of = {site: zone for zone, sites in rules["zones"].items() for site in sites}
    for site in asks["asks"]:
        candidates = [line for line in data if line["key"] in (site, zone_of[site])]
        value = "unknown"
        if candidates:
            at = max(line["at"] for line in candidates)
            latest = [line["status"] for line in candidates if line["at"] == at]
            value = latest[0] if missing == "dispute" or len(set(latest)) == 1 else "disputed"
        code, reply = yield "POST", "/answer", {"site": site, "status": value}
        assert code == 200, reply


def play(seat, missing, strategy="special_feed", evict=False, writer_only=False, dominance=False):
    rules = yield from get("/rules")
    own = (yield from get("/feed"))["reports"]
    if rules["phase"] == "writing":
        yield from publish(rules, own, missing, strategy, dominance)
        if not (yield from close(seat, evict)):
            return
    yield from answer(rules, own, None if writer_only else missing)


no_pool = partial(play, missing="pool")
no_tombstone = partial(play, missing="tombstone")
no_cascade = partial(play, missing="cascade")
no_dispute = partial(play, missing="dispute")
no_tombstone_writer = partial(play, missing="tombstone", writer_only=True)
no_cascade_writer = partial(play, missing="cascade", writer_only=True)
no_dispute_writer = partial(play, missing="dispute", writer_only=True)
no_select_key = partial(play, missing="select", strategy="key")
no_select_zone = partial(play, missing="select", strategy="zone")
no_select_status = partial(play, missing="select", strategy="status")
no_select_special_early = partial(play, missing="select", strategy="special_early")
no_select_special_late = partial(play, missing="select", strategy="special_late")
no_select_special_zone = partial(play, missing="select", strategy="special_zone")
no_select_special_feed = partial(play, missing="select", strategy="special_feed")
no_select_special_key = partial(play, missing="select", strategy="special_key")
no_select_pressure = partial(play, missing="select", strategy="special_feed", evict=True)
no_select_dominance_early = partial(
    play, missing="select", strategy="special_early", dominance=True)
no_select_dominance_late = partial(
    play, missing="select", strategy="special_late", dominance=True)
full = partial(play, missing=None)
