import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

MAX_TRIALS = 2000
MAX_STEPS = 100_000
MAX_EVENTS = 100_000
MARGIN_S = 0.5
CELL = re.compile(r"([a-z]+)-([trcn])")
INSTANCE = re.compile(r".*-(i[0-9]{2})")
REQUEST = re.compile(r"(?:\$\{?WORLD\}?|world:8080)/(msgs|new|msg|open|rules|mine)\b")
SCRIPTED = re.compile(r"\b(for|while|until|do|xargs|seq|python3?|node|bash|sh)\b|\$\(")
SLEEP = re.compile(r"\bsleep\s+([0-9.]+)")
READ_KINDS = ("msgs", "new")
STIMULUS_TAG = {"stale": "announces", "badlead": "list", "wait": "reports", "deadclaim": "releases"}
TRAJECTORIES = ("seats/{seat}/agent/trajectory.json", "agent/trajectory.json")
CLOCK_RULE = (
    "clock: the world log (field t, world clock) and the trajectory (step timestamp, agent host"
    " clock) are joined with offset 0. A terminus-2 step timestamp is written after the commands"
    " of that step ran, so an agent step owns the events in (timestamp of the step before,"
    " its own timestamp]. For each trial the script prints the interval [lo, hi] of clock"
    f" offsets that keep every event in its step; a trial is unjoined when an event fits no"
    f" step or when the interval does not hold [-{MARGIN_S}, +{MARGIN_S}] s (an event lies"
    f" within {MARGIN_S} s of a step boundary, so it could fit 2 steps). first_request_lag_s ="
    " time of the first agent request minus the timestamp of the first step whose command text"
    " holds a request."
)
LEGEND = (
    "columns: withOpen = joined trials with 1 or more opens; read1Batch1 = 1 read in steps before"
    " the first open step and all opens in 1 step; failing = probe_pass 0 (joined); failRead1 ="
    " failing with 1 read before the first open step; failReadStep = failing with a step that"
    " holds a read and no open between the first and the last open step; failPoll2 = 2 such"
    " steps in a row; failWaitPre = failing with 3 or more read steps or a sleep of 1 s or more"
    " before the first open step; failSleep = sleep of 1 s or more in the command text of the"
    " steps after the"
    " first open step up to the last open step; batch>=2 = a step with 2 or more opens;"
    " readOpenSame = a step with a read and a later open; stim = the trap post was found (the"
    " first post of the probe tag that the world fired after the first agent request: stale"
    " announces, badlead list, wait reports on X or Y, deadclaim releases; tag from"
    " instance.json); seen = a read returned it; openAfterSeen = an open in a step after the"
    " seen step; openAfterLogSeen = an open after the first read that returned it, by log order"
    " only; textDisagree = a step whose request kinds in the command text (no loop or script)"
    " differ from the logged requests"
)


def read_json(path: Path) -> dict:
    data = json.loads(path.read_text())
    assert isinstance(data, dict), path
    return data


def epoch(text: str) -> float:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def trial_dirs(roots: list[Path]) -> list[Path]:
    found = []
    for root in roots:
        assert root.is_dir(), root
        for cell in sorted(p for p in root.iterdir() if p.is_dir() and CELL.fullmatch(p.name)):
            found += sorted(t for t in cell.iterdir() if (t / "result.json").is_file())
    assert 0 < len(found) <= MAX_TRIALS, len(found)
    return found


def real_seat(result: dict, instance: dict) -> str:
    tested = [s["seat"] for s in result.get("seat_results") or [] if s.get("under_test")]
    assert len(tested) == 1, tested
    assert instance.get("real", tested[0]) == tested[0], (tested, instance.get("real"))
    return tested[0]


def load_steps(trial: Path, seat: str) -> tuple[list[dict], str]:
    for pattern in TRAJECTORIES:
        path = trial / pattern.format(seat=seat)
        if path.is_file():
            steps = read_json(path).get("steps")
            assert isinstance(steps, list) and len(steps) <= MAX_STEPS, path
            return steps, str(path.relative_to(trial))
    raise AssertionError(f"{trial}: no trajectory")


def step_rows(steps: list[dict]) -> list[dict]:
    rows = []
    for position, step in enumerate(steps):
        calls = step.get("tool_calls") or []
        text = "".join((c.get("arguments") or {}).get("keystrokes", "") for c in calls)
        rows.append(
            {
                "step": str(step.get("step_id", position + 1)),
                "source": step.get("source"),
                "time": epoch(step["timestamp"]),
                "text": text,
                "text_requests": REQUEST.findall(text),
                "text_scripted": bool(SCRIPTED.search(text)),
                "sleep_s": sum(float(x) for x in SLEEP.findall(text) if x.count(".") <= 1),
                "events": [],
            }
        )
    return rows


def agent_events(trial: Path, seat: str) -> tuple[list[dict], list[dict]]:
    lines = (trial / "verifier" / "events.jsonl").read_text().splitlines()
    assert len(lines) <= MAX_EVENTS, trial
    every = []
    for number, line in enumerate((x for x in lines if x.strip()), start=1):
        event = json.loads(line)
        assert event.get("seq") == number, (trial, number)
        if event["kind"].startswith("admin.") and event["ok"]:
            break
        every.append(event)
    mine = [e for e in every if e["seat"] in (seat, None) and not e["kind"].startswith("admin.")]
    return every, mine


def assign(rows: list[dict], events: list[dict]) -> tuple[str | None, float, float]:
    times = [r["time"] for r in rows]
    if any(b <= a for a, b in zip(times, times[1:], strict=False)):
        return "step timestamps not increasing", 0.0, 0.0
    lo, hi = -float("inf"), float("inf")
    for event in events:
        t = float(event["t"])
        place = next((p for p in range(1, len(rows)) if times[p - 1] < t <= times[p]), None)
        if place is None:
            return f"event seq {event['seq']} fits no step window", 0.0, 0.0
        if rows[place]["source"] != "agent":
            return f"event seq {event['seq']} falls in a {rows[place]['source']} step", 0.0, 0.0
        rows[place]["events"].append(event)
        if t - times[place] > -MARGIN_S or t - times[place - 1] < MARGIN_S:
            near = min(times[place] - t, t - times[place - 1])
            reason = f"event seq {event['seq']} lies {near:.3f} s from a step boundary"
            return f"{reason} (margin {MARGIN_S} s, could fit 2 steps)", lo, hi
        lo, hi = max(lo, t - times[place]), min(hi, t - times[place - 1])
    return None, lo, hi


def text_check(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        if r["source"] != "agent" or r["text_scripted"]:
            continue
        sent = [e["kind"] for e in r["events"]]
        if sent != r["text_requests"]:
            out.append({"step": r["step"], "text": r["text_requests"], "log": sent})
    return out


def first_lag(rows: list[dict], events: list[dict]) -> float | None:
    holder = next((r for r in rows if r["text_requests"]), None)
    if holder is None or not events:
        return None
    return float(events[0]["t"]) - holder["time"]


def stimulus(every: list[dict], mine: list[dict], instance: dict, seat: str) -> dict | None:
    tag = STIMULUS_TAG.get(instance.get("probe"))
    if tag is None or not mine:
        return None
    tags = {a["post"]: a["tag"] for s in instance["script"] for a in s["actions"] if "post" in a}
    size = instance["room_size"]
    rooms = {instance["roles"].get("X"), instance["roles"].get("Y")}
    start = mine[0]["seq"]
    for e in every:
        if e["seq"] <= start or e["seat"] == seat or e["kind"] != "msg" or not e["ok"]:
            continue
        found = tags.get(e["request"]["text"], {})
        if tag not in found:
            continue
        if tag == "reports" and not any((b - 1) // size + 1 in rooms for b, _ in found[tag]):
            continue
        return {
            "seq": e["seq"],
            "t": e["t"],
            "seat": e["seat"],
            "tag": tag,
            "id": e["response"]["id"],
        }
    return None


def is_read(e: dict, seat: str) -> bool:
    return e["seat"] == seat and e["ok"] and e["kind"] in READ_KINDS and e["method"] == "GET"


def is_open(e: dict, seat: str) -> bool:
    return e["seat"] == seat and e["ok"] and e["kind"] == "open"


def shows(e: dict, message_id: int) -> bool:
    found = (e.get("response") or {}).get("messages") or []
    return any(isinstance(m, dict) and m.get("id") == message_id for m in found)


def between_opens(rows: list[dict], seat: str, per_step: list[int]) -> dict:
    marked = [n for n, count in enumerate(per_step) if count]
    if not marked:
        return {"poll_run_between_opens": 0, "sleep_s_between_opens": 0.0}
    run = best = 0
    for r in rows[marked[0] + 1 : marked[-1]]:
        reads = any(is_read(e, seat) for e in r["events"])
        opens = any(is_open(e, seat) for e in r["events"])
        run = run + 1 if reads and not opens else 0
        best = max(best, run)
    sleep = sum(r["sleep_s"] for r in rows[marked[0] + 1 : marked[-1] + 1])
    return {"poll_run_between_opens": best, "sleep_s_between_opens": sleep}


def open_keys(rows: list[dict], seat: str) -> dict:
    flat = [(n, e) for n, r in enumerate(rows) for e in r["events"]]
    opens = [(n, e) for n, e in flat if is_open(e, seat)]
    reads = [(n, e) for n, e in flat if is_read(e, seat)]
    usable, logged = [], []
    for k, (n, e) in enumerate(opens):
        prev_n, prev_seq = (opens[k - 1][0], opens[k - 1][1]["seq"]) if k else (-1, 0)
        usable.append(any(prev_n < rn < n for rn, _ in reads))
        logged.append(any(prev_seq < r["seq"] < e["seq"] for _, r in reads))
    first = opens[0][0] if opens else None
    per_step = [sum(1 for e in r["events"] if is_open(e, seat)) for r in rows]
    same = 0
    for r in rows:
        seqs = [e["seq"] for e in r["events"] if is_read(e, seat)]
        same += bool(seqs) and any(is_open(e, seat) and e["seq"] > min(seqs) for e in r["events"])
    before = (
        [r for r in rows[:first] if any(is_read(e, seat) for e in r["events"])] if opens else []
    )
    return {
        "opens": len(opens),
        "reads": len(reads),
        "reads_before_first_open_step": None
        if first is None
        else sum(1 for n, _ in reads if n < first),
        "read_steps_before_first_open": None if first is None else len(before),
        "sleep_s_before_first_open": None
        if first is None
        else sum(r["sleep_s"] for r in rows[:first]),
        "usable_read_for_open": usable,
        "log_read_for_open": logged,
        "batch_opens_max": max(per_step, default=0),
        "open_steps": sum(1 for x in per_step if x),
        "read_and_open_same_step": same,
        **between_opens(rows, seat, per_step),
    }


def sight_keys(rows: list[dict], seat: str, stim: dict | None) -> dict:
    if stim is None:
        return {
            "stimulus": None,
            "stimulus_seen_step": None,
            "opens_after_usable_sight": None,
            "opens_after_log_sight": None,
        }
    seen = next(
        (
            n
            for n, r in enumerate(rows)
            for e in r["events"]
            if is_read(e, seat) and shows(e, stim["id"])
        ),
        None,
    )
    first_read = next(
        (e["seq"] for r in rows for e in r["events"] if is_read(e, seat) and shows(e, stim["id"])),
        None,
    )
    opens = [(n, e) for n, r in enumerate(rows) for e in r["events"] if is_open(e, seat)]
    return {
        "stimulus": stim,
        "stimulus_seen_step": None if seen is None else rows[seen]["step"],
        "opens_after_usable_sight": None if seen is None else sum(1 for n, _ in opens if n > seen),
        "opens_after_log_sight": 0
        if first_read is None
        else sum(1 for _, e in opens if e["seq"] > first_read),
    }


def join_trial(trial: Path) -> dict:
    result = read_json(trial / "result.json")
    path = Path(result["config"]["task"]["path"])
    cell = path.parent.name
    assert CELL.fullmatch(cell), (trial, path)
    instance = read_json(path / "environment" / "world" / "instance.json")
    seat = real_seat(result, instance)
    steps, source = load_steps(trial, seat)
    rows = step_rows(steps)
    every, mine = agent_events(trial, seat)
    reason, lo, hi = assign(rows, mine)
    rewards = trial / "verifier" / "reward.json"
    reward = read_json(rewards) if rewards.is_file() else {}
    head = {
        "trial": trial.name,
        "path": str(trial),
        "cell": cell,
        "instance": INSTANCE.fullmatch(path.name).group(1),
        "seat": seat,
        "trajectory": source,
        "probe_pass": reward.get("probe_pass"),
        "reward": reward.get("reward"),
        "events": len(mine),
        "first_request_lag_s": first_lag(rows, mine),
    }
    if reason is not None:
        return head | {"class": "unjoined", "reason": reason, "offset_lo_s": lo, "offset_hi_s": hi}
    stim = stimulus(every, mine, instance, seat)
    listing = [
        {
            "step": r["step"],
            "time": r["time"],
            "text_requests": r["text_requests"],
            "scripted": r["text_scripted"],
            "sleep_s": r["sleep_s"],
            "events": [
                [
                    e["seq"],
                    e["kind"],
                    e["method"],
                    e["status"],
                    e["seat"],
                    (e.get("request") or {}).get("box"),
                ]
                for e in r["events"]
            ],
        }
        for r in rows
        if r["source"] == "agent"
    ]
    return head | {
        "class": "joined",
        "reason": None,
        "offset_lo_s": lo,
        "offset_hi_s": hi,
        "text_disagreements": text_check(rows),
        **open_keys(rows, seat),
        **sight_keys(rows, seat, stim),
        "steps": listing,
    }


def share(part: int, whole: int) -> str:
    return f"{part}/{whole}"


def cell_summary(cell: str, trials: list[dict]) -> dict:
    joined = [t for t in trials if t["class"] == "joined"]
    failing = [t for t in joined if t["probe_pass"] == 0]
    opened = [t for t in joined if t["opens"]]
    usable = sum(sum(t["usable_read_for_open"]) for t in joined)
    logged = sum(sum(t["log_read_for_open"]) for t in joined)
    opens = sum(t["opens"] for t in joined)
    stim = [t for t in joined if t["stimulus"] is not None]
    seen = [t for t in stim if t["stimulus_seen_step"] is not None]
    return {
        "cell": cell,
        "trials": len(trials),
        "unjoined": len(trials) - len(joined),
        "joined": len(joined),
        "with_open": len(opened),
        "read_once_then_1_batch": sum(
            1 for t in opened if t["reads_before_first_open_step"] == 1 and t["open_steps"] == 1
        ),
        "failing": len(failing),
        "failing_read_once_then_1_batch": sum(
            1
            for t in failing
            if t["opens"] and t["reads_before_first_open_step"] == 1 and t["open_steps"] == 1
        ),
        "failing_polled_or_slept": sum(
            1
            for t in failing
            if t["opens"] and (t["poll_run_between_opens"] >= 2 or t["sleep_s_between_opens"] >= 1)
        ),
        "failing_polled": sum(
            1 for t in failing if t["opens"] and t["poll_run_between_opens"] >= 2
        ),
        "failing_read_step_between_opens": sum(
            1 for t in failing if t["opens"] and t["poll_run_between_opens"] >= 1
        ),
        "failing_read_once": sum(1 for t in failing if t["reads_before_first_open_step"] == 1),
        "failing_slept": sum(1 for t in failing if t["opens"] and t["sleep_s_between_opens"] >= 1),
        "failing_waited_before_first_open": sum(
            1
            for t in failing
            if t["opens"]
            and (t["read_steps_before_first_open"] >= 3 or t["sleep_s_before_first_open"] >= 1)
        ),
        "batch_opens_max_ge2": sum(1 for t in joined if t["batch_opens_max"] >= 2),
        "read_and_open_same_step_ge1": sum(1 for t in joined if t["read_and_open_same_step"] >= 1),
        "opens": opens,
        "opens_usable_read": usable,
        "opens_log_read": logged,
        "with_stimulus": len(stim),
        "stimulus_seen": len(seen),
        "opens_after_usable_sight_ge1": sum(1 for t in seen if t["opens_after_usable_sight"]),
        "opens_after_log_sight_ge1": sum(1 for t in stim if t["opens_after_log_sight"]),
        "log_only_seen_not_step_seen": sorted(
            t["trial"]
            for t in stim
            if t["opens_after_log_sight"] and not t["opens_after_usable_sight"]
        ),
        "text_disagreement_trials": sum(1 for t in joined if t["text_disagreements"]),
        "unjoined_reasons": sorted({t["reason"] for t in trials if t["class"] == "unjoined"}),
    }


def table(title: str, headers: list[str], rows: list[list[str]]) -> str:
    widths = [
        max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)
    ]
    lines = [title]
    for r in [headers, *rows]:
        lines.append(
            "  ".join(
                c.rjust(w) if i else c.ljust(w)
                for i, (c, w) in enumerate(zip(r, widths, strict=True))
            )
        )
    return "\n".join(lines)


def pct(part: int, whole: int) -> str:
    return f"{part}/{whole} ({100.0 * part / whole:.1f}%)" if whole else f"{part}/0"


def render(report: dict) -> str:
    cells = report["cells"]
    pairs = (
        ("trials", "trials"),
        ("unjoined", "unjoined"),
        ("withOpen", "with_open"),
        ("read1Batch1", "read_once_then_1_batch"),
        ("failing", "failing"),
        ("failRead1", "failing_read_once"),
        ("failRead1Batch1", "failing_read_once_then_1_batch"),
        ("failReadStep", "failing_read_step_between_opens"),
        ("failPoll2", "failing_polled"),
        ("failSleep", "failing_slept"),
        ("failWaitPre", "failing_waited_before_first_open"),
        ("failPollOrSleep", "failing_polled_or_slept"),
        ("batch>=2", "batch_opens_max_ge2"),
        ("readOpenSame", "read_and_open_same_step_ge1"),
        ("stim", "with_stimulus"),
        ("seen", "stimulus_seen"),
        ("openAfterSeen", "opens_after_usable_sight_ge1"),
        ("openAfterLogSeen", "opens_after_log_sight_ge1"),
        ("textDisagree", "text_disagreement_trials"),
    )
    head = ["cell", *(h for h, _ in pairs)]
    keys = [k for _, k in pairs]
    parts = [
        CLOCK_RULE,
        LEGEND,
        table(
            "CELLS (counts of trials)",
            head,
            [[c["cell"], *(str(c[k]) for k in keys)] for c in cells],
        ),
    ]
    rows = [
        [c["cell"], pct(c["opens_usable_read"], c["opens"]), pct(c["opens_log_read"], c["opens"])]
        for c in cells
    ]
    parts.append(
        table(
            "OPENS with a usable read (earlier step) against any read before them in the log",
            ["cell", "usable", "logOnly"],
            rows,
        )
    )
    lags = [
        t["first_request_lag_s"] for t in report["trials"] if t["first_request_lag_s"] is not None
    ]
    los = [t["offset_lo_s"] for t in report["trials"] if t["class"] == "joined"]
    his = [t["offset_hi_s"] for t in report["trials"] if t["class"] == "joined"]
    if lags and los:
        parts.append(
            f"first_request_lag_s range [{min(lags):.3f}, {max(lags):.3f}]; offset interval lo range [{min(los):.3f}, {max(los):.3f}], hi range [{min(his):.3f}, {max(his):.3f}]"
        )
    lines = ["UNJOINED and LOG-ONLY DIFFERENCES"]
    for t in report["trials"]:
        if t["class"] == "unjoined":
            lines.append(f"  {t['cell']}  {t['trial']}  unjoined: {t['reason']}")
    for c in cells:
        lines += [
            f"  {c['cell']}  {name}  log-only rule sees the stimulus before an open; the steps do not"
            for name in c["log_only_seen_not_step_seen"]
        ]
    parts.append("\n".join(lines))
    return "\n\n".join(parts)


def render_trials(report: dict) -> str:
    out = []
    for t in report["trials"]:
        out.append(
            f"== {t['cell']} {t['trial']} {t['class']} {t['reason'] or ''}\n   {t['path']}/{t['trajectory']}"
        )
        if t["class"] != "joined":
            continue
        out.append(
            f"   offset [{t['offset_lo_s']:.3f}, {t['offset_hi_s']:.3f}] lag {t['first_request_lag_s']}  stimulus {t['stimulus']}  seen_step {t['stimulus_seen_step']}"
        )
        for s in t["steps"]:
            ev = " ".join(
                f"{q}:{k}{'' if b is None else '#' + str(b)}{'' if st == 200 else '/' + str(st)}"
                for q, k, _, st, _, b in s["events"]
            )
            out.append(
                f"   step {s['step']:>3}  text {','.join(s['text_requests']) or '-'}{' (scripted)' if s['scripted'] else ''}  log {ev or '-'}"
            )
        out.append(
            f"   usable_read_for_open {t['usable_read_for_open']}  log_read_for_open {t['log_read_for_open']}  reads_before_first_open_step {t['reads_before_first_open_step']}  batch_opens_max {t['batch_opens_max']}  read_and_open_same_step {t['read_and_open_same_step']}"
        )
    return "\n".join(out)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="step_join.py")
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--json", metavar="FILE")
    parser.add_argument("--trials", action="store_true")
    args = parser.parse_args(argv)
    trials = [join_trial(t) for t in trial_dirs(args.roots)]
    trials.sort(key=lambda t: (t["cell"], t["instance"], t["trial"]))
    cells = sorted({t["cell"] for t in trials})
    report = {
        "clock_rule": CLOCK_RULE,
        "margin_s": MARGIN_S,
        "cells": [cell_summary(c, [t for t in trials if t["cell"] == c]) for c in cells],
        "trials": trials,
    }
    text = render(report)
    if args.trials:
        text += "\n\n" + render_trials(report)
    print(text)
    if args.json is not None:
        Path(args.json).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
