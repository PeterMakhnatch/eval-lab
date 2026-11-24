from __future__ import annotations

import os
import shlex
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from campaign import evidence, fill, ledger
from campaign.plan import Plan, PlanError, arm_command, render
from campaign.runner import Stop


def _lane_lines(plan: Plan) -> list[str]:
    lines = []
    for lane in plan.lanes:
        lines.append(f"lane {lane.name}: {lane.concurrency} trials at once: {' -> '.join(lane.arms)}")
    return lines


def _arm_lines(plan: Plan, problems: list[str]) -> list[str]:
    lines = []
    for lane in plan.lanes:
        for name in lane.arms:
            arm = plan.arm(name)
            tasks = evidence.task_instances(arm.tasks)
            if not tasks:
                problems.append(f"arm {name}: {arm.tasks} holds no task")
            argv, env = arm_command(plan, name, arm.tasks, arm.attempts, lane.concurrency)
            pilot = ", pilot" if arm.pilot else ""
            lines.append(
                f"arm {name} (lane {lane.name}{pilot}): {len(tasks)} tasks x {arm.attempts} attempts = "
                f"{len(tasks) * arm.attempts} trials; reserve ${arm.reserve_usd:.2f}, rerun reserve "
                f"${arm.rerun_reserve_usd:.2f}, expected ${arm.expected_usd:.2f}"
            )
            lines.append(f"  cwd {plan.cwd}")
            lines.extend(f"  env {key}={shlex.quote(value)}" for key, value in sorted(env.items()))
            lines.append(f"  $ {shlex.join(argv)}")
    return lines


def _place_problems(plan: Plan) -> list[str]:
    problems = []
    for root in plan.cap_roots:
        if not root.is_dir() and root != plan.output_root:
            problems.append(f"cap root {root} is not a folder; the cap would miss its cost")
    if not plan.cwd.is_dir():
        problems.append(f"command cwd {plan.cwd} is not a folder")
    if not os.access(plan.harbor_program, os.X_OK):
        problems.append(f"harbor program {plan.harbor_program} is not executable")
    if not plan.ledger.parent.is_dir():
        problems.append(f"ledger folder {plan.ledger.parent} does not exist")
    return problems


def _rerun_form(plan: Plan) -> str:
    values = {
        "tasks": str(plan.state_root / "fill-tasks" / "<arm>.r1" / "m<missing>"),
        "out": str(plan.output_root / "<arm>.r1-m<missing>"), "arm": "<arm>.r1-m<missing>",
        "attempts": "<missing>", "concurrency": "<lane concurrency>",
        "harbor": str(plan.wrapper_path()), "out_root": str(plan.output_root),
    }
    return shlex.join(render(arg, values) for arg in plan.argv)


def _foreign_lines(plan: Plan, problems: list[str], now: datetime) -> list[str]:
    window = plan.foreign_reserve_window_hours
    head = (f"money lock {plan.ledger.name}.lock beside the ledger; open reserves of other writers in the ledger "
            f"(provider {plan.provider}, younger than {window:g} h) count against the balance:")
    try:
        found = ledger.foreign_open(plan.ledger, plan.id, plan.provider, now, window, plan.largest_reserve_usd())
    except ledger.LedgerError as error:
        problems.append(str(error))
        return [head, "  unreadable"]
    shown = [f"  {item.writer} {item.arm}: ${item.reserved_usd:.2f} since {item.ts_utc}"
             + (" (amount is not a number; counted as the largest reserve of this plan)" if item.guessed else "")
             for item in found[:32]]
    total = sum(item.reserved_usd for item in found)
    return [head, *(shown or ["  none"]), f"  total ${total:.2f} in {len(found)} open reserves"]


def _rules() -> list[str]:
    return [
        "rules for a paid run:",
        "  1. every other writer of this ledger writes 'experiment' and 'provider' in each reserve line",
        "  2. floor_usd covers the next reserve of every bash lane that runs beside this plan (they take no lock)",
        "  3. cap_usd = charged cost under the cap roots now (campaign status) + the budget of this run",
        "  4. start each runner with nohup; never kill a runner that prints STOPPING or WAIT",
        "  5. after a harbor-alive line, run its commands, then check `daytona sandbox list`",
    ]


def check(plan: Plan, now: datetime) -> tuple[list[str], list[str]]:
    assert now.tzinfo is not None
    problems = _place_problems(plan)
    lines = [
        f"plan {plan.path}",
        f"sha256 {plan.sha256}",
        f"campaign {plan.id}: output {plan.output_root}, state {plan.state_root}",
        f"at once: lanes {sum(lane.concurrency for lane in plan.lanes)} of max {plan.max_trials_at_once}",
        f"money: floor ${plan.floor_usd:.2f}, cap ${plan.cap_usd:.2f} of recorded cost (a seat without a "
        f"cost counts ${plan.unknown_seat_usd:.4f}), ledger {plan.ledger}, provider {plan.provider}",
        f"cap roots: {' '.join(str(root) for root in plan.cap_roots)}",
        *_foreign_lines(plan, problems, now),
        f"balance probe ({plan.probe_timeout_seconds:.0f} s): {shlex.join(plan.probe)}",
        f"watch: poll {plan.poll_seconds:.0f} s, arm limit {plan.max_arm_minutes:.0f} min, stop grace "
        f"{plan.stop_grace_seconds:.0f} s, STOP file {plan.stop_file}, stop after "
        f"{plan.no_reward_streak} finished trials in a row without a reward",
        "rerun: 1 round into <arm>.r1-m<missing>; detector: "
        + (shlex.join(plan.detector) if plan.detector else "none"),
        f"rerun form: $ {_rerun_form(plan)}",
        "environment: run passes the whole environment of the caller to each arm, plus the env lines below",
        f"harbor wrapper {plan.wrapper_path()}: 'run ARGS' becomes "
        f"{shlex.join([str(plan.harbor_program), 'run', 'ARGS', *plan.harbor_run_args])}",
        "order:",
        *(f"  {line}" for line in _lane_lines(plan)),
        *_arm_lines(plan, problems),
        f"expected ${sum(arm.expected_usd for arm in plan.arms):.2f}; reserves "
        f"${sum(arm.reserve_usd for arm in plan.arms):.2f}",
        *_rules(),
    ]
    return lines, problems


def _detected(plan: Plan, name: str) -> frozenset[str]:
    try:
        return fill.detected(plan, name)
    except Stop as stop:
        raise PlanError(stop.detail) from stop


def _merge_counts(rows: list[dict[str, Any]]) -> Counter[str]:
    merged: Counter[str] = Counter()
    for row in rows:
        merged.update(row["measured_by_instance"])
    return merged


def arm_status(plan: Plan, name: str, state: dict[str, ledger.ArmLedger]) -> dict[str, Any]:
    arm = plan.arm(name)
    found = _detected(plan, name)
    instances = evidence.task_instances(arm.tasks)
    planned = len(instances) * arm.attempts
    main = evidence.folder_counts(plan.output_root / name, found)
    folders = evidence.rerun_folders(plan.output_root, name)
    reruns = {folder.name: evidence.folder_counts(folder, found) for folder in folders}
    unmeasured = dict(main["unmeasured"])
    if planned > main["started"]:
        unmeasured["not-started"] = planned - main["started"]
    merged = _merge_counts([main, *reruns.values()])
    cost = evidence.scan_cost([folder for folder in [plan.output_root / name, *folders] if folder.is_dir()])
    outs = [name, *reruns]
    return {
        "arm": name, "lane": plan.lane_of(name).name, "pilot": arm.pilot, "planned": planned,
        "started": main["started"], "with_reward": main["with_reward"], "unmeasured": unmeasured,
        "reruns": {out: {key: row[key] for key in ("started", "with_reward", "unmeasured")}
                   for out, row in reruns.items()},
        "with_reward_all": sum(merged.values()),
        "instances_complete": sum(1 for inst in instances if merged[inst] >= arm.attempts),
        "instances": len(instances), "detected": len(found),
        "recorded_usd": cost.recorded_usd, "seats_without_cost": cost.seats_without_cost,
        "unreadable_results": cost.unreadable_results,
        "ledger": {out: state[out].record() for out in outs if out in state},
    }


def status(plan: Plan) -> dict[str, Any]:
    try:
        state = ledger.state(plan.ledger, plan.id)
        arms = [arm_status(plan, name, state) for lane in plan.lanes for name in lane.arms]
        cost = evidence.scan_cost([root for root in plan.cap_roots if root.is_dir()])
    except (ledger.LedgerError, OSError) as error:
        raise PlanError(str(error)) from error
    missing = [str(root) for root in plan.cap_roots if not root.is_dir() and root != plan.output_root]
    return {
        "campaign": plan.id, "plan_sha256": plan.sha256, "arms": arms,
        "cap": {"cap_usd": plan.cap_usd, "charged_usd": cost.charged(plan.unknown_seat_usd),
                "recorded_usd": cost.recorded_usd, "seats_without_cost": cost.seats_without_cost,
                "unreadable_results": cost.unreadable_results, "missing_roots": missing},
        "stop_file": plan.stop_file.exists(),
        "open_reservations": sorted(out for out, item in state.items() if item.open),
    }


def _kinds(unmeasured: dict[str, int]) -> str:
    return ", ".join(f"{kind} {count}" for kind, count in unmeasured.items()) or "-"


def status_text(report: dict[str, Any]) -> str:
    lines = [f"campaign {report['campaign']} plan {report['plan_sha256'][:12]}"]
    for row in report["arms"]:
        led = row["ledger"].get(row["arm"])
        led_text = "no ledger line" if led is None else (
            f"reserved {led['reserves']}, settled {led['settles']} {'/'.join(led['statuses'])}")
        lines.append(
            f"{row['arm']:<12} lane {row['lane']:<4} planned {row['planned']:>3} started {row['started']:>3} "
            f"reward {row['with_reward']:>3} unmeasured [{_kinds(row['unmeasured'])}] "
            f"${row['recorded_usd']:.4f} {led_text}"
        )
        for out, rerun in row["reruns"].items():
            lines.append(f"  {out:<14} started {rerun['started']:>3} reward {rerun['with_reward']:>3} "
                         f"unmeasured [{_kinds(rerun['unmeasured'])}]")
        lines.append(f"  instances complete {row['instances_complete']} of {row['instances']}, "
                     f"rewards with reruns {row['with_reward_all']}, detector listed {row['detected']}")
    cap = report["cap"]
    lines.append(f"cap: charged ${cap['charged_usd']:.4f} of ${cap['cap_usd']:.2f} "
                 f"(recorded ${cap['recorded_usd']:.4f}, {cap['seats_without_cost']} seats without a cost)"
                 + (f"; missing cap roots: {', '.join(cap['missing_roots'])}" if cap["missing_roots"] else ""))
    lines.append(f"STOP file present: {report['stop_file']}; open reservations: "
                 f"{', '.join(report['open_reservations']) or 'none'}")
    return "\n".join(lines) + "\n"


def merge(plan: Plan, dest: Path) -> dict[str, int]:
    dest = Path(os.path.abspath(dest))
    if dest == plan.output_root or plan.output_root in dest.parents:
        raise PlanError("DEST must be outside the output root")
    try:
        state = ledger.state(plan.ledger, plan.id)
    except ledger.LedgerError as error:
        raise PlanError(str(error)) from error
    running = sorted(out for out, item in state.items() if item.open)
    if running:
        raise PlanError(f"arms still hold open reservations: {', '.join(running)}")
    names = [name for lane in plan.lanes for name in lane.arms if not plan.arm(name).pilot]
    taken = [dest / name for name in names if (dest / name).exists()]
    if taken:
        raise PlanError(f"{taken[0]} exists; merge into a new DEST")
    linked: dict[str, int] = {}
    sets = {name: _detected(plan, name) for name in names}
    try:
        for name in names:
            linked[name] = _link_arm(plan, name, sets[name], dest / name)
    except OSError as error:
        raise PlanError(f"cannot write {dest}: {error}; DEST may hold a partial merge; remove it") from error
    return linked


def _link_arm(plan: Plan, name: str, found: frozenset[str], out: Path) -> int:
    out.mkdir(parents=True)
    folders = [plan.output_root / name, *evidence.rerun_folders(plan.output_root, name)]
    for folder in folders:
        for trial in evidence.trial_dirs(folder):
            if evidence.failure_kind(trial, found) is None:
                (out / trial.name).symlink_to(trial)
    return sum(1 for _ in out.iterdir())
