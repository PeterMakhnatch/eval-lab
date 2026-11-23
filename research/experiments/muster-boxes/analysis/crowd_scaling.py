import argparse
import json
import math
import random
import re
import sys
from collections import Counter
from itertools import combinations
from pathlib import Path
from statistics import fmean, stdev

MAX_ARMS = 32
MAX_TRIALS = 1000
MAX_EVENTS = 100_000
MAX_INSTANCES = 20
RESAMPLES = 10_000
SEED = 0
BISECT_STEPS = 200
K_EFF_CEILING = 1000.0
REWARD_TOLERANCE = 1e-6
SIGN_TOLERANCE = 1e-9
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


def walk(events: list[dict], budget: int) -> tuple[Counter, dict[str, int], int]:
    first: dict[int, tuple[str, int]] = {}
    chan: dict = {"posts": [], "seen": {}, "last_read": {}}
    state: dict[str, tuple[bool, bool]] = {}
    first_box: dict[str, int] = {}
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
    assert c["opens"] == c["flags"] + c["empty"] + c["repeats"]
    assert c["repeats"] == sum(c[key] for key in SPLIT_KEYS)
    assert c["repeats"] == c["repeat_own"] + sum(c[key] for key in BOX_SPLIT_KEYS)
    return c, first_box, len(first)


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


def trial_row(trial: Path, arm: dict, boxes: int, flags_total: int) -> dict:
    result = json.loads((trial / "result.json").read_text())
    assert isinstance(result, dict) and isinstance(result.get("seat_results"), list), trial
    seats = sorted(item["seat"] for item in result["seat_results"])
    assert len(seats) == arm["seats"], (trial, seats, arm["label"])
    row = {"instance": trial.name.split("__", 1)[0], "trial": trial.name, **usage(result)}
    row["reward"] = read_reward(trial, result)
    if row["reward"] is None:
        return row
    events = played(trial)
    channel = arm["kind"] in CHANNEL_KINDS
    check_seats(events, seats, channel)
    c, first_box, distinct = walk(events, arm["budget"])
    assert abs(row["reward"] - c["flags"] / flags_total) < REWARD_TOLERANCE, (trial, c["flags"])
    unused = arm["seats"] * arm["budget"] - c["opens"]
    assert unused >= 0, trial
    row.update({key: c[key] for key in ("opens", "flags", "empty", "repeats", "messages", "reads")})
    row.update({key: c[key] if channel else None for key in (*SPLIT_KEYS, *BOX_SPLIT_KEYS)})
    row["msgs_per_seat"] = c["messages"] / len(seats)
    has_msgs = channel and c["messages"] > 0
    row["p1_msg_share"] = c["messages_p1"] / c["messages"] if has_msgs else None
    row.update(
        distinct=distinct,
        unused=unused,
        stranded=unused if c["flags"] < flags_total else 0,
        post_read_share=c["post_read"] / c["opens"] if c["opens"] else None,
        k_eff=k_eff(distinct, arm["budget"], boxes),
        first_collision=first_collision(seats, first_box),
    )
    return row


def instance_means(rows: list[dict]) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    for r in rows:
        grouped.setdefault(r["instance"], []).append(r["reward"])
    return {name: fmean(values) for name, values in sorted(grouped.items())}


def mean_of(rows: list[dict], key: str) -> float | None:
    values = [r[key] for r in rows if r[key] is not None]
    return fmean(values) if values else None


def summarize(arm: dict, rows: list[dict]) -> dict:
    scored = [r for r in rows if r["reward"] is not None]
    assert scored, f"{arm['label']}: no trial has a reward"
    by_instance = instance_means(scored)
    means = list(by_instance.values())
    assert len(means) <= MAX_INSTANCES, arm["label"]
    out = {
        "label": arm["label"],
        "kind": arm["kind"],
        "seats": arm["seats"],
        "budget_per_seat": arm["budget"],
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
        )
        out += [contrast(a, b, q, means) for a, b, q in plan if a in means and b in means]
    return out


def slope(xs: list[float], ys: list[float]) -> float:
    mx, my = fmean(xs), fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    assert sxx > 0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx


def trend(kind: str, means: dict[str, dict[str, float]], ks: list[int], total: int) -> dict | None:
    solo = f"solo-{total}"
    present = [k for k in ks if f"{kind}-{k}" in means]
    if solo not in means or len(present) < 2:
        return None
    shared = sorted(set(means[solo]).intersection(*(means[f"{kind}-{k}"] for k in present)))
    assert 0 < len(shared) <= MAX_INSTANCES, (kind, len(shared))
    gaps = {
        k: [100.0 * (means[solo][i] - means[f"{kind}-{k}"][i]) for i in shared] for k in present
    }
    xs = [math.log2(k) for k in present]

    def statistic(sample: list[int]) -> float:
        return slope(xs, [fmean(gaps[k][i] for i in sample) for k in present])

    lo, hi = bootstrap(len(shared), statistic)
    return {
        "gap": f"{solo} - {kind}-k",
        "ks": present,
        "instances": len(shared),
        "mean_gap_pts": {str(k): fmean(gaps[k]) for k in present},
        "slope_pts_per_doubling": statistic(list(range(len(shared)))),
        "lo": lo,
        "hi": hi,
    }


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


def analyze(arms: list[dict], boxes: int, flags_total: int, total: int) -> dict:
    summaries = []
    for arm in arms:
        rows = [trial_row(t, arm, boxes, flags_total) for t in trial_dirs(arm["job"])]
        summaries.append(summarize(arm, rows))
    means = {s["label"]: s["instance_rewards"] for s in summaries}
    ks = sorted({a["n"] for a in arms if a["kind"] != "solo"})
    trends = [trend(kind, means, ks, total) for kind in CHANNEL_KINDS]
    base_ks = sorted({1, *ks}) if f"solo-{total}" in means else ks
    return {
        "total_budget": total,
        "boxes": boxes,
        "flags": flags_total,
        "arms": summaries,
        "contrasts": contrasts(means, ks, total),
        "trends": [t for t in trends if t is not None],
        "baselines": baselines(base_ks, total, boxes),
    }


def cell(value, digits: int = 2) -> str:
    if value is None:
        return "-"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def table(title: str, headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    lines = [
        title,
        "  ".join(
            h.rjust(w) if i else h.ljust(w)
            for i, (h, w) in enumerate(zip(headers, widths, strict=True))
        ),
    ]
    for r in rows:
        lines.append(
            "  ".join(
                c.rjust(w) if i else c.ljust(w)
                for i, (c, w) in enumerate(zip(r, widths, strict=True))
            )
        )
    return "\n".join(lines)


def arm_table(title: str, arms: list[dict], columns: tuple) -> str:
    headers = ["arm", *(header for header, _, _ in columns)]
    rows = [[a["label"], *(cell(a[key], digits) for _, key, digits in columns)] for a in arms]
    return table(title, headers, rows)


def render_arms(arms: list[dict]) -> list[str]:
    shown = [a | {"reward_pct": 100.0 * a["reward"]} for a in arms]
    shown = [
        a | {"se_pct": None if a["reward_se"] is None else 100.0 * a["reward_se"]} for a in shown
    ]
    outcome = (("trials", "trials", 0), ("noRwd", "no_reward", 0), ("reward%", "reward_pct", 1))
    outcome += (("se", "se_pct", 1), ("opens", "opens", 2), ("distinct", "distinct", 2))
    outcome += (("flags", "flags", 2), ("empty", "empty", 2), ("repeats", "repeats", 2))
    outcome += (("unused", "unused", 2), ("strand", "stranded", 2))
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
    return [
        arm_table(
            "ARMS: outcome (per-trial means; reward = mean of instance means)", shown, outcome
        ),
        arm_table(
            "ARMS: repeat split, channel arms (F13 primary; _box = box number)", shown, repeats
        ),
        arm_table("ARMS: channel and spread", shown, channel),
        arm_table("ARMS: recorded cost per trial", shown, cost),
    ]


def render(report: dict) -> str:
    parts = render_arms(report["arms"])
    rows = [
        [
            c["contrast"],
            c["question"],
            cell(c["instances"]),
            cell(c["diff_pts"], 1),
            cell(c["lo"], 1),
            cell(c["hi"], 1),
            f"{c['p_sign_flip']:.4f}",
        ]
        for c in report["contrasts"]
    ]
    headers = ["contrast", "question", "inst", "diffPts", "lo95", "hi95", "pSignFlip"]
    parts.append(
        table(
            "CONTRASTS paired by instance (bootstrap 10000, seed 0; exact sign-flip)", headers, rows
        )
        if rows
        else "CONTRASTS: none"
    )
    rows = [
        [
            t["gap"],
            " ".join(f"k{k}:{t['mean_gap_pts'][str(k)]:.1f}" for k in t["ks"]),
            cell(t["instances"]),
            cell(t["slope_pts_per_doubling"], 2),
            cell(t["lo"], 2),
            cell(t["hi"], 2),
        ]
        for t in report["trends"]
    ]
    headers = ["gap", "meanGapPts", "inst", "slopePerDoubling", "lo95", "hi95"]
    parts.append(
        table("TREND of the gap on log2 k", headers, rows)
        if rows
        else "TREND: fewer than 2 k values with solo-T"
    )
    rows = [
        [
            cell(b["k"]),
            cell(b["budget_per_seat"]),
            cell(b["random_distinct"]),
            cell(b["random_repeats"]),
            cell(b["identical_distinct"]),
            cell(b["identical_repeats"]),
        ]
        for b in report["baselines"]
    ]
    headers = ["k", "m", "randDistinct", "randRepeats", "identDistinct", "identRepeats"]
    parts.append(
        table(
            f"BASELINES (n={report['boxes']} boxes, total {report['total_budget']} opens)",
            headers,
            rows,
        )
    )
    return "\n\n".join(parts)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="crowd_scaling.py")
    parser.add_argument("--arm", action="append", required=True, metavar="LABEL=JOB_DIR")
    parser.add_argument("--total-budget", type=int, default=24)
    parser.add_argument("--boxes", type=int, default=60)
    parser.add_argument("--flags", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    assert 0 < args.total_budget < args.boxes and 0 < args.flags <= args.boxes
    assert 0 < len(args.arm) <= MAX_ARMS
    arms = [parse_arm(text, args.total_budget) for text in args.arm]
    assert len({a["label"] for a in arms}) == len(arms), "arm labels repeat"
    report = analyze(arms, args.boxes, args.flags, args.total_budget)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
