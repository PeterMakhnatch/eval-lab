import argparse
import json
import math
import random
import re
import sys
from collections import Counter
from datetime import datetime
from itertools import combinations
from pathlib import Path
from statistics import fmean, stdev

MAX_ARMS = 32
MAX_TRIALS = 1000
MAX_EVENTS = 100_000
MAX_INSTANCES = 20
MAX_STEPS = 100_000
RESAMPLES = 10_000
SEED = 0
BISECT_STEPS = 200
K_EFF_CEILING = 1000.0
REWARD_TOLERANCE = 1e-6
SIGN_TOLERANCE = 1e-9
P_PASS = 0.05
SLOPE_KS = (2, 3, 4, 8)
MIN_SLOPE_KS = 3
SLOPE_KINDS = (("team", "primary"), ("claim", "secondary"), ("indep", "secondary"))
MARGIN_KS = ((8, "primary"), (2, "secondary"), (3, "secondary"), (4, "secondary"))
LABEL = re.compile(r"(solo|indep|team|claim)-([1-9][0-9]*)")
NUMBER = re.compile(r"(?<![A-Za-z0-9_])[0-9]+(?![A-Za-z0-9_])")
CHANNEL_KINDS = ("team", "claim")
SPLIT_KEYS = ("repeat_unsent", "repeat_unread", "repeat_read", "repeat_own")
BOX_SPLIT_KEYS = ("repeat_unposted_box", "repeat_unread_box", "repeat_read_box")
EVENT_KEYS = (
    "opens",
    "distinct",
    "flags",
    "empty",
    "repeats",
    *SPLIT_KEYS,
    *BOX_SPLIT_KEYS,
    "unused",
    "stranded",
    "messages",
    "msgs_per_seat",
    "p1_msg_share",
    "reads",
    "post_read_share",
    "k_eff",
    "first_collision",
)
COST_FIELDS = (
    ("cost_usd", "cost_usd"),
    ("tokens_in", "n_input_tokens"),
    ("tokens_out", "n_output_tokens"),
    ("tokens_cache", "n_cache_tokens"),
)
RUN_SUMS = ("trial_exception", "seat_exceptions", "zero_calls", "zero_calls_unknown")


def parse_arm(text: str, total: int) -> dict:
    label, sep, path = text.partition("=")
    match = LABEL.fullmatch(label)
    assert sep and match and path, (
        f"--arm wants LABEL=JOB_DIR, LABEL solo|indep|team|claim-N: {text}"
    )
    kind, n = match.group(1), int(match.group(2))
    assert kind == "solo" or n >= 2, f"{label}: a multi-seat arm needs at least 2 seats"
    assert kind == "solo" or total % n == 0, f"{label}: total budget {total} does not divide by {n}"
    job = Path(path)
    assert job.is_dir(), f"{label}: {job} is not a directory"
    return {
        "label": label,
        "kind": kind,
        "n": n,
        "seats": 1 if kind == "solo" else n,
        "budget": n if kind == "solo" else total // n,
        "job": job,
    }


def load_reference(path: str | None) -> dict:
    if path is None:
        return {}
    data = json.loads(Path(path).read_text())
    assert isinstance(data, dict), f"{path}: want {{condition: {{policy: value}}}}"
    for condition, values in data.items():
        assert isinstance(values, dict), (path, condition)
        for policy, value in values.items():
            assert isinstance(value, int | float) and not isinstance(value, bool), (
                path,
                condition,
                policy,
            )
    return data


def trial_dirs(job: Path) -> list[Path]:
    found = sorted(p for p in job.iterdir() if "__" in p.name and (p / "result.json").is_file())
    assert 0 < len(found) <= MAX_TRIALS, f"{job}: {len(found)} trial folders"
    return found


def played(trial: Path) -> list[dict]:
    lines = (trial / "verifier" / "events.jsonl").read_text().splitlines()
    assert len(lines) <= MAX_EVENTS, trial
    events = [json.loads(line) for line in lines if line.strip()]
    kept = []
    for number, e in enumerate(events, start=1):
        assert isinstance(e, dict) and e.get("seq") == number, (trial, number)
        if e["kind"] == "admin.close" and e["ok"]:
            break
        if e["ok"] and e["seat"] is not None and not e.get("late"):
            kept.append(e)
    return kept


def read_reward(trial: Path, result: dict) -> float | None:
    path = trial / "verifier" / "reward.json"
    if path.is_file():
        value = json.loads(path.read_text()).get("reward")
    else:
        value = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    if value is None:
        return None
    assert isinstance(value, int | float) and not isinstance(value, bool), trial
    assert 0.0 <= value <= 1.0, (trial, value)
    return float(value)


def usage(result: dict) -> dict:
    out: dict = {key: 0 for key, _ in COST_FIELDS} | {"cost_usd": 0.0, "no_cost": 0}
    for item in result["seat_results"]:
        got = item.get("agent_result") or {}
        assert isinstance(got, dict), item.get("seat")
        cost = got.get("cost_usd")
        out["no_cost"] += isinstance(cost, bool) or not isinstance(cost, int | float)
        for key, field in COST_FIELDS:
            value = got.get(field)
            if isinstance(value, int | float) and not isinstance(value, bool):
                out[key] += value
    return out


def seconds(result: dict) -> float | None:
    start, end = result.get("started_at"), result.get("finished_at")
    if not (isinstance(start, str) and isinstance(end, str)):
        return None
    span = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
    assert span >= 0, (start, end)
    return span


def zero_calls(item: dict) -> bool | None:
    got = item.get("agent_result") or {}
    times = (got.get("metadata") or {}).get("api_request_times_msec")
    if isinstance(times, list):
        return not times
    tokens = got.get("n_input_tokens")
    if isinstance(tokens, int) and not isinstance(tokens, bool):
        return tokens == 0
    return None


def agent_steps(trial: Path, seat: str) -> int | None:
    path = trial / "seats" / seat / "agent" / "trajectory.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    steps = data.get("steps") if isinstance(data, dict) else None
    assert isinstance(steps, list) and len(steps) <= MAX_STEPS, path
    return sum(1 for s in steps if isinstance(s, dict) and s.get("source") == "agent")


def run_info(trial: Path, result: dict, seats: list[str], cap: int) -> dict:
    items = result["seat_results"]
    zero = [zero_calls(item) for item in items]
    steps = [agent_steps(trial, seat) for seat in seats]
    return {
        "wall_s": seconds(result),
        "trial_exception": int(result.get("exception_info") is not None),
        "seat_exceptions": sum(item.get("exception_info") is not None for item in items),
        "zero_calls": sum(z is True for z in zero),
        "zero_calls_unknown": sum(z is None for z in zero),
        "cap_seats": sum(s is not None and s >= cap for s in steps),
        "cap_unknown": sum(s is None for s in steps),
    }


def mentioned(text: str) -> set[int]:
    assert isinstance(text, str)
    return {int(token) for token in NUMBER.findall(text)}


def repeat_kinds(first: tuple[str, int], box: int, e: dict, chan: dict) -> tuple[str, str]:
    first_seat, first_seq = first
    seat = e["seat"]
    if first_seat == seat:
        return "own", "own"
    reports = [p[2] for p in chan["posts"] if p[1] == first_seat and p[0] > first_seq]
    known = chan["seen"].get(seat, {})
    informed = any(i in known and known[i] <= e["t"] for i in reports)
    f13 = "read" if informed else ("unread" if reports else "unsent")
    times = [p[0] for p in chan["posts"] if p[1] == first_seat and box in p[3]]
    if not times:
        return f13, "unposted_box"
    return f13, "read_box" if chan["last_read"].get(seat, 0) > times[0] else "unread_box"


def note_message(e: dict, chan: dict, c: Counter) -> None:
    seat = e["seat"]
    if e["method"] == "POST":
        c["messages"] += 1
        c["messages_p1"] += seat == "p1"
        text = e["response"]["text"]
        chan["posts"].append((e["seq"], seat, e["response"]["id"], mentioned(text)))
        return
    c["reads"] += 1
    chan["last_read"][seat] = e["seq"]
    seen = chan["seen"].setdefault(seat, {})
    for m in e["response"].get("messages", []):
        seen.setdefault(m["id"], e["t"])


def walk(events: list[dict], budget: int) -> tuple[Counter, dict[str, int], set[int], int, dict]:
    first: dict[int, tuple[str, int]] = {}
    chan: dict = {"posts": [], "seen": {}, "last_read": {}}
    state: dict[str, tuple[bool, bool]] = {}
    first_box: dict[str, int] = {}
    flag_boxes: set[int] = set()
    used: Counter = Counter()
    c: Counter = Counter()
    for e in events:
        seat, kind, method = e["seat"], e["kind"], e["method"]
        posted, read = state.get(seat, (False, False))
        if (kind, method) in (("msg", "POST"), ("msgs", "GET")):
            note_message(e, chan, c)
            state[seat] = (posted or method == "POST", read or method == "GET")
        elif kind == "open" and method == "POST":
            box = e["request"]["box"]
            assert isinstance(box, int) and e["response"]["box"] == box, e["seq"]
            used[seat] += 1
            assert e["response"]["budget_left"] == budget - used[seat], (seat, e["seq"], budget)
            first_box.setdefault(seat, box)
            c["opens"] += 1
            c["post_read"] += posted and read
            state[seat] = (False, False)
            if box in first:
                f13, by_box = repeat_kinds(first[box], box, e, chan)
                c["repeats"] += 1
                c["repeat_" + f13] += 1
                c["repeat_" + by_box] += by_box != "own"
            else:
                first[box] = (seat, e["seq"])
                c["flags" if e["response"]["flag"] else "empty"] += 1
                flag_boxes.update([box] if e["response"]["flag"] else [])
    assert c["opens"] == c["flags"] + c["empty"] + c["repeats"] and c["flags"] == len(flag_boxes)
    assert c["repeats"] == sum(c[key] for key in SPLIT_KEYS)
    assert c["repeats"] == c["repeat_own"] + sum(c[key] for key in BOX_SPLIT_KEYS)
    return c, first_box, flag_boxes, len(first), chan


def k_eff(distinct: int, budget: int, boxes: int) -> float:
    assert 0 <= distinct < boxes and 0 < budget < boxes, (distinct, budget, boxes)

    def covered(k: float) -> float:
        return boxes * (1.0 - (1.0 - budget / boxes) ** k)

    lo, hi = 0.0, K_EFF_CEILING
    assert covered(hi) >= distinct
    for _ in range(BISECT_STEPS):
        mid = (lo + hi) / 2.0
        lo, hi = (mid, hi) if covered(mid) < distinct else (lo, mid)
    return (lo + hi) / 2.0


def first_collision(seats: list[str], first_box: dict[str, int]) -> float | None:
    pairs = list(combinations(seats, 2))
    if not pairs:
        return None
    same = sum(1 for a, b in pairs if a in first_box and first_box.get(a) == first_box.get(b))
    return same / len(pairs)


def check_seats(events: list[dict], seats: list[str], channel: bool) -> None:
    for e in events:
        assert e["seat"] in seats, (e["seq"], e["seat"])
        if e["kind"] == "rules":
            assert e["response"]["channel"] is channel, (e["seq"], channel)
            assert e["response"]["seats"] == seats, (e["seq"], seats)


def trial_row(trial: Path, arm: dict, cfg: dict) -> dict:
    result = json.loads((trial / "result.json").read_text())
    assert isinstance(result, dict) and isinstance(result.get("seat_results"), list), trial
    seats = sorted(item["seat"] for item in result["seat_results"])
    assert len(seats) == arm["seats"], (trial, seats, arm["label"])
    row = {"instance": trial.name.split("__", 1)[0], "trial": trial.name, **usage(result)}
    row |= run_info(trial, result, seats, cfg["cap"])
    row["reward"] = read_reward(trial, result)
    row["budget"] = arm["seats"] * arm["budget"]
    row["seat_count"] = len(seats)
    if row["reward"] is None:
        return row
    events = played(trial)
    channel = arm["kind"] in CHANNEL_KINDS
    check_seats(events, seats, channel)
    c, first_box, flag_boxes, distinct, chan = walk(events, arm["budget"])
    assert abs(row["reward"] - c["flags"] / cfg["flags"]) < REWARD_TOLERANCE, (trial, c["flags"])
    unused = row["budget"] - c["opens"]
    assert unused >= 0, trial
    row.update({key: c[key] for key in ("opens", "flags", "empty", "repeats", "messages", "reads")})
    row.update({key: c[key] if channel else None for key in (*SPLIT_KEYS, *BOX_SPLIT_KEYS)})
    row["msgs_per_seat"] = c["messages"] / len(seats)
    has_msgs = channel and c["messages"] > 0
    row["p1_msg_share"] = c["messages_p1"] / c["messages"] if has_msgs else None
    row.update(
        distinct=distinct,
        unused=unused,
        stranded=unused if c["flags"] < cfg["flags"] else 0,
        post_read_share=c["post_read"] / c["opens"] if c["opens"] else None,
        k_eff=k_eff(distinct, arm["budget"], cfg["boxes"]),
        first_collision=first_collision(seats, first_box),
        first_box=first_box,
        first_poster=(chan["posts"][0][1] if chan["posts"] else "none") if channel else None,
        flag_boxes=frozenset(flag_boxes),
    )
    assert row["flags"] + row["empty"] + row["repeats"] + row["unused"] == row["budget"], trial
    return row


def reward_rows(rows: list[dict], missing_as_zero: bool) -> list[dict]:
    if missing_as_zero:
        return [r | {"reward": 0.0} if r["reward"] is None else r for r in rows]
    return [r for r in rows if r["reward"] is not None]


def instance_means(rows: list[dict]) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    for r in rows:
        grouped.setdefault(r["instance"], []).append(r["reward"])
    return {name: fmean(values) for name, values in sorted(grouped.items())}


def mean_of(rows: list[dict], key: str) -> float | None:
    values = [r[key] for r in rows if r[key] is not None]
    return fmean(values) if values else None


def first_boxes(rows: list[dict]) -> dict[str, dict]:
    counts: dict[str, Counter] = {}
    for r in rows:
        for seat, box in r["first_box"].items():
            counts.setdefault(seat, Counter())[box] += 1
    out = {}
    for seat, counter in sorted(counts.items()):
        box, n = max(counter.items(), key=lambda item: (item[1], -item[0]))
        out[seat] = {"box": box, "share": n / sum(counter.values())}
    return out


def first_posters(rows: list[dict]) -> dict[str, float] | None:
    posters = [r["first_poster"] for r in rows if r["first_poster"] is not None]
    if not posters:
        return None
    return {seat: n / len(posters) for seat, n in sorted(Counter(posters).items())}


def run_measures(rows: list[dict]) -> dict:
    out: dict = {key: sum(r[key] for r in rows) for key in RUN_SUMS}
    seat_total = sum(r["seat_count"] for r in rows)
    out["cap_unknown"] = sum(r["cap_unknown"] for r in rows)
    all_unknown = out["cap_unknown"] == seat_total
    out["cap_seats"] = None if all_unknown else sum(r["cap_seats"] for r in rows)
    out["wall_s"] = mean_of(rows, "wall_s")
    return out


def summarize(arm: dict, rows: list[dict], cfg: dict) -> dict:
    scored = [r for r in rows if r["reward"] is not None]
    assert scored, f"{arm['label']}: no trial has a reward"
    by_instance = instance_means(reward_rows(rows, cfg["missing_as_zero"]))
    means = list(by_instance.values())
    assert len(means) <= MAX_INSTANCES, arm["label"]
    out = {
        "label": arm["label"],
        "kind": arm["kind"],
        "seats": arm["seats"],
        "budget_per_seat": arm["budget"],
        "budget": arm["seats"] * arm["budget"],
        "job": str(arm["job"]),
        "trials": len(rows),
        "no_reward": len(rows) - len(scored),
        "instances": len(means),
        "reward": fmean(means),
        "reward_se": stdev(means) / math.sqrt(len(means)) if len(means) > 1 else None,
    }
    out |= {key: mean_of(scored, key) for key in EVENT_KEYS}
    out |= {key: mean_of(rows, key) for key, _ in COST_FIELDS}
    out["no_cost_seats"] = sum(r["no_cost"] for r in rows)
    out |= run_measures(rows)
    out["first_box_by_seat"] = first_boxes(scored)
    out["first_poster"] = first_posters(scored)
    out["reference"] = cfg["reference"].get(arm["label"])
    out["instance_rewards"] = by_instance
    return out


def bootstrap(n: int, statistic) -> tuple[float, float]:
    assert 0 < n <= MAX_INSTANCES
    rng = random.Random(SEED)
    values = sorted(statistic([rng.randrange(n) for _ in range(n)]) for _ in range(RESAMPLES))
    return values[int(0.025 * RESAMPLES)], values[int(0.975 * RESAMPLES) - 1]


def sign_flip(diffs: list[float]) -> float:
    assert 0 < len(diffs) <= MAX_INSTANCES, len(diffs)
    sums = [0.0]
    for d in diffs:
        sums += [s + d for s in sums]
    total = sum(diffs)
    extreme = sum(1 for s in sums if abs(total - 2.0 * s) >= abs(total) - SIGN_TOLERANCE)
    assert len(sums) == 2 ** len(diffs) and 0 < extreme <= len(sums)
    return extreme / len(sums)


def contrast(a: str, b: str, question: str, means: dict[str, dict[str, float]]) -> dict:
    shared = sorted(set(means[a]) & set(means[b]))
    assert 0 < len(shared) <= MAX_INSTANCES, (a, b, len(shared))
    diffs = [100.0 * (means[a][i] - means[b][i]) for i in shared]
    lo, hi = bootstrap(len(diffs), lambda sample: fmean(diffs[i] for i in sample))
    return {
        "contrast": f"{a} - {b}",
        "question": question,
        "instances": len(shared),
        "diff_pts": fmean(diffs),
        "lo": lo,
        "hi": hi,
        "p_sign_flip": sign_flip(diffs),
    }


def contrasts(means: dict[str, dict[str, float]], ks: list[int], total: int) -> list[dict]:
    solo = f"solo-{total}"
    out = []
    for k in ks:
        plan = (
            (f"team-{k}", f"indep-{k}", "communication"),
            (f"claim-{k}", f"team-{k}", "claim rule"),
            (solo, f"team-{k}", "budget split"),
            (solo, f"claim-{k}", "budget split, claim"),
            (f"indep-{k}", f"solo-{total // k}", "headcount"),
            (f"indep-{k}", f"union-{k}", "interference"),
        )
        out += [contrast(a, b, q, means) for a, b, q in plan if a in means and b in means]
    return out


def slope(xs: list[float], ys: list[float]) -> float:
    mx, my = fmean(xs), fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    assert sxx > 0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx


def slope_test(kind: str, role: str, means: dict[str, dict[str, float]]) -> dict | None:
    present = [k for k in SLOPE_KS if f"{kind}-{k}" in means]
    if len(present) < MIN_SLOPE_KS:
        return None
    shared = sorted(set.intersection(*(set(means[f"{kind}-{k}"]) for k in present)))
    assert 0 < len(shared) <= MAX_INSTANCES, (kind, len(shared))
    xs = [math.log2(k) for k in present]
    slopes = [slope(xs, [100.0 * means[f"{kind}-{k}"][i] for k in present]) for i in shared]
    lo, hi = bootstrap(len(slopes), lambda sample: fmean(slopes[i] for i in sample))
    mean, p = fmean(slopes), sign_flip(slopes)
    return {
        "test": f"slope of {kind}-k on log2 k",
        "role": role,
        "ks": present,
        "instances": len(shared),
        "mean_slope_pts_per_doubling": mean,
        "lo": lo,
        "hi": hi,
        "p_sign_flip": p,
        "verdict": "pass" if mean < 0 and p < P_PASS else "fail",
    }


def margin_test(k: int, role: str, means: dict, total: int, margin: float) -> dict | None:
    a, b = f"solo-{total}", f"claim-{k}"
    if a not in means or b not in means:
        return None
    found = contrast(a, b, "split cost, claim", means)
    edge = 100.0 * margin
    verdict = "inconclusive"
    verdict = "pass" if found["hi"] < edge else verdict
    verdict = "fail" if found["lo"] > edge else verdict
    return found | {"role": role, "margin_pts": edge, "verdict": verdict}


def union_pool(rows: list[dict], k: int, cfg: dict) -> dict:
    pools: dict[str, list[frozenset]] = {r["instance"]: [] for r in rows}
    for r in reward_rows(rows, cfg["missing_as_zero"]):
        pools[r["instance"]].append(r.get("flag_boxes") or frozenset())
    means: dict[str, float] = {}
    groups = 0
    for name, pool in sorted(pools.items()):
        count = len(pool) // k
        scores = [len(frozenset().union(*pool[g * k : (g + 1) * k])) for g in range(count)]
        groups += count
        if scores:
            means[name] = fmean(scores) / cfg["flags"]
    missing = sorted(set(pools) - set(means))
    return {
        "k": k,
        "groups": groups,
        "instances": len(means),
        "missing_instances": missing,
        "reward": fmean(means.values()) if means else None,
        "instance_rewards": means,
    }


def unions(arms: list[dict], rows_by: dict[str, list[dict]], cfg: dict) -> list[dict]:
    total = cfg["total"]
    out = []
    for arm in arms:
        if arm["kind"] != "solo" or arm["n"] >= total or total % arm["n"]:
            continue
        k = total // arm["n"]
        out.append(union_pool(rows_by[arm["label"]], k, cfg) | {"pool": arm["label"]})
    return out


def baselines(ks: list[int], total: int, boxes: int) -> list[dict]:
    out = []
    for k in ks:
        m = total // k
        assert m * k == total and 0 < m < boxes, (k, total, boxes)
        distinct = boxes * (1.0 - (1.0 - m / boxes) ** k)
        out.append(
            {
                "k": k,
                "budget_per_seat": m,
                "random_distinct": distinct,
                "random_repeats": k * m - distinct,
                "identical_distinct": m,
                "identical_repeats": k * m - m,
            }
        )
    return out


def analyze(arms: list[dict], cfg: dict) -> dict:
    total = cfg["total"]
    summaries, rows_by = [], {}
    for arm in arms:
        rows_by[arm["label"]] = [trial_row(t, arm, cfg) for t in trial_dirs(arm["job"])]
        summaries.append(summarize(arm, rows_by[arm["label"]], cfg))
    means = {s["label"]: s["instance_rewards"] for s in summaries}
    pooled = unions(arms, rows_by, cfg)
    means |= {f"union-{u['k']}": u["instance_rewards"] for u in pooled if u["instance_rewards"]}
    ks = sorted({a["n"] for a in arms if a["kind"] != "solo"})
    slopes = [slope_test(kind, role, means) for kind, role in SLOPE_KINDS]
    margins = [margin_test(k, role, means, total, cfg["margin"]) for k, role in MARGIN_KS]
    base_ks = sorted({1, *ks}) if f"solo-{total}" in means else ks
    return {
        "settings": {key: cfg[key] for key in ("total", "boxes", "flags", "cap", "margin")}
        | {"missing_as_zero": cfg["missing_as_zero"]},
        "arms": summaries,
        "test1_slope": [t for t in slopes if t is not None],
        "test2_margin": [t for t in margins if t is not None],
        "unions": pooled,
        "contrasts": contrasts(means, ks, total),
        "baselines": baselines(base_ks, total, cfg["boxes"]),
    }


def cell(value, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def table(title: str, headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return f"{title}: none"
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    lines = [title]
    for r in [headers, *rows]:
        lines.append(
            "  ".join(
                c.rjust(w) if i else c.ljust(w)
                for i, (c, w) in enumerate(zip(r, widths, strict=True))
            )
        )
    return "\n".join(lines)


def spec_table(title: str, items: list[dict], first: str, columns: tuple) -> str:
    headers = [first, *(header for header, _, _ in columns)]
    rows = [[cell(a[first]), *(cell(a[key], digits) for _, key, digits in columns)] for a in items]
    return table(title, headers, rows)


def seat_text(a: dict) -> dict:
    boxes = " ".join(f"{s}:{v['box']}@{v['share']:.2f}" for s, v in a["first_box_by_seat"].items())
    posters = a["first_poster"]
    reference = a["reference"]
    return {
        "label": a["label"],
        "firstBoxBySeat": boxes or None,
        "firstPoster": " ".join(f"{s}:{v:.2f}" for s, v in posters.items()) if posters else None,
        "reference": " ".join(f"{p}:{v:.3f}" for p, v in reference.items()) if reference else None,
    }


def render_arms(arms: list[dict]) -> list[str]:
    shown = [a | {"reward_pct": 100.0 * a["reward"]} for a in arms]
    shown = [
        a | {"se_pct": None if a["reward_se"] is None else 100.0 * a["reward_se"]} for a in shown
    ]
    outcome = (("trials", "trials", 0), ("noRwd", "no_reward", 0), ("reward%", "reward_pct", 1))
    outcome += (("se", "se_pct", 1), ("opens", "opens", 2), ("distinct", "distinct", 2))
    outcome += (("budget", "budget", 0), ("=flags", "flags", 2), ("+empty", "empty", 2))
    outcome += (("+repeats", "repeats", 2), ("+unused", "unused", 2), ("strand", "stranded", 2))
    repeats = (("rpUnsent", "repeat_unsent", 2), ("rpUnread", "repeat_unread", 2))
    repeats += (("rpRead", "repeat_read", 2), ("rpOwn", "repeat_own", 2))
    repeats += (("rpUnposted_box", "repeat_unposted_box", 2),)
    repeats += (("rpUnread_box", "repeat_unread_box", 2), ("rpRead_box", "repeat_read_box", 2))
    channel = (("msgs", "messages", 2), ("msgsPerSeat", "msgs_per_seat", 2))
    channel += (("p1MsgShare", "p1_msg_share", 2), ("reads", "reads", 2))
    channel += (("postRead", "post_read_share", 2), ("k_eff", "k_eff", 2))
    channel += (("collide", "first_collision", 2),)
    cost = (("usd", "cost_usd", 4), ("tokIn", "tokens_in", 0), ("tokOut", "tokens_out", 0))
    cost += (("tokCache", "tokens_cache", 0), ("seatsNoCost", "no_cost_seats", 0))
    cost += (("wallS", "wall_s", 1), ("trialExc", "trial_exception", 0))
    cost += (("seatExc", "seat_exceptions", 0), ("zeroCall", "zero_calls", 0))
    cost += (("zeroCallUnk", "zero_calls_unknown", 0), ("capSeats", "cap_seats", 0))
    cost += (("capUnk", "cap_unknown", 0),)
    seats = (("firstBoxBySeat", "firstBoxBySeat", 0), ("firstPoster", "firstPoster", 0))
    seats += (("reference", "reference", 0),)
    return [
        spec_table(
            "ARMS: outcome (per-trial means; reward = mean of instance means)",
            shown,
            "label",
            outcome,
        ),
        spec_table(
            "ARMS: repeat split, channel arms (F13 primary; _box = box number)",
            shown,
            "label",
            repeats,
        ),
        spec_table("ARMS: channel and spread", shown, "label", channel),
        spec_table(
            "ARMS: recorded cost and run (sums of seats unless a mean)", shown, "label", cost
        ),
        spec_table("ARMS: seats", [seat_text(a) for a in arms], "label", seats),
    ]


def render_tests(report: dict) -> list[str]:
    slope_cols = (("role", "role", 0), ("ks", "ks_text", 0), ("inst", "instances", 0))
    slope_cols += (("meanSlopePts", "mean_slope_pts_per_doubling", 2), ("lo95", "lo", 2))
    slope_cols += (("hi95", "hi", 2), ("pSignFlip", "p_sign_flip", 4), ("verdict", "verdict", 0))
    slopes = [t | {"ks_text": ",".join(str(k) for k in t["ks"])} for t in report["test1_slope"]]
    margin_cols = (("role", "role", 0), ("inst", "instances", 0), ("diffPts", "diff_pts", 1))
    margin_cols += (("lo95", "lo", 1), ("hi95", "hi", 1), ("marginPts", "margin_pts", 1))
    margin_cols += (("pSignFlip", "p_sign_flip", 4), ("verdict", "verdict", 0))
    union_cols = (("pool", "pool", 0), ("groups", "groups", 0), ("inst", "instances", 0))
    union_cols += (("reward%", "reward_pct", 1), ("missing", "missing_text", 0))
    pooled = [
        u
        | {
            "reward_pct": None if u["reward"] is None else 100.0 * u["reward"],
            "missing_text": ",".join(u["missing_instances"]) or None,
        }
        for u in report["unions"]
    ]
    return [
        spec_table(
            "TEST 1: per-instance slope of reward on log2 k (pass: mean < 0, p < 0.05)",
            slopes,
            "test",
            slope_cols,
        ),
        spec_table(
            "TEST 2: d = solo-T - claim-k (pass: hi95 < margin; fail: lo95 > margin)",
            report["test2_margin"],
            "contrast",
            margin_cols,
        ),
        spec_table(
            "UNION-k: disjoint groups of k solo-(T/k) trials, distinct flags / flags",
            pooled,
            "k",
            union_cols,
        ),
    ]


def render(report: dict) -> str:
    s = report["settings"]
    mode = (
        "counted as reward 0 in reward means and contrasts"
        if s["missing_as_zero"]
        else "left out of the means (unmeasured)"
    )
    header = f"total budget {s['total']}, boxes {s['boxes']}, flags {s['flags']}, turn cap {s['cap']}, margin {s['margin']}"
    parts = [f"{header}\nmode: trials without a reward are {mode}"]
    parts += render_arms(report["arms"])
    parts += render_tests(report)
    contrast_cols = (("question", "question", 0), ("inst", "instances", 0))
    contrast_cols += (("diffPts", "diff_pts", 1), ("lo95", "lo", 1), ("hi95", "hi", 1))
    contrast_cols += (("pSignFlip", "p_sign_flip", 4),)
    title = "CONTRASTS paired by instance (bootstrap 10000, seed 0; exact sign-flip)"
    parts.append(spec_table(title, report["contrasts"], "contrast", contrast_cols))
    base_cols = (("m", "budget_per_seat", 0), ("randDistinct", "random_distinct", 2))
    base_cols += (("randRepeats", "random_repeats", 2), ("identDistinct", "identical_distinct", 0))
    base_cols += (("identRepeats", "identical_repeats", 0),)
    title = f"BASELINES (n={s['boxes']} boxes, total {s['total']} opens)"
    parts.append(spec_table(title, report["baselines"], "k", base_cols))
    return "\n\n".join(parts)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="crowd_scaling.py")
    parser.add_argument("--arm", action="append", required=True, metavar="LABEL=JOB_DIR")
    parser.add_argument("--total-budget", type=int, default=24)
    parser.add_argument("--boxes", type=int, default=60)
    parser.add_argument("--flags", type=int, default=20)
    parser.add_argument("--turn-cap", type=int, default=30)
    parser.add_argument("--margin", type=float, default=0.05)
    parser.add_argument("--missing-as-zero", action="store_true")
    parser.add_argument("--reference", metavar="JSON_FILE")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    assert 0 < args.total_budget < args.boxes and 0 < args.flags <= args.boxes
    assert args.turn_cap > 0 and 0.0 <= args.margin < 1.0
    assert 0 < len(args.arm) <= MAX_ARMS
    arms = [parse_arm(text, args.total_budget) for text in args.arm]
    assert len({a["label"] for a in arms}) == len(arms), "arm labels repeat"
    cfg = {
        "total": args.total_budget,
        "boxes": args.boxes,
        "flags": args.flags,
        "cap": args.turn_cap,
        "margin": args.margin,
        "missing_as_zero": args.missing_as_zero,
        "reference": load_reference(args.reference),
    }
    report = analyze(arms, cfg)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
