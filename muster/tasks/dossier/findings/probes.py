from collections import Counter
from functools import partial
from statistics import mean

from muster.tasks.dossier.zoo import ALL_SKILLS, entries, get, status_of, writing, zones_of

SPECIAL = ("withdrawn", "closed", "disputed")


def reader_candidates(rules, own, asks, lines, omitted):
    zones = zones_of(rules)
    base = {s: status_of(s, zones[s], own + entries(lines)) for s in asks}
    if omitted == "closed" and len(rules["zones"]) == 6:
        candidates = [s for s in asks if s in lines and lines[s]["by"] == "p2"
                      and not any(r["key"] == s for r in own)]
    else:
        candidates = [s for s in asks if base[s] == "unknown"]
    return base, candidates


def feature_rows(rules, own, asks, lines, candidates):
    zone_of = zones_of(rules)
    watch = set(rules["watch"])
    result = {}
    ordered = list(lines)
    for site in candidates:
        zone = zone_of[site]
        members = rules["zones"][zone]
        neighboring = [k for k in members if k != site and k in lines]
        peers = [lines[k] for k in neighboring]
        neighbor_times = [r["at"] for r in peers]
        own_here = [r for r in own if r["key"] in members and r["key"] != site]
        off = [r for r in own_here if r["key"] not in watch]
        ranks = [ordered.index(k) for k in neighboring]
        own_ranks = [i for i, r in enumerate(own) if r in off]
        line = lines.get(site, {})
        result[site] = {
            "ask_index": asks.index(site), "site_number": int(site[1:]),
            "zone_index": list(rules["zones"]).index(zone),
            "within_zone_index": members.index(site),
            "zone_size": len(members), "watch_density": len(watch.intersection(members)),
            "other_asks_here": len(set(asks).intersection(members)) - 1,
            "nonask_watch_here": len(watch.intersection(members) - set(asks)),
            "line_time": line.get("at", 0), "line_rank": ordered.index(site) if site in lines else -1,
            "neighbor_count": len(neighboring), "neighbor_min": min(neighbor_times, default=0),
            "neighbor_max": max(neighbor_times, default=0),
            "neighbor_mean": mean(neighbor_times) if neighbor_times else 0,
            "neighbor_rank_min": min(ranks, default=-1),
            "neighbor_rank_max": max(ranks, default=-1),
            "neighbor_p2": sum(r["by"] == "p2" for r in peers),
            "neighbor_p3": sum(r["by"] == "p3" for r in peers),
            "own_neighbor_count": len(own_here),
            "own_neighbor_max": max((r["at"] for r in own_here), default=0),
            "off_count": len(off), "off_min": min((r["at"] for r in off), default=0),
            "off_max": max((r["at"] for r in off), default=0),
            "off_mean": mean(r["at"] for r in off) if off else 0,
            "off_rank_min": min(own_ranks, default=-1), "off_rank_max": max(own_ranks, default=-1),
            "off_late": sum(r["at"] >= 21 for r in off),
        }
    return result


def infer_read(rules, own, asks, lines, omitted, feature, high):
    visible = {k: r for k, r in lines.items() if r["status"] != omitted}
    base, candidates = reader_candidates(rules, own, asks, visible, omitted)
    if len(candidates) != 4:
        return base
    features = feature_rows(rules, own, asks, visible, candidates)
    chosen = sorted(candidates, key=lambda s: features[s][feature], reverse=high)[:2]
    return {s: omitted if s in chosen else value for s, value in base.items()}


def answer_all(rules, own, omitted=None, feature=None, high=False):
    asks = (yield from get("/asks"))["asks"]
    lines = (yield from get("/register"))["lines"]
    zones = zones_of(rules)
    if omitted is None:
        data = own + entries(lines)
        answers = {s: status_of(s, zones[s], data) for s in asks}
    else:
        answers = infer_read(rules, own, asks, lines, omitted, feature, high)
    for site, status in answers.items():
        code, _ = yield "POST", "/answer", {"site": site, "status": status}
        assert code == 200


def reader(seat, omitted, feature, high):
    rules = yield from get("/rules")
    own = (yield from get("/feed"))["reports"]
    if not (yield from writing(seat, rules, own, ALL_SKILLS, "normal")):
        return
    yield from answer_all(rules, own, omitted, feature, high)


def summarize(own):
    result = {}
    for r in own:
        old = result.get(r["key"])
        if old is None or old["at"] < r["at"]:
            result[r["key"]] = dict(r)
        elif old["at"] == r["at"] and old["status"] != r["status"]:
            result[r["key"]] = {**r, "status": "disputed"}
    return result


def proposals(rules, own, start, mode):
    zones = zones_of(rules)
    summary = summarize(own)
    pending = []
    for key, line in summary.items():
        old = start.get(key)
        if old is not None and old["at"] >= line["at"]:
            if old["at"] == line["at"] and old["status"] not in (line["status"], "disputed"):
                pending.append({**line, "status": "disputed"})
            continue
        zone = zones.get(key, key)
        closing = summary.get(zone) or start.get(zone)
        if key in zones and closing and closing["at"] > line["at"]:
            continue
        pending.append(line)
    count = Counter(zones[k] for k in summary if k in zones)
    fixed = Counter(zones[k] for k in start if k in zones)
    for k in start:
        if k in rules["zones"]:
            fixed[k] += 1
    known = Counter(zones[r["key"]] for r in pending if r["key"] in zones and
                    (r["key"] in start or r["status"] in SPECIAL))
    for r in pending:
        if r["key"] in rules["zones"]:
            fixed[r["key"]] += 1
    def priority(r):
        key = r["key"]
        zone = zones.get(key, key)
        first = (key not in start, r["status"] not in SPECIAL)
        if mode == "time_high":
            return (*first, -r["at"])
        if mode == "time_band":
            return (*first, r["at"] < 21)
        if mode == "local_count":
            return (*first, count[zone] <= 2, r["at"] < 21, -count[zone])
        if mode == "peer_only":
            return (*first, fixed[zone] + known[zone] >= 2, -count[zone])
        if mode == "peer_time":
            return (*first, fixed[zone] + known[zone] >= 2, r["at"] < 21)
        if mode == "peer_capacity":
            return (*first, fixed[zone] + known[zone] >= 2, r["at"] < 21, -count[zone])
        assert mode == "combined"
        return (*first, count[zone] <= 2 or fixed[zone] + known[zone] >= 2,
                r["at"] < 21, -count[zone])
    return sorted(pending, key=priority)


def writer(seat, mode):
    rules = yield from get("/rules")
    own = (yield from get("/feed"))["reports"]
    start = yield from get("/register")
    version = start["version"]
    for line in proposals(rules, own, start["lines"], mode):
        code, reply = yield "POST", "/put", line
        assert code in (200, 409)
        if code == 200:
            version = reply["version"]
    for _ in range(60):
        code, reply = yield "POST", "/ready", {"version": version}
        if code == 200 and reply["closed"]:
            yield from answer_all(rules, own)
            return
        if code == 409:
            version = reply["version"]


reader_time_early = partial(reader, omitted="closed", feature="line_time", high=False)
reader_time_late = partial(reader, omitted="closed", feature="line_time", high=True)
writer_time_high = partial(writer, mode="time_high")
writer_time_band = partial(writer, mode="time_band")
writer_local_count = partial(writer, mode="local_count")
writer_peer_capacity = partial(writer, mode="peer_capacity")
writer_combined = partial(writer, mode="combined")
writer_peer_time = partial(writer, mode="peer_time")
reader_gone_zone_position = partial(reader, omitted="withdrawn", feature="within_zone_index", high=False)
reader_dispute_local_time = partial(reader, omitted="disputed", feature="own_neighbor_max", high=True)
reader_mix_closed_off_time = partial(reader, omitted="closed", feature="off_max", high=True)
reader_mix_closed_off_order = partial(reader, omitted="closed", feature="off_rank_min", high=True)
reader_mix_closed_neighbor_time = partial(reader, omitted="closed", feature="own_neighbor_max", high=True)
reader_mix_closed_site_number = partial(reader, omitted="closed", feature="site_number", high=False)
reader_mix_closed_zone_position = partial(reader, omitted="closed", feature="within_zone_index", high=False)
reader_mix_dispute_off_time = partial(reader, omitted="disputed", feature="off_max", high=True)
