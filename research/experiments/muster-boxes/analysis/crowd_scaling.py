import argparse
import json
import math
import random
import re
import sys
import tomllib
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
RULE_KINDS = ("zones", "share", "chief")
RULE_PAIRS = (("zones", "share"), ("zones", "chief"), ("share", "chief"))
HOLM_FAMILY = len(RULE_KINDS)
RULE_PRIMARY_K = 8
PLANNED_INSTANCES = 10
LABEL = re.compile(r"(solo|indep|team|claim|scout|share|chief|zones)-([1-9][0-9]*)")
NUMBER = re.compile(r"(?<![A-Za-z0-9_])[0-9]+(?![A-Za-z0-9_])")
CHANNEL_KINDS = ("team", "claim", "scout", *RULE_KINDS)
E6_SETS = (("team", "primary"), ("scout", "secondary"))
FIELD_KEYS = (
    "flags_per_open",
    "shared_rooms",
    "treasure_rooms_found",
    "empty_rooms_reprobed",
    "called_empty_opens",
    "called_empty_opens_read",
    "called_treasure_opens",
    "called_treasure_opens_read",
    "first_room1_share",
)
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


def parse_arm(text: str, total: int | None, per_seat: int | None) -> dict:
    label, sep, path = text.partition("=")
    match = LABEL.fullmatch(label)
    assert sep and match and path, (
        f"--arm wants LABEL=JOB_DIR, LABEL solo|indep|team|claim|scout|share|chief|zones-N: {text}"
    )
    kind, n = match.group(1), int(match.group(2))
    assert (total is None) != (per_seat is None)
    assert kind == "solo" or n >= 2, f"{label}: a multi-seat arm needs at least 2 seats"
    if per_seat is None:
        assert kind == "solo" or total % n == 0, (
            f"{label}: total budget {total} does not divide by {n}"
        )
    job = Path(path)
    assert job.is_dir(), f"{label}: {job} is not a directory"
    shared = per_seat if per_seat is not None else total // n
    return {
        "label": label,
        "kind": kind,
        "n": n,
        "seats": 1 if kind == "solo" else n,
        "budget": n if kind == "solo" else shared,
        "job": job,
    }


def number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def toml_reference(path: str) -> dict:
    tables = tomllib.loads(Path(path).read_text()).get("reference")
    assert isinstance(tables, dict), f"{path}: no [reference] tables"
    out: dict = {}
    for condition, policies in tables.items():
        assert isinstance(policies, dict), (path, condition)
        for policy, value in policies.items():
            value = value.get("mean") if isinstance(value, dict) else value
            assert number(value), (path, condition, policy)
            out.setdefault(condition, {})[policy] = value
    return out


def load_reference(path: str | None) -> dict:
    if path is None:
        return {}
    if path.endswith(".toml"):
        return toml_reference(path)
    data = json.loads(Path(path).read_text())
    assert isinstance(data, dict), f"{path}: want {{condition: {{policy: value}}}}"
    for condition, values in data.items():
        assert isinstance(values, dict), (path, condition)
        for policy, value in values.items():
            assert number(value), (path, condition, policy)
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


def room_size(result: dict, arm: dict, seats: list[str], cfg: dict) -> int:
    task = ((result.get("config") or {}).get("task") or {}).get("path")
    path = Path(task) / "environment" / "world" / "instance.json" if task else None
    if path is None or not path.is_file():
        return cfg["room_size"]
    inst = json.loads(path.read_text())
    size = inst["room_size"]
    assert size == cfg["room_size"], (path, "--room-size", cfg["room_size"])
    treasure = inst["treasure_rooms"]
    treasure_count = len(treasure) if isinstance(treasure, list) else treasure
    assert inst["rooms"] * size == cfg["boxes"], (path, "--boxes", cfg["boxes"])
    assert treasure_count * size == cfg["flags"], (path, "--flags", cfg["flags"])
    assert inst["budget"] == arm["budget"] and inst["seats"] == seats, (path, arm["label"])
    assert inst["channel"] is (arm["kind"] in CHANNEL_KINDS), (path, arm["label"])
    return size


def called_rooms(text: str, visited: set[int], size: int) -> set[int]:
    tokens = mentioned(text)
    rooms = {t for t in tokens if t in visited}
    rooms |= {(t - 1) // size + 1 for t in tokens if t > 0 and (t - 1) // size + 1 in visited}
    return rooms


def field_measures(events: list[dict], seats: list[str], size: int) -> dict:
    openers: dict[int, set[str]] = {}
    room_opens: Counter = Counter()
    found: dict[bool, set[int]] = {True: set(), False: set()}
    visited: dict[str, set[int]] = {seat: set() for seat in seats}
    calls: dict[int, list[tuple[str, int]]] = {}
    last_read: dict[str, int] = {}
    first_room: dict[str, int] = {}
    c: Counter = Counter()
    for e in events:
        seat, kind = e["seat"], e["kind"]
        if kind == "msg" and e["method"] == "POST":
            for room in called_rooms(e["response"]["text"], visited[seat], size):
                calls.setdefault(room, []).append((seat, e["seq"]))
        elif kind == "msgs" and e["method"] == "GET":
            last_read[seat] = e["seq"]
        elif kind == "open" and e["method"] == "POST":
            box, room, flag = e["response"]["box"], e["response"]["room"], e["response"]["flag"]
            assert room == (box - 1) // size + 1, (e["seq"], box, room, size)
            first_room.setdefault(seat, room)
            openers.setdefault(room, set()).add(seat)
            room_opens[room] += 1
            found[bool(flag)].add(room)
            visited[seat].add(room)
            prior = [seq for sender, seq in calls.get(room, []) if sender != seat]
            name = "treasure" if flag else "empty"
            c[f"called_{name}_opens"] += bool(prior)
            c[f"called_{name}_opens_read"] += bool(prior) and last_read.get(seat, 0) > min(prior)
    assert not found[True] & found[False]
    return {
        "shared_rooms": sum(len(s) > 1 for s in openers.values()),
        "treasure_rooms_found": len(found[True]),
        "empty_rooms_reprobed": sum(room_opens[r] > 1 for r in found[False]),
        **{key: c[key] for key in FIELD_KEYS if key.startswith("called_")},
        "first_room1_share": sum(r == 1 for r in first_room.values()) / len(seats),
    }


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
    size = room_size(result, arm, seats, cfg)
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
    if cfg["per_seat"] is not None:
        row |= field_measures(events, seats, size)
        row["flags_per_open"] = c["flags"] / c["opens"] if c["opens"] else None
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
    if cfg["per_seat"] is not None:
        out |= {key: mean_of(scored, key) for key in FIELD_KEYS}
        oracle = (out["reference"] or {}).get("oracle")
        out["reward_over_ref"] = out["reward"] / oracle if oracle else None
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


def margin_test(
    k: int, role: str, means: dict, total: int, margin: float, kind: str
) -> dict | None:
    a, b = f"solo-{total}", f"{kind}-{k}"
    if a not in means or b not in means:
        return None
    found = contrast(a, b, f"split cost, {kind}", means)
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


def load_arms(arms: list[dict], cfg: dict) -> tuple[list[dict], dict, dict]:
    summaries, rows_by = [], {}
    for arm in arms:
        rows_by[arm["label"]] = [trial_row(t, arm, cfg) for t in trial_dirs(arm["job"])]
        summaries.append(summarize(arm, rows_by[arm["label"]], cfg))
    means = {s["label"]: s["instance_rewards"] for s in summaries}
    return summaries, rows_by, means


def arm_status(label: str, rows: list[dict], planned: int) -> dict:
    scored = [r for r in rows if r["reward"] is not None]
    instances = len({r["instance"] for r in scored})
    complete = len(scored) >= planned and instances >= PLANNED_INSTANCES
    return {
        "label": label,
        "scored": len(scored),
        "instances": instances,
        "status": "complete" if complete else "incomplete",
    }


def unavailable(a: str, b: str, question: str) -> dict:
    keys = ("instances", "diff_pts", "lo", "hi", "p_sign_flip")
    return {"contrast": f"{a} - {b}", "question": question, **dict.fromkeys(keys)}


def rule_contrast(a: str, b: str, question: str, means: dict, status: dict) -> dict | None:
    if a not in means or b not in means:
        return None
    if status[a]["status"] != "complete" or status[b]["status"] != "complete":
        return unavailable(a, b, question) | {"verdict": "not available"}
    return contrast(a, b, question, means)


def holm(found: list[dict]) -> list[dict]:
    assert len(found) <= HOLM_FAMILY, len(found)
    tested = sorted(
        (c for c in found if c["p_sign_flip"] is not None), key=lambda c: c["p_sign_flip"]
    )
    passing = True
    decided = {}
    for c in tested:
        place = next(i for i, d in enumerate(tested) if d["p_sign_flip"] == c["p_sign_flip"])
        threshold = P_PASS / (HOLM_FAMILY - place)
        passing = passing and c["p_sign_flip"] <= threshold
        verdict = resolved(c, "helps", "hurts", "not resolved") if passing else "not resolved"
        decided[c["contrast"]] = {
            "holm_threshold": threshold,
            "holm": "pass" if passing else "fail",
            "verdict": verdict,
        }
    blank = {"holm_threshold": None, "holm": None}
    return [c | decided.get(c["contrast"], blank) for c in found]


def rule_margin(kind: str, k: int, means: dict, status: dict, cfg: dict) -> dict | None:
    a, b = f"solo-{cfg['total']}", f"{kind}-{k}"
    found = rule_contrast(a, b, f"split cost, {kind}", means, status)
    extra = {"k": k, "role": "secondary", "margin_pts": 100.0 * cfg["margin"]}
    if found is None or found["p_sign_flip"] is None:
        return found if found is None else found | extra
    return margin_test(k, "secondary", means, cfg["total"], cfg["margin"], kind) | extra


def rule_pair(a: str, b: str, k: int, means: dict, status: dict) -> dict | None:
    found = rule_contrast(f"{a}-{k}", f"{b}-{k}", "between rules", means, status)
    if found is None:
        return None
    extra = {"k": k, "role": "secondary", "correction": "none"}
    if found["p_sign_flip"] is None:
        return found | extra
    return (
        found | extra | {"verdict": resolved(found, f"{a} higher", f"{b} higher", "not resolved")}
    )


def rule_block(arms: list[dict], rows_by: dict, means: dict, cfg: dict) -> dict:
    planned = cfg["planned_trials"]
    status = {label: arm_status(label, rows, planned) for label, rows in rows_by.items()}
    versus, margins, pairs = [], [], []
    for k in sorted({a["n"] for a in arms if a["kind"] == "team"}):
        role = "primary" if k == RULE_PRIMARY_K else "secondary"
        found = [
            rule_contrast(f"{r}-{k}", f"team-{k}", f"rule {r}", means, status) for r in RULE_KINDS
        ]
        versus += holm([c | {"k": k, "role": role} for c in found if c is not None])
        margins += [rule_margin(r, k, means, status, cfg) for r in RULE_KINDS]
        pairs += [rule_pair(a, b, k, means, status) for a, b in RULE_PAIRS]
    return {
        "settings": {
            "planned_trials": planned,
            "planned_instances": PLANNED_INSTANCES,
            "holm_family_size": HOLM_FAMILY,
            "alpha": P_PASS,
            "primary_k": RULE_PRIMARY_K,
            "margin_pts": 100.0 * cfg["margin"],
        },
        "arms": [status[a["label"]] for a in arms],
        "versus_team": versus,
        "margin": [m for m in margins if m is not None],
        "pairwise": [p for p in pairs if p is not None],
    }


def analyze(arms: list[dict], cfg: dict) -> dict:
    if cfg["per_seat"] is not None:
        return analyze_per_seat(arms, cfg)
    total = cfg["total"]
    summaries, rows_by, means = load_arms(arms, cfg)
    pooled = unions(arms, rows_by, cfg)
    means |= {f"union-{u['k']}": u["instance_rewards"] for u in pooled if u["instance_rewards"]}
    ks = sorted({a["n"] for a in arms if a["kind"] != "solo"})
    slopes = [slope_test(kind, role, means) for kind, role in SLOPE_KINDS]
    margins = [margin_test(k, role, means, total, cfg["margin"], "claim") for k, role in MARGIN_KS]
    base_ks = sorted({1, *ks}) if f"solo-{total}" in means else ks
    report = {
        "settings": {key: cfg[key] for key in ("total", "boxes", "flags", "cap", "margin")}
        | {"missing_as_zero": cfg["missing_as_zero"]},
        "arms": summaries,
        "test1_slope": [t for t in slopes if t is not None],
        "test2_margin": [t for t in margins if t is not None],
        "unions": pooled,
        "contrasts": contrasts(means, ks, total),
        "baselines": baselines(base_ks, total, cfg["boxes"]),
    }
    if any(a["kind"] in RULE_KINDS for a in arms):
        report["rules"] = rule_block(arms, rows_by, means, cfg)
    return report


def resolved(found: dict, above: str, below: str, neither: str) -> str:
    if found["lo"] > 0:
        return above
    return below if found["hi"] < 0 else neither


def gap_change(kind: str, m: int, lo: int, hi: int, means: dict) -> dict:
    labels = (f"solo-{m * hi}", f"{kind}-{hi}", f"solo-{m * lo}", f"{kind}-{lo}")
    shared = sorted(set.intersection(*(set(means[label]) for label in labels)))
    assert 0 < len(shared) <= MAX_INSTANCES, (labels, len(shared))
    a, b, c, d = (means[label] for label in labels)
    diffs = [100.0 * ((a[i] - b[i]) - (c[i] - d[i])) for i in shared]
    lo95, hi95 = bootstrap(len(diffs), lambda sample: fmean(diffs[i] for i in sample))
    mean, p = fmean(diffs), sign_flip(diffs)
    return {
        "contrast": f"({labels[0]} - {labels[1]}) - ({labels[2]} - {labels[3]})",
        "question": "P3 gap grows with k",
        "instances": len(shared),
        "diff_pts": mean,
        "lo": lo95,
        "hi": hi95,
        "p_sign_flip": p,
        "verdict": "pass" if mean > 0 and p < P_PASS else "fail",
    }


def e6_set(kind: str, role: str, means: dict, ks: list[int], m: int) -> list[dict]:
    out = []
    talk = [k for k in ks if f"{kind}-{k}" in means and f"indep-{k}" in means]
    for k in reversed(talk):
        found = contrast(f"{kind}-{k}", f"indep-{k}", "P1 communication", means)
        verdict = resolved(found, "resolved", "resolved", "not resolved")
        out.append(found | {"test": "P1", "set": kind, "k": k, "verdict": verdict})
    split = [k for k in ks if f"{kind}-{k}" in means and f"solo-{m * k}" in means]
    for k in reversed(split):
        found = contrast(f"solo-{m * k}", f"{kind}-{k}", "P2 one agent vs team", means)
        verdict = resolved(found, "1 agent is better", "the team is better", "not resolved")
        out.append(found | {"test": "P2", "set": kind, "k": k, "verdict": verdict})
    if len(split) >= 2:
        found = gap_change(kind, m, split[0], split[-1], means)
        out.append(found | {"test": "P3", "set": kind, "k": split[-1]})
    for row in out:
        largest = row["test"] == "P3" or row["k"] == (talk if row["test"] == "P1" else split)[-1]
        row["role"] = role if largest else f"{role}, other k"
    return out


def gaps(means: dict, ks: list[int], m: int) -> list[dict]:
    out = []
    for k in ks:
        plan = (
            (f"indep-{k}", f"solo-{m}", "parallelism"),
            (f"team-{k}", f"indep-{k}", "communication"),
            (f"scout-{k}", f"team-{k}", "rule (scout)"),
            (f"claim-{k}", f"team-{k}", "rule (claim)"),
            (f"team-{k}", f"solo-{m * k}", "equal budget"),
            (f"indep-{k}", f"union-{k}", "interference"),
        )
        out += [
            contrast(a, b, q, means) | {"k": k} for a, b, q in plan if a in means and b in means
        ]
    return out


def baselines_per_seat(ks: list[int], m: int, boxes: int, flags: int) -> list[dict]:
    assert 0 < m < boxes and 0 < flags <= boxes
    out = []
    for k in ks:
        distinct = boxes * (1.0 - (1.0 - m / boxes) ** k)
        out.append(
            {
                "k": k,
                "budget_per_seat": m,
                "total_budget": m * k,
                "random_distinct": distinct,
                "random_repeats": k * m - distinct,
                "random_flags": distinct * flags / boxes,
                "identical_distinct": m,
                "identical_repeats": k * m - m,
                "identical_flags_random_pick": m * flags / boxes,
            }
        )
    return out


def analyze_per_seat(arms: list[dict], cfg: dict) -> dict:
    m = cfg["per_seat"]
    summaries, rows_by, means = load_arms(arms, cfg)
    ks = sorted({a["n"] for a in arms if a["kind"] != "solo"})
    pool = rows_by.get(f"solo-{m}")
    pooled = [union_pool(pool, k, cfg) | {"pool": f"solo-{m}"} for k in ks] if pool else []
    means |= {f"union-{u['k']}": u["instance_rewards"] for u in pooled if u["instance_rewards"]}
    tests = [row for kind, role in E6_SETS for row in e6_set(kind, role, means, ks, m)]
    keys = ("per_seat", "boxes", "flags", "room_size", "cap", "missing_as_zero")
    return {
        "settings": {key: cfg[key] for key in keys},
        "arms": summaries,
        "e6_tests": tests,
        "gaps": gaps(means, ks, m),
        "unions": pooled,
        "baselines": baselines_per_seat(sorted({1, *ks}), m, cfg["boxes"], cfg["flags"]),
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


def missing_mode(settings: dict) -> str:
    if settings["missing_as_zero"]:
        return "counted as reward 0 in reward means and contrasts"
    return "left out of the means (unmeasured)"


def render(report: dict) -> str:
    s = report["settings"]
    if "per_seat" in s:
        return render_per_seat(report)
    mode = missing_mode(s)
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
    if "rules" in report:
        parts += render_rules(report["rules"])
    return "\n\n".join(parts)


def render_rules(block: dict) -> list[str]:
    s = block["settings"]
    planned, inst = s["planned_trials"], s["planned_instances"]
    alpha, family = s["alpha"], s["holm_family_size"]
    arm_cols = (("scored", "scored", 0), ("inst", "instances", 0), ("status", "status", 0))
    lead = (("k", "k", 0), ("role", "role", 0), ("inst", "instances", 0))
    lead += (("diffPts", "diff_pts", 1), ("lo95", "lo", 1), ("hi95", "hi", 1))
    lead += (("pSignFlip", "p_sign_flip", 4),)
    versus_cols = lead + (("holmThreshold", "holm_threshold", 4), ("holm", "holm", 0))
    versus_cols += (("verdict", "verdict", 0),)
    margin_cols = lead + (("marginPts", "margin_pts", 1), ("verdict", "verdict", 0))
    pair_cols = lead + (("correction", "correction", 0), ("verdict", "verdict", 0))
    return [
        spec_table(
            f"RULES: arms (complete: >= {planned} trials with a reward and >= {inst} instances;"
            " contrasts of an incomplete arm are not available)",
            block["arms"],
            "label",
            arm_cols,
        ),
        spec_table(
            f"RULES: rule-k - team-k, Holm step-down at each k (family size {family}, fixed even"
            f" when an arm is missing; alpha {alpha}; p <= threshold; helps/hurts need a Holm pass"
            f" and a 95% interval on one side of 0; primary at k = {s['primary_k']})",
            block["versus_team"],
            "contrast",
            versus_cols,
        ),
        spec_table(
            f"RULES: d = solo-T - rule-k, secondary (pass: hi95 < margin; fail: lo95 > margin;"
            f" margin {s['margin_pts']:.1f} pts)",
            block["margin"],
            "contrast",
            margin_cols,
        ),
        spec_table(
            "RULES: pairwise between rules at each k, secondary, no correction",
            block["pairwise"],
            "contrast",
            pair_cols,
        ),
    ]


def render_field(arms: list[dict]) -> str:
    shown = list(arms)
    cols = (("flagsPerOpen", "flags_per_open", 3), ("rewardOverRef", "reward_over_ref", 3))
    cols += (("sharedRooms", "shared_rooms", 2), ("treasureRooms", "treasure_rooms_found", 2))
    cols += (("emptyReprobed", "empty_rooms_reprobed", 2),)
    cols += (("calledEmpty", "called_empty_opens", 2), ("read", "called_empty_opens_read", 2))
    cols += (("calledTreasure", "called_treasure_opens", 2),)
    cols += (("read", "called_treasure_opens_read", 2), ("firstRoom1", "first_room1_share", 2))
    title = "ARMS: field mechanism (per-trial means; called = room named in a teammate's post)"
    return spec_table(title, shown, "label", cols)


def render_e6(report: dict) -> list[str]:
    cols = (("set", "set", 0), ("role", "role", 0), ("k", "k", 0), ("contrast", "contrast", 0))
    cols += (("inst", "instances", 0), ("diffPts", "diff_pts", 1), ("lo95", "lo", 1))
    cols += (("hi95", "hi", 1), ("pSignFlip", "p_sign_flip", 4), ("verdict", "verdict", 0))
    gap_cols = (("gap", "question", 0), ("contrast", "contrast", 0), ("inst", "instances", 0))
    gap_cols += (("diffPts", "diff_pts", 1), ("lo95", "lo", 1), ("hi95", "hi", 1))
    gap_cols += (("pSignFlip", "p_sign_flip", 4),)
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
    title = "E6 TESTS (P1 resolved: 95% interval excludes 0; P2 by interval sign; P3 pass: mean > 0, p < 0.05)"
    return [
        spec_table(title, report["e6_tests"], "test", cols),
        spec_table(
            "GAPS per k, paired by instance (MAC Bench style)", report["gaps"], "k", gap_cols
        ),
        spec_table(
            "UNION-k: disjoint groups of k solo-M trials of 1 instance", pooled, "k", union_cols
        ),
    ]


def render_per_seat(report: dict) -> str:
    s = report["settings"]
    header = f"per-seat budget {s['per_seat']} (k-seat arm total {s['per_seat']}k; solo-N total N), boxes {s['boxes']}, flags {s['flags']}, room size {s['room_size']}, turn cap {s['cap']}"
    parts = [f"{header}\nmode: trials without a reward are {missing_mode(s)}"]
    parts += render_arms(report["arms"])
    parts.append(render_field(report["arms"]))
    parts += render_e6(report)
    base_cols = (("m", "budget_per_seat", 0), ("total", "total_budget", 0))
    base_cols += (("randDistinct", "random_distinct", 2), ("randRepeats", "random_repeats", 2))
    base_cols += (("randFlags", "random_flags", 2), ("identDistinct", "identical_distinct", 0))
    base_cols += (("identRepeats", "identical_repeats", 0),)
    base_cols += (("identFlags", "identical_flags_random_pick", 2),)
    title = f"BASELINES (n={s['boxes']} boxes, {s['flags']} flags, random distinct picks per seat)"
    parts.append(spec_table(title, report["baselines"], "k", base_cols))
    return "\n\n".join(parts)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="crowd_scaling.py")
    parser.add_argument("--arm", action="append", required=True, metavar="LABEL=JOB_DIR")
    budget = parser.add_mutually_exclusive_group()
    budget.add_argument("--total-budget", type=int)
    budget.add_argument("--per-seat-budget", type=int)
    parser.add_argument("--boxes", type=int, default=60)
    parser.add_argument("--flags", type=int, default=20)
    parser.add_argument("--room-size", type=int, default=10)
    parser.add_argument("--turn-cap", type=int, default=30)
    parser.add_argument("--margin", type=float, default=0.05)
    parser.add_argument("--missing-as-zero", action="store_true")
    parser.add_argument("--reference", metavar="JSON_OR_FAMILY_TOML")
    parser.add_argument("--planned-trials", type=int, default=20)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    per_seat = args.per_seat_budget
    total = None if per_seat is not None else (args.total_budget or 24)
    assert 0 < (per_seat or total) < args.boxes and 0 < args.flags <= args.boxes
    assert args.boxes % args.room_size == 0 and args.flags % args.room_size == 0
    assert args.turn_cap > 0 and 0.0 <= args.margin < 1.0
    assert 0 < args.planned_trials <= MAX_TRIALS
    assert 0 < len(args.arm) <= MAX_ARMS
    arms = [parse_arm(text, total, per_seat) for text in args.arm]
    assert len({a["label"] for a in arms}) == len(arms), "arm labels repeat"
    assert all(a["budget"] < args.boxes for a in arms), "a seat budget must stay below --boxes"
    cfg = {
        "total": total,
        "per_seat": per_seat,
        "boxes": args.boxes,
        "flags": args.flags,
        "room_size": args.room_size,
        "cap": args.turn_cap,
        "margin": args.margin,
        "missing_as_zero": args.missing_as_zero,
        "planned_trials": args.planned_trials,
        "reference": load_reference(args.reference),
    }
    report = analyze(arms, cfg)
    print(json.dumps(report, indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
