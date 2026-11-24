import argparse
import json
import math
import random
import re
import sys
from datetime import datetime
from functools import cache
from itertools import combinations
from pathlib import Path
from statistics import fmean

MAX_GROUPS = 8
MAX_ARMS = 64
MAX_TRIALS = 1000
MAX_STEPS = 100_000
MAX_SEATS = 16
RESAMPLES = 10_000
SEED = 0
SIGN_TOLERANCE = 1e-9
P_PASS = 0.05
STALL_GAP_S = 300.0
STALL_TYPE = "AgentTimeoutError"
PLANNED_INSTANCES = 10
PLANNED_NUMBERS = tuple(f"i{n:02d}" for n in range(1, PLANNED_INSTANCES + 1))
INSTANCE = re.compile(r".*-(i[0-9]{2})")
CELL = re.compile(r"([a-z]+)-([trcn])")
SUFFIX_ORDER = "trcn"
FAITHFUL = "script_faithful"
LEFT_SOURCE = "unused_opens_with_closed_flag_boxes"
LEFT = "left"
CLASS_KEY = "claim_behaviour"
CLASS_PREFIX = "claim_"
HEADLINE = ("reward", "probe_pass", "behaviour_ok")
PASS = "probe_pass"
PRIMARY = (
    ("stale", PASS, "stale-c", "stale-t"),
    ("badlead", PASS, "badlead-c", "badlead-t"),
    ("wait", PASS, "wait-n", "wait-t"),
    ("deadclaim", LEFT, "deadclaim-t", "deadclaim-n"),
)
HOLM_FAMILY = len(PRIMARY)
OPPOSITE = (
    ("stale-n", "stale-c"),
    ("badlead-n", "badlead-c"),
    ("wait-n", "wait-c"),
    ("deadclaim-n", "deadclaim-c"),
)
RATE_CELLS = ("stale-n", "badlead-n", "wait-n", "deadclaim-n", "deadclaim-r")
RELEASE = ("deadclaim-r", "deadclaim-c")
STALE_CELL = "stale-t"
STALE_KEY = "stale_repeats"
REREAD_KEY = "rereads_before_opens"
EDGE = 1e-6
STALE_BINS = (("=0", -math.inf, 0.5), ("=1", 0.5, 1.5), ("=2", 1.5, 2.5), (">=3", 2.5, math.inf))
REREAD_BINS = (
    ("=0", -math.inf, EDGE),
    ("(0,0.5)", EDGE, 0.5),
    ("[0.5,1)", 0.5, 1.0 - EDGE),
    ("=1", 1.0 - EDGE, math.inf),
)
STATUSES = ("measured", "no_reward", "stalled", "void")
INTERVAL = ("lo", "hi")


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text())
    assert isinstance(data, dict), path
    return data


def number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def stamp(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def trial_dirs(job: Path) -> list[Path]:
    found = sorted(
        p for p in job.iterdir() if p.is_dir() and "__" in p.name and (p / "result.json").is_file()
    )
    assert 0 < len(found) <= MAX_TRIALS, f"{job}: {len(found)} trial folders"
    return found


def task_path(trial: Path, result: dict) -> Path:
    text = ((result.get("config") or {}).get("task") or {}).get("path")
    if text is None and (trial / "config.json").is_file():
        text = (read_json(trial / "config.json").get("task") or {}).get("path")
    assert isinstance(text, str) and text, f"{trial}: no task path in result.json or config.json"
    return Path(text)


def identity(trial: Path, result: dict) -> tuple[str, str]:
    path = task_path(trial, result)
    cell = path.parent.name
    assert CELL.fullmatch(cell), f"{trial}: task folder {path.parent} is not a cell folder"
    match = INSTANCE.fullmatch(trial.name.split("__", 1)[0])
    assert match and match.group(1) in PLANNED_NUMBERS, f"{trial}: no planned instance iNN"
    found = INSTANCE.fullmatch(path.name)
    assert found and found.group(1) == match.group(1), (trial, path.name)
    return cell, match.group(1)


def read_rewards(trial: Path, result: dict) -> dict[str, float] | None:
    path = trial / "verifier" / "reward.json"
    if path.is_file():
        data = read_json(path)
    else:
        data = (result.get("verifier_result") or {}).get("rewards") or {}
    if data.get("reward") is None:
        return None
    values = {key: float(value) for key, value in data.items() if number(value)}
    assert "reward" in values and 0.0 <= values["reward"] <= 1.0, (trial, data.get("reward"))
    assert all(math.isfinite(value) for value in values.values()), trial
    assert values.get(FAITHFUL) in (0.0, 1.0), f"{trial}: {FAITHFUL} is not 0 or 1"
    return values


def seat_items(result: dict) -> list[tuple[str, dict]]:
    seats = result.get("seat_results") or []
    if isinstance(seats, dict):
        items = [(name, seat or {}) for name, seat in seats.items()]
    else:
        items = [(s.get("name") or s.get("seat"), s) for s in seats if isinstance(s, dict)]
    assert len(items) <= MAX_SEATS and all(isinstance(name, str) for name, _ in items), items
    return items


def seat_steps(trial: Path, seat: str) -> list[dict] | None:
    path = trial / "seats" / seat / "agent" / "trajectory.json"
    if not path.is_file():
        return None
    found = read_json(path).get("steps")
    assert isinstance(found, list) and len(found) <= MAX_STEPS, path
    return [step for step in found if isinstance(step, dict)]


def stalled_seats(result: dict, steps: dict[str, list[dict] | None]) -> list[str]:
    end_text = (result.get("agent_execution") or {}).get("finished_at") or result.get("finished_at")
    if not end_text:
        return []
    end = stamp(end_text)
    found = []
    for name, seat in seat_items(result):
        info = seat.get("exception_info") or {}
        if info.get("exception_type") != STALL_TYPE:
            continue
        times = [stamp(s["timestamp"]) for s in steps.get(name) or [] if s.get("timestamp")]
        silent = (end - max(times)).total_seconds() if times else math.inf
        if silent >= STALL_GAP_S:
            found.append(name)
    return found


def turn_caps(result: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in (result.get("config") or {}).get("participants") or []:
        if not isinstance(item, dict):
            continue
        cap = (item.get("kwargs") or {}).get("max_turns")
        if isinstance(item.get("seat"), str) and isinstance(cap, int) and cap > 0:
            out[item["seat"]] = cap
    return out


def cap_counts(result: dict, steps: dict[str, list[dict] | None]) -> tuple[int, int]:
    caps = turn_caps(result)
    at_cap = unknown = 0
    for name, _ in seat_items(result):
        found = steps.get(name)
        if found is None or name not in caps:
            unknown += 1
            continue
        at_cap += sum(1 for s in found if s.get("source") == "agent") >= caps[name]
    return at_cap, unknown


def binned(key: str, value: float, bins: tuple) -> dict[str, float]:
    out = {f"{key} {label}": float(lo <= value < hi) for label, lo, hi in bins}
    assert sum(out.values()) == 1.0, (key, value)
    return out


def derived(values: dict[str, float]) -> dict[str, float]:
    out: dict[str, float] = {}
    if LEFT_SOURCE in values:
        out[LEFT] = float(values[LEFT_SOURCE] > 0)
    if STALE_KEY in values:
        out |= binned(STALE_KEY, values[STALE_KEY], STALE_BINS)
    if REREAD_KEY in values:
        out |= binned(REREAD_KEY, values[REREAD_KEY], REREAD_BINS)
    return out


def trial_row(trial: Path) -> dict:
    result = read_json(trial / "result.json")
    cell, instance = identity(trial, result)
    steps = {name: seat_steps(trial, name) for name, _ in seat_items(result)}
    stalled = stalled_seats(result, steps)
    at_cap, cap_unknown = cap_counts(result, steps)
    values = read_rewards(trial, result)
    status = "measured"
    if stalled:
        status = "stalled"
    elif values is None:
        status = "no_reward"
    elif values[FAITHFUL] == 0.0:
        status = "void"
    return {
        "trial": trial.name,
        "cell": cell,
        "instance": instance,
        "status": status,
        "stalled_seats": stalled,
        "cap_seats": at_cap,
        "cap_unknown": cap_unknown,
        "values": values,
        "derived": derived(values) if values is not None else None,
    }


@cache
def resamples(n: int) -> tuple[tuple[int, ...], ...]:
    assert 0 < n <= PLANNED_INSTANCES, n
    rng = random.Random(SEED)
    return tuple(tuple(rng.randrange(n) for _ in range(n)) for _ in range(RESAMPLES))


def bootstrap(values: list[float]) -> tuple[float, float]:
    n = len(values)
    stats = sorted(sum(values[i] for i in sample) / n for sample in resamples(n))
    assert len(stats) == RESAMPLES
    return stats[int(0.025 * RESAMPLES)], stats[int(0.975 * RESAMPLES) - 1]


def sign_flip(diffs: list[float]) -> float:
    assert 0 < len(diffs) <= PLANNED_INSTANCES, len(diffs)
    sums = [0.0]
    for d in diffs:
        sums += [s + d for s in sums]
    total = sum(diffs)
    extreme = sum(1 for s in sums if abs(total - 2.0 * s) >= abs(total) - SIGN_TOLERANCE)
    assert len(sums) == 2 ** len(diffs) and 0 < extreme <= len(sums)
    return extreme / len(sums)


def estimate(means: dict[str, float]) -> dict:
    if not means:
        return {"instances": 0, "mean": None, "lo": None, "hi": None}
    values = [means[i] for i in sorted(means)]
    lo, hi = bootstrap(values)
    return {"instances": len(values), "mean": fmean(values), "lo": lo, "hi": hi}


def instance_means(rows: list[dict], key: str) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    for r in rows:
        merged = r["values"] | r["derived"]
        if key in merged:
            grouped.setdefault(r["instance"], []).append(merged[key])
    return {name: fmean(values) for name, values in sorted(grouped.items())}


def claim_classes(rows: list[dict], estimates: dict[str, dict]) -> list[dict] | None:
    if not rows or CLASS_KEY not in rows[0]["values"]:
        return None
    names = sorted(k for k in rows[0]["values"] if k.startswith(CLASS_PREFIX) and k != CLASS_KEY)
    codes: dict[str, int] = {}
    for r in rows:
        on = [name for name in names if r["values"][name] == 1.0]
        assert len(on) == 1 and sum(r["values"][n] for n in names) == 1.0, r["trial"]
        code = int(r["values"][CLASS_KEY])
        assert codes.setdefault(on[0], code) == code, (r["trial"], on[0], code)
    out = []
    for name in names:
        label = name.removeprefix(CLASS_PREFIX)
        out.append({"class": label, "code": codes.get(name), **estimates[name]})
    return out


def status_counts(rows: list[dict], planned: int) -> dict:
    names = {s: [r["trial"] for r in rows if r["status"] == s] for s in STATUSES}
    measured = {r["instance"] for r in rows if r["status"] == "measured"}
    return {
        "planned": planned,
        "trials": len(rows),
        "with_reward": sum(1 for r in rows if r["values"] is not None),
        "void": len(names["void"]),
        "stalled": len(names["stalled"]),
        "no_reward": len(names["no_reward"]),
        "measured": len(names["measured"]),
        "instances": len(measured),
        "missing_instances": [i for i in PLANNED_NUMBERS if i not in measured],
        "cap_seats": sum(r["cap_seats"] for r in rows),
        "cap_unknown": sum(r["cap_unknown"] for r in rows),
        "void_trials": names["void"],
        "stalled_trials": names["stalled"],
        "no_reward_trials": names["no_reward"],
    }


def load_arm(arm: dict, planned: int) -> dict:
    rows = [trial_row(t) for t in trial_dirs(arm["job"])]
    cells = sorted({r["cell"] for r in rows})
    assert len(cells) == 1, f"{arm['label']}: trials of more than 1 cell: {cells}"
    measured = [r for r in rows if r["status"] == "measured"]
    keys = sorted({k for r in measured for k in r["values"]})
    for r in measured:
        assert set(r["values"]) == set(keys), f"{arm['label']}: {r['trial']} has other keys"
    extra = list(dict.fromkeys(k for r in measured for k in r["derived"]))
    means = {k: instance_means(measured, k) for k in [*keys, *extra]}
    estimates = {k: estimate(values) for k, values in means.items()}
    return {
        "label": arm["label"],
        "job": str(arm["job"]),
        "cell": cells[0],
        "probe": CELL.fullmatch(cells[0]).group(1),
        **status_counts(rows, planned),
        "keys": keys,
        "derived_keys": extra,
        "estimates": estimates,
        "claim_classes": claim_classes(measured, estimates),
        "instance_means": means,
        "stalled_seats": {r["trial"]: r["stalled_seats"] for r in rows if r["stalled_seats"]},
    }


def unavailable(reason: str) -> dict:
    keys = ("instances", "diff_pts", "lo", "hi", "p_sign_flip")
    return {"available": False, "reason": reason, **dict.fromkeys(keys)}


def series(arms: dict, ref: tuple[str, str]) -> dict[str, float] | str:
    key, cell = ref
    if cell not in arms:
        return f"no {cell} arm"
    found = arms[cell]["instance_means"].get(key)
    if found is None:
        return f"{cell} has no {key}"
    gaps = [i for i in PLANNED_NUMBERS if i not in found]
    return f"{cell} has no value for {' '.join(gaps)}" if gaps else found


def paired(a: dict[str, float], b: dict[str, float]) -> dict:
    diffs = [100.0 * (a[i] - b[i]) for i in PLANNED_NUMBERS]
    lo, hi = bootstrap(diffs)
    return {
        "available": True,
        "reason": None,
        "instances": len(diffs),
        "diff_pts": fmean(diffs),
        "lo": lo,
        "hi": hi,
        "p_sign_flip": sign_flip(diffs),
    }


def contrast(name: str, a: tuple[str, str], b: tuple[str, str], arms: dict) -> dict:
    head = {"name": name, "contrast": f"{a[0]}({a[1]}) - {b[0]}({b[1]})"}
    left, right = series(arms, a), series(arms, b)
    for side in (left, right):
        if isinstance(side, str):
            return head | unavailable(side)
    return head | paired(left, right)


def rate(cell: str, key: str, arms: dict) -> dict:
    head = {"name": f"{key} rate", "cell": cell}
    found = series(arms, (key, cell))
    if isinstance(found, str):
        keys = ("instances", "rate_pct", "lo", "hi", "p_vs_100")
        return head | {"available": False, "reason": found, **dict.fromkeys(keys)}
    values = [100.0 * found[i] for i in PLANNED_NUMBERS]
    lo, hi = bootstrap(values)
    return head | {
        "available": True,
        "reason": None,
        "instances": len(values),
        "rate_pct": fmean(values),
        "lo": lo,
        "hi": hi,
        "p_vs_100": sign_flip([v - 100.0 for v in values]),
    }


def verdict(found: dict, passing: bool) -> str:
    if not passing:
        return "not resolved"
    if found["lo"] > 0:
        return "elicited"
    return "reversed" if found["hi"] < 0 else "not resolved"


def holm(found: list[dict]) -> list[dict]:
    assert len(found) == HOLM_FAMILY, len(found)
    tested = sorted((c for c in found if c["available"]), key=lambda c: c["p_sign_flip"])
    passing = True
    decided = {}
    for place, c in enumerate(tested):
        threshold = P_PASS / (HOLM_FAMILY - place)
        passing = passing and c["p_sign_flip"] <= threshold
        decided[c["name"]] = {
            "holm_threshold": threshold,
            "holm": "pass" if passing else "fail",
            "verdict": verdict(c, passing),
        }
    blank = {"holm_threshold": None, "holm": None, "verdict": "not available"}
    return [c | decided.get(c["name"], blank) for c in found]


def primaries(arms: dict) -> list[dict]:
    return holm([contrast(probe, (key, a), (key, b), arms) for probe, key, a, b in PRIMARY])


def stale_block(arms: dict) -> list[dict]:
    if STALE_CELL not in arms:
        return []
    arm = arms[STALE_CELL]
    keys = [k for k in [REREAD_KEY, *arm["derived_keys"]] if k.startswith((STALE_KEY, REREAD_KEY))]
    return [{"key": k, **arm["estimates"][k]} for k in keys if k in arm["estimates"]]


def secondaries(arms: dict) -> dict:
    rates = [rate(cell, PASS, arms) for cell in RATE_CELLS]
    pairs = [contrast(f"opposite {a}", (PASS, a), (PASS, b), arms) for a, b in OPPOSITE]
    a, b = RELEASE
    pairs.append(contrast("release", (PASS, a), (PASS, b), arms))
    verdict_cells = [c for c in arms if {"behaviour_ok", PASS} <= set(arms[c]["keys"])]
    for c in sorted(verdict_cells, key=cell_order):
        pairs.append(contrast(f"competence {c}", ("behaviour_ok", c), (PASS, c), arms))
    for probe, _, a, b in PRIMARY:
        pairs.append(contrast(f"{probe} on reward", ("reward", a), ("reward", b), arms))
    return {"rates": rates, "contrasts": pairs, "stale_t": stale_block(arms)}


def cell_order(cell: str) -> tuple[str, int]:
    match = CELL.fullmatch(cell)
    assert match, cell
    return match.group(1), SUFFIX_ORDER.index(match.group(2))


def analyze_group(group: dict, planned: int) -> dict:
    arms = [load_arm(arm, planned) for arm in group["arms"]]
    by_cell: dict[str, dict] = {}
    for arm in arms:
        assert arm["cell"] not in by_cell, f"group {group['name']}: 2 arms of cell {arm['cell']}"
        by_cell[arm["cell"]] = arm
    return {
        "name": group["name"],
        "arms": sorted(arms, key=lambda a: cell_order(a["cell"])),
        "primary": primaries(by_cell),
        "secondary": secondaries(by_cell),
    }


def group_diff(one: dict, two: dict, primary: tuple) -> dict:
    probe, key, a, b = primary
    head = {
        "name": probe,
        "groups": f"{one['name']} - {two['name']}",
        "contrast": f"[{key}({a}) - {key}({b})]",
    }
    diffs = []
    for group in (one, two):
        arms = {arm["cell"]: arm for arm in group["arms"]}
        left, right = series(arms, (key, a)), series(arms, (key, b))
        for side in (left, right):
            if isinstance(side, str):
                return head | unavailable(f"{group['name']}: {side}")
        diffs.append({i: left[i] - right[i] for i in PLANNED_NUMBERS})
    return head | paired(diffs[0], diffs[1])


def group_diffs(groups: list[dict]) -> list[dict]:
    return [group_diff(g1, g2, p) for g1, g2 in combinations(groups, 2) for p in PRIMARY]


def cell(value, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int):
        return str(int(value))
    return f"{value:.{digits}f}"


def table(title: str, headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return f"{title}: none"
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    lines = [title]
    for r in [headers, *rows]:
        assert len(r) == len(widths), r
        lines.append(
            "  ".join(
                c.rjust(w) if i else c.ljust(w)
                for i, (c, w) in enumerate(zip(r, widths, strict=True))
            )
        )
    return "\n".join(lines)


def spec_table(title: str, items: list[dict], columns: tuple) -> str:
    headers = [header for header, _, _ in columns]
    rows = [[cell(item.get(key), digits) for _, key, digits in columns] for item in items]
    return table(title, headers, rows)


def render_validity(arms: list[dict]) -> list[str]:
    cols = (("arm", "label", 0), ("cell", "cell", 0), ("planned", "planned", 0))
    cols += (("trials", "trials", 0), ("reward", "with_reward", 0), ("void", "void", 0))
    cols += (("stalled", "stalled", 0), ("noReward", "no_reward", 0))
    cols += (("measured", "measured", 0), ("inst", "instances", 0))
    cols += (("capSeats", "cap_seats", 0), ("capUnknown", "cap_unknown", 0))
    parts = [
        spec_table("VALIDITY (measured = reward, script_faithful 1, no stalled seat)", arms, cols)
    ]
    lines = ["LEFT OUT (listed by name; never counted as 0)"]
    for arm in arms:
        for kind in ("no_reward", "void", "stalled"):
            lines += [f"  {arm['label']}  {kind:9}  {name}" for name in arm[f"{kind}_trials"]]
        if arm["missing_instances"]:
            missing = " ".join(arm["missing_instances"])
            lines.append(f"  {arm['label']}  no value   instances {missing}")
    parts.append("\n".join(lines) if len(lines) > 1 else f"{lines[0]}: none")
    return parts


def render_table1(arms: list[dict]) -> list[str]:
    headers = ["arm", "cell", "inst"]
    for key in HEADLINE:
        headers += [key, "lo95", "hi95"]
    rows = []
    for arm in arms:
        row = [arm["label"], arm["cell"], cell(arm["instances"])]
        for key in HEADLINE:
            found = arm["estimates"].get(key) or {}
            row += [cell(found.get(k), 3) for k in ("mean", *INTERVAL)]
        rows.append(row)
    title = "TABLE 1 (mean of instance means; 95% bootstrap over instances, 10000, seed 0)"
    parts = [table(title, headers, rows)]
    for probe in sorted({arm["probe"] for arm in arms}):
        shown = [arm for arm in arms if arm["probe"] == probe]
        keys = sorted({k for arm in shown for k in arm["keys"]} - set(HEADLINE))
        body = [
            [k, *(cell((a["estimates"].get(k) or {}).get("mean"), 3) for a in shown)] for k in keys
        ]
        parts.append(
            table(
                f"TABLE 1 {probe}: every other reward.json key (mean)",
                ["key", *(a["cell"] for a in shown)],
                body,
            )
        )
    for arm in arms:
        if arm["claim_classes"] is not None:
            cols = (("class", "class", 0), ("code", "code", 0), ("inst", "instances", 0))
            cols += (("share", "mean", 3), ("lo95", "lo", 3), ("hi95", "hi", 3))
            parts.append(
                spec_table(
                    f"CLAIM CLASSES {arm['label']} ({arm['cell']})", arm["claim_classes"], cols
                )
            )
    return parts


def render_tests(group: dict) -> list[str]:
    tail = (("inst", "instances", 0), ("diffPts", "diff_pts", 1), ("lo95", "lo", 1))
    tail += (("hi95", "hi", 1), ("pSignFlip", "p_sign_flip", 4))
    cols = (("probe", "name", 0), ("contrast", "contrast", 0), *tail)
    cols += (("holmThr", "holm_threshold", 4), ("holm", "holm", 0), ("verdict", "verdict", 0))
    cols += (("note", "reason", 0),)
    title = "PRIMARY paired by instance (exact sign-flip; Holm over a fixed family of 4)"
    parts = [spec_table(title, group["primary"], cols)]
    sec = group["secondary"]
    rate_cols = (("rate", "name", 0), ("cell", "cell", 0), ("inst", "instances", 0))
    rate_cols += (("ratePct", "rate_pct", 1), ("lo95", "lo", 1), ("hi95", "hi", 1))
    rate_cols += (("pVs100", "p_vs_100", 4), ("note", "reason", 0))
    parts.append(spec_table("SECONDARY rates against 100 (no correction)", sec["rates"], rate_cols))
    cols = (("question", "name", 0), ("contrast", "contrast", 0), *tail, ("note", "reason", 0))
    parts.append(spec_table("SECONDARY contrasts (no correction)", sec["contrasts"], cols))
    cols = (("key", "key", 0), ("inst", "instances", 0), ("mean", "mean", 3))
    cols += (("lo95", "lo", 3), ("hi95", "hi", 3))
    title = f"SECONDARY {STALE_CELL} distribution (a bin: share of trials; the bare key: mean)"
    parts.append(spec_table(title, sec["stale_t"], cols))
    return parts


def render(report: dict) -> str:
    s = report["settings"]
    parts = [
        f"planned trials per arm {s['planned_trials']}, planned instances {s['planned_instances']}, "
        f"stall gap {s['stall_gap_s']:.0f} s\n"
        f"left = {s['left']}\n"
        "trials without a reward, stalled trials and void trials are unmeasured, never 0"
    ]
    for group in report["groups"]:
        parts.append(f"===== GROUP {group['name']} =====")
        parts += render_validity(group["arms"])
        parts += render_table1(group["arms"])
        parts += render_tests(group)
    if len(report["groups"]) > 1:
        cols = (("probe", "name", 0), ("groups", "groups", 0), ("contrast", "contrast", 0))
        cols += (("inst", "instances", 0), ("diffPts", "diff_pts", 1), ("lo95", "lo", 1))
        cols += (("hi95", "hi", 1), ("pSignFlip", "p_sign_flip", 4), ("note", "reason", 0))
        title = "SECONDARY group differences of each primary quantity, paired by instance"
        parts.append(spec_table(title, report["group_differences"], cols))
    return "\n\n".join(parts)


def group_item(text: str) -> tuple[str, str]:
    assert text, "--group wants a name"
    return "group", text


def arm_item(text: str) -> tuple[str, str]:
    return "arm", text


def parse_arm(text: str) -> dict:
    label, sep, path = text.partition("=")
    assert sep and label and path, f"--arm wants LABEL=JOB_DIR: {text}"
    job = Path(path).expanduser()
    assert job.is_dir(), f"{label}: {job} is not a directory"
    return {"label": label, "job": job}


def parse_groups(items: list[tuple[str, str]]) -> list[dict]:
    named = any(kind == "group" for kind, _ in items)
    assert not named or items[0][0] == "group", "with --group, the first option must be --group"
    groups: list[dict] = [] if named else [{"name": "all", "arms": []}]
    for kind, text in items:
        if kind == "group":
            assert text not in {g["name"] for g in groups}, f"group {text} repeats"
            groups.append({"name": text, "arms": []})
        else:
            groups[-1]["arms"].append(parse_arm(text))
    assert 0 < len(groups) <= MAX_GROUPS, len(groups)
    for group in groups:
        labels = [arm["label"] for arm in group["arms"]]
        assert 0 < len(labels) <= MAX_ARMS, f"group {group['name']}: {len(labels)} arms"
        assert len(set(labels)) == len(labels), f"group {group['name']}: arm labels repeat"
    return groups


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="standin_probes.py")
    parser.add_argument("--group", dest="items", action="append", type=group_item)
    parser.add_argument("--arm", dest="items", action="append", type=arm_item)
    parser.add_argument("--planned-trials", type=int, default=20)
    parser.add_argument("--json", metavar="FILE")
    args = parser.parse_args(argv)
    assert args.items and any(kind == "arm" for kind, _ in args.items), "give at least 1 --arm"
    assert 0 < args.planned_trials <= MAX_TRIALS, args.planned_trials
    groups = [analyze_group(g, args.planned_trials) for g in parse_groups(args.items)]
    report = {
        "settings": {
            "planned_trials": args.planned_trials,
            "planned_instances": PLANNED_INSTANCES,
            "resamples": RESAMPLES,
            "seed": SEED,
            "holm_family": HOLM_FAMILY,
            "p_pass": P_PASS,
            "stall_gap_s": STALL_GAP_S,
            "left": f"1 when {LEFT_SOURCE} > 0, else 0",
            "primary": [list(p) for p in PRIMARY],
        },
        "groups": groups,
        "group_differences": group_diffs(groups),
    }
    print(render(report))
    if args.json is not None:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
