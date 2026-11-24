from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tidegauge import render
from tidegauge.load import walk
from tidegauge.model import Problem, Trial
from tidegauge.timeline import LANE_GAP_SECONDS, Span, Window, lanes, peak, rerun_of, stamp

DESCRIPTION = f"""``tidegauge process``: how did the runs go over time?

Each job directory is 1 arm. The report reads started_at and finished_at of
every trial and gives the window of each arm, the order of the arms, the lanes
(arms that ran one after the other; a gap of more than {LANE_GAP_SECONDS / 60:.0f} minutes starts a new lane),
the peak number of trials at once, and the reruns (job directories named
<arm>.rN-*). --also adds trials of other runs: they count toward the peak while
the given jobs run, but get no rows of their own.

--plan reads a JSON file such as
  {{"max_at_once": 8, "lanes": {{"a": ["solo-24", "zones-8"], "b": ["team-8"]}}, "reruns": false}}
(every key is optional, at least 1 is needed) and lists each deviation: more
trials at once than max_at_once (counted with --also), arms of a lane in a
different order or at the same time, planned arms with no trial, arms in no lane,
and reruns when "reruns" is false.

Exit status: 0 = no deviation, 1 = at least 1 deviation from --plan.
"""
HELP = "show the timeline of runs: arm windows and order, lanes, peak trials at once, reruns, plan deviations"
PLAN_KEYS = frozenset({"max_at_once", "lanes", "reruns"})
MAX_LANES = 256
MAX_ARMS = 10_000


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("paths", nargs="+", metavar="JOB", help="a Harbor job directory, or a directory of them")
    parser.add_argument(
        "--also",
        action="append",
        default=[],
        metavar="ROOT",
        help="a directory of other runs whose trials count toward the peak (repeatable)",
    )
    parser.add_argument("--plan", metavar="FILE", help="a JSON plan to compare with (see above)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")


@dataclass
class Timed:
    name: str
    spans: list[Span] = field(default_factory=list)
    untimed: int = 0

    def window(self) -> Window:
        assert self.spans
        return Window(self.name, min(s.start for s in self.spans), max(s.end for s in self.spans))


def _collect(roots: Iterable[str], own: bool, skip: set[Path]) -> tuple[dict[str, Timed], set[Path]]:
    arms: dict[str, Timed] = {}
    seen: set[Path] = set()
    for root in roots:
        path = Path(root)
        if not path.is_dir():
            raise FileNotFoundError(f"not a directory: {path}")
        for item in walk(path):
            if isinstance(item, Trial):
                where = item.path.resolve()
                if where in skip or where in seen:
                    continue
                seen.add(where)
                label = item.path.parent.name if own else f"also:{item.path.parent.name}"
                arm = arms.setdefault(label, Timed(label))
                start, end = item.started_at, item.finished_at
                if start is not None and end is not None and end > start:
                    arm.spans.append(Span(arm.name, start, end, own))
                else:
                    arm.untimed += 1
            elif own and isinstance(item, Problem) and "did not finish" in item.reason:
                arms.setdefault(item.path.parent.name, Timed(item.path.parent.name)).untimed += 1
    assert len(arms) <= MAX_ARMS
    return arms, seen


def read_plan(path: str) -> dict[str, Any]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        raise ValueError(f"cannot read --plan {path}: {error.strerror or error}") from None
    except ValueError as error:
        raise ValueError(f"--plan {path} is not JSON: {error}") from None
    if not isinstance(data, dict) or not data or set(data) - PLAN_KEYS:
        raise ValueError(f"--plan must be a JSON object with some of the keys {', '.join(sorted(PLAN_KEYS))}")
    limit = data.get("max_at_once")
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ValueError("--plan: max_at_once must be a whole number of 1 or more")
    if "reruns" in data and not isinstance(data["reruns"], bool):
        raise ValueError("--plan: reruns must be true or false")
    planned = data.get("lanes", {})
    if not isinstance(planned, dict) or len(planned) > MAX_LANES or not all(
        isinstance(arms, list) and arms and all(isinstance(a, str) and a for a in arms) for arms in planned.values()
    ):
        raise ValueError("--plan: lanes must map each lane name to a list of arm names")
    names = [a for arms in planned.values() for a in arms]
    if len(set(names)) != len(names):
        raise ValueError("--plan: an arm appears more than once in lanes")
    return data


def _lane_deviations(name: str, planned: list[str], windows: Mapping[str, Window]) -> list[dict]:
    out: list[dict[str, Any]] = [
        {"kind": "missing-arm", "lane": name, "arm": arm, "message": f"Lane {name}: planned arm {arm} has no timed trial."}
        for arm in planned
        if arm not in windows
    ]
    present = [arm for arm in planned if arm in windows]
    observed = sorted(present, key=lambda arm: (windows[arm].start, arm))
    if observed != present:
        out.append({
            "kind": "arm-order",
            "lane": name,
            "observed": observed,
            "planned": present,
            "message": f"Lane {name} ran {', '.join(observed)}; the plan says {', '.join(present)}.",
        })
    for before, after in zip(present, present[1:], strict=False):
        if windows[after].start < windows[before].end:
            out.append({
                "kind": "lane-overlap",
                "lane": name,
                "arms": [before, after],
                "message": f"Lane {name}: {after} started at {stamp(windows[after].start)} "
                f"before {before} ended at {stamp(windows[before].end)}.",
            })
    return out


def deviations(plan: Mapping[str, Any], report: Mapping[str, Any], windows: Mapping[str, Window]) -> list[dict]:
    out: list[dict] = []
    limit, top = plan.get("max_at_once"), report["peak_with_also"] or report["peak"]
    if limit is not None and top["count"] > limit:
        out.append({
            "kind": "too-many-at-once",
            "count": top["count"],
            "at": top["at"],
            "limit": limit,
            "message": f"{top['count']} trials ran at once at {top['at']} ({report['peak']['count']} at most inside "
            f"the given jobs; running then: {_running(top['running'])}). The plan allows {limit}.",
        })
    planned = plan.get("lanes", {})
    for name, arms in planned.items():
        out.extend(_lane_deviations(name, arms, windows))
    if planned:
        known = {arm for arms in planned.values() for arm in arms}
        for row in report["arms"]:
            base = row["rerun_of"] or row["name"]
            if base not in known:
                out.append({"kind": "unplanned-arm", "arm": row["name"], "message": f"Arm {row['name']} is in no lane of the plan."})
    if plan.get("reruns") is False:
        for row in report["reruns"]:
            out.append({
                "kind": "rerun",
                "arm": row["name"],
                "message": f"{row['name']} reran {row['arm']} (round {row['round']}, {row['trials']} trials); the plan has no reruns.",
            })
    return out


def _running(running: Mapping[str, int]) -> str:
    return ", ".join(f"{name} ×{count}" for name, count in list(running.items())[:8]) or "none"


def _arm_rows(arms: Mapping[str, Timed], lane_of: Mapping[str, int]) -> list[dict]:
    rows = []
    for arm in arms.values():
        window = arm.window() if arm.spans else None
        alone = peak(arm.spans, lambda s: True, lambda s: True)
        rerun = rerun_of(arm.name)
        rows.append({
            "name": arm.name,
            "rerun_of": rerun[0] if rerun else None,
            "round": rerun[1] if rerun else None,
            "lane": lane_of.get(arm.name),
            "trials": len(arm.spans) + arm.untimed,
            "untimed": arm.untimed,
            "started": stamp(window.start) if window else None,
            "finished": stamp(window.end) if window else None,
            "peak": alone.count,
            "peak_at": stamp(alone.at),
        })
    return sorted(rows, key=lambda r: (r["started"] is None, r["started"] or "", r["name"]))


def build(paths: list[str], also: list[str], plan: Mapping[str, Any] | None) -> dict:
    own, seen = _collect(paths, True, set())
    others, _ = _collect(also, False, seen)
    timed = {name: arm for name, arm in own.items() if arm.spans}
    if not timed:
        raise ValueError(f"no trial with start and end times in: {', '.join(paths)}")
    windows = {name: arm.window() for name, arm in timed.items()}
    grouped = lanes(list(windows.values()))
    lane_of = {w.name: i for i, lane in enumerate(grouped, start=1) for w in lane}
    mine = [s for arm in timed.values() for s in arm.spans]
    theirs = [s for arm in others.values() for s in arm.spans]
    spans = mine + theirs

    def everything(span: Span) -> bool:
        return True

    def inside(span: Span) -> bool:
        return span.own

    lane_rows = []
    for index, lane in enumerate(grouped, start=1):
        members = {w.name for w in lane}

        def gate(span: Span, members: set[str] = members) -> bool:
            return span.own and span.label in members

        lane_rows.append({
            "lane": index,
            "arms": [w.name for w in lane],
            "started": stamp(min(w.start for w in lane)),
            "finished": stamp(max(w.end for w in lane)),
            "peak": peak(spans, gate, gate).as_dict(),
            "peak_with_others": peak(spans, everything, gate).as_dict(),
        })
    rows = _arm_rows(own, lane_of)
    report: dict[str, Any] = {
        "command": "process",
        "jobs": paths,
        "also": also,
        "lane_gap_seconds": LANE_GAP_SECONDS,
        "trials": len(mine),
        "also_trials": len(theirs),
        "arms": rows,
        "lanes": lane_rows,
        "peak": peak(spans, inside, inside).as_dict(),
        "peak_with_also": peak(spans, everything, inside).as_dict() if also else None,
        "reruns": [
            {"name": r["name"], "arm": r["rerun_of"], "round": r["round"], "trials": r["trials"]}
            for r in rows
            if r["rerun_of"]
        ],
        "plan": dict(plan) if plan is not None else None,
    }
    report["deviations"] = deviations(plan, report, windows) if plan is not None else []
    return report


def text(report: Mapping[str, Any]) -> str:
    rows = [
        [render.shorten(r["name"], 32), r["lane"] or "-", r["trials"], _clock(r["started"]), _clock(r["finished"]), r["peak"]]
        for r in report["arms"]
    ]
    out = [render.table(["arm", "lane", "trials", "start (UTC)", "end (UTC)", "peak"], rows, right=(1, 2, 5)), ""]
    lanes_rows = [
        [
            lane["lane"],
            render.count(len(lane["arms"]), "arm"),
            _clock(lane["started"]),
            _clock(lane["finished"]),
            lane["peak"]["count"],
            f"{lane['peak_with_others']['count']} at {_clock(lane['peak_with_others']['at'])}",
        ]
        for lane in report["lanes"]
    ]
    headers = ["lane", "arms", "start (UTC)", "end (UTC)", "peak", "peak with all runs"]
    out += [render.table(headers, lanes_rows, right=(0, 4)), ""]
    top = report["peak"]
    out.append(f"Peak inside the given jobs: {top['count']} trials at once at {_clock(top['at'])} ({_running(top['running'])}).")
    if report["peak_with_also"] is not None:
        both = report["peak_with_also"]
        out.append(
            f"Peak with the {report['also_trials']} trials under --also, while the given jobs ran: {both['count']} at "
            f"{_clock(both['at'])} ({_running(both['running'])})."
        )
    if report["reruns"]:
        listed = "; ".join(f"{r['name']} (round {r['round']} of {r['arm']}, {r['trials']} trials)" for r in report["reruns"][:12])
        out.append(f"Reruns: {listed}.")
    if report["plan"] is not None:
        out.append(f"Plan deviations: {len(report['deviations'])}.")
        out += [f"  {d['kind']}: {d['message']}" for d in report["deviations"]]
    return "\n".join(out)


def _clock(value: str | None) -> str:
    return "-" if value is None else value.replace("T", " ")


def run(args: argparse.Namespace) -> int:
    plan = read_plan(args.plan) if args.plan else None
    report = build(args.paths, args.also, plan)
    print(render.to_json(report) if args.json else text(report))
    return 1 if report["deviations"] else 0
