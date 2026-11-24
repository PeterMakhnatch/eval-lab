import argparse
import json
import math
import re
import sys
from collections import Counter
from functools import cache
from pathlib import Path
from statistics import fmean
from types import ModuleType

import crowd_scaling as cs
from crowd_rules import load_module, parse_list, parse_list_v2

TASKS = Path(__file__).resolve().parents[4] / "muster" / "tasks"
FAMILY_TOML = TASKS / "horde" / "family.toml"
KS = (8, 16, 32, 48)
KINDS = ("solo", "team", "zones", "chief")
SOLO = "solo-96"
PRIMARY_FAMILY = 3
PRIMARY_K = 48
SECONDARY_KS = (8, 16, 32)
LEAD = "p1"
TASK_NAME = re.compile(r"horde-i([0-9]{2})")
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
    return out


def restricted(label: str, numbered: dict, base: list[str]) -> dict | None:
    if label not in numbered or not base or not set(base) <= set(numbered[label]):
        return None
    return {i: numbered[label][i] for i in base}


def blank_test(head: dict) -> dict:
    return head | dict.fromkeys(TEST_KEYS) | {"verdict": "not available"}


def pair_test(a: str, b: str, question: str, numbered: dict, base: list[str]) -> dict:
    head = {"contrast": f"{a} - {b}", "question": question}
    ma, mb = restricted(a, numbered, base), restricted(b, numbered, base)
    if ma is None or mb is None:
        return blank_test(head)
    found = cs.contrast(a, b, question, {a: ma, b: mb})
    assert found["instances"] == len(base)
    return (
        head
        | {key: found[key] for key in TEST_KEYS if key != "estimate_pts"}
        | {"estimate_pts": found["diff_pts"]}
    )


def slope_test(kind: str, numbered: dict, base: list[str]) -> dict:
    labels = [f"{kind}-{k}" for k in KS]
    head = {"contrast": f"slope of {kind}-k on log2 k", "question": "k = 8, 16, 32, 48"}
    got = [restricted(label, numbered, base) for label in labels]
    if any(g is None for g in got):
        return blank_test(head)
    xs = [math.log2(k) for k in KS]
    slopes = [cs.slope(xs, [100.0 * g[i] for g in got]) for i in base]
    lo, hi = cs.bootstrap(len(slopes), lambda s: fmean(slopes[i] for i in s))
    found = {"instances": len(slopes), "estimate_pts": fmean(slopes), "lo": lo, "hi": hi}
    return head | found | {"p_sign_flip": cs.sign_flip(slopes)}


def holm(rows: list[dict]) -> list[dict]:
    assert len(rows) == PRIMARY_FAMILY
    tested = sorted(
        (r for r in rows if r["p_sign_flip"] is not None), key=lambda r: r["p_sign_flip"]
    )
    passing = True
    decided = {}
    for r in tested:
        place = next(i for i, d in enumerate(tested) if d["p_sign_flip"] == r["p_sign_flip"])
        threshold = cs.P_PASS / (PRIMARY_FAMILY - place)
        passing = passing and r["p_sign_flip"] <= threshold
        above, below = VERDICT_WORDS[r["test"]]
        verdict = cs.resolved(r, above, below, "not resolved") if passing else "not resolved"
        decided[r["test"]] = {"holm_threshold": threshold, "holm": "pass" if passing else "fail"}
        decided[r["test"]]["verdict"] = verdict
    blank = {"holm_threshold": None, "holm": None}
    return [r | decided.get(r["test"], blank) for r in rows]


def primary(numbered: dict, base: list[str]) -> list[dict]:
    rows = [
        {"test": "P1"} | slope_test("team", numbered, base),
        {"test": "P2"}
        | pair_test(f"zones-{PRIMARY_K}", f"team-{PRIMARY_K}", "zones rule", numbered, base),
        {"test": "P3"}
        | pair_test(f"chief-{PRIMARY_K}", f"team-{PRIMARY_K}", "chief rule", numbered, base),
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


def secondary(numbered: dict, base: list[str], margin_pts: float) -> list[dict]:
    rows = [
        interval_verdict(slope_test(kind, numbered, base), "rises", "falls") for kind in KINDS[2:]
    ]
    for rule in KINDS[2:]:
        for k in SECONDARY_KS:
            found = pair_test(f"{rule}-{k}", f"team-{k}", f"{rule} rule", numbered, base)
            rows.append(interval_verdict(found, "helps", "hurts"))
    for k in KS:
        found = pair_test(f"zones-{k}", f"chief-{k}", "between rules", numbered, base)
        rows.append(interval_verdict(found, "zones higher", "chief higher"))
    for kind in KINDS[1:]:
        for k in KS:
            found = pair_test(SOLO, f"{kind}-{k}", "split cost", numbered, base)
            rows.append(margin_verdict(found, margin_pts))
    return [r if "margin_pts" in r else r | {"margin_pts": None} for r in rows]


def base_instances(rows_by: dict) -> list[str]:
    if SOLO not in rows_by:
        return []
    names = {r["instance"] for r in rows_by[SOLO]}
    return sorted(cs.by_number(SOLO, dict.fromkeys(names, 0.0)))


def horde_arm(summary: dict, rows: list[dict]) -> dict:
    scored = [r for r in rows if r["reward"] is not None]
    extra = {key: cs.mean_of(scored, key) for key in ("reads_new", "seats_left")}
    return summary | extra


def analyze(arms: list[dict], cfg: dict) -> dict:
    summaries, rows_by, means = cs.load_arms(arms, cfg)
    numbered = {label: cs.by_number(label, values) for label, values in means.items()}
    base = base_instances(rows_by)
    parser = cfg["parser"]
    zones = [
        zones_summary(a["label"], rule_rows(a, rows_by[a["label"]], parser))
        for a in arms
        if a["kind"] == "zones"
    ]
    chief = [
        chief_summary(a["label"], rule_rows(a, rows_by[a["label"]], parser), parser)
        for a in arms
        if a["kind"] == "chief"
    ]
    ks = sorted({1, *(a["n"] for a in arms if a["kind"] != "solo")})
    keys = ("total", "boxes", "flags", "room_size", "cap", "margin", "missing_as_zero", "parser")
    settings = {key: cfg[key] for key in keys} | {
        "instances": base,
        "n": len(base),
        "smallest_p": 2.0 / 2 ** len(base) if base else None,
        "alpha": cs.P_PASS,
        "holm_family_size": PRIMARY_FAMILY,
        "primary_k": PRIMARY_K,
    }
    return {
        "settings": settings,
        "arms": [horde_arm(s, rows_by[s["label"]]) for s in summaries],
        "zones_obedience": zones,
        "chief_obedience": chief,
        "primary": primary(numbered, base),
        "secondary": secondary(numbered, base, 100.0 * cfg["margin"]),
        "baselines": cs.baselines(ks, cfg["total"], cfg["boxes"]),
    }


def arm_table(arms: list[dict]) -> str:
    shown = [a | {"reward_pct": 100.0 * a["reward"]} for a in arms]
    cols = (("trials", "trials", 0), ("noRwd", "no_reward", 0), ("reward%", "reward_pct", 1))
    cols += (("opens", "opens", 2), ("distinct", "distinct", 2), ("newFlags", "flags", 2))
    cols += (("newEmpty", "empty", 2), ("repeats", "repeats", 2), ("unused", "unused", 2))
    cols += (("msgs", "messages", 2), ("msgsPerSeat", "msgs_per_seat", 2))
    cols += (("reads", "reads", 2), ("readsNew", "reads_new", 2))
    cols += (("seatsOpensLeft", "seats_left", 2), ("capSeats", "cap_seats", 0))
    cols += (("seatExc", "seat_exceptions", 0), ("usd", "cost_usd", 4), ("wallS", "wall_s", 1))
    title = "HORDE ARMS (per-trial means; reads = GET /msgs + GET /new; capSeats, seatExc: sums)"
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
            "waits": ",".join(f"{w:.1f}" for w in a["first_list_sec"]) or None,
            "parses": " ".join(f"{k}:{v}" for k, v in a["p1_posts_by_parse"].items()) or None,
        }
        for a in chief
    ]
    cols = (("parser", "parser", 0), ("trials", "trials", 0), ("opens", "opens", 0))
    cols += (("classified", "classified", 0), ("classifiedShare", "classified_share", 3))
    cols += (("allowed", "allowed", 0), ("outside", "outside", 0))
    cols += (("strictObedience", "obedience", 3), ("instMean", "instance_mean", 3))
    cols += (("95%CI(inst)", "ci", 0), ("opensBeforeList", "opens_before_first_list", 0))
    cols += (("firstListSec", "waits", 0), ("noList", "trials_without_list", 0))
    cols += (("xChecked", "cross_checked_trials", 0), ("p1Posts", "parses", 0))
    title = (
        "CHIEF obedience (allowed: box under the seat in an earlier parsed p1 list;"
        " after an unparsed p1 list post, other opens are unclassified)"
    )
    return cs.spec_table(title, shown, "label", cols)


def test_tables(report: dict) -> list[str]:
    s = report["settings"]
    lead = (("question", "question", 0), ("n", "instances", 0), ("estPts", "estimate_pts", 2))
    lead += (("lo95", "lo", 2), ("hi95", "hi", 2), ("pSignFlip", "p_sign_flip", 4))
    p_cols = (("contrast", "contrast", 0), *lead, ("holmThreshold", "holm_threshold", 4))
    p_cols += (("holm", "holm", 0), ("verdict", "verdict", 0))
    s_cols = (*lead, ("marginPts", "margin_pts", 1), ("verdict", "verdict", 0))
    names = ",".join(s["instances"]) or "none"
    head = (
        f"E9 instances (the instances of {SOLO}): {names}; n = {s['n']};"
        f" smallest sign-flip p that n allows: {cs.cell(s['smallest_p'], 4)};"
        " a test is available only when each of these instances has a value in every arm it uses"
    )
    title = (
        f"E9 PRIMARY (Holm step-down, fixed family of {s['holm_family_size']}, alpha {s['alpha']}:"
        " thresholds 0.05/3, 0.05/2, 0.05 by rank of p, stop at the first failure; estPts: P1"
        " points per doubling of k, P2 and P3 points; a verdict other than not resolved needs"
        " a Holm pass and a 95% interval on 1 side of 0)"
    )
    return [
        head,
        cs.spec_table(title, report["primary"], "test", p_cols),
        cs.spec_table(
            "E9 SECONDARY (no correction; split cost: pass hi95 < margin, fail lo95 > margin)",
            report["secondary"],
            "contrast",
            s_cols,
        ),
    ]


def render(report: dict) -> str:
    s = report["settings"]
    header = (
        f"E9 horde: total budget {s['total']}, boxes {s['boxes']}, flags {s['flags']},"
        f" room size {s['room_size']}, turn cap {s['cap']}, margin {s['margin']},"
        f" chief lists: parser {s['parser']}"
    )
    parts = [f"{header}\nmode: trials without a reward are {cs.missing_mode(s)}"]
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
    assert len({a["label"] for a in arms}) == len(arms), "arm labels repeat"
    for a in arms:
        assert a["label"] in config["conditions"], f"{a['label']}: no horde condition"
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
    }
    report = analyze(arms, cfg)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
