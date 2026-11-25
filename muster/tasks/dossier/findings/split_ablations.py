from collections import Counter
from functools import partial

MAX_PUTS = 32
MAX_READY = 60
SPECIAL = ("withdrawn", "closed", "disputed")


def get(path):
    code, body = yield ("GET", path, None)
    assert code == 200, (path, code, body)
    return body


def put(line):
    code, body = yield ("POST", "/put", line)
    assert code in (200, 409), (code, body)
    return code, body


def zones_of(rules):
    return {site: zone for zone, sites in rules["zones"].items() for site in sites}


def summarize(reports):
    result = {}
    for line in reports:
        key = line["key"]
        old = result.get(key)
        if old is None or line["at"] > old["at"]:
            result[key] = dict(line)
        elif line["at"] == old["at"] and line["status"] != old["status"]:
            result[key] = {**line, "status": "disputed"}
    return result


def local_markers(own):
    return {line["status"] for line in summarize(own).values()} & set(SPECIAL)


def value(site, zones, data):
    relevant = [line for line in data if line["key"] == site or (
        line["key"] == zones[site] and line["status"] == "closed")]
    if not relevant:
        return "unknown"
    latest = max(line["at"] for line in relevant)
    statuses = {line["status"] for line in relevant if line["at"] == latest}
    return next(iter(statuses)) if len(statuses) == 1 else "disputed"


def register_entries(lines):
    return [{"key": key, "status": line["status"], "at": line["at"]}
            for key, line in lines.items()]


def selected(rules, own):
    zone_of = zones_of(rules)
    relevant = set(rules["watch"]) | {zone_of[site] for site in rules["watch"]}
    return [line for line in own if line["key"] in relevant]


def proposals(rules, own, lines, omission):
    zone_of = zones_of(rules)
    if omission == "pool":
        return []
    own = selected(rules, own)
    if omission == "dispute":
        own = list({line["key"]: line for line in sorted(own, key=lambda x: x["at"])}.values())
    if omission in ("withdrawn", "closed"):
        own = [line for line in own if line["status"] != omission]
    planned = []
    known = [*own, *register_entries(lines)]
    for key, line in summarize(own).items():
        relevant_sites = [site for site in rules["watch"] if key in (site, zone_of[site])]
        if not any(all(x["at"] <= line["at"] for x in known
                       if x["key"] in (site, zone_of[site])) for site in relevant_sites):
            continue
        old = lines.get(key)
        if old is None or line["at"] > old["at"]:
            planned.append(line)
        elif (line["at"] == old["at"] and old["status"] not in (line["status"], "disputed")
              and omission != "dispute"):
            planned.append({**line, "status": "disputed"})
    return planned


def capacity_order(rules, own, lines, mode):
    zone_of = zones_of(rules)
    summaries = summarize(own)
    watch_density = Counter(zone_of[site] for site in rules["watch"])
    peer_density = Counter(zone_of[key] for key in lines if key in zone_of)
    prior_density = Counter(zone_of[key] for key in summaries if key in zone_of and key in lines)
    def priority(line):
        key = line["key"]
        zone = zone_of.get(key, key)
        available = watch_density[zone] - peer_density[zone]
        first = (key not in lines, line["status"] not in SPECIAL)
        if mode == "density":
            return (*first, -available, -watch_density[zone], key)
        if mode == "overlap":
            return (*first, -prior_density[zone], -available, key)
        assert mode == "distance"
        peers = [old["at"] for k, old in lines.items() if zone_of.get(k, k) == zone]
        distance = min((abs(line["at"] - at) for at in peers), default=40)
        return (*first, distance, -available, key)
    planned = []
    for line in sorted(summaries.values(), key=priority):
        old = lines.get(line["key"])
        if old is None or line["at"] > old["at"]:
            planned.append(line)
        elif line["at"] == old["at"] and old["status"] not in (line["status"], "disputed"):
            planned.append({**line, "status": "disputed"})
    return planned


def close(version):
    for _ in range(MAX_READY):
        code, reply = yield ("POST", "/ready", {"version": version})
        if code == 200 and reply["closed"]:
            return True
        if code == 409:
            if "version" not in reply:
                return True
            version = reply["version"]
        else:
            assert code == 200 and reply["waiting_for"]
    return False


def write(rules, own, omission, capacity):
    start = yield from get("/register")
    lines, version = start["lines"], start["version"]
    if capacity:
        planned = capacity_order(rules, own, lines, capacity)
    else:
        planned = proposals(rules, own, lines, omission)
    assert len(planned) <= MAX_PUTS
    for line in planned:
        code, reply = yield from put(line)
        if code == 200:
            version = reply["version"]
    return (yield from close(version))


def read_guess(site, zones, own, lines, omission):
    filtered = [line for line in register_entries(lines) if line["status"] != omission]
    if omission is None:
        return value(site, zones, own + filtered)
    if not any(line["key"] == site for line in filtered):
        markers = local_markers(own)
        if omission in markers:
            return omission
    return value(site, zones, own + filtered)


def clerk(seat, writer=None, reader=None, capacity=None, skip_shared=False):
    rules = yield from get("/rules")
    own = (yield from get("/feed"))["reports"]
    if rules["phase"] == "writing":
        if writer == "local":
            version = 0
            for line in summarize(selected(rules, own)).values():
                code, reply = yield from put(line)
                if code == 200:
                    version = reply["version"]
            closed = yield from close(version)
        else:
            closed = yield from write(rules, own, writer, capacity)
        if not closed:
            return
    asks = (yield from get("/asks"))["asks"]
    lines = (yield from get("/register"))["lines"]
    zones = zones_of(rules)
    skipped = False
    for site in asks:
        status = read_guess(site, zones, own, lines, reader)
        if skip_shared and not skipped and value(site, zones, own) == status:
            skipped = True
            continue
        code, reply = yield ("POST", "/answer", {"site": site, "status": status})
        assert code == 200, reply


def blind_answer(site, asks, own, rules, mode):
    zones = zones_of(rules)
    summaries = summarize(own)
    markers = local_markers(own)
    rows = [summaries[s] for s in asks if s in summaries]
    newest = max(rows, key=lambda x: x["at"])["key"] if rows else None
    if site == newest:
        return value(site, zones, own)
    if len(markers) == 3 and site not in summaries:
        return "closed"
    others = [s for s in asks if s != newest and s in summaries]
    if len(markers) == 1 and len(others) == 2:
        guessed_special = min(others, key=lambda s: (summaries[s]["at"], s))
        if mode == "zone":
            density = Counter(zones[s] for s in rules["watch"])
            guessed_special = max(others, key=lambda s: (density[zones[s]], s))
        if site == guessed_special:
            return next(iter(markers))
    if len(markers) == 3 and site in others:
        if len(others) == 1:
            return "withdrawn" if mode == "time" else "disputed"
        return "disputed" if site == min(others, key=lambda s: summaries[s]["at"]) else "withdrawn"
    ordinary = rules["statuses"][:6]
    former = summaries.get(site, {}).get("status")
    return next(status for status in ordinary if status != former)


def blind(seat, mode):
    rules = yield from get("/rules")
    own = (yield from get("/feed"))["reports"]
    if rules["phase"] == "writing":
        version = 0
        for line in summarize(selected(rules, own)).values():
            code, reply = yield from put(line)
            if code == 200:
                version = reply["version"]
        if not (yield from close(version)):
            return
    asks = (yield from get("/asks"))["asks"]
    for site in asks:
        status = blind_answer(site, asks, own, rules, mode)
        code, reply = yield ("POST", "/answer", {"site": site, "status": status})
        assert code == 200, reply


full = partial(clerk)
no_pool = partial(clerk, writer="pool")
no_tombstone_writer = partial(clerk, writer="withdrawn")
no_cascade_writer = partial(clerk, writer="closed")
no_dispute_writer = partial(clerk, writer="dispute")
no_tombstone_reader = partial(clerk, reader="withdrawn")
no_cascade_reader = partial(clerk, reader="closed")
no_dispute_reader = partial(clerk, reader="disputed")
no_select_density = partial(clerk, capacity="density")
no_select_overlap = partial(clerk, capacity="overlap")
no_select_distance = partial(clerk, capacity="distance")
no_register_time = partial(blind, mode="time")
no_register_zone = partial(blind, mode="zone")
skip_shared = partial(clerk, skip_shared=True)
local_writer = partial(clerk, writer="local")


SPLITS = (
    "site_low", "site_high", "ask_first", "ask_last", "zone_first", "zone_last",
    "neighbor_early", "neighbor_late", "private_present", "private_absent",
)


def split_candidates(seat, rules, asks, own, filtered, omitted):
    zones = zones_of(rules)
    shared = register_entries(filtered)
    baseline = {site: value(site, zones, own + shared) for site in asks}
    candidates = []
    for site in asks:
        line = filtered.get(site)
        stale_copy = (omitted == "closed" and line is not None and line["by"] != seat
                      and any(report["key"] == site and report["at"] == line["at"]
                              and report["status"] == line["status"] for report in own))
        if (line is None or stale_copy) and baseline[site] not in SPECIAL:
            candidates.append(site)
    return baseline, candidates


def choose_split(rules, asks, own, filtered, candidates, mode):
    zones = zones_of(rules)
    zone_order = list(rules["zones"])
    if mode.startswith("site_"):
        return sorted(candidates, reverse=mode == "site_high")[0]
    if mode.startswith("ask_"):
        return sorted(candidates, key=asks.index, reverse=mode == "ask_last")[0]
    if mode.startswith("zone_"):
        def key(site):
            return zone_order.index(zones[site]), asks.index(site)
        return sorted(candidates, key=key, reverse=mode == "zone_last")[0]
    if mode.startswith("neighbor_"):
        def key(site):
            at = max((line["at"] for k, line in filtered.items()
                      if k not in candidates and zones.get(k) == zones[site]
                      and line["status"] not in SPECIAL), default=0)
            return at, asks.index(site)
        return sorted(candidates, key=key, reverse=mode == "neighbor_late")[0]
    assert mode in ("private_present", "private_absent")
    def key(site):
        return any(report["key"] == site for report in own), asks.index(site)
    return sorted(candidates, key=key, reverse=mode == "private_present")[0]


def split_reader(seat, omitted, mode):
    rules = yield from get("/rules")
    own = (yield from get("/feed"))["reports"]
    if rules["phase"] == "writing" and not (yield from write(rules, own, None, None)):
        return
    asks = (yield from get("/asks"))["asks"]
    lines = (yield from get("/register"))["lines"]
    filtered = {key: line for key, line in lines.items() if line["status"] != omitted}
    baseline, candidates = split_candidates(seat, rules, asks, own, filtered, omitted)
    if len(candidates) > 1 and omitted in local_markers(own):
        chosen = choose_split(rules, asks, own, filtered, candidates, mode)
        for site in candidates:
            baseline[site] = omitted if site == chosen else "unknown"
    for site in asks:
        code, reply = yield ("POST", "/answer", {"site": site, "status": baseline[site]})
        assert code == 200, reply


for omitted in SPECIAL:
    for mode in SPLITS:
        globals()[f"{omitted}_{mode}"] = partial(split_reader, omitted=omitted, mode=mode)
