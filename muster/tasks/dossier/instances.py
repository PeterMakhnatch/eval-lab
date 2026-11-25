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

SEED = "dossier-v3.1"
COUNT = 10
FACETS = ("share", "scarce", "gone", "cascade", "dispute", "mix")
ARMS = ("probe", "reader", "solo", "team")
CLERKS = ("p1", "p2", "p3")
ORDINARY = ("open", "limited", "busy", "quiet", "repair", "ready")
STATUSES = (*ORDINARY, "withdrawn", "closed", "disputed", "unknown")
ZONE_NAMES = ("north", "south", "east", "west")
MAX_DRAWS = 4096
SCARCE = ("scarce", "mix")
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


def layout(draw):
    sites = [f"s{i:02}" for i in range(1, 17)]
    draw.shuffle(sites)
    names = list(ZONE_NAMES)
    draw.shuffle(names)
    zones = {zone: sites[i * 4:i * 4 + 4] for i, zone in enumerate(names)}
    roles = draw.sample(sites, 9)
    asks = {seat: roles[i * 3:i * 3 + 3] for i, seat in enumerate(CLERKS)}
    unused = [site for site in sites if site not in roles]
    draw.shuffle(unused)
    for row in zones.values():
        draw.shuffle(row)
    return zones, asks, unused


def site_case(feeds, site, reader, writer, kind, zones, draw, witness, *, include_prior_site):
    good = draw.choice(ORDINARY)
    old = draw.choice([status for status in ORDINARY if status != good])
    early, late = sorted(draw.sample(range(1, 41), 2))
    at = late if kind in ("cascade", "gone") or witness is not None else draw.randint(1, 40)
    if kind == "cascade":
        if include_prior_site:
            feeds[reader].append(entry(site, old, early))
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


def cases(facet, index, asks):
    a, b, c = asks["p1"]
    d, e, f = asks["p2"]
    g, h, i = asks["p3"]
    special = facet if facet in ("gone", "cascade", "dispute") else "normal"
    if facet == "mix":
        incoming = (("gone", "cascade"), ("cascade", "dispute"), ("dispute", "gone"))[index % 3]
        return [(a, "p1", "p2", incoming[0], "p1"),
                (b, "p1", "p3", incoming[1], "p1"),
                (c, "p1", "p1", "normal", None),
                (d, "p2", "p1", "gone", None),
                (e, "p2", "p1", "cascade", None),
                (f, "p2", "p2", "normal", None),
                (g, "p3", "p1", "dispute", None),
                (h, "p3", "p1", "normal", None),
                (i, "p3", "p3", "normal", None)]
    return [(a, "p1", "p2", special, "p1"),
            (b, "p1", "p3", "normal", "p1"),
            (c, "p1", "p1", "normal", "p2"),
            (d, "p2", "p1", special, None),
            (e, "p2", "p1", "normal", None),
            (f, "p2", "p2", "normal", None),
            (g, "p3", "p1", "normal", None),
            (h, "p3", "p2", "normal", None),
            (i, "p3", "p3", "normal", None)]


def reports_for(facet, roles, zones, unused, draw):
    feeds = {seat: [] for seat in CLERKS}
    expected = {}
    zone_of = site_zones({"zones": zones})
    for site, reader, writer, kind, witness in roles:
        good = site_case(feeds, site, reader, writer, kind, zone_of, draw, witness,
                         include_prior_site=facet != "mix")
        expected[site] = {"gone": "withdrawn", "cascade": "closed",
                          "dispute": "disputed"}.get(kind, good)
    if facet in SCARCE:
        feeds["p1"].extend(entry(site, draw.choice(ORDINARY), draw.randint(1, 40))
                           for site in unused[:5])
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
    if set(ideal) != reported_watch_keys(data) or len(ideal) != data["slots"]:
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
        expected_focal.update(site for site, reader, writer, _, _ in roles
                              if reader == writer == "p1")
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
        if len(keys) != data["slots"] or not required - keys:
            return False
    return True


def validate(data, ideal, roles):
    zones, asks, feeds, truth = (data[k] for k in ("zones", "asks", "feeds", "truth"))
    assert len(zones) == 4 and all(len(row) == 4 for row in zones.values())
    assert set(data["zone_order"]) == set(zones) and len(data["zone_order"]) == len(zones)
    assert len({site for row in zones.values() for site in row}) == 16
    assert tuple(feeds) == tuple(asks) == CLERKS
    assert [len(row) for row in asks.values()] == (
        [3, 4, 5] if data["facet"] in SCARCE else [3, 3, 3])
    assert all(len(row) == len(set(row)) for row in asks.values())
    assert sorted(truth) == sorted(data["watch"]) and len(truth) == 9
    assert "unknown" not in truth.values()
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
    assert register_diagnostics(ideal, {**data, "arm": "probe"})["register_sufficient"] == 1.0
    assert len(ideal) <= len(reported_watch_keys(data)) <= data["slots"]
    if data["facet"] in SCARCE:
        assert len(reported_watch_keys(data)) == data["slots"]
        assert register_required(data, ideal)
        decoys = [line for line in feeds["p1"] if line["key"] not in relevant_keys(data)]
        assert len({line["key"] for line in decoys}) == len(decoys) == 5
        assert capacity_trap(data, ideal)


def candidate(facet, index, draw):
    zones, asks, unused = layout(draw)
    roles = cases(facet, index, asks)
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
    ideal = produce(data, CLERKS)
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
    clerks = CLERKS if arm == "reader" else CLERKS[1:] if arm == "probe" else ()
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
    assert set(counts) - set(ORDINARY) == required
    return result
