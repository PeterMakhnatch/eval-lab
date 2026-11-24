import argparse
import json
import math
import re
import sys
from collections import Counter
from datetime import datetime
from functools import cache
from pathlib import Path
from statistics import fmean, median
from types import ModuleType

import crowd_scaling as cs
from crowd_rules import load_module, parse_list, parse_list_v2

TASKS = Path(__file__).resolve().parents[4] / "muster" / "tasks"
FAMILY_TOML = TASKS / "horde" / "family.toml"
KINDS = ("solo", "team", "zones", "chief")
SOLO = "solo-96"
PRIMARY_FAMILY = 3
PRIMARY_K = 48
LEAD = "p1"
TASK_NAME = re.compile(r"horde-i([0-9]{2})")
INSTANCE = re.compile(r"i([0-9]{2})")
MAX_UNCLASSIFIED = 0.2
STALL_GAP_S = 300.0
TIMEOUT_ERROR = "AgentTimeoutError"
DEFINITIONS = (
    "unit: 1 instance; an arm's value for an instance = mean reward of its trials with a reward;"
    " points = reward x 100",
    "instances: the set of --instances; a test is available only when every instance of the set"
    " has a value in each arm that the test uses; otherwise not available, with the missing"
    " arm:instance pairs",
    "sign-flip p: crowd_scaling.sign_flip, exact and two-sided; statistic = sum of the n instance"
    " values; all 2^n sign patterns; a pattern counts when |pattern sum| >= |observed sum| - 1e-9;"
    " zero values stay in; smallest p = 2 / 2^n",
    "bootstrap 95% interval: percentile; random.Random(0); 10000 resamples of n instance indices"
    " with replacement; statistic = mean; ends sorted[250] and sorted[9749]",
    "slope: for each instance, least squares of points on x = log2 k over the --ks team arms:"
    " slope = sum of w_k * points_k, w_k = (x_k - mean x) / sum (x - mean x)^2;"
    " the estimate is the mean of the instance slopes (points per doubling of k)",
    "Holm: family of 3 (P1, P2, P3), fixed even when a test is not available; the available"
    " tests are sorted by p, ties in the registered order P1, P2, P3; the test at position j"
    " (0, 1, 2) passes when p <= 0.05 / (3 - j) and every earlier test passed; falls / rises"
    " (P1) and helps / hurts (P2, P3) need a Holm pass and a 95% interval on 1 side of 0,"
    " otherwise not resolved",
    "secondary: no correction; split cost solo-96 - arm: pass when hi95 < 5 points, fail when"
    " lo95 > 5 points, otherwise inconclusive; other contrasts: the side of 0 of the 95% interval",
    "repeats: opens of an already opened box (= verifier duplicate_opens, checked for each"
    " trial); unused opens = 96 - opens; board reads = events GET /msgs + GET /new; a GET /new"
    " read counts as a read of every earlier post",
    "turn cap: a seat is at the cap when its agent steps (trajectory steps with source agent)"
    " >= max_turns of the seat in the trial config.json; opens left: seat opens < seat budget"
    " at the end of play",
    "stalled seat (muster-runs/stalled.py): exception AgentTimeoutError and no trajectory step in"
    " the last 300 s before the end of the agent phase; time-limit seat: AgentTimeoutError (the"
    " 600 s agent limit) and not stalled",
    "model-call time: median of agent_result.metadata.api_request_times_msec over all calls of"
    " the arm; step gap: median time between consecutive agent steps of 1 seat's trajectory,"
    " over all seats of the arm",
    "zones obedience: every open counts, repeats too; allowed when the box belongs to the seat"
    " in reference.py zones() for the true treasure rooms; obedience = allowed / opens",
    "chief: p1 posts read by crowd_rules parser v2; an open is allowed when its box is under the"
    " seat in an earlier p1 post read as a list; after a p1 post that v2 cannot read, the other"
    " opens are unclassified; first list = p1's first post that v2 reads as a list (any number"
    " of seats); strict obedience = allowed / classified, not measured when more than 20% of"
    " the opens are unclassified",
)
PARSERS = {"v1": parse_list, "v2": parse_list_v2}
ZONES_CLASSES = {
    "own scout box": True,
    "own plan box": True,
    "scout box of another seat": False,
    "plan box of another seat": False,
    "box in no seat's plan": False,
}
CHIEF_CLASSES = {
    "in a list for the seat": True,
    "in no list for the seat": False,
    "before any list existed": False,
    "list not parsed": None,
}
VERDICT_WORDS = {"P1": ("rises", "falls"), "P2": ("helps", "hurts"), "P3": ("helps", "hurts")}
TEST_KEYS = ("instances", "estimate_pts", "lo", "hi", "p_sign_flip")
BLANK_TEST = dict.fromkeys(TEST_KEYS) | {"verdict": "not available"}


@cache
def family_ref() -> ModuleType:
    return load_module("horde_reference", TASKS / "horde" / "reference.py")


@cache
def family_config() -> dict:
    return family_ref().family()


def is_post(e: dict) -> bool:
    return e["kind"] == "msg" and e["method"] == "POST"


def is_open(e: dict) -> bool:
    return e["kind"] == "open" and e["method"] == "POST"


def instance_of(trial: Path, label: str) -> dict:
    result = json.loads((trial / "result.json").read_text())
    task = ((result.get("config") or {}).get("task") or {}).get("path")
    assert isinstance(task, str), f"{trial}: no task path in result.json"
    path = Path(task)
    assert trial.name.split("__", 1)[0] == path.name, (trial, task)
    stored = path / "environment" / "world" / "instance.json"
    if stored.is_file():
        inst = json.loads(stored.read_text())
    else:
        match = TASK_NAME.fullmatch(path.name)
        assert match, (trial, task)
        inst = family_ref().instances(family_config(), label)[int(match.group(1)) - 1]
    expected = family_ref().instances(family_config(), label)[inst["instance"] - 1]
    assert inst["family"] == "horde" and inst["condition"] == label, (trial, label)
    for key in ("seats", "budget", "rule", "treasure_rooms"):
        assert inst[key] == expected[key], (trial, key)
    return inst


def zones_rule(inst: dict) -> dict:
    ref, config = family_ref(), family_config()
    plan = ref.zones(config, len(inst["seats"]))
    scout_of = {ref.box_at(config, r, 1): s for s, part in plan.items() for r in part["scout"]}
    owner = dict(ref.owners(config, plan, inst["treasure_rooms"]))
    assert set(scout_of) <= set(owner) and len(owner) == config["total_budget"]
    return {"scout_of": scout_of, "owner": owner}


def zone_class(seat: str, box: int, rule: dict) -> str:
    owner = rule["owner"].get(box)
    if owner == seat:
        return "own scout box" if box in rule["scout_of"] else "own plan box"
    if owner is None:
        return "box in no seat's plan"
    return "scout box of another seat" if box in rule["scout_of"] else "plan box of another seat"


def zones_trial(trial: Path, events: list[dict], inst: dict) -> dict:
    rule = zones_rule(inst)
    first: dict[int, str] = {}
    classes: Counter = Counter()
    outside, repeats = [], []
    start = events[0]["t"]
    for e in events:
        if not is_open(e):
            continue
        seat, box = e["seat"], e["request"]["box"]
        cls = zone_class(seat, box, rule)
        classes[cls] += 1
        item = {"trial": trial.name, "seq": e["seq"], "sec": e["t"] - start, "seat": seat}
        item |= {"box": box, "class": cls, "plan_owner": rule["owner"].get(box)}
        item |= {"first_opener": first.get(box)}
        if box in first:
            repeats.append(item)
        if not ZONES_CLASSES[cls]:
            outside.append(item)
        first.setdefault(box, seat)
    expected = family_ref().outside_rule(events, inst)
    assert len(outside) == expected, f"{trial}: {len(outside)} outside, reference {expected}"
    return {"classes": classes, "outside": outside, "repeats": repeats}


def chief_open(e: dict, state: dict) -> str:
    if e["request"]["box"] in state["lists"].get(e["seat"], set()):
        return "in a list for the seat"
    if state["unparsed"]:
        return "list not parsed"
    return "in no list for the seat" if state["first_list"] else "before any list existed"


def chief_post(e: dict, seats: list[str], state: dict, parser: str) -> None:
    lists, how = PARSERS[parser](e["request"]["text"], seats)
    if lists is None:
        state["unparsed"] |= how == "not parsed"
        state["how"][how or "not a list"] += 1
        return
    state["how"][how] += 1
    for seat, boxes in lists.items():
        state["lists"].setdefault(seat, set()).update(boxes)
    if state["first_list"] is None:
        state["first_list"] = e["t"]


def chief_trial(trial: Path, events: list[dict], inst: dict, parser: str) -> dict:
    seats = sorted(inst["seats"], key=cs.seat_number)
    assert seats[0] == LEAD
    state: dict = {"lists": {}, "unparsed": False, "first_list": None, "how": Counter()}
    classes: Counter = Counter()
    before = 0
    for e in events:
        if is_post(e) and e["seat"] == LEAD:
            chief_post(e, seats, state, parser)
        if is_open(e):
            classes[chief_open(e, state)] += 1
            before += state["first_list"] is None
    outside = sum(n for cls, n in classes.items() if CHIEF_CLASSES[cls] is False)
    exact = not state["unparsed"] and set(state["how"]) <= {"json", "not a list"}
    if exact:
        expected = family_ref().outside_rule(events, inst)
        assert outside == expected, f"{trial}: {outside} outside, reference {expected}"
    wait = None if state["first_list"] is None else state["first_list"] - events[0]["t"]
    found = {"classes": classes, "before": before, "first_list_sec": wait, "how": state["how"]}
    return found | {"cross_checked": exact}


def rule_rows(arm: dict, rows: list[dict], parser: str) -> list[dict]:
    out = []
    for r in rows:
        if r["reward"] is None:
            continue
        trial = arm["job"] / r["trial"]
        events = cs.played(trial)
        inst = instance_of(trial, arm["label"])
        if arm["kind"] == "zones":
            found = zones_trial(trial, events, inst)
        else:
            found = chief_trial(trial, events, inst, parser)
        assert sum(found["classes"].values()) == r["opens"], (trial, r["opens"])
        out.append(found | {"instance": r["instance"], "trial": r["trial"], "opens": r["opens"]})
    return out


def obedience(found: list[dict], status: dict) -> dict:
    allowed: Counter = Counter()
    classified: Counter = Counter()
    for f in found:
        for cls, n in f["classes"].items():
            allowed[f["instance"]] += n if status[cls] else 0
            classified[f["instance"]] += n if status[cls] is not None else 0
    per = {i: allowed[i] / n for i, n in sorted(classified.items()) if n}
    values = list(per.values())
    lo, hi = (
        cs.bootstrap(len(values), lambda s: fmean(values[i] for i in s)) if values else (None,) * 2
    )
    total = sum(classified.values())
    return {
        "allowed": sum(allowed.values()),
        "classified": total,
        "outside": total - sum(allowed.values()),
        "obedience": sum(allowed.values()) / total if total else None,
        "instance_mean": fmean(values) if values else None,
        "ci_lo": lo,
        "ci_hi": hi,
        "per_instance": per,
    }


def zones_summary(label: str, found: list[dict]) -> dict:
    classes: Counter = Counter()
    for f in found:
        classes.update(f["classes"])
    out = {"label": label, "trials": len(found), "opens": sum(f["opens"] for f in found)}
    out |= obedience(found, ZONES_CLASSES)
    out["classes"] = {cls: classes[cls] for cls in ZONES_CLASSES}
    out["outside_by_trial"] = {f["trial"]: len(f["outside"]) for f in found}
    out["outside_opens"] = [o | {"instance": f["instance"]} for f in found for o in f["outside"]]
    out["repeat_opens"] = [o | {"instance": f["instance"]} for f in found for o in f["repeats"]]
    return out


def chief_summary(label: str, found: list[dict], parser: str) -> dict:
    classes: Counter = Counter()
    how: Counter = Counter()
    for f in found:
        classes.update(f["classes"])
        how.update(f["how"])
    opens = sum(f["opens"] for f in found)
    waits = [f["first_list_sec"] for f in found if f["first_list_sec"] is not None]
    out = {"label": label, "parser": parser, "trials": len(found), "opens": opens}
    out |= obedience(found, CHIEF_CLASSES)
    out["classified_share"] = out["classified"] / opens if opens else None
    out["unclassified"] = opens - out["classified"]
    out["opens_before_first_list"] = sum(f["before"] for f in found)
    out["first_list_sec"] = waits
    out["first_list_sec_mean"] = fmean(waits) if waits else None
    out["trials_without_list"] = len(found) - len(waits)
    out["cross_checked_trials"] = sum(1 for f in found if f["cross_checked"])
    out["classes"] = {cls: classes[cls] for cls in CHIEF_CLASSES}
    out["p1_posts_by_parse"] = dict(sorted(how.items()))
    measured = opens and out["unclassified"] / opens <= MAX_UNCLASSIFIED
    out["obedience_status"] = "measured" if measured else "not measured"
    if not measured:
        out |= {key: None for key in ("obedience", "instance_mean", "ci_lo", "ci_hi")}
    return out


def parse_instances(text: str) -> list[str]:
    out: list[str] = []
    for part in text.split(","):
        low, sep, high = part.strip().partition("-")
        first = INSTANCE.fullmatch(low)
        last = INSTANCE.fullmatch(high) if sep else first
        assert first and last, f"--instances wants iNN-iNN or iNN,iNN: {text}"
        lo, hi = int(first.group(1)), int(last.group(1))
        assert 1 <= lo <= hi <= cs.MAX_INSTANCES, f"--instances: {part}"
        out += [f"i{n:02d}" for n in range(lo, hi + 1)]
    assert 0 < len(out) == len(set(out)) <= cs.MAX_INSTANCES, f"--instances: {text}"
    return sorted(out)


def parse_ks(text: str | None, arms: list[dict]) -> list[int]:
    given = sorted(a["n"] for a in arms if a["kind"] == "team")
    if text is None:
        return given
    ks = sorted({int(token) for token in text.split(",")})
    for k in ks:
        assert f"team-{k}" in family_config()["conditions"], f"--ks: no condition team-{k}"
    return ks


def missing_of(labels: list[str], numbered: dict, base: list[str]) -> list[str]:
    out: list[str] = []
    for label in labels:
        if label not in numbered:
            out.append(f"{label}:arm missing")
            continue
        out += [f"{label}:{i}" for i in base if i not in numbered[label]]
    return out


def values_of(label: str, numbered: dict, base: list[str]) -> dict[str, float]:
    return {i: numbered[label][i] for i in base}


def pair_test(a: str, b: str, question: str, numbered: dict, base: list[str]) -> dict:
    head = {"contrast": f"{a} - {b}", "question": question}
    missing = missing_of([a, b], numbered, base)
    if missing:
        return head | BLANK_TEST | {"missing": ",".join(missing)}
    means = {a: values_of(a, numbered, base), b: values_of(b, numbered, base)}
    found = cs.contrast(a, b, question, means)
    assert found["instances"] == len(base)
    found["estimate_pts"] = found["diff_pts"]
    return head | {key: found[key] for key in TEST_KEYS} | {"missing": None}


def slope_weights(ks: list[int]) -> dict[int, float]:
    xs = [math.log2(k) for k in ks]
    mx = fmean(xs)
    sxx = sum((x - mx) ** 2 for x in xs)
    assert len(ks) >= 2 and sxx > 0, ks
    weights = {k: (x - mx) / sxx for k, x in zip(ks, xs, strict=True)}
    assert abs(sum(weights.values())) < cs.SIGN_TOLERANCE
    return weights


def slope_test(ks: list[int], numbered: dict, base: list[str]) -> dict:
    head = {"contrast": "slope of team-k on log2 k", "question": f"k = {','.join(map(str, ks))}"}
    if len(ks) < 2:
        return head | BLANK_TEST | {"missing": "fewer than 2 team arms", "weights": None}
    weights = slope_weights(ks)
    head["weights"] = {str(k): w for k, w in weights.items()}
    labels = [f"team-{k}" for k in ks]
    missing = missing_of(labels, numbered, base)
    if missing:
        return head | BLANK_TEST | {"missing": ",".join(missing)}
    xs = [math.log2(k) for k in ks]
    slopes = []
    for i in base:
        points = [100.0 * numbered[label][i] for label in labels]
        value = sum(weights[k] * y for k, y in zip(ks, points, strict=True))
        assert abs(value - cs.slope(xs, points)) < 1e-6, (i, value)
        slopes.append(value)
    lo, hi = cs.bootstrap(len(slopes), lambda s: fmean(slopes[i] for i in s))
    found = {"instances": len(slopes), "estimate_pts": fmean(slopes), "lo": lo, "hi": hi}
    found |= {"p_sign_flip": cs.sign_flip(slopes), "missing": None}
    return head | found | {"instance_slopes": dict(zip(base, slopes, strict=True))}


def holm(rows: list[dict]) -> list[dict]:
    assert [r["test"] for r in rows] == list(VERDICT_WORDS)
    order = {name: place for place, name in enumerate(VERDICT_WORDS)}
    tested = sorted(
        (r for r in rows if r["p_sign_flip"] is not None),
        key=lambda r: (r["p_sign_flip"], order[r["test"]]),
    )
    passing = True
    decided = {}
    for place, r in enumerate(tested):
        threshold = cs.P_PASS / (PRIMARY_FAMILY - place)
        passing = passing and r["p_sign_flip"] <= threshold
        above, below = VERDICT_WORDS[r["test"]]
        verdict = cs.resolved(r, above, below, "not resolved") if passing else "not resolved"
        decided[r["test"]] = {"holm_threshold": threshold, "holm": "pass" if passing else "fail"}
        decided[r["test"]]["verdict"] = verdict
    blank = {"holm_threshold": None, "holm": None}
    return [r | decided.get(r["test"], blank) for r in rows]


def primary(numbered: dict, base: list[str], ks: list[int]) -> list[dict]:
    team, zones, chief = (f"{kind}-{PRIMARY_K}" for kind in KINDS[1:])
    rows = [
        {"test": "P1"} | slope_test(ks, numbered, base),
        {"test": "P2"} | pair_test(zones, team, "zones rule", numbered, base),
        {"test": "P3"} | pair_test(chief, team, "chief rule", numbered, base),
    ]
    return holm(rows)


def interval_verdict(row: dict, above: str, below: str) -> dict:
    if row["p_sign_flip"] is None:
        return row
    return row | {"verdict": cs.resolved(row, above, below, "not resolved")}


def margin_verdict(row: dict, margin_pts: float) -> dict:
    row = row | {"margin_pts": margin_pts}
    if row["p_sign_flip"] is None:
        return row
    verdict = "pass" if row["hi"] < margin_pts else "inconclusive"
    return row | {"verdict": "fail" if row["lo"] > margin_pts else verdict}


def optional_rules(numbered: dict, base: list[str]) -> list[dict]:
    rows = []
    ks = sorted({int(label.split("-")[1]) for label in numbered if not label.startswith("solo")})
    for k in [k for k in ks if k != PRIMARY_K]:
        for rule in KINDS[2:]:
            if f"{rule}-{k}" in numbered and f"team-{k}" in numbered:
                found = pair_test(f"{rule}-{k}", f"team-{k}", f"{rule} rule", numbered, base)
                rows.append(interval_verdict(found, "helps", "hurts"))
        if f"zones-{k}" in numbered and f"chief-{k}" in numbered:
            found = pair_test(f"zones-{k}", f"chief-{k}", "between rules", numbered, base)
            rows.append(interval_verdict(found, "zones higher", "chief higher"))
    return rows


def secondary(numbered: dict, base: list[str], ks: list[int], cfg: dict) -> list[dict]:
    zones, chief = f"zones-{PRIMARY_K}", f"chief-{PRIMARY_K}"
    found = pair_test(zones, chief, "between rules", numbered, base)
    rows = [interval_verdict(found, "zones higher", "chief higher")]
    for low, high in zip(ks, ks[1:], strict=False):
        found = pair_test(f"team-{high}", f"team-{low}", "team size", numbered, base)
        rows.append(interval_verdict(found, f"team-{high} higher", f"team-{low} higher"))
    for label in cfg["multi_seat"]:
        found = pair_test(SOLO, label, "split cost", numbered, base)
        rows.append(margin_verdict(found, 100.0 * cfg["margin"]))
    rows += optional_rules(numbered, base)
    return [r if "margin_pts" in r else r | {"margin_pts": None} for r in rows]


def stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def seat_measures(trial: Path, item: dict, cap: object, end: str | None) -> dict:
    path = trial / "seats" / item["seat"] / "agent" / "trajectory.json"
    steps = json.loads(path.read_text()).get("steps") if path.is_file() else None
    assert steps is None or len(steps) <= cs.MAX_STEPS, path
    agent = [s for s in steps or [] if s.get("source") == "agent"]
    times = sorted(stamp(s["timestamp"]) for s in agent if s.get("timestamp"))
    got = item.get("agent_result") or {}
    calls = (got.get("metadata") or {}).get("api_request_times_msec") or []
    known = steps is not None and type(cap) is int
    out = {
        "cap_unknown": int(not known),
        "cap_seat": int(known and len(agent) >= cap),
        "gaps": [(b - a).total_seconds() for a, b in zip(times, times[1:], strict=False)],
        "calls": [ms / 1000.0 for ms in calls if cs.number(ms)],
        "stalled": 0,
        "time_limit": 0,
    }
    if (item.get("exception_info") or {}).get("exception_type") != TIMEOUT_ERROR:
        return out
    every = [stamp(s["timestamp"]) for s in steps or [] if s.get("timestamp")]
    silent = (stamp(end) - max(every)).total_seconds() if every and end else math.inf
    stalled = end is not None and silent >= STALL_GAP_S
    return out | {"stalled": int(stalled), "time_limit": int(not stalled)}


def trial_extras(trial: Path, row: dict) -> dict:
    result = json.loads((trial / "result.json").read_text())
    config_path = trial / "config.json"
    config = json.loads(config_path.read_text()) if config_path.is_file() else {}
    participants = [p for p in config.get("participants") or [] if isinstance(p, dict)]
    turns = {p.get("seat"): (p.get("kwargs") or {}).get("max_turns") for p in participants}
    phase = result.get("agent_execution") or {}
    end = phase.get("finished_at") or result.get("finished_at")
    items = result["seat_results"]
    seats = [seat_measures(trial, item, turns.get(item["seat"]), end) for item in items]
    keys = ("cap_unknown", "cap_seat", "stalled", "time_limit")
    out: dict = {key: sum(s[key] for s in seats) for key in keys}
    out |= {key: [v for s in seats for v in s[key]] for key in ("gaps", "calls")}
    path = trial / "verifier" / "reward.json"
    duplicates = json.loads(path.read_text()).get("duplicate_opens") if path.is_file() else None
    out["duplicates_checked"] = int(row["reward"] is not None and cs.number(duplicates))
    if out["duplicates_checked"]:
        assert duplicates == row["repeats"], (trial, duplicates, row["repeats"])
    return out


def horde_arm(summary: dict, rows: list[dict], job: Path) -> dict:
    scored = [r for r in rows if r["reward"] is not None]
    extras = [trial_extras(job / r["trial"], r) for r in rows]
    out = summary | {key: cs.mean_of(scored, key) for key in ("reads_new", "seats_left")}
    out["cap_seats_config"] = sum(e["cap_seat"] for e in extras)
    out["cap_unknown_config"] = sum(e["cap_unknown"] for e in extras)
    out["stalled_seats"] = sum(e["stalled"] for e in extras)
    out["time_limit_seats"] = sum(e["time_limit"] for e in extras)
    out["duplicates_checked_trials"] = sum(e["duplicates_checked"] for e in extras)
    calls = [v for e in extras for v in e["calls"]]
    gaps = [v for e in extras for v in e["gaps"]]
    out["model_calls"] = len(calls)
    out["model_call_s_median"] = median(calls) if calls else None
    out["step_gaps"] = len(gaps)
    out["step_gap_s_median"] = median(gaps) if gaps else None
    return out


def analyze(arms: list[dict], cfg: dict) -> dict:
    summaries, rows_by, means = cs.load_arms(arms, cfg)
    numbered = {label: cs.by_number(label, values) for label, values in means.items()}
    base, ks, parser = cfg["instances"], cfg["ks"], cfg["parser"]
    jobs = {a["label"]: a["job"] for a in arms}
    ruled = [a for a in arms if a["kind"] in KINDS[2:]]
    found = {a["label"]: rule_rows(a, rows_by[a["label"]], parser) for a in ruled}
    zones = [zones_summary(a["label"], found[a["label"]]) for a in ruled if a["kind"] == "zones"]
    chief = [
        chief_summary(a["label"], found[a["label"]], parser) for a in ruled if a["kind"] == "chief"
    ]
    keys = ("total", "boxes", "flags", "room_size", "cap", "margin", "missing_as_zero", "parser")
    settings = {key: cfg[key] for key in keys} | {
        "instances": base,
        "n": len(base),
        "smallest_p": 2.0 / 2 ** len(base),
        "ks": ks,
        "alpha": cs.P_PASS,
        "holm_family_size": PRIMARY_FAMILY,
        "primary_k": PRIMARY_K,
        "definitions": list(DEFINITIONS),
    }
    seat_counts = sorted({1, *(a["n"] for a in arms if a["kind"] != "solo")})
    return {
        "settings": settings,
        "arms": [horde_arm(s, rows_by[s["label"]], jobs[s["label"]]) for s in summaries],
        "zones_obedience": zones,
        "chief_obedience": chief,
        "primary": primary(numbered, base, ks),
        "secondary": secondary(numbered, base, ks, cfg),
        "baselines": cs.baselines(seat_counts, cfg["total"], cfg["boxes"]),
    }


def arm_table(arms: list[dict]) -> str:
    shown = [a | {"reward_pct": 100.0 * a["reward"]} for a in arms]
    cols = (("trials", "trials", 0), ("noRwd", "no_reward", 0), ("reward%", "reward_pct", 1))
    cols += (("opens", "opens", 2), ("distinct", "distinct", 2), ("newFlags", "flags", 2))
    cols += (("newEmpty", "empty", 2), ("repeats", "repeats", 2), ("unused", "unused", 2))
    cols += (("msgs", "messages", 2), ("msgsPerSeat", "msgs_per_seat", 2))
    cols += (("reads", "reads", 2), ("readsNew", "reads_new", 2))
    cols += (("seatsOpensLeft", "seats_left", 2), ("capSeats", "cap_seats_config", 0))
    cols += (("capUnk", "cap_unknown_config", 0), ("timeLimit", "time_limit_seats", 0))
    cols += (("stalled", "stalled_seats", 0), ("seatExc", "seat_exceptions", 0))
    cols += (("callSMed", "model_call_s_median", 2), ("stepGapSMed", "step_gap_s_median", 1))
    cols += (("usd", "cost_usd", 4), ("wallS", "wall_s", 1))
    cols += (("dupChecked", "duplicates_checked_trials", 0),)
    title = (
        "HORDE ARMS (per-trial means; capSeats to seatExc: sums of seats over the trials;"
        " callSMed, stepGapSMed: medians in s, - when the trajectories do not record them)"
    )
    return cs.spec_table(title, shown, "label", cols)


def ci_text(a: dict) -> str | None:
    return None if a["ci_lo"] is None else f"[{a['ci_lo']:.3f}, {a['ci_hi']:.3f}]"


def zones_tables(zones: list[dict]) -> list[str]:
    shown = [
        a | {"ci": ci_text(a), "by_trial": ",".join(map(str, a["outside_by_trial"].values()))}
        for a in zones
    ]
    cols = (("trials", "trials", 0), ("opens", "opens", 0), ("allowed", "allowed", 0))
    cols += (("outside", "outside", 0), ("obedience", "obedience", 3))
    cols += (("instMean", "instance_mean", 3), ("95%CI(inst)", "ci", 0))
    cols += (("outsideByTrial", "by_trial", 0),)
    title = "ZONES obedience (allowed: own scout box or own plan box for the true treasure rooms)"
    item_cols = (("instance", "instance", 0), ("seq", "seq", 0), ("sec", "sec", 1))
    item_cols += (("seat", "seat", 0), ("box", "box", 0), ("class", "class", 0))
    item_cols += (("planOwner", "plan_owner", 0), ("firstOpener", "first_opener", 0))
    outside = [o | {"label": a["label"]} for a in zones for o in a["outside_opens"]]
    repeats = [o | {"label": a["label"]} for a in zones for o in a["repeat_opens"]]
    repeat_title = "ZONES repeated opens (firstOpener opened the box first)"
    return [
        cs.spec_table(title, shown, "label", cols),
        cs.spec_table("ZONES opens outside the plan", outside, "label", item_cols),
        cs.spec_table(repeat_title, repeats, "label", item_cols),
    ]


def chief_table(chief: list[dict]) -> str:
    shown = [
        a
        | {
            "ci": ci_text(a),
            "strict": a["obedience"] if a["obedience_status"] == "measured" else "not measured",
            "waits": ",".join(f"{w:.1f}" for w in a["first_list_sec"]) or None,
            "parses": " ".join(f"{k}:{v}" for k, v in a["p1_posts_by_parse"].items()) or None,
        }
        for a in chief
    ]
    cols = (("parser", "parser", 0), ("trials", "trials", 0), ("opens", "opens", 0))
    cols += (("classified", "classified", 0), ("classifiedShare", "classified_share", 3))
    cols += (("allowed", "allowed", 0), ("outside", "outside", 0))
    cols += (("strictObedience", "strict", 3), ("instMean", "instance_mean", 3))
    cols += (("95%CI(inst)", "ci", 0), ("opensBeforeList", "opens_before_first_list", 0))
    cols += (("firstListSec", "waits", 0), ("noList", "trials_without_list", 0))
    cols += (("xChecked", "cross_checked_trials", 0), ("p1Posts", "parses", 0))
    title = (
        "CHIEF obedience (allowed: box under the seat in an earlier p1 post that parser v2"
        " reads as a list; after a p1 post that v2 cannot read, other opens are unclassified;"
        " not measured when more than 20% of the opens are unclassified)"
    )
    return cs.spec_table(title, shown, "label", cols)


def weights_text(primary_rows: list[dict]) -> str:
    weights = primary_rows[0].get("weights")
    if not weights:
        return "P1 least-squares weights: none (fewer than 2 team arms)"
    shown = ", ".join(f"k={k}: {w:+.4f}" for k, w in weights.items())
    return f"P1 least-squares weights w_k (points on x = log2 k): {shown}"


def test_tables(report: dict) -> list[str]:
    s = report["settings"]
    lead = (("question", "question", 0), ("n", "instances", 0), ("estPts", "estimate_pts", 2))
    lead += (("lo95", "lo", 2), ("hi95", "hi", 2), ("pSignFlip", "p_sign_flip", 4))
    p_cols = (("contrast", "contrast", 0), *lead, ("holmThreshold", "holm_threshold", 4))
    p_cols += (("holm", "holm", 0), ("verdict", "verdict", 0), ("missing", "missing", 0))
    s_cols = (*lead, ("marginPts", "margin_pts", 1), ("verdict", "verdict", 0))
    s_cols += (("missing", "missing", 0),)
    head = (
        f"E9 instances (--instances): {','.join(s['instances'])}; n = {s['n']};"
        f" smallest sign-flip p that n allows: 2/2^{s['n']} = {s['smallest_p']:.4f};"
        f" team ks of P1: {','.join(map(str, s['ks'])) or 'none'}"
    )
    title = (
        f"E9 PRIMARY (Holm, fixed family of {s['holm_family_size']}, alpha {s['alpha']};"
        " estPts: P1 points per doubling of k, P2 and P3 points)"
    )
    return [
        head + "\n" + weights_text(report["primary"]),
        cs.spec_table(title, report["primary"], "test", p_cols),
        cs.spec_table(
            "E9 SECONDARY (no correction; split cost against the margin)",
            report["secondary"],
            "contrast",
            s_cols,
        ),
    ]


def render(report: dict) -> str:
    s = report["settings"]
    header = (
        f"E9 horde: total budget {s['total']}, boxes {s['boxes']}, flags {s['flags']},"
        f" room size {s['room_size']}, turn cap of the crowd_scaling tables {s['cap']},"
        f" margin {s['margin']}, chief lists: parser {s['parser']}"
    )
    lines = [header, f"mode: trials without a reward are {cs.missing_mode(s)}", "definitions:"]
    lines += [f"- {text}" for text in s["definitions"]]
    parts = ["\n".join(lines)]
    parts += cs.render_arms(report["arms"])
    parts.append(arm_table(report["arms"]))
    parts += zones_tables(report["zones_obedience"])
    parts.append(chief_table(report["chief_obedience"]))
    parts += test_tables(report)
    base_cols = (("m", "budget_per_seat", 0), ("randDistinct", "random_distinct", 2))
    base_cols += (("randRepeats", "random_repeats", 2), ("identDistinct", "identical_distinct", 0))
    base_cols += (("identRepeats", "identical_repeats", 0),)
    title = f"BASELINES (n={s['boxes']} boxes, total {s['total']} opens)"
    parts.append(cs.spec_table(title, report["baselines"], "k", base_cols))
    return "\n\n".join(parts)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="horde_scaling.py")
    parser.add_argument("--arm", action="append", required=True, metavar="LABEL=JOB_DIR")
    parser.add_argument("--instances", required=True, metavar="iNN-iNN")
    parser.add_argument("--ks", metavar="K,K,...")
    parser.add_argument("--turn-cap", type=int, default=30)
    parser.add_argument("--margin", type=float, default=0.05)
    parser.add_argument("--missing-as-zero", action="store_true")
    parser.add_argument("--parser", choices=tuple(PARSERS), default="v2")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    config = family_config()
    total = config["total_budget"]
    assert args.turn_cap > 0 and 0.0 <= args.margin < 1.0
    assert 0 < len(args.arm) <= cs.MAX_ARMS
    arms = [cs.parse_arm(text, total, None) for text in args.arm]
    labels = [a["label"] for a in arms]
    assert len(set(labels)) == len(labels), "arm labels repeat"
    for label in labels:
        assert label in config["conditions"], f"{label}: no horde condition"
    cfg = {
        "total": total,
        "per_seat": None,
        "boxes": config["rooms"] * config["room_size"],
        "flags": family_ref().flags(config),
        "room_size": config["room_size"],
        "cap": args.turn_cap,
        "margin": args.margin,
        "missing_as_zero": args.missing_as_zero,
        "parser": args.parser,
        "reference": cs.load_reference(str(FAMILY_TOML)),
        "instances": parse_instances(args.instances),
        "ks": parse_ks(args.ks, arms),
        "multi_seat": [c for c in config["conditions"] if c in labels and c != SOLO],
    }
    report = analyze(arms, cfg)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
