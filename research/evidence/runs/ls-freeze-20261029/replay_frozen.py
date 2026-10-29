"""Replay the shipped frozen-lane heartbeat trigger over recorded lane sessions (LS-FREEZE).

Each simulated supervisor cycle (every 150 s) builds the real ``lane_observation`` for every authorized
lane that had an item in flight (the keeper log's dispatch until its outcome or the next dispatch), with
the pane status the frozen lanes showed (``unknown``) and the clock rule after a host wake, then runs the real
``fleet_triggers``. Only session timestamps, tool names and call timeouts are used; nothing is printed
from message content. Session paths come from ``windows-v1.jsonl`` metrics of lanes the fleet source read.

    uv run python replay_frozen.py > replay-frozen.json

``window``: 2026-10-28 12:00Z to the first freeze 2026-10-29 21:54:55Z, minus the host sleep. The
``after_freeze`` block continues to ``AFTER_END`` and reports when the heartbeat rule alone would have fired.
"""

from __future__ import annotations

import bisect
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from evallab.production import live_supervisor as ls
from evallab.production import live_supervisor_fleet as fl

HOME = Path.home()
BRIDGE = HOME / "Developer/research-context/bridge/2026-10-27-run-atlas"
WINDOWS = HOME / ".local/state/evallab-prod-live/live-supervisor/windows-v1.jsonl"
START = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
FREEZE = datetime(2026, 10, 29, 21, 54, 55, tzinfo=UTC)  # zsh "suspended (signal)" 5:54:55 PM EDT
AFTER_END = datetime(2026, 10, 29, 23, 0, tzinfo=UTC)
SLEEP = (datetime(2026, 10, 29, 5, 36, 29, tzinfo=UTC), datetime(2026, 10, 29, 12, 45, 43, tzinfo=UTC))  # pmset
CADENCE = timedelta(seconds=150)
LOOKBACK = timedelta(hours=12)
THRESHOLDS = [600, 900, 1200, 1800, 2700, 3600]


def lane_sessions() -> dict[str, str]:
    paths: dict[str, str] = {}
    for line in WINDOWS.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        metrics = row["metrics"]
        if row["subject"]["kind"] == "omp_lane" and metrics.get("session_read") and metrics.get("session_path"):
            paths[row["key"].split("@")[0].removeprefix("live:omp_lane:")] = metrics["session_path"]
    return paths


def open_items() -> dict[str, list[tuple[str, datetime, datetime | None]]]:
    """Per pane the (item, dispatched, closed) intervals: dispatch until its outcome or the next dispatch."""
    log = fl.parse_queue_log((BRIDGE / "_queue.log").read_text(encoding="utf-8"))
    out: dict[str, list[tuple[str, datetime, datetime | None]]] = {}
    current: dict[str, tuple[str, datetime]] = {}
    for at, body in log.lines:
        dispatch, outcome = fl._DISPATCH.match(body), fl._OUTCOME.match(body)
        if dispatch:
            pane = dispatch.group(1)
            if pane in current:
                out.setdefault(pane, []).append((*current.pop(pane), at))
            current[pane] = (dispatch.group(2), at)
        elif outcome and current.get(outcome.group(1), ("",))[0] == outcome.group(2):
            out.setdefault(outcome.group(1), []).append((*current.pop(outcome.group(1)), at))
    for pane, (item, at) in current.items():
        out.setdefault(pane, []).append((item, at, None))
    return out


def load(path: str) -> tuple[list[datetime], list[dict[str, Any]]]:
    pairs: list[tuple[datetime, dict[str, Any]]] = []
    with Path(path).open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if (event := fl._load_line(line)) and (at := fl._ts(event.get("timestamp"))):
                pairs.append((at, event))
    pairs.sort(key=lambda p: p[0])
    return [p[0] for p in pairs], [p[1] for p in pairs]


def lane_cycle(pane: str, stamps: list[datetime], events: list[dict[str, Any]], item: tuple[str, datetime],
               t: datetime) -> tuple[list[ls.TriggerHit], dict[str, Any]]:
    """The ``frozen`` hits and the metrics of one lane at one simulated cycle."""
    lo, hi = bisect.bisect_left(stamps, t - LOOKBACK), bisect.bisect_right(stamps, t)
    awake = SLEEP[1] if t >= SLEEP[1] else None
    observation = fl.lane_observation(events[lo:hi], subject=ls.Subject("omp_lane", pane, "fleet"), now=t,
                                      agent_status="unknown", queue=fl.LaneQueue(1, item[0], item[1]),
                                      awake_since=awake)
    return [h for h in fl.fleet_triggers(observation) if h.trigger == "frozen"], dict(observation.metrics)


def replay() -> dict[str, Any]:
    intervals = open_items()
    lanes: dict[str, Any] = {}
    totals = {t: {"alarm_lane_cycles": 0, "episodes": 0} for t in THRESHOLDS}
    shipped = fl.HEARTBEAT_STALE_S
    in_flight_total = 0
    after: dict[str, Any] = {}
    #: in-flight cycles quiet for at least the shipped threshold that the rule deliberately leaves alone
    excused = {"declared_tool_timeout": 0, "open_ask": 0, "turn_ended": 0}
    for pane, path in lane_sessions().items():
        stamps, events = load(path)
        rows = intervals.get(pane, [])
        stats = {t: {"alarm_lane_cycles": 0, "episodes": 0, "was": False} for t in THRESHOLDS}
        in_flight = cycles = 0
        t = START
        while t < AFTER_END:
            if SLEEP[0] <= t < SLEEP[1]:
                t += CADENCE
                continue
            item = next(((i, a) for i, a, b in rows if a <= t and (b is None or t < b)), None)
            if t < FREEZE:
                cycles += 1
            if item is not None:
                if t < FREEZE:
                    in_flight += 1
                for th in THRESHOLDS:
                    fl.HEARTBEAT_STALE_S = th
                    hits, m = lane_cycle(pane, stamps, events, item, t)
                    fired = bool(hits)
                    if th == shipped and t < FREEZE and not fired and (m["last_activity_age_s"] or 0) >= shipped:
                        key = ("declared_tool_timeout" if m["silence_excused"] else "open_ask" if m["awaiting_human"]
                               else "turn_ended" if not m["turn_open"] else None)
                        if key:
                            excused[key] += 1
                    if t < FREEZE:
                        stats[th]["alarm_lane_cycles"] += fired
                        stats[th]["episodes"] += fired and not stats[th]["was"]
                    stats[th]["was"] = fired
                    if th == shipped and t >= FREEZE and fired and pane not in after:
                        after[pane] = round((t - FREEZE).total_seconds())
            else:
                for th in THRESHOLDS:
                    stats[th]["was"] = False
            t += CADENCE
        fl.HEARTBEAT_STALE_S = shipped
        in_flight_total += in_flight
        lanes[pane] = {"cycles": cycles, "in_flight_cycles": in_flight,
                       "by_threshold_s": {str(th): {k: v for k, v in s.items() if k != "was"} for th, s in stats.items()}}
        for th in THRESHOLDS:
            totals[th]["alarm_lane_cycles"] += stats[th]["alarm_lane_cycles"]
            totals[th]["episodes"] += stats[th]["episodes"]
    return {"cadence_s": CADENCE.total_seconds(), "window": [START.isoformat(), FREEZE.isoformat()],
            "host_sleep_excluded": [s.isoformat() for s in SLEEP], "shipped_threshold_s": shipped,
            "lanes": lanes, "in_flight_lane_cycles": in_flight_total, "quiet_hour_but_not_alarmed": excused,
            "totals_by_threshold_s": {str(t): v for t, v in totals.items()},
            "after_freeze_first_heartbeat_hit_s": after, "after_end": AFTER_END.isoformat()}


def wake_cycle() -> dict[str, Any]:
    """The first supervisor cycle after the 7 h host sleep (12:46:54Z), lane by lane, before and after the clock rule.

    Pane status, open item and its age come from the recorded window metrics of that cycle; hits are recomputed
    by the real code from the session records up to that moment. ``before`` is the pre-change behaviour (ages
    from the wall clock); ``after`` passes ``awake_since`` and the supervisor gap, as the source does.
    """
    at = datetime(2026, 10, 29, 12, 46, 54, tzinfo=UTC)
    rows = [json.loads(line) for line in WINDOWS.read_text(encoding="utf-8").splitlines()]
    out: dict[str, Any] = {"cycle": at.isoformat(), "wake": SLEEP[1].isoformat(), "lanes": {}}
    for row in rows:
        m = row["metrics"]
        if not row["observed_at"].startswith("2026-10-29T12:46:54") or row["subject"]["kind"] != "omp_lane" \
                or not m.get("session_path"):
            continue
        pane = row["key"].split("@")[0].removeprefix("live:omp_lane:")
        stamps, events = load(m["session_path"])
        window = events[bisect.bisect_left(stamps, at - LOOKBACK):bisect.bisect_right(stamps, at)]
        queue = fl.LaneQueue(1, m["queue_open_item"], at - timedelta(seconds=m["queue_open_item_age_s"])) \
            if m.get("queue_open_item") else fl.LaneQueue()
        subject = ls.Subject("omp_lane", pane, "fleet")
        before = fl.lane_observation(window, subject=subject, now=at, agent_status=m["agent_status"], queue=queue)
        after = fl.lane_observation(window, subject=subject, now=at, agent_status=m["agent_status"], queue=queue,
                                    awake_since=at)
        after = fl._with_metrics(after, supervisor_gap_s=(at - SLEEP[0]).total_seconds())
        out["lanes"][pane] = {"agent_status": m["agent_status"], "open_item": bool(m.get("queue_open_item")),
                              "before": sorted({h.trigger for h in fl.fleet_triggers(before)}),
                              "after": sorted({h.trigger for h in fl.fleet_triggers(after)})}
    lanes = out["lanes"]
    out["lanes_with_a_hit_before"] = sum(bool(v["before"]) for v in lanes.values())
    out["lanes_with_a_hit_after"] = sum(bool(v["after"]) for v in lanes.values())
    out["wall_time_or_silence_hits_before"] = sum(len({"wall_time", "silence"} & set(v["before"])) for v in lanes.values())
    out["wall_time_or_silence_hits_after"] = sum(len({"wall_time", "silence"} & set(v["after"])) for v in lanes.values())
    out["frozen_hits_after"] = sum("frozen" in v["after"] for v in lanes.values())
    return out


if __name__ == "__main__":
    json.dump({"replay": replay(), "wake_cycle": wake_cycle()}, sys.stdout, indent=1)
