import json
import sys
from pathlib import Path

WORLD_DIR = Path("/world")


def status_of(site, zone, entries):
    relevant = [entry for entry in entries if entry["key"] == site or (
        entry["key"] == zone and entry["status"] == "closed"
    )]
    if not relevant:
        return "unknown"
    newest = max(entry["at"] for entry in relevant)
    statuses = {entry["status"] for entry in relevant if entry["at"] == newest}
    return next(iter(statuses)) if len(statuses) == 1 else "disputed"


def site_zones(instance):
    return {site: zone for zone, sites in instance["zones"].items() for site in sites}


def entries(register):
    return [{"key": key, "status": line["status"], "at": line["at"]}
            for key, line in register.items()]


def before_close(events):
    for event in events:
        if event["kind"] == "admin.close" and event["ok"]:
            break
        yield event


def replay(events, instance):
    register = {}
    version = 0
    phase = "answering" if instance["arm"] == "solo" else "writing"
    ready = set()
    answers = {seat: {} for seat in instance["clerks"]}
    for event in before_close(events):
        if not event["ok"]:
            continue
        seat, kind, body = event["seat"], event["kind"], event["request"]
        if kind == "stage" and body.get("phase") == "answering":
            assert phase == "writing"
            phase = "answering"
        if event["method"] != "POST":
            continue
        if kind == "put":
            assert phase == "writing"
            key = body["key"]
            own = instance["feeds"][seat]
            assert body in own or (body["status"] == "disputed" and key in site_zones(instance)
                                   and any(r["key"] == key and r["at"] == body["at"] for r in own))
            line = {"status": body["status"], "at": body["at"], "by": seat}
            if register.get(key) != line:
                register[key] = line
                version += 1
                ready.clear()
            assert event["response"]["line"] == line
            assert event["response"]["version"] == version
            assert len(register) <= instance["slots"]
        elif kind == "drop":
            assert phase == "writing"
            key = body["key"]
            assert register[key]["by"] == seat
            del register[key]
            version += 1
            ready.clear()
            assert event["response"]["version"] == version
        elif kind == "ready":
            assert body["version"] == version
            ready.add(seat)
        elif kind == "answer":
            site = body["site"]
            assert phase == "answering"
            assert site in instance["asks"][seat] and site not in answers[seat]
            answers[seat][site] = body["status"]
    return {"register": register, "version": version, "phase": phase,
            "ready": sorted(ready), "answers": answers}


def ratio(right, total):
    assert 0 <= right <= total
    return right / total if total else 1.0


def score_answers(state, instance):
    truth, real = instance["truth"], set(instance["real"])
    questions = {(seat, site) for seat, asks in instance["asks"].items() for site in asks}
    reader = {(seat, site) for seat, site in questions if seat in real}
    writer = {(seat, site) for seat, site in questions if seat not in real
              and site in instance["writer_focal"]}
    right = {(seat, site) for seat, asks in state["answers"].items() for site, status in asks.items()
             if status == truth[site]}
    assert right <= questions
    reader_right = len(right.intersection(reader))
    writer_right = len(right.intersection(writer))
    result = {"reward": ratio(len(right), len(questions)),
              "focal": ratio(reader_right + writer_right, len(reader) + len(writer)),
              "reader_asks": float(len(reader)), "writer_asks": float(len(writer))}
    if reader:
        result["reader_right"] = ratio(reader_right, len(reader))
    if writer:
        result["writer_right"] = ratio(writer_right, len(writer))
    return result


def relevant_keys(instance):
    zones = site_zones(instance)
    watch = set(instance["watch"])
    return watch | {zones[site] for site in watch}


def register_diagnostics(register, instance):
    zones, truth = site_zones(instance), instance["truth"]
    lines = entries(register)
    relevant = relevant_keys(instance)
    reported = {entry["key"] for feed in instance["feeds"].values() for entry in feed
                if entry["key"] in relevant}
    sufficient = sum(status_of(site, zones[site], instance["feeds"][seat] + lines) == truth[site]
                     for seat, asks in instance["asks"].items() for site in asks)
    return {"register_sufficient": ratio(sufficient, sum(map(len, instance["asks"].values()))),
            "lines_off_watch": float(len(set(register) - relevant)),
            "reported_watch_keys_missing": float(len(reported - set(register))
                                                 if instance["arm"] != "solo" else 0)}


def read_diagnostics(events, instance):
    reads = {}
    keys = set()
    relevant = relevant_keys(instance)
    version = 0
    register_reads = ready_calls = fresh_reads = refused = unknown = peak = forced = 0
    for event in before_close(events):
        seat, kind, ok = event["seat"], event["kind"], event["ok"]
        body = event["request"]
        if kind == "stage" and body.get("forced_close"):
            forced = 1
        if kind == "put" and not ok:
            refused += 1
        if ok and kind in ("put", "drop"):
            version = event["response"]["version"]
            if kind == "put":
                keys.add(body["key"])
            else:
                keys.remove(body["key"])
            peak = max(peak, len(keys - relevant))
        if kind == "answer" and ok and body["status"] == "unknown":
            unknown += 1
        if seat not in instance["real"] or event["response"].get("played"):
            continue
        if kind == "register" and ok:
            reads[seat] = event["response"]["version"]
            register_reads += 1
        elif kind == "ready":
            ready_calls += 1
            fresh_reads += int(isinstance(body, dict) and type(body.get("version")) is int and
                               reads.get(seat) == body["version"] == version)
    return {"refused_puts": float(refused), "register_reads": float(register_reads),
            "ready_calls": float(ready_calls), "peak_off_watch": float(peak),
            "used_off_watch": float(peak > 0),
            "reads_after_last_change_before_ready": float(fresh_reads),
            "forced_close": float(forced), "answers_unknown": float(unknown)}


def score(events, instance):
    state = replay(events, instance)
    return {**score_answers(state, instance),
            **register_diagnostics(state["register"], instance),
            **read_diagnostics(events, instance)}


def main():
    sys.path.insert(0, str(WORLD_DIR))
    import worldkit

    worldkit.close_world()
    instance = json.loads((WORLD_DIR / "instance.json").read_text())
    rewards = score(worldkit.load_events(), instance)
    worldkit.write_rewards(rewards)
    print(json.dumps(rewards, sort_keys=True))


if __name__ == "__main__":
    main()
