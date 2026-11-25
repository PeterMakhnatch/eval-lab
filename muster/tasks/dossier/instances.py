from collections import Counter
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from random import Random

from muster.tasks.dossier.template.verify import (
    entries,
    register_diagnostics,
    site_zones,
    status_of,
)
from muster.tasks.dossier.zoo import next_put

SEED = "dossier-v3.2"
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
MIX_ZONE_SIZE = 6
MIX_DECOYS = 12
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


def layout(draw, facet="share"):
    width = MIX_ZONE_SIZE if facet == "mix" else ZONE_SIZE
    sites = [f"s{i:02}" for i in range(1, len(ZONE_NAMES) * width + 1)]
    draw.shuffle(sites)
    names = list(ZONE_NAMES)
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
    if kind == "cascade":
        prior_holder = writer if facet == "mix" and reader != "p1" else reader
        feeds[prior_holder].append(entry(site, old, early))
        if facet == "cascade" and reader == "p1":
            assert writer == "p3"
            feeds["p2"].append(entry(site, old, early))
        feeds[writer].append(entry(zones[site], "closed", at))
    elif kind == "dispute":
        if reader == "p1":
            other = next(seat for seat in CLERKS[1:] if seat != writer)
            feeds[reader].append(entry(site, draw.choice(ORDINARY), early))
            feeds[other].append(entry(site, old, at))
        else:
            feeds[writer].append(entry(site, old, at))
        feeds[writer].append(entry(site, good, at))
    elif kind == "gone":
        feeds[reader].append(entry(site, old, early))
        feeds[writer].append(entry(site, "withdrawn", at))
    else:
        feeds[writer].append(entry(site, good, at))
        if witness is not None:
            feeds[witness].append(entry(site, old, early))
    return good


def cases(facet, index, asks, unused, draw):
    a, b, c = asks["p1"]
    d, e, f = asks["p2"]
    g, h, i = asks["p3"]
    if facet == "mix":
        incoming = list((("gone", "cascade"), ("cascade", "dispute"),
                         ("dispute", "gone"))[index % 3])
        draw.shuffle(incoming)
        outgoing_kinds = list(SPECIAL)
        draw.shuffle(outgoing_kinds)
        unknown, extra2, extra3 = unused[:3]
        del unused[:3]
        asks["p1"][2] = unknown
        asks["p2"].append(extra2)
        asks["p3"].extend((c, extra3))
        return [(a, "p1", "p2", incoming[0], "p1"),
                (b, "p1", "p3", incoming[1], "p1"),
                (unknown, "p1", "p1", "unknown", None),
                (d, "p2", "p1", outgoing_kinds[0], None),
                (e, "p2", "p1", outgoing_kinds[1], None),
                (f, "p2", "p2", "normal", None),
                (g, "p3", "p1", outgoing_kinds[2], None),
                (h, "p3", "p1", "normal", None),
                (i, "p3", "p3", "normal", None),
                (c, "p3", "p1", "normal", None),
                (extra2, "p2", "p1", "normal", None),
                (extra3, "p3", "p1", "normal", None)]
    special = facet if facet in SPECIAL else "normal"
    incoming = [(a, "p1", "p3", special, "p1"),
                (b, "p1", "p2", "normal", "p1")]
    if draw.randrange(2):
        incoming = [(b, "p1", "p3", special, "p1"),
                    (a, "p1", "p2", "normal", "p1")]
    outgoing = [(d, "p2", "p1", special, None),
                (e, "p2", "p1", "normal", None)]
    if draw.randrange(2):
        outgoing = [(e, "p2", "p1", special, None),
                    (d, "p2", "p1", "normal", None)]
    if facet in SPECIAL:
        unknown = unused.pop()
        asks["p1"].append(unknown)
        incoming.append((unknown, "p1", "p1", "unknown", None))
    return [*incoming, *outgoing,
            (c, "p1", "p1", "normal", "p2"),
            (f, "p2", "p2", "normal", None),
            (g, "p3", "p1", "normal", None),
            (h, "p3", "p2", "normal", None),
            (i, "p3", "p3", "normal", None)]


def reports_for(facet, roles, zones, unused, draw):
    feeds = {seat: [] for seat in CLERKS}
    expected = {}
    zone_of = site_zones({"zones": zones})
    for site, reader, writer, kind, witness in roles:
        good = site_case(feeds, site, reader, writer, kind, zone_of, draw, witness, facet=facet)
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


def add_peer_asks(asks):
    control = asks["p1"][2]
    peer2, peer3 = asks["p2"][2], asks["p3"][2]
    asks["p2"].append(peer3)
    asks["p3"].extend((control, peer2))


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


def special_dependencies(data, roles, ideal):
    if data["facet"] not in (*SPECIAL, "mix"):
        return True
    zones = site_zones(data)
    own = data["feeds"]["p1"]
    truth = data["truth"]
    private_statuses = {line["status"] for line in own} | {"disputed"}
    unknown = [site for site in data["asks"]["p1"] if site not in ideal
               and truth[site] == "unknown" and truth[site] not in private_statuses]
    if not unknown:
        return False
    incoming = 0
    for site, reader, writer, kind, _ in roles:
        if reader != "p1" or writer == "p1" or kind not in SPECIAL:
            continue
        incoming += 1
        key = zones[site] if kind == "cascade" else site
        if key not in ideal or ideal[key]["status"] != SPECIAL[kind]:
            return False
        remaining = entries({k: v for k, v in ideal.items() if k != key})
        if status_of(site, zones[site], own + remaining) == truth[site]:
            return False
        if kind == "cascade" and not any(
                line["key"] == site and line["at"] < ideal[key]["at"] for line in own):
            return False
        if (kind == "cascade" and data["facet"] == "cascade"
                and (site not in ideal or ideal[site]["at"] >= ideal[key]["at"])):
            return False
    return incoming == (2 if data["facet"] == "mix" else 1)


def validate(data, ideal, roles):
    zones, asks, feeds, truth = (data[k] for k in ("zones", "asks", "feeds", "truth"))
    facet = data["facet"]
    width = MIX_ZONE_SIZE if facet == "mix" else ZONE_SIZE
    assert len(zones) == 4 and all(len(row) == width for row in zones.values())
    assert set(data["zone_order"]) == set(zones) and len(data["zone_order"]) == len(zones)
    assert len({site for row in zones.values() for site in row}) == len(zones) * width
    assert tuple(feeds) == tuple(asks) == CLERKS
    lengths = [3, 5, 7] if facet == "mix" else [3, 4, 5] if facet in SCARCE else (
        [4, 3, 3] if facet in SPECIAL else [3, 3, 3])
    assert [len(row) for row in asks.values()] == lengths
    assert all(len(row) == len(set(row)) for row in asks.values())
    assert sorted(truth) == sorted(data["watch"]) and len(truth) == (
        12 if facet == "mix" else 10 if facet in SPECIAL else 9)
    assert list(truth.values()).count("unknown") == int(facet in (*SPECIAL, "mix"))
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
        assert len({line["key"] for line in decoys}) == len(decoys) == (
            MIX_DECOYS if facet == "mix" else 5)
        assert capacity_trap(data, ideal)


def candidate(facet, index, draw):
    zones, asks, unused = layout(draw, facet)
    roles = cases(facet, index, asks, unused, draw)
    feeds, expected = reports_for(facet, roles, zones, unused, draw)
    truth = truth_of(zones, asks, feeds)
    if truth != expected:
        return None
    if any(len({(r["key"], r["at"], r["status"]) for r in feed}) != len(feed)
           for feed in feeds.values()):
        return None
    if facet in SCARCE:
        add_peer_asks(asks)
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
        assert sum(special_counts.values()) == COUNT * 2
        assert max(special_counts.values()) - min(special_counts.values()) <= 1
    return result
