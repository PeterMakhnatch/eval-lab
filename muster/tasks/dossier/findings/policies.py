def get(path):
    status, body = yield "GET", path, None
    assert status == 200, (path, status, body)
    return body


def ready(version):
    for _ in range(12):
        status, body = yield "POST", "/ready", {"version": version}
        if status == 200:
            return body["closed"]
        assert status == 409 and "version" in body, body
        version = body["version"]
    return False


def publish(own):
    version = 0
    for entry in own:
        status, body = yield "POST", "/put", entry
        assert status in (200, 409), body
        if status == 200:
            version = body["version"]
    return version


def register_reader(rules, asks):
    lines = (yield from get("/register"))["lines"]
    zone_of = {site: zone for zone, sites in rules["zones"].items() for site in sites}
    for site in asks:
        candidates = [lines[key] for key in (site, zone_of[site]) if key in lines]
        statuses = {line["status"] for line in candidates
                    if line["at"] == max(other["at"] for other in candidates)}
        value = next(iter(statuses)) if len(statuses) == 1 else "disputed" if statuses else "unknown"
        yield "POST", "/answer", {"site": site, "status": value}


def play(order):
    rules = yield from get("/rules")
    own = (yield from get("/feed"))["reports"]
    if order == "ascending":
        own.sort(key=lambda entry: entry["at"])
    elif order == "descending":
        own.sort(key=lambda entry: -entry["at"])
    elif order == "reverse":
        own.reverse()
    elif order == "watch":
        relevant = set(rules["watch"]) | set(rules["zones"])
        own.sort(key=lambda entry: (entry["key"] not in relevant, entry["at"]))
    if rules["phase"] == "writing":
        version = yield from publish(own)
        if not (yield from ready(version)):
            return
    asks = (yield from get("/asks"))["asks"]
    if rules["slots"] == 0:
        zones = {site: zone for zone, sites in rules["zones"].items() for site in sites}
        for site in asks:
            candidates = [r for r in own if r["key"] in (site, zones[site])]
            at = max((r["at"] for r in candidates), default=0)
            values = {r["status"] for r in candidates if r["at"] == at}
            value = next(iter(values)) if len(values) == 1 else "disputed" if values else "unknown"
            yield "POST", "/answer", {"site": site, "status": value}
    else:
        yield from register_reader(rules, asks)


def feed_order(seat):
    yield from play("feed")


def ascending(seat):
    yield from play("ascending")


def descending(seat):
    yield from play("descending")


def reverse(seat):
    yield from play("reverse")


def watch_first(seat):
    yield from play("watch")


def ready_first(seat):
    rules = yield from get("/rules")
    if (yield from ready(0)):
        yield from register_reader(rules, (yield from get("/asks"))["asks"])


def no_ready(seat):
    own = (yield from get("/feed"))["reports"]
    yield from publish(sorted(own, key=lambda entry: entry["at"]))
    yield from get("/asks")


def private_pattern(seat):
    rules = yield from get("/rules")
    own = (yield from get("/feed"))["reports"]
    version = yield from publish(sorted(own, key=lambda entry: entry["at"]))
    if not (yield from ready(version)):
        return
    asks = (yield from get("/asks"))["asks"]
    zones = rules["zones"]
    latest = {entry["key"]: entry for entry in sorted(own, key=lambda entry: entry["at"])}
    a, b, c = (zones[zone][0] for zone in ("north", "south", "east"))
    g = zones["east"][1]
    if set(asks) != {a, b, c} or not {a, b, c, g} <= set(latest):
        return
    values = {a: "closed" if latest[a]["at"] == 3 else latest[g]["status"],
              b: latest[a]["status"], c: latest[c]["status"]}
    if any(line["status"] == "withdrawn" for line in own):
        values[a] = "withdrawn"
    elif latest[a]["at"] == 25:
        values[a] = "disputed"
    for site in asks:
        yield "POST", "/answer", {"site": site, "status": values[site]}


def private_mix(seat):
    own = (yield from get("/feed"))["reports"]
    version = yield from publish(sorted(own, key=lambda entry: entry["at"]))
    if not (yield from ready(version)):
        return
    asks = (yield from get("/asks"))["asks"]
    latest = {entry["key"]: entry for entry in sorted(own, key=lambda entry: entry["at"])}
    for index, site in enumerate(asks):
        report = latest.get(site)
        if report is None:
            return
        value = report["status"] if index == 2 else {3: "closed", 20: "withdrawn",
                                                     25: "disputed"}.get(report["at"])
        if value is None:
            return
        yield "POST", "/answer", {"site": site, "status": value}


def copy_site(seat):
    own = (yield from get("/feed"))["reports"]
    version = yield from publish(own)
    if not (yield from ready(version)):
        return
    asks = (yield from get("/asks"))["asks"]
    lines = (yield from get("/register"))["lines"]
    for site in asks:
        yield "POST", "/answer", {"site": site, "status": lines.get(site, {}).get(
            "status", "unknown")}


def skip_special(seat):
    rules = yield from get("/rules")
    own = (yield from get("/feed"))["reports"]
    ordinary = set(rules["statuses"][:6])
    version = yield from publish(entry for entry in own if entry["status"] in ordinary)
    if not (yield from ready(version)):
        return
    asks = (yield from get("/asks"))["asks"]
    yield from register_reader(rules, asks)
