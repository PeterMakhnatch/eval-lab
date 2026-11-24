import argparse
import json
import re
import shlex
import sys
from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from math import inf, isfinite
from pathlib import Path

from seat_codes import (
    LABEL,
    MAX_ARMS,
    MAX_EVENTS,
    MAX_SEATS,
    MAX_TRIALS,
    NUMBER,
    boxes_named,
    expand,
    is_read,
)
from step_join import MARGIN_S, MAX_STEPS, epoch, read_json, table

RULES = ("frozen", "wide")
REPEAT_CLASSES = ("no_post", "not_read", "read_same_step", "read_earlier_step", "own_repeat", "unjoined")
LOG_CLASSES = ("no_post", "not_read", "read", "own_repeat")
PROTOCOL_CLASSES = ("earlier_step", "same_step", "missing", "unjoined")
SEPARATOR = r"\s*(?:,|/|and|&|\+|or)\s*"
BOXES = rf"{NUMBER}(?:{SEPARATOR}{NUMBER})*"
BOX_LABEL = r'box(?:es)?"?\s*[#=:]?\s*'
EXPLICIT_BOX = re.compile(rf"\b{BOX_LABEL}({BOXES})(?!\d)", re.I)
RESULT_PAIR = re.compile(r"(?<![\w.])(\d+)\s*[:=]\s*(?:flag|true|false|empty|no flag|treasure)\b", re.I)
CLAIM_HEAD = (
    r"(?:claim(?:s|ing)?|open(?:s|ing)?|(?:will|going to)\s+(?:open|probe|test|take)|"
    r"plan(?:s|ning)?(?:\s+to\s+open)?|intend(?:s)?(?:\s+to\s+open)?|"
    r"propos(?:e|es|ing)(?:\s+opening)?|tak(?:e|es|ing)|prob(?:e|es|ing)|test(?:s|ing)?)"
)
CLAIM = re.compile(
    rf"(?:^|[.;!\n])\s*(?:(p[1-9]\d*|I)\b\s*:?\s*)?"
    rf"(?:(?:now|next|final|revised)\s+)*{CLAIM_HEAD}\s*:?\s*"
    rf"(?:unopened\s+)?(?:{BOX_LABEL})?({BOXES})(?!\d|\s+(?:opens?|rooms?|more)\b)",
    re.I,
)
BARE_CLAIM = re.compile(rf"\s*(?:(p[1-9]\d*)\s*:\s*)?(?:{BOX_LABEL})?({BOXES})\s*[.!]?\s*", re.I)
ASSIGNMENT = re.compile(
    rf"\bp([1-9]\d*)\b(?:\s*(?:=|:|->|→)\s*|\s+(?=please\b|open|box|take|probe))"
    rf"(?:please\s+)?(?:(?:you\s+)?(?:must\s+)?(?:opens?|take|probe)\s+)?"
    rf"(?:exactly\s+)?\[?\s*(?:{BOX_LABEL})?({BOXES})"
    rf"(?!\d|\s*(?:opens?|more|left|agents?)\b)",
    re.I,
)
CHIEF_JSON = re.compile(r'\{\s*"list"\s*:')
METHOD = (
    "OBSERVED: Successful world opens, reads and posts are counted in seq order. A repeat is any "
    "open after the first successful open of its box. Own repeat takes precedence if the opener "
    "already opened it. Posts by the opener do not count for the board classes. Reads count only "
    "returned message IDs. The frozen rule imports seat_codes.boxes_named unchanged. The wide "
    "rule adds explicit box/boxes lists without the frozen trailing-word restriction and bare "
    "integers before : or = followed by flag, true, false, empty, no flag or treasure. "
    "Both use in-range boxes and the frozen list separators and bounded range expansion."
)
CLOCK = (
    "OBSERVED: An authenticated non-admin event belongs to its seat's agent step (previous "
    "timestamp, end timestamp] at offset zero. Add an offset in [lo, hi) to every step timestamp "
    "to keep the same windows. Refuse the whole trial if a seat has no valid window or an event "
    "is less than 0.5 s from a boundary. World-only classes include rejected trials; they do not "
    "recover timing. Null interval endpoints are unbounded; a null interval means no valid join."
)
PROTOCOL = (
    "OBSERVED: Claim timing uses the first recognized own claim before the open. List timing "
    "uses the first recognized p1 assignment before the open, on the opener's clock. A worker's "
    "first read returning a matching list is timed separately. A prior list is not proof of "
    "receipt or of compliance with the latest list. For cross-seat posts, offset_sensitive "
    "marks a post less than 0.5 s from the start of the open step. Per-seat step metrics require "
    "a valid seat join; a different seat can still cause trial refusal."
)


@dataclass(frozen=True)
class Step:
    id: int
    source: str
    timestamp: str
    time: float


@dataclass
class SeatJoin:
    path: Path
    steps: list[Step] = field(default_factory=list)
    positions: dict[int, int] = field(default_factory=dict)
    issues: list[dict] = field(default_factory=list)
    lo: float = -inf
    hi: float = inf
    has_windows: bool = True


@dataclass
class Board:
    named: dict = field(default_factory=dict)
    read: dict = field(default_factory=dict)

    def post(self, boxes: set[int], event: dict) -> None:
        for box in boxes:
            self.named.setdefault(box, {}).setdefault(event["seat"], event)

    def see(self, boxes: set[int], event: dict, post: dict) -> None:
        for box in boxes:
            self.read.setdefault((event["seat"], box), (event, post))

    def evidence(self, seat: str, box: int) -> tuple[str, dict | None, dict | None]:
        seen = self.read.get((seat, box))
        if seen is not None:
            return "read", seen[1], seen[0]
        post = next((p for s, p in self.named.get(box, {}).items() if s != seat), None)
        return ("not_read" if post else "no_post"), post, None


@dataclass
class TrialReplay:
    joins: dict[str, SeatJoin]
    limit: int
    protocol: str | None
    joined: bool
    boards: tuple[Board, Board] = field(default_factory=lambda: (Board(), Board()))
    posts: dict = field(default_factory=dict)
    first_opens: dict = field(default_factory=dict)
    own_opens: dict = field(default_factory=dict)
    plans: dict = field(default_factory=dict)
    list_reads: dict = field(default_factory=dict)


def listed_boxes(text: str, limit: int) -> set[int]:
    return {b for token in re.split(SEPARATOR, text) for b in expand(token) if 1 <= b <= limit}


def wide_boxes(text: str, limit: int, frozen: set[int]) -> set[int]:
    assert limit > 0 and len(text) <= 2000
    named = {b for m in EXPLICIT_BOX.finditer(text) for b in listed_boxes(m[1], limit)}
    paired = {int(m[1]) for m in RESULT_PAIR.finditer(text)}
    return frozen | named | {b for b in paired if 1 <= b <= limit}


def own_claims(text: str, seat: str, limit: int) -> set[tuple[str, int]]:
    bare = BARE_CLAIM.fullmatch(text)
    matches = [bare] if bare else list(CLAIM.finditer(text))
    return {
        (seat, b)
        for m in matches
        if not m[1] or m[1].lower() in (seat, "i")
        for b in listed_boxes(m[2], limit)
    }


def chief_lists(text: str, seats: dict[str, SeatJoin], limit: int, structured: bool) -> set[tuple[str, int]]:
    if not structured:
        return {
            (seat, b)
            for m in ASSIGNMENT.finditer(text)
            if (seat := f"p{m[1]}") in seats
            for b in listed_boxes(m[2], limit)
        }
    pairs = set()
    for match in CHIEF_JSON.finditer(text):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if not isinstance(obj["list"], dict):
            continue
        for seat, boxes in obj["list"].items():
            if seat in seats and isinstance(boxes, list):
                pairs.update((seat, b) for b in boxes if type(b) is int and 1 <= b <= limit)
    return pairs


def trial_protocol(task: Path, arm: str, seats: list[str]) -> str | None:
    coordinator = task / "instructions" / "p1.md"
    if arm == "claim":
        for seat in seats:
            text = (task / "instructions" / f"{seat}.md").read_text()
            assert "Before you open a box, post its number with POST /msg" in text, task
        return "claim"
    if arm == "chief":
        assert "p1" in seats and len(seats) > 1, task
        text = coordinator.read_text()
        assert "coordinator" in text and "list" in text, task
        assert all(
            "p1 is the coordinator" in (task / "instructions" / f"{s}.md").read_text()
            for s in seats if s != "p1"
        ), task
        return "chief_json" if '"list":' in text else "chief_text"
    return None


def world_events(trial: Path, seats: list[str]) -> tuple[list[dict], Counter]:
    events = []
    skipped: Counter = Counter()
    closed = False
    with (trial / "verifier" / "events.jsonl").open() as stream:
        for n, line in enumerate(filter(str.strip, stream), start=1):
            assert n <= MAX_EVENTS, trial
            event = json.loads(line)
            assert event["seq"] == n and isfinite(event["t"]), (trial, n)
            assert not closed, (trial, n)
            if event["kind"].startswith("admin."):
                skipped["admin"] += 1
                closed = event["kind"] == "admin.close" and event["ok"]
            elif event["seat"] is None:
                assert not event["ok"], (trial, n)
                skipped["no_seat"] += 1
            elif event["seat"] not in seats:
                skipped["other_seat"] += 1
                assert not (event["ok"] and event["kind"] in ("open", "msg")), (trial, n)
            else:
                events.append(event)
    return events, skipped


def seat_join(path: Path, events: list[dict]) -> SeatJoin:
    joined = SeatJoin(path)
    if not path.is_file():
        joined.issues.append({"kind": "missing_trajectory"})
        joined.has_windows = False
        return joined
    steps = read_json(path)["steps"]
    assert len(steps) <= MAX_STEPS, path
    joined.steps = [Step(s["step_id"], s["source"], s["timestamp"], epoch(s["timestamp"])) for s in steps]
    times = [s.time for s in joined.steps]
    if any(b <= a for a, b in zip(times, times[1:], strict=False)):
        joined.issues.append({"kind": "nonincreasing_timestamps"})
        joined.has_windows = False
        return joined
    for e in events:
        t, seq = e["t"], e["seq"]
        i = bisect_left(times, t)
        issue = {"seq": seq, "event_kind": e["kind"], "t": t}
        if not 0 < i < len(times):
            joined.issues.append(issue | {"kind": "no_window"})
            joined.has_windows = False
            continue
        step = joined.steps[i]
        joined.lo, joined.hi = max(joined.lo, t - times[i]), min(joined.hi, t - times[i - 1])
        if step.source != "agent":
            joined.issues.append(issue | {"kind": "non_agent_step", "step": step.id, "source": step.source})
            joined.has_windows = False
            continue
        joined.positions[seq] = i
        distance = min(times[i] - t, t - times[i - 1])
        if distance < MARGIN_S:
            joined.issues.append(issue | {"kind": "boundary", "step": step.id, "distance_s": distance})
    if not joined.issues:
        assert len(joined.positions) == len(events) and joined.lo <= -MARGIN_S <= MARGIN_S <= joined.hi
    return joined


def offset_interval(joins: list[SeatJoin]) -> list[float | None] | None:
    if not all(j.has_windows for j in joins):
        return None
    lo = max(j.lo for j in joins)
    hi = min(j.hi for j in joins)
    assert lo <= 0 < hi
    return [lo if isfinite(lo) else None, hi if isfinite(hi) else None]


def seat_counts(joined: SeatJoin, events: list[dict]) -> dict:
    reads, posts, opens = 0, 0, 0
    read_steps, mixed_steps = set(), set()
    step_opens: Counter = Counter()
    valid = not joined.issues
    for e in events:
        step = joined.positions[e["seq"]] if valid else None
        if is_read(e):
            reads += 1
            if valid:
                read_steps.add(step)
        elif e["kind"] == "msg" and e["ok"]:
            posts += 1
        elif e["kind"] == "open" and e["ok"]:
            opens += 1
            if valid:
                step_opens[step] += 1
                if step in read_steps:
                    mixed_steps.add(step)
    assert len(mixed_steps) <= min(reads, opens)
    return {
        "trajectory": str(joined.path),
        "events": len(events),
        "steps": sum(s.source == "agent" for s in joined.steps) if joined.path.is_file() else None,
        "trajectory_steps": len(joined.steps) if joined.path.is_file() else None,
        "reads": reads,
        "posts": posts,
        "opens": opens,
        "read_then_open_steps": len(mixed_steps) if valid else None,
        "max_opens_in_step": max(step_opens.values(), default=0) if valid else None,
        "offset_interval_s": offset_interval([joined]),
        "issues": joined.issues,
    }


def remember_post(replay: TrialReplay, e: dict) -> None:
    response = e["response"]
    number, seat, text = response["id"], e["seat"], response["text"]
    assert number not in replay.posts and response["from"] == seat, e["seq"]
    frozen = boxes_named(text, replay.limit)
    names = (frozen, wide_boxes(text, replay.limit, frozen))
    pairs = set()
    if replay.protocol == "claim":
        pairs = own_claims(text, seat, replay.limit)
    elif replay.protocol in ("chief_text", "chief_json") and seat == "p1":
        pairs = chief_lists(text, replay.joins, replay.limit, replay.protocol == "chief_json")
    for pair in pairs:
        replay.plans.setdefault(pair, e)
    replay.posts[number] = (e, names, pairs)
    for board, named in zip(replay.boards, names, strict=True):
        board.post(named, e)


def remember_read(replay: TrialReplay, e: dict) -> None:
    messages = e["response"]["messages"]
    assert len(messages) <= MAX_EVENTS
    seat = e["seat"]
    for msg in messages:
        post, names, pairs = replay.posts[msg["id"]]
        assert post["seq"] < e["seq"], (post["seq"], e["seq"])
        if post["seat"] != seat:
            for board, named in zip(replay.boards, names, strict=True):
                board.see(named, e, post)
        if replay.protocol in ("chief_text", "chief_json"):
            for pair in pairs:
                if pair[0] == seat:
                    replay.list_reads.setdefault(pair, (e, post))


def event_ref(replay: TrialReplay, e: dict | None) -> dict | None:
    if e is None:
        return None
    ref = {k: e[k] for k in ("seq", "t", "seat", "kind")}
    if e["kind"] == "msg":
        ref |= {"message_id": e["response"]["id"], "text": e["response"]["text"]}
    ref["step"] = None
    if replay.joined:
        joined = replay.joins[e["seat"]]
        i = joined.positions[e["seq"]]
        ref["step"] = joined.steps[i].id
        ref["steps_around"] = [
            [step.id, step.source, step.timestamp] for step in joined.steps[i - 1 : i + 2]
        ]
    return ref


def timing(replay: TrialReplay, e: dict, prior: dict | None) -> str:
    if prior is None:
        return "missing"
    assert prior["seq"] < e["seq"]
    if not replay.joined:
        return "unjoined"
    joined = replay.joins[e["seat"]]
    pos = joined.positions[e["seq"]]
    if prior["seat"] == e["seat"]:
        prior_pos = joined.positions[prior["seq"]]
        assert prior_pos <= pos, (prior, e)
        earlier = prior_pos < pos
    else:
        assert prior["t"] <= e["t"], (prior["seq"], e["seq"])
        earlier = prior["t"] <= joined.steps[pos - 1].time
    return "earlier_step" if earlier else "same_step"


def open_protocol(replay: TrialReplay, e: dict, box: int) -> dict | None:
    if replay.protocol is None:
        return None
    pair = (e["seat"], box)
    planned = replay.plans.get(pair)
    seen, seen_post = replay.list_reads.get(pair, (None, None))
    out = {"class": timing(replay, e, planned), "post": event_ref(replay, planned)}
    if replay.protocol != "claim" and e["seat"] != "p1":
        sensitive = None
        if replay.joined and planned is not None:
            joined = replay.joins[e["seat"]]
            start = joined.steps[joined.positions[e["seq"]] - 1].time
            sensitive = abs(planned["t"] - start) < MARGIN_S
        out |= {
            "offset_sensitive": sensitive,
            "returned_class": timing(replay, e, seen),
            "read": event_ref(replay, seen),
            "returned_post": event_ref(replay, seen_post),
        }
    return out


def repeat_labels(replay: TrialReplay, e: dict, first: dict, own: bool, box: int) -> tuple[dict, str]:
    classes = {}
    legacy = None
    for rule, board in zip(RULES, replay.boards, strict=True):
        world, post, read = board.evidence(e["seat"], box)
        if rule == "frozen":
            legacy = "own_repeat" if first["seat"] == e["seat"] else world
        world = "own_repeat" if own else world
        timed = f"read_{timing(replay, e, read)}" if replay.joined and world == "read" else world
        if not replay.joined:
            timed = "unjoined"
        assert timed in REPEAT_CLASSES and world in LOG_CLASSES
        classes[rule] = {"class": timed, "world_class": world, "post": event_ref(replay, post), "read": event_ref(replay, read)}
    assert legacy in LOG_CLASSES
    return classes, legacy


def replay_opens(replay: TrialReplay, events: list[dict]) -> list[dict]:
    opens = []
    for e in events:
        if not e["ok"]:
            continue
        if is_read(e):
            remember_read(replay, e)
        elif e["kind"] == "msg":
            remember_post(replay, e)
        elif e["kind"] == "open":
            box, seat = e["request"]["box"], e["seat"]
            assert type(box) is int and 1 <= box <= replay.limit, e["seq"]
            first = replay.first_opens.setdefault(box, e)
            own = replay.own_opens.setdefault((seat, box), e)
            repeat = first is not e
            row = {
                "type": "repeat" if repeat else "first", "box": box, "open": event_ref(replay, e),
                "protocol": open_protocol(replay, e, box),
            }
            if repeat:
                rules, legacy = repeat_labels(replay, e, first, own is not e, box)
                row |= {
                    "first": event_ref(replay, first), "previous_own": event_ref(replay, own if own is not e else None),
                    "rules": rules, "legacy_frozen_class": legacy,
                }
            opens.append(row)
    return opens


def join_trial(trial: Path, arm: str, listed: bool) -> dict:
    result = read_json(trial / "result.json")
    task = Path(result["config"]["task"]["path"])
    instance = read_json(task / "environment" / "world" / "instance.json")
    real = {s["seat"] for s in result["seat_results"] if s["under_test"]}
    seats = [s for s in instance["seats"] if s in real]
    assert 0 < len(seats) == len(real) <= MAX_SEATS, trial
    limit = instance["rooms"] * instance["room_size"]
    assert limit > 0, trial
    protocol = trial_protocol(task, arm, seats)
    events, skipped = world_events(trial, seats)
    grouped = {s: [] for s in seats}
    for e in events:
        grouped[e["seat"]].append(e)
    joins = {s: seat_join(trial / "seats" / s / "agent/trajectory.json", grouped[s]) for s in seats}
    joined = not any(j.issues for j in joins.values())
    replay = TrialReplay(joins, limit, protocol, joined)
    opens = replay_opens(replay, events)
    metrics = {s: seat_counts(j, grouped[s]) for s, j in joins.items()}
    assert sum(m["opens"] for m in metrics.values()) == len(opens), trial
    row = {
        "path": str(trial), "task": str(task), "team_size": len(seats), "class": "joined" if joined else "unjoined",
        "offset_interval_s": offset_interval(list(joins.values())), "seats": metrics, "skipped_events": dict(skipped),
        "protocol": protocol, "opens": opens,
    }
    if listed:
        row["events"] = [
            [e[k] for k in ("seq", "t", "seat", "kind", "method", "status", "ok")]
            + [joins[e["seat"]].steps[joins[e["seat"]].positions[e["seq"]]].id if joined else None]
            for e in events
        ]
    return row


def counts(keys: tuple[str, ...], values) -> dict[str, int]:
    found = Counter(values)
    assert found.keys() <= set(keys), found
    return {k: found[k] for k in keys}


def summarize(trials: list[dict]) -> dict:
    opens = [o for t in trials for o in t["opens"]]
    repeats = [o for o in opens if o["type"] == "repeat"]
    joined = sum(t["class"] == "joined" for t in trials)
    protocols = [o["protocol"] for o in opens if o["protocol"] is not None]
    returned = [p["returned_class"] for p in protocols if "returned_class" in p]
    issues = [
        issue for t in trials for seat in t["seats"].values() for issue in seat["issues"]
    ]
    return {
        "trials": len(trials), "joined": joined, "unjoined": len(trials) - joined,
        "seats": sum(t["team_size"] for t in trials), "opens": len(opens), "first": len(opens) - len(repeats),
        "repeats": len(repeats),
        "split": {r: counts(REPEAT_CLASSES, (o["rules"][r]["class"] for o in repeats)) for r in RULES},
        "world_only": {r: counts(LOG_CLASSES, (o["rules"][r]["world_class"] for o in repeats)) for r in RULES},
        "legacy_frozen": counts(LOG_CLASSES, (o["legacy_frozen_class"] for o in repeats)),
        "protocol": counts(PROTOCOL_CLASSES, (p["class"] for p in protocols)),
        "list_returned": counts(PROTOCOL_CLASSES, returned),
        "issues": dict(sorted(Counter(i["kind"] for i in issues).items())),
        "boundary_event_kinds": dict(sorted(Counter(i["event_kind"] for i in issues if i["kind"] == "boundary").items())),
    }


def load_arm(arg: str, listed: bool) -> dict:
    label, sep, directory = arg.partition("=")
    match = LABEL.fullmatch(label.rsplit("/", 1)[-1])
    assert sep and directory and match, arg
    root = Path(directory).expanduser().resolve()
    paths = sorted(p for p in root.iterdir() if "__" in p.name and (p / "result.json").is_file())
    assert 0 < len(paths) <= MAX_TRIALS, root
    trials = [join_trial(p, match[1], listed) for p in paths]
    sizes = {t["team_size"] for t in trials}
    assert len(sizes) == 1, (root, sizes)
    return {"arm": label, "path": str(root), "team_size": sizes.pop(), "summary": summarize(trials), "trials": trials}


def shares(items: list[tuple[str, dict]], field: str, keys: tuple[str, ...], rule: str | None = None) -> list[list[str]]:
    rows = []
    for name, summary in items:
        found = summary[field][rule] if rule else summary[field]
        total = sum(found.values())
        if rule or total:
            rows.append([name, *(f"{found[k]}/{total}" for k in keys)])
    return rows


def report_tables(arms: list[dict], sizes: dict) -> list[str]:
    items = [(a["arm"], a["summary"]) for a in arms] + [(f"n={n}", s) for n, s in sizes.items()]
    rows = [
        [name, str(s["seats"]), f"{s['unjoined']}/{s['trials']}", str(s["first"]), f"{s['repeats']}/{s['opens']}"]
        for name, s in items
    ]
    out = [table("All trials", ["arm/size", "seat trials", "unjoined/trials", "first opens", "repeats/opens"], rows)]
    for rule in RULES:
        out.append(table(f"{rule}: step classes / all repeats", ["arm/size", *REPEAT_CLASSES], shares(items, "split", REPEAT_CLASSES, rule)))
        out.append(table(f"{rule}: world-only classes / all repeats", ["arm/size", *LOG_CLASSES], shares(items, "world_only", LOG_CLASSES, rule)))
    for title, key in (("Own claim or coordinator list posted", "protocol"), ("Coordinator list returned to worker", "list_returned")):
        rows = shares(items, key, PROTOCOL_CLASSES)
        if rows:
            out.append(table(title + " / applicable opens", ["arm/size", *PROTOCOL_CLASSES], rows))
    return out


def interval_text(interval: list[float | None] | None) -> str:
    if interval is None:
        return "no interval"
    lo, hi = interval
    return f"[{lo if lo is not None else -inf:.6f}, {hi if hi is not None else inf:.6f}) s"


def render_trial(t: dict, listed: bool) -> list[str]:
    out = [f"{t['path']} {t['class']} {interval_text(t['offset_interval_s'])}"]
    for name, s in t["seats"].items():
        for issue in s["issues"]:
            out.append(f"  {name} {json.dumps(issue, sort_keys=True)}")
        if listed:
            keys = ("steps", "reads", "posts", "opens", "read_then_open_steps", "max_opens_in_step")
            out.append(f"  {name} " + " ".join(f"{k}={s[k]}" for k in keys))
    if listed:
        for row in t["opens"]:
            if row["type"] == "repeat":
                out.append("  repeat " + json.dumps(row, ensure_ascii=True, sort_keys=True))
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="team_join.py")
    parser.add_argument("--arm", action="append", required=True, metavar="LABEL=DIR")
    parser.add_argument("--json", type=Path, metavar="NEW_PATH")
    parser.add_argument("--trials", action="store_true", help="list seat counts and repeat evidence")
    args = parser.parse_args(argv)
    assert 0 < len(args.arm) <= MAX_ARMS
    labels = [a.partition("=")[0] for a in args.arm]
    assert len(set(labels)) == len(labels), labels
    arms = [load_arm(arg, args.trials) for arg in args.arm]
    groups = defaultdict(list)
    for arm in arms:
        groups[arm["team_size"]].extend(arm["trials"])
    sizes = {n: summarize(groups[n]) for n in sorted(groups)}
    command = shlex.join(["env", "PYTHONDONTWRITEBYTECODE=1", sys.executable, sys.argv[0], *argv])
    print("OBSERVED: command:", command)
    print(METHOD, CLOCK, PROTOCOL, sep="\n\n")
    print("\n".join(["", *report_tables(arms, sizes), "\nPer-trial clock offsets:"]))
    for arm in arms:
        for t in arm["trials"]:
            print("\n".join(render_trial(t, args.trials)))
    if args.json is not None:
        with args.json.expanduser().open("x") as stream:
            report = {
                "command": command, "method": [METHOD, CLOCK, PROTOCOL], "arms": arms, "team_sizes": sizes,
                "events_columns": ["seq", "t", "seat", "kind", "method", "status", "ok", "step"],
            }
            json.dump(report, stream, allow_nan=False, separators=(",", ":"))
            stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
