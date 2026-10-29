"""Live Supervisor fleet-health report: idle lane-hours, item durations, blocked loops and open items.

Deterministic, $0 and repeatable: it reads only the queue keeper's own files (``_queue.log``,
``queue.json``, ``queue-state.json``) and never a lane's session history, so it needs no
authorization and makes no model, network or Docker call. It replaces the ad-hoc RH-3 / RH-p11c
scripts (``research-context/bridge/.../raw/rh3`` and ``raw/rh-p11c``, read, not imported) that
produced this report once.

``fleet_report`` is a pure function of the log text, the two JSON files and ``now``;
``render_section`` turns it into the markdown section the lane reads in ``live-supervisor.md``;
``write_section`` replaces only the marked region of that file, atomically, under a lock.

How items and gaps are read from the keeper log (the same reading RH-p11c used, so numbers compare)
    * ``dispatch <pane> <id> ... -> ok`` opens an item on the pane. A later dispatch on the same pane
      *supersedes* a still-open item (a keeper restart is the usual cause): the item ends there with
      no outcome, so it is neither ``done`` nor ``blocked``. A repeated dispatch of the already open
      item is ignored. ``-> FAILED`` dispatches never reached the lane and are not read.
    * ``outcome <pane> <id>: done|blocked`` closes the open item with that id. An outcome with no
      earlier dispatch of that item in the log (dispatched before the log began) is an *orphan*: it has no
      known start, so it gives no duration, but it does start an idle gap. An outcome for an item
      already closed or superseded is *late* and ignored.
    * An idle gap on a pane runs from an outcome to that pane's next dispatch; one still running ends at
      ``now`` (at the keeper's stop line when the log ends in ``queue keeper stop``). A gap longer than
      ``HOLD_S`` is a *hold*: RH-3 found the long gaps were lanes starved of work, not slow lanes.
    * Intervals are counted in the window by the time they end; gaps are clipped to the window.

Every count carries its n; a quantity with no data is ``None``, never ``0``.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from evallab.production.live_supervisor import DEFAULT_ROOT
from evallab.production.live_supervisor_fleet import (
    _DISPATCH,
    _OUTCOME,
    LANE_IDLE_S,
    WALL_TIME_S,
    parse_queue_log,
    pending_by_pane,
)

SCHEMA = "live-supervisor-fleet-report/1"
DEFAULT_BRIDGE = Path("~/Developer/research-context/bridge/2026-10-27-run-atlas")
TARGET_FILE = "live-supervisor.md"
QUEUE_LOG = "_queue.log"
QUEUE_FILE = "queue.json"
STATE_FILE = "queue-state.json"
DEFAULT_WINDOW_HOURS = 24.0

# --------------------------------------------------------------------------- thresholds
#: RH-p11c STORM (fleet-detectors.md §1, fixed before scoring): a pane with >= 3 items that each end
#: ``blocked`` <= 300 s after dispatch, all three spans inside one 30-minute window.
STORM_MIN_ITEMS = 3
STORM_ITEM_S = 300
STORM_WINDOW_S = 30 * 60
#: Two flagged items of one pane belong to one episode when the gap between them is at most a window.
STORM_MERGE_S = STORM_WINDOW_S
#: RH-3: a gap after an outcome longer than this is a hold (the lane starved of work).
HOLD_S = LANE_IDLE_S
#: The supervisor's wall_time rule: one item open longer than this.
OPEN_ITEM_S = WALL_TIME_S

BEGIN_MARK = b"<!-- fleet-health:start -->"
END_MARK = b"<!-- fleet-health:end -->"
LOCK_SUFFIX = ".lock"

#: What the detectors measured on the keeper log through 10-28 21:10 ET (RH-p11c fleet-detectors.md §4).
CAVEAT = (
    "STORM had 1 episode in the validation log (precision 1/1) and SILENT was 2/5 precise, so this report "
    "claims nothing about lane health beyond the keeper log: it never reads a session, so a lane with a "
    "long open item may be working and a quiet one may be stuck."
)

_ET = ZoneInfo("America/New_York")
_START_LINE = "queue keeper start"
_STOP_LINE = "queue keeper stop"


# --------------------------------------------------------------------------- ledger


@dataclass(frozen=True)
class Item:
    pane: str
    item_id: str
    start: datetime
    #: ``None`` while open.
    end: datetime | None
    #: ``done``, ``blocked``, ``superseded`` or ``open``.
    outcome: str

    @property
    def seconds(self) -> float | None:
        return None if self.end is None else (self.end - self.start).total_seconds()


@dataclass(frozen=True)
class Gap:
    """A pane idle after an outcome; ``end`` is ``None`` while the pane is still idle."""

    pane: str
    start: datetime
    end: datetime | None


@dataclass(frozen=True)
class Ledger:
    items: tuple[Item, ...]
    gaps: tuple[Gap, ...]
    #: Outcome times with no earlier dispatch of that item in the log, and late outcomes.
    orphans: tuple[datetime, ...]
    late: tuple[datetime, ...]
    restarts: tuple[datetime, ...]
    first_at: datetime | None
    last_at: datetime | None
    stopped_cleanly: bool


def build_ledger(log_text: str) -> Ledger:
    """Items, idle gaps and unmatched outcomes of the whole keeper log, in log order."""
    log = parse_queue_log(log_text)
    items: list[Item] = []
    gaps: list[Gap] = []
    orphans: list[datetime] = []
    late: list[datetime] = []
    restarts: list[datetime] = []
    open_item: dict[str, int] = {}  # pane -> index in ``items``
    idle_since: dict[str, datetime] = {}
    seen: set[tuple[str, str]] = set()
    for at, body in log.lines:
        if body.startswith(_START_LINE):
            restarts.append(at)
        elif dispatched := _DISPATCH.match(body):
            pane, item_id = dispatched.groups()
            index = open_item.get(pane)
            if index is not None and items[index].item_id == item_id:
                continue
            if index is not None:
                items[index] = _closed(items[index], at, "superseded")
            if pane in idle_since:
                gaps.append(Gap(pane, idle_since.pop(pane), at))
            open_item[pane] = len(items)
            items.append(Item(pane, item_id, at, None, "open"))
            seen.add((pane, item_id))
        elif closed := _OUTCOME.match(body):
            pane, item_id, outcome = closed.groups()
            index = open_item.get(pane)
            if index is not None and items[index].item_id == item_id:
                items[index] = _closed(items[index], at, outcome)
                del open_item[pane]
                idle_since[pane] = at
            elif (pane, item_id) not in seen:
                orphans.append(at)
                seen.add((pane, item_id))
                if index is None and pane not in idle_since:
                    idle_since[pane] = at
            else:
                late.append(at)
    gaps.extend(Gap(pane, since, None) for pane, since in idle_since.items())
    return Ledger(
        tuple(items), tuple(gaps), tuple(orphans), tuple(late), tuple(restarts),
        log.lines[0][0] if log.lines else None, log.lines[-1][0] if log.lines else None, log.stopped_cleanly,
    )


def _closed(item: Item, at: datetime, outcome: str) -> Item:
    return Item(item.pane, item.item_id, item.start, at, outcome)


# --------------------------------------------------------------------------- numbers


def _percentile(sorted_values: Sequence[float], q: float) -> float | None:
    """Linear-interpolated percentile of a sorted sequence; ``None`` when it is empty."""
    if not sorted_values:
        return None
    position = (len(sorted_values) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    return sorted_values[low] + (sorted_values[high] - sorted_values[low]) * (position - low)


def _duration_stats(seconds: Sequence[float]) -> dict[str, Any]:
    ordered = sorted(seconds)
    return {
        "n": len(ordered),
        "median_min": _scaled(_percentile(ordered, 0.5), 60),
        "p90_min": _scaled(_percentile(ordered, 0.9), 60),
        "total_h": _scaled(sum(ordered), 3600) if ordered else None,
    }


def _scaled(value: float | None, unit: float) -> float | None:
    return None if value is None else value / unit


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else moment.astimezone(UTC).isoformat()


def storm_episodes(items: Sequence[Item]) -> list[dict[str, Any]]:
    """RH-p11c STORM over closed items: per pane, merged episodes of fast ``blocked`` items.

    An item is flagged when it lies in a window that starts at some quick blocked item of the pane
    and holds at least ``STORM_MIN_ITEMS`` of them (start and end inside the window, all bounds
    inclusive). Flagged items whose gap to the previous flagged item is at most ``STORM_MERGE_S``
    form one episode. Wasted lane-minutes are first dispatch to last outcome.
    """
    by_pane: dict[str, list[Item]] = {}
    for item in items:
        if item.outcome == "blocked" and item.seconds is not None and item.seconds <= STORM_ITEM_S:
            by_pane.setdefault(item.pane, []).append(item)
    episodes: list[dict[str, Any]] = []
    for pane, quick in by_pane.items():
        quick.sort(key=lambda item: item.start)
        flagged: set[int] = set()
        for first in quick:
            horizon = first.start + timedelta(seconds=STORM_WINDOW_S)
            inside = [k for k, other in enumerate(quick)
                      if other.start >= first.start and other.end is not None and other.end <= horizon]
            if len(inside) >= STORM_MIN_ITEMS:
                flagged.update(inside)
        groups: list[list[Item]] = []
        for k in sorted(flagged):
            item = quick[k]
            last_end = groups[-1][-1].end if groups else None
            if last_end is not None and (item.start - last_end).total_seconds() <= STORM_MERGE_S:
                groups[-1].append(item)
            else:
                groups.append([item])
        for group in groups:
            first_start, last_end = group[0].start, group[-1].end
            assert last_end is not None
            episodes.append({
                "pane": pane,
                "start": _iso(first_start),
                "end": _iso(last_end),
                "n_items": len(group),
                "items": [item.item_id for item in group],
                "wasted_lane_min": (last_end - first_start).total_seconds() / 60,
                "item_min": sum(item.seconds or 0.0 for item in group) / 60,
            })
    episodes.sort(key=lambda episode: (episode["start"], episode["pane"]))
    return episodes


def fleet_report(log_text: str, queue: Mapping[str, Any], state: Mapping[str, Any], *,
                 now: datetime, window_hours: float = DEFAULT_WINDOW_HOURS) -> dict[str, Any]:
    """The fleet-health numbers over the ``window_hours`` ending at ``now`` (pure)."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not (math.isfinite(window_hours) and window_hours > 0):
        raise ValueError("window_hours must be finite and positive")
    ledger = build_ledger(log_text)
    window_start = now - timedelta(hours=window_hours)
    managed = frozenset(queue.get("panes") or {}) or None  # no queue.json lanes: every pane counts
    pending = pending_by_pane(queue, state)
    horizon = min(now, ledger.last_at) if ledger.stopped_cleanly and ledger.last_at else now

    def in_window(moment: datetime) -> bool:
        return window_start < moment <= now

    closed = [i for i in ledger.items if i.end is not None and in_window(i.end)]
    done = [i.seconds for i in closed if i.outcome == "done" and i.seconds is not None]
    blocked = [i.seconds for i in closed if i.outcome == "blocked" and i.seconds is not None]
    superseded = [i for i in closed if i.outcome == "superseded"]
    both_h = sum(done) + sum(blocked)
    items = {
        "n_intervals": len(done) + len(blocked),
        "done": _duration_stats(done),
        "blocked": _duration_stats(blocked),
        "blocked_share_of_hours": sum(blocked) / both_h if both_h else None,
        "superseded": {"n": len(superseded),
                       "total_h": sum(i.seconds or 0.0 for i in superseded) / 3600 if superseded else None},
        "orphan_outcomes": sum(1 for at in ledger.orphans if in_window(at)),
        "late_outcomes": sum(1 for at in ledger.late if in_window(at)),
    }

    idle_gaps: list[tuple[Gap, float, float]] = []  # (gap, seconds in window, full seconds)
    unmanaged_idle: list[str] = []
    idle_now: list[dict[str, Any]] = []
    for gap in ledger.gaps:
        if gap.end is None and managed is not None and gap.pane not in managed:
            unmanaged_idle.append(gap.pane)
            continue
        end = gap.end if gap.end is not None else horizon
        inside = (min(end, horizon) - max(gap.start, window_start)).total_seconds()
        if inside <= 0:
            continue
        idle_gaps.append((gap, inside, (end - gap.start).total_seconds()))
        if gap.end is None:
            idle_now.append({
                "pane": gap.pane,
                "since": _iso(gap.start),
                "idle_h": (end - gap.start).total_seconds() / 3600,
                "parked": bool((state.get(gap.pane) or {}).get("parked")),
                "pending": pending.get(gap.pane),
            })
    holds = [(gap, inside) for gap, inside, full in idle_gaps if full > HOLD_S]
    idle = {
        "n_gaps": len(idle_gaps),
        "n_panes": len({gap.pane for gap, _, _ in idle_gaps}),
        "total_h": sum(s for _, s, _ in idle_gaps) / 3600 if idle_gaps else None,
        "holds": {
            "threshold_min": HOLD_S / 60,
            "n": len(holds),
            "total_h": sum(s for _, s in holds) / 3600 if holds else None,
        },
        "idle_now": sorted(idle_now, key=lambda row: row["pane"]),
        "idle_now_with_pending": sorted(row["pane"] for row in idle_now if (row["pending"] or 0) > 0),
        "unmanaged_idle_panes": sorted(set(unmanaged_idle)),
    }

    quick_pool = [i for i in closed if i.outcome in ("done", "blocked")]
    episodes = storm_episodes(quick_pool)
    storm = {
        "thresholds": {"min_items": STORM_MIN_ITEMS, "item_s": STORM_ITEM_S, "window_s": STORM_WINDOW_S},
        "n_intervals": len(quick_pool),
        "n_episodes": len(episodes) if quick_pool else None,
        "episodes": episodes,
        "wasted_lane_min": sum(e["wasted_lane_min"] for e in episodes) if episodes else None,
    }

    open_rows: list[dict[str, Any]] = []
    for item in ledger.items:
        if item.end is not None:
            continue
        age_s = (now - item.start).total_seconds()
        open_rows.append({
            "pane": item.pane,
            "item": item.item_id,
            "since": _iso(item.start),
            "age_h": age_s / 3600,
            "over_limit": age_s > OPEN_ITEM_S,
            "managed": managed is None or item.pane in managed,
            "parked": bool((state.get(item.pane) or {}).get("parked")),
        })
    open_rows.sort(key=lambda row: -row["age_h"])
    opened = {
        "n": len(open_rows),
        "limit_h": OPEN_ITEM_S / 3600,
        "n_over_limit": sum(1 for row in open_rows if row["over_limit"]),
        "items": open_rows,
    }

    first, last = ledger.first_at, ledger.last_at
    covered = (now - max(window_start, first)).total_seconds() / 3600 if first and now > first else None
    return {
        "schema": SCHEMA,
        "generated_at": _iso(now),
        "window": {
            "hours": window_hours,
            "start": _iso(window_start),
            "end": _iso(now),
            "log_first_at": _iso(first),
            "log_last_at": _iso(last),
            "log_age_h": (now - last).total_seconds() / 3600 if last else None,
            "covered_h": covered,
            "starts_before_log": first is not None and window_start < first,
            "keeper_stopped": ledger.stopped_cleanly,
            "keeper_starts": sum(1 for at in ledger.restarts if in_window(at)),
        },
        "items": items,
        "idle": idle,
        "storm": storm,
        "open": opened,
    }


def supervisor_coverage(text: str, *, now: datetime, window_hours: float,
                        sleep_times: Sequence[datetime] = ()) -> dict[str, Any]:
    """Summarize heartbeat intervals; only adjacent cycles at most three minutes apart count."""
    now = now.astimezone(UTC)
    start = now - timedelta(hours=window_hours)
    rows: list[tuple[datetime, str]] = []
    malformed = 0
    for line in text.splitlines():
        try:
            row = json.loads(line)
            timestamp = datetime.fromisoformat(row["started_at"])
            status = row["reader_status"]
            if timestamp.tzinfo is None or status not in {"ok", "rules", "budget_exhausted", "headroom_blocked"}:
                raise ValueError("invalid heartbeat")
            rows.append((timestamp.astimezone(UTC), status))
        except (ValueError, TypeError, KeyError):
            malformed += 1
    rows.sort(key=lambda row: row[0])
    if not rows:
        return {"awake_h": None, "cycles": None, "non_ok": None, "first_cap_today": None,
                "gaps": None, "malformed": malformed}
    in_window = [(at, status) for at, status in rows if start < at <= now]
    awake_seconds = sum(
        max(0.0, (min(b, now) - max(a, start)).total_seconds())
        for (a, _), (b, _) in zip(rows, rows[1:], strict=False)
        if (b - a).total_seconds() <= 180
    )
    gaps: list[dict[str, Any]] = []
    # Include window edges for unknown coverage, but do not extrapolate heartbeat cadence.
    points = [start] + [at for at, _ in in_window] + [now]
    for a, b in zip(points, points[1:], strict=False):
        seconds = (b - a).total_seconds()
        if seconds > 600:
            slept = any(a <= t.astimezone(UTC) <= b for t in sleep_times)
            gaps.append({"start": _iso(a), "end": _iso(b), "cause": "sleep observed" if slept else "unknown"})
    today = now.date()
    cap = next((at for at, status in rows if at.date() == today and at <= now
                and status == "budget_exhausted"), None)
    return {"awake_h": awake_seconds / 3600, "cycles": len(in_window),
            "non_ok": sum(status != "ok" for _, status in in_window),
            "first_cap_today": _iso(cap), "gaps": gaps, "malformed": malformed}


def _sleep_times() -> list[datetime]:
    """Read explicit macOS sleep events, without inferring cause from missing cycles."""
    try:
        result = subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True,
                                timeout=3, check=True)
    except (OSError, subprocess.SubprocessError):
        return []
    times = []
    for line in result.stdout.splitlines():
        match = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\s+[+-]\d{4})\s+Sleep\b", line)
        if match:
            try:
                times.append(datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S %z"))
            except ValueError:
                continue
    return times


# --------------------------------------------------------------------------- rendering


def _et(value: str | datetime | None) -> str:
    """``10-28 5:57 PM ET`` for an ISO string or datetime; ``unknown`` for ``None``."""
    if value is None:
        return "unknown"
    moment = datetime.fromisoformat(value) if isinstance(value, str) else value
    local = moment.astimezone(_ET)
    hour = local.hour % 12 or 12
    return f"{local:%m-%d} {hour}:{local:%M} {'AM' if local.hour < 12 else 'PM'} ET"


def _minutes(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.0f} min" if value < 120 else f"{value / 60:.1f} h"


def _hours(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} h"


def _names(panes: Sequence[str], limit: int = 8) -> str:
    shown = ", ".join(f"`{pane}`" for pane in panes[:limit])
    return shown + (f" and {len(panes) - limit} more" if len(panes) > limit else "")


def _stats_text(label: str, stats: Mapping[str, Any]) -> str:
    if not stats["n"]:
        return f"{label} n=0"
    return (f"{label} n={stats['n']}: median {_minutes(stats['median_min'])}, "
            f"p90 {_minutes(stats['p90_min'])}, {_hours(stats['total_h'])} total")


def render_section(report: Mapping[str, Any], *, now: datetime) -> str:
    """The markdown section for ``live-supervisor.md``: question → what ran → number → meaning."""
    window, items, idle = report["window"], report["items"], report["idle"]
    storm, opened = report["storm"], report["open"]
    lines = [f"## Fleet health (auto, refreshed {_et(now)})", ""]
    hours = f"{window['hours']:g}"
    if window["log_first_at"] is None:
        lines.append(f"Window: the last {hours} h to {_et(window['end'])}. The keeper log is empty.")
    else:
        covered = (f"the log covers {_hours(window['covered_h'])} of it, because it starts {_et(window['log_first_at'])}"
                   if window["starts_before_log"] else "the log covers all of it")
        lines.append(
            f"Window: the last {hours} h to {_et(window['end'])}; {covered}; last log line "
            f"{_et(window['log_last_at'])} ({_hours(window['log_age_h'])} ago"
            + ("; the keeper has stopped" if window["keeper_stopped"] else "")
            + f"); {window['keeper_starts']} keeper start(s) in the window. Fleet source: the keeper's files.")
    lines.append("")
    done, blocked = items["done"], items["blocked"]
    share = items["blocked_share_of_hours"]
    lines.append(
        "- **How long do items take?** → dispatch→outcome pairs ending in the window, `done` vs `blocked` → "
        f"{_stats_text('done', done)}; {_stats_text('blocked', blocked)}; superseded by a later dispatch "
        f"n={items['superseded']['n']} (no outcome); orphan outcomes n={items['orphan_outcomes']} dropped (dispatched "
        "before the log) → "
        + (f"blocked items took {share:.0%} of item hours." if share is not None else "no closed interval to compare."))
    holds = idle["holds"]
    if idle["n_gaps"]:
        idle_text = (f"{_hours(idle['total_h'])} idle over {idle['n_gaps']} gaps on {idle['n_panes']} panes; "
                     f"holds over {holds['threshold_min']:.0f} min n={holds['n']}, {_hours(holds['total_h'])}")
    else:
        idle_text = "no outcome→dispatch gap in the window (n=0)"
    now_with = idle["idle_now_with_pending"]
    now_text = (f"idle now with items pending n={len(now_with)}: {_names(now_with)}" if now_with
                else f"idle now with items pending n=0 of {len(idle['idle_now'])} idle panes")
    lines.append(
        "- **How much lane time is idle?** → per pane, outcome → next dispatch, clipped to the window → "
        f"{idle_text}; {now_text} → long holds are lanes starved of work (RH-3), not slow ones.")
    if storm["n_episodes"] is None:
        storm_text = "no closed interval to check (n=0)"
    elif not storm["n_episodes"]:
        storm_text = f"0 episodes in {storm['n_intervals']} intervals"
    else:
        worst = max(storm["episodes"], key=lambda e: e["wasted_lane_min"])
        storm_text = (f"{storm['n_episodes']} episode(s) in {storm['n_intervals']} intervals, "
                      f"{_minutes(storm['wasted_lane_min'])} of lane time; worst `{worst['pane']}` "
                      f"{_et(worst['start'])}, {worst['n_items']} items")
    lines.append(
        f"- **Are there blocked loops?** → STORM: ≥{STORM_MIN_ITEMS} items on a pane each `blocked` within "
        f"{STORM_ITEM_S} s of dispatch, inside {STORM_WINDOW_S // 60} min → {storm_text} → a hit is the same "
        "objective re-dispatched and blocked at once.")
    over = [f"`{row['pane']}` {row['item']} {_hours(row['age_h'])}" for row in opened["items"] if row["over_limit"]]
    unmanaged = sum(1 for row in opened["items"] if row["over_limit"] and not row["managed"])
    parked = sum(1 for row in opened["items"] if row["over_limit"] and row["parked"])
    lines.append(
        f"- **What is still open?** → dispatches with no outcome, flagged over {opened['limit_h']:g} h → "
        f"{opened['n']} open, {opened['n_over_limit']} over {opened['limit_h']:g} h"
        + (f": {'; '.join(over[:6])}" if over else "")
        + (f" ({unmanaged} on lanes no longer in `queue.json`)" if unmanaged else "")
        + (f" ({parked} on parked lanes)" if parked else "")
        + " → age says the item is long, not that the lane is stuck.")
    coverage = report.get("supervisor_coverage")
    if coverage is not None:
        if coverage["cycles"] is None:
            coverage_text = "n/a (heartbeat file missing, empty, or without valid rows)"
        else:
            gaps = "; ".join(
                f"{_et(gap['start'])}–{_et(gap['end'])} ({gap['cause']})"
                for gap in coverage["gaps"]
            ) or "none"
            cap = _et(coverage["first_cap_today"]) if coverage["first_cap_today"] else "not observed"
            coverage_text = (
                f"{_hours(coverage['awake_h'])} covered, {coverage['cycles']} cycles, "
                f"{coverage['non_ok']} non-ok; gaps >10 min: {gaps}; "
                f"first budget exhaustion today (UTC): {cap}"
            )
        lines.append(
            "- **Supervisor coverage: did scans run?** → adjacent heartbeats ≤3 min apart "
            f"in the window → {coverage_text}; malformed lines {coverage['malformed']} → "
            "coverage is observed cadence, not proof of machine wakefulness; unconfirmed gap causes are unknown."
        )
    lines += ["", f"Not claimed: {CAVEAT}"]
    return "\n".join(lines)


# --------------------------------------------------------------------------- the marked region


@contextmanager
def _exclusive(lock_path: Path) -> Iterator[None]:
    """An exclusive flock on a sibling file that is never unlinked, so writers agree on one inode."""
    with open(lock_path, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def replace_region(data: bytes, text: str) -> bytes:
    """``data`` with the fleet-health region set to ``text``; every other byte is kept.

    The region is the start marker through the end marker; without one the marked region is appended
    after a blank line. Half a region, a second region or reversed markers are refused rather than guessed.
    """
    region = BEGIN_MARK + b"\n" + text.strip("\n").encode("utf-8") + b"\n" + END_MARK
    starts, ends = data.count(BEGIN_MARK), data.count(END_MARK)
    if starts != ends or starts > 1:
        raise ValueError(f"fleet-health markers are unbalanced or repeated ({starts} start, {ends} end)")
    if starts == 1:
        begin = data.index(BEGIN_MARK)
        end = data.index(END_MARK)
        if end < begin:
            raise ValueError("fleet-health end marker precedes the start marker")
        return data[:begin] + region + data[end + len(END_MARK):]
    if not data:
        return region + b"\n"
    gap = b"" if data.endswith(b"\n\n") else b"\n" if data.endswith(b"\n") else b"\n\n"
    return data + gap + region + b"\n"


def write_section(path: Path, text: str) -> bool:
    """Set the fleet-health region of ``path`` to ``text``; return whether the file changed.

    Atomic (temp file in the same directory, then ``os.replace``) under an exclusive flock on
    ``<path>.lock``, so two writers cannot lose each other's update. An editor writing the file
    without this lock can still race the replace; the window is the read-to-replace gap.
    """
    target = Path(os.path.realpath(path))
    with _exclusive(target.with_name(target.name + LOCK_SUFFIX)):
        try:
            current = target.read_bytes()
        except FileNotFoundError:
            current = b""
        updated = replace_region(current, text)
        if updated == current and target.exists():
            return False
        handle, temp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
        temp = Path(temp_name)
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(updated)
                out.flush()
                os.fsync(out.fileno())
            if target.exists():
                shutil.copymode(target, temp)
            else:
                temp.chmod(0o644)
            os.replace(temp, target)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return True


# --------------------------------------------------------------------------- entry point


def _json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} is not a JSON object")
    return value


def run_fleet_report(bridge: Path, *, window_hours: float = DEFAULT_WINDOW_HOURS,
                     now: datetime | None = None, root: Path = DEFAULT_ROOT) -> tuple[dict[str, Any], str]:
    """Read keeper files and supervisor heartbeats, returning report and markdown."""
    bridge = bridge.expanduser()
    moment = now if now is not None else datetime.now(UTC)
    log_text = (bridge / QUEUE_LOG).read_text(encoding="utf-8", errors="replace")
    report = fleet_report(log_text, _json_file(bridge / QUEUE_FILE), _json_file(bridge / STATE_FILE),
                          now=moment, window_hours=window_hours)
    try:
        cycles = (root.expanduser() / "cycles-v1.jsonl").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        cycles = ""
    report["supervisor_coverage"] = supervisor_coverage(
        cycles, now=moment, window_hours=window_hours, sleep_times=_sleep_times() if cycles else ())
    return report, render_section(report, now=moment)
