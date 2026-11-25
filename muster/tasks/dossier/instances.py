from collections import Counter
from fractions import Fraction
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from math import comb
from pathlib import Path
from random import Random

from muster.tasks.dossier.template.verify import (
    entries,
    register_diagnostics,
    site_zones,
    status_of,
)
from muster.tasks.dossier.zoo import next_put

SEED = "dossier-v3.3"
COUNT = 10
FACETS = ("share", "scarce", "gone", "cascade", "dispute", "mix")
ARMS = ("probe", "reader", "solo", "team")
CLERKS = ("p1", "p2", "p3")
ORDINARY = ("open", "limited", "busy", "quiet", "repair", "ready")
STATUSES = (*ORDINARY, "withdrawn", "closed", "disputed", "unknown")
ZONE_NAMES = ("north", "south", "east", "west")
MAX_DRAWS = 4096
SCARCE = ("scarce", "mix")
SPECIAL = {"gone": "withdrawn", "cascade": "closed", "dispute": "disputed"}
REGISTER_ORDER = ("p2", "p3", "p1")
ZONE_SIZE = 4
CLOSURE_ZONE_NAMES = (*ZONE_NAMES, "central", "outer")
MIX_ZONE_NAMES = (*CLOSURE_ZONE_NAMES, "inner", "upper", "lower")
MIX_ZONE_SIZE = 4
CASCADE_ZONE_SIZE = 5
MIX_DECOYS = 18
KIT = Path(__file__).resolve().parents[1] / "_kit" / "build.py"
_spec = spec_from_file_location("dossier_build", KIT)
assert _spec is not None and _spec.loader is not None
build = module_from_spec(_spec)
_spec.loader.exec_module(build)


def conditions():
    return [f"{facet}-{arm}" for facet in FACETS for arm in ARMS]


def rng(facet, index):
    return Random(int.from_bytes(sha256(f"{SEED}/{facet}/{index}".encode()).digest(), "big"))


def entry(key, status, at):
    return {"key": key, "status": status, "at": at}


def zone_shape(facet):
    if facet == "mix":
        return MIX_ZONE_NAMES, MIX_ZONE_SIZE
    if facet == "cascade":
        return CLOSURE_ZONE_NAMES, CASCADE_ZONE_SIZE
    return ZONE_NAMES, ZONE_SIZE


def layout(draw, facet="share"):
    names, width = zone_shape(facet)
    sites = [f"s{i:02}" for i in range(1, len(names) * width + 1)]
    draw.shuffle(sites)
    names = list(names)
    draw.shuffle(names)
    zones = {zone: sites[i * width:(i + 1) * width] for i, zone in enumerate(names)}
    roles = draw.sample(sites, 9)
    asks = {seat: roles[i * 3:i * 3 + 3] for i, seat in enumerate(CLERKS)}
    unused = [site for site in sites if site not in roles]
    draw.shuffle(unused)
    for row in zones.values():
        draw.shuffle(row)
    return zones, asks, unused


def site_case(feeds, site, reader, writer, kind, zones, draw, witness, *, facet):
    if kind == "unknown":
        return "unknown"
    good = draw.choice(ORDINARY)
    old = draw.choice([status for status in ORDINARY if status != good])
    early, late = sorted(draw.sample(range(1, 41), 2))
    at = late if kind in ("cascade", "gone") or witness is not None else draw.randint(1, 40)
    if facet == "mix":
        early = draw.randint(1, 10)
        at = draw.randint(11, 20) if kind == "cascade" else draw.randint(21, 40)
    if kind == "cascade":
        prior_holder = writer if facet == "mix" else reader
        feeds[prior_holder].append(entry(site, old, early))
        feeds[writer].append(entry(zones[site], "closed", at))
    elif kind == "dispute":
        if reader == "p1":
            other = next(seat for seat in CLERKS[1:] if seat != writer)
            feeds[other].append(entry(site, old, at))
        else:
            feeds[writer].append(entry(site, old, at))
            if facet == "mix":
                feeds[reader].append(entry(site, old, early))
        feeds[writer].append(entry(site, good, at))
    elif kind == "gone":
        if reader != "p1":
            feeds[reader].append(entry(site, old, early))
        feeds[writer].append(entry(site, "withdrawn", at))
    else:
        feeds[writer].append(entry(site, good, at))
        if witness is not None:
            feeds[witness].append(entry(site, old, early))
    return good


def mixed_cases(asks, zones, draw):
    pairs = [draw.sample(row, 2) for row in zones.values()]
    draw.shuffle(pairs)
    a, b, u, v, w, x, y, z, d = [pair[0] for pair in pairs]
    free = [pair[1] for pair in pairs]
    draw.shuffle(free)
    c, e, f, g, h, i, j, k, extra_peer = free
    asks["p1"][:] = [a, b, u, v, w, x, y, z, c]
    asks["p2"][:] = [d, e, f, k]
    asks["p3"][:] = [g, h, i, j, extra_peer]
    outgoing = ["gone", "dispute"]
    draw.shuffle(outgoing)
    return [(a, "p1", "p2", "cascade", None),
            (b, "p1", "p3", "cascade", None),
            (u, "p1", "p1", "unknown", None),
            (v, "p1", "p1", "unknown", None),
            (w, "p1", draw.choice(CLERKS[1:]), "gone", None),
            (x, "p1", draw.choice(CLERKS[1:]), "gone", None),
            (y, "p1", draw.choice(CLERKS[1:]), "dispute", None),
            (z, "p1", draw.choice(CLERKS[1:]), "dispute", None),
            (c, "p1", "p2", "normal", "p1"),
            (d, "p2", "p1", "cascade", None),
            (e, "p2", "p1", outgoing[0], None),
            (f, "p2", "p2", "normal", None),
            (g, "p3", "p1", outgoing[1], None),
            (h, "p3", "p1", "normal", None),
            (i, "p3", "p3", "normal", None),
            (j, "p3", "p1", "normal", None),
            (k, "p2", "p1", "normal", None),
            (extra_peer, "p3", "p1", "normal", None)]


def cascade_cases(asks, zones, draw):
    names = list(zones)
    draw.shuffle(names)
    a, b, u, v = [draw.choice(zones[zone]) for zone in names[:4]]
    d, *neighbors = draw.sample(zones[names[4]], CASCADE_ZONE_SIZE)
    unknown_a, unknown_b, *other = draw.sample(zones[names[5]], CASCADE_ZONE_SIZE)
    free = [*neighbors, *other]
    draw.shuffle(free)
    c, e, f, g, h, i, j = free
    asks["p1"][:] = [a, b, u, v, unknown_a, unknown_b, c, j]
    asks["p2"][:] = [d, e, f]
    asks["p3"][:] = [g, h, i]
    return [(a, "p1", "p3", "cascade", None),
            (b, "p1", "p3", "cascade", None),
            (u, "p1", "p2", "matched", None),
            (v, "p1", "p2", "matched", None),
            (unknown_a, "p1", "p1", "unknown", None),
            (unknown_b, "p1", "p1", "unknown", None),
            (c, "p1", "p2", "normal", "p1"),
            (j, "p1", "p1", "normal", "p2"),
            (d, "p2", "p1", "cascade", None),
            (e, "p2", "p1", "normal", None),
            (f, "p2", "p2", "normal", None),
            (g, "p3", "p1", "normal", None),
            (h, "p3", "p2", "normal", None),
            (i, "p3", "p3", "normal", None)]


def closure_cases(facet, asks, unused, zones, draw):
    roles = mixed_cases(asks, zones, draw) if facet == "mix" else (
        cascade_cases(asks, zones, draw))
    watched = {role[0] for role in roles}
    unused[:] = [site for row in zones.values() for site in row if site not in watched]
    draw.shuffle(unused)
    return roles


def cases(facet, asks, unused, zones, draw):
    if facet in ("cascade", "mix"):
        return closure_cases(facet, asks, unused, zones, draw)
    a, b, c = asks["p1"]
    d, e, f = asks["p2"]
    g, h, i = asks["p3"]
    special = facet if facet in SPECIAL else "normal"
    if draw.randrange(2):
        a, b = b, a
    incoming = [(a, "p1", "p3", special, None if facet in SPECIAL else "p1"),
                (b, "p1", "p2", "normal", "p1")]
    if draw.randrange(2):
        d, e = e, d
    if facet in SPECIAL:
        second, u, v = unused[:3]
        del unused[:3]
        asks["p1"].extend((second, u, v))
        incoming.extend([(second, "p1", "p2", special, None),
                         (u, "p1", "p1", "unknown", None),
                         (v, "p1", "p1", "unknown", None)])
    return [*incoming,
            (d, "p2", "p1", special, None),
            (e, "p2", "p1", "normal", None),
            (c, "p1", "p1", "normal", "p2"),
            (f, "p2", "p2", "normal", None),
            (g, "p3", "p1", "normal", None),
            (h, "p3", "p2", "normal", None),
            (i, "p3", "p3", "normal", None)]


def reports_for(facet, roles, zones, unused, draw):
    feeds = {seat: [] for seat in CLERKS}
    expected = {}
    zone_of = site_zones({"zones": zones})
    matched = draw.choice(ORDINARY) if facet == "cascade" else None
    for site, reader, writer, kind, witness in roles:
        if facet == "cascade" and reader == "p1" and kind in ("cascade", "matched"):
            feeds["p2"].append(entry(site, matched, draw.randint(1, 20)))
            if kind == "cascade":
                feeds[writer].append(entry(zone_of[site], "closed", draw.randint(21, 40)))
            expected[site] = "closed" if kind == "cascade" else matched
        else:
            good = site_case(feeds, site, reader, writer, kind, zone_of, draw, witness,
                             facet=facet)
            expected[site] = SPECIAL.get(kind, good)
    if facet in SCARCE:
        count = MIX_DECOYS if facet == "mix" else 5
        feeds["p1"].extend(entry(site, draw.choice(ORDINARY), draw.randint(1, 40))
                           for site in unused[:count])
    for feed in feeds.values():
        draw.shuffle(feed)
    return feeds, expected


def relevant_keys(data):
    return set(data["watch"]) | {zone for zone, sites in data["zones"].items()
                                 if set(sites) & set(data["watch"])}


def reported_watch_keys(data):
    relevant = relevant_keys(data)
    return {line["key"] for feed in data["feeds"].values() for line in feed
            if line["key"] in relevant}


def produce(data, clerks):
    register = {}
    counts = Counter()
    for seat in clerks:
        for _ in range(32):
            line = next_put(data["feeds"][seat], register, data["watch"], data["zones"])
            if line is None:
                break
            key = line["key"]
            register[key] = {"status": line["status"], "at": line["at"], "by": seat}
            counts[seat] += 1
            assert len(register) <= data["slots"]
        else:
            raise AssertionError("produce bound")
    assert all(count <= len(data["feeds"][seat]) + 4 for seat, count in counts.items())
    return register


def truth_of(zones, asks, feeds):
    reports = [report for feed in feeds.values() for report in feed]
    zone_of = site_zones({"zones": zones})
    return {site: status_of(site, zone_of[site], reports) for row in asks.values() for site in row}


def writer_focal(data):
    peers = data["feeds"]["p2"] + data["feeds"]["p3"]
    zone_of = site_zones(data)
    return [site for seat in CLERKS[1:] for site in data["asks"][seat]
            if status_of(site, zone_of[site], peers) != data["truth"][site]]

def register_required(data, ideal):
    zones = site_zones(data)
    if not set(ideal) <= reported_watch_keys(data) or len(ideal) > data["slots"]:
        return False
    reports = [r for feed in data["feeds"].values() for r in feed]
    for key in reported_watch_keys(data) - set(ideal):
        if key not in zones or not any(
                r["key"] == zones[key] and r["status"] == "closed" and
                r["at"] > max(s["at"] for s in reports if s["key"] == key) for r in reports):
            return False
    for key, line in ideal.items():
        remaining = entries({k: v for k, v in ideal.items() if k != key})
        if not any(status_of(site, zones[site], data["feeds"][seat] + remaining)
                   != data["truth"][site]
                   for seat, row in data["asks"].items() if seat != line["by"]
                   for site in row):
            return False
    return True


def add_peer_asks(asks, facet):
    control = asks["p1"][2:4] if facet == "mix" else asks["p1"][2:3]
    peer2, peer3 = asks["p2"][2], asks["p3"][2]
    asks["p2"].append(peer3)
    asks["p3"].extend((*control, peer2))


def case_dependencies(data, roles):
    zone_of = site_zones(data)
    feeds, truth = data["feeds"], data["truth"]
    expected_focal = {site for site, reader, writer, _, _ in roles
                      if writer == "p1" and reader != "p1"}
    if data["facet"] in SCARCE:
        expected_focal.update(site for site, reader, writer, kind, _ in roles
                              if reader == writer == "p1" and kind != "unknown")
    if set(data["writer_focal"]) != expected_focal:
        return False
    for site, reader, writer, kind, _ in roles:
        if reader == "p1":
            local = status_of(site, zone_of[site], feeds[reader])
            if (local == truth[site]) != (writer == reader):
                return False
        if kind == "dispute" and reader != "p1":
            tie = [line for line in feeds["p1"] if line["key"] == site]
            assert len(tie) == 2 and tie[0]["at"] == tie[1]["at"]
            assert tie[0]["status"] != tie[1]["status"]
            assert not any(line["key"] == site and line["at"] == tie[0]["at"]
                           for peer in CLERKS[1:] for line in feeds[peer])
    return True


def writing_orders(feed):
    return (feed, list(reversed(feed)), sorted(feed, key=lambda line: line["at"]),
            sorted(feed, key=lambda line: -line["at"]))


def capacity_trap(data, ideal):
    start = produce(data, CLERKS[1:])
    zones = site_zones(data)
    required = set()
    for key in set(ideal) - set(start):
        without = entries({k: v for k, v in ideal.items() if k != key})
        if any(status_of(site, zones[site], data["feeds"][seat] + without) != data["truth"][site]
               for seat in CLERKS[1:] for site in data["asks"][seat]
               if site in data["writer_focal"]):
            required.add(key)
    if required != set(ideal) - set(start):
        return False
    for order in writing_orders(data["feeds"]["p1"]):
        keys = set(start)
        for line in order:
            if len(keys) == data["slots"]:
                break
            keys.add(line["key"])
        missing = len(required - keys)
        if len(keys) != data["slots"] or missing < (2 if data["facet"] == "mix" else 1):
            return False
    return True


def filtered_chance(data, ideal, omitted):
    filtered = {key: line for key, line in ideal.items() if line["status"] != omitted}
    zones = site_zones(data)
    own = {line["key"] for line in data["feeds"]["p1"]}
    groups = {}
    for site in data["asks"]["p1"]:
        line = filtered.get(site, {})
        signature = (bool(line), line.get("by"), site in own, zones[site] in filtered,
                     line.get("status"))
        groups.setdefault(signature, []).append(data["truth"][site])
    chance = Fraction(1)
    for truths in groups.values():
        chance /= comb(len(truths), truths.count(omitted))
    return chance


def special_dependencies(data, roles, ideal):
    facet = data["facet"]
    if facet not in (*SPECIAL, "mix"):
        return True
    zones = site_zones(data)
    own = data["feeds"]["p1"]
    truth = data["truth"]
    unknown = [site for site in data["asks"]["p1"] if truth[site] == "unknown"
               and site not in ideal and not any(line["key"] == site for line in own)]
    if len(unknown) != 2:
        return False
    incoming = Counter()
    for site, reader, writer, kind, _ in roles:
        if reader != "p1" or writer == "p1" or kind not in SPECIAL:
            continue
        incoming[SPECIAL[kind]] += 1
        key = zones[site] if kind == "cascade" else site
        if key not in ideal or ideal[key]["status"] != SPECIAL[kind]:
            return False
        if any(line["key"] == site for line in own):
            return False
        remaining = entries({k: v for k, v in ideal.items() if k != key})
        if status_of(site, zones[site], own + remaining) == truth[site]:
            return False
        if kind == "cascade":
            if not any(line["key"] == site and line["at"] < ideal[key]["at"]
                       for feed in data["feeds"].values() for line in feed):
                return False
            if facet == "cascade" and (
                    site not in ideal or ideal[site]["at"] >= ideal[key]["at"]):
                return False
    expected = Counter({kind: 2 for kind in SPECIAL.values()}) if facet == "mix" else (
        Counter({SPECIAL[facet]: 2}))
    return incoming == expected and all(
        filtered_chance(data, ideal, kind) <= Fraction(1, 6) for kind in incoming)


def validate(data, ideal, roles):
    zones, asks, feeds, truth = (data[k] for k in ("zones", "asks", "feeds", "truth"))
    facet = data["facet"]
    names, width = zone_shape(facet)
    assert set(zones) == set(names) and all(len(row) == width for row in zones.values())
    assert set(data["zone_order"]) == set(zones) and len(data["zone_order"]) == len(zones)
    assert len({site for row in zones.values() for site in row}) == len(zones) * width
    assert tuple(feeds) == tuple(asks) == CLERKS
    lengths = {"mix": [9, 5, 8], "scarce": [3, 4, 5], "cascade": [8, 3, 3],
               "gone": [6, 3, 3], "dispute": [6, 3, 3]}.get(facet, [3, 3, 3])
    assert [len(row) for row in asks.values()] == lengths
    assert all(len(row) == len(set(row)) for row in asks.values())
    assert sorted(truth) == sorted(data["watch"]) and len(truth) == (
        18 if facet == "mix" else 14 if facet == "cascade" else 12 if facet in SPECIAL else 9)
    assert list(truth.values()).count("unknown") == 2 * int(facet in (*SPECIAL, "mix"))
    for feed in feeds.values():
        assert all(type(line["at"]) is int and 1 <= line["at"] <= 40 for line in feed)
        assert len({(line["key"], line["at"], line["status"]) for line in feed}) == len(feed)
    zone_of = site_zones(data)
    for seat, row in asks.items():
        assert all(status_of(site, zone_of[site], feeds[seat] + entries(ideal)) == truth[site]
                   for site in row)
        if seat != "p1":
            assert 0 < len(set(row) & set(data["writer_focal"])) < len(row)
    assert case_dependencies(data, roles)
    assert special_dependencies(data, roles, ideal)
    assert register_diagnostics(ideal, {**data, "arm": "probe"})["register_sufficient"] == 1.0
    assert len(ideal) <= len(reported_watch_keys(data)) <= data["slots"]
    if data["facet"] in SCARCE:
        assert len(reported_watch_keys(data)) == data["slots"]
        assert register_required(data, ideal)
        decoys = [line for line in feeds["p1"] if line["key"] not in relevant_keys(data)]
        if facet == "mix":
            assert all(len(set(row) & set(data["watch"])) == 2 for row in zones.values())
        assert len({line["key"] for line in decoys}) == len(decoys) == (
            MIX_DECOYS if facet == "mix" else 5)
        assert capacity_trap(data, ideal)


def candidate(facet, index, draw):
    zones, asks, unused = layout(draw, facet)
    roles = cases(facet, asks, unused, zones, draw)
    feeds, expected = reports_for(facet, roles, zones, unused, draw)
    truth = truth_of(zones, asks, feeds)
    if truth != expected:
        return None
    if any(len({(r["key"], r["at"], r["status"]) for r in feed}) != len(feed)
           for feed in feeds.values()):
        return None
    if facet in SCARCE:
        add_peer_asks(asks, facet)
    for row in asks.values():
        draw.shuffle(row)
    data = {"facet": facet, "clerks": list(CLERKS), "zones": zones, "zone_order": list(zones),
            "statuses": list(STATUSES), "feeds": feeds, "asks": asks, "truth": truth,
            "watch": sorted(truth)}
    data["slots"] = len(reported_watch_keys(data)) + (0 if facet in SCARCE else 6)
    data["writer_focal"] = writer_focal(data)
    if not case_dependencies(data, roles):
        return None
    ideal = produce(data, REGISTER_ORDER)
    if not special_dependencies(data, roles, ideal):
        return None
    if facet in SCARCE and (not register_required(data, ideal) or not capacity_trap(data, ideal)):
        return None
    validate(data, ideal, roles)
    return data, ideal


def base(facet, index):
    assert facet in FACETS and type(index) is int and 0 <= index < COUNT
    draw = rng(facet, index)
    for _ in range(MAX_DRAWS):
        result = candidate(facet, index, draw)
        if result is not None:
            return result
    raise AssertionError((facet, index, "draw bound"))


def stage_for(arm, real):
    clerks = REGISTER_ORDER if arm == "reader" else CLERKS[1:] if arm == "probe" else ()
    units = [{"id": f"start-{seat}", "seat": seat} for seat in clerks]
    rules = [{"id": "start", "when": "start", "units": [unit["id"] for unit in units]}] if units else []
    return {"real": real, "units": units, "rules": rules}


def make_instance(data, ideal, arm, index):
    facet = data["facet"]
    condition = f"{facet}-{arm}"
    number = (FACETS.index(facet) * len(ARMS) + ARMS.index(arm)) * COUNT + index
    real = list(CLERKS) if arm == "team" else ["p1"]
    result = {**data, "id": f"i{index + 1:02}", "condition": condition,
              "arm": arm, "real": real, "channel": False,
              "tokens": {seat: build.token(SEED, number, seat) for seat in CLERKS}}
    if arm == "reader":
        result["ideal"] = ideal
    if arm == "solo":
        result["clerks"] = ["p1"]
        result["feeds"] = {"p1": [line for feed in data["feeds"].values() for line in feed]}
        result["asks"] = {"p1": list(dict.fromkeys(
            site for row in data["asks"].values() for site in row))}
        result["slots"] = 0
    if arm != "probe":
        result["writer_focal"] = []
    result["budgets"] = {seat: {"put": len(feed) + 4, "answer": len(result["asks"][seat])}
                         for seat, feed in result["feeds"].items()}
    result["stage"] = stage_for(arm, real)
    return result


def instance(facet, arm, index):
    assert arm in ARMS
    data, ideal = base(facet, index)
    arms = {name: make_instance(data, ideal, name, index) for name in ARMS}
    for name in ("probe", "reader", "team"):
        assert arms[name]["feeds"] == data["feeds"] and arms[name]["asks"] == data["asks"]
    assert arms["solo"]["feeds"]["p1"] == [line for feed in data["feeds"].values() for line in feed]
    assert arms["solo"]["asks"]["p1"] == list(dict.fromkeys(
        site for row in data["asks"].values() for site in row))
    assert all(value["truth"] == data["truth"] and value["watch"] == data["watch"]
               for value in arms.values())
    return arms[arm]


def instances(condition):
    assert condition in conditions()
    facet, arm = condition.split("-")
    result = [instance(facet, arm, index) for index in range(COUNT)]
    counts = Counter(row["truth"][site] for row in result
                     for seat, asks in row["asks"].items() for site in asks
                     if seat in row["real"] or site in row["writer_focal"])
    assert max(counts.values()) / sum(counts.values()) <= 0.34, (condition, counts)
    required = {"gone": {"withdrawn"}, "cascade": {"closed"}, "dispute": {"disputed"},
                "mix": {"withdrawn", "closed", "disputed"}}.get(facet, set())
    if facet in (*SPECIAL, "mix"):
        required = required | {"unknown"}
    assert set(counts) - set(ORDINARY) == required
    if condition == "mix-reader":
        special_counts = Counter(row["truth"][site] for row in result
                                 for site in row["asks"]["p1"]
                                 if row["truth"][site] in SPECIAL.values())
        assert sum(special_counts.values()) == COUNT * 6
        assert max(special_counts.values()) - min(special_counts.values()) <= 1
    return result
