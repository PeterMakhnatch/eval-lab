from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from campaign import evidence, ledger
from campaign.plan import RERUN_TAG, Arm, Lane, Plan, render
from campaign.runner import LaneRun, Runner, Step, Stop, write_atomic
from campaign.system import System

MAX_DETECTOR_LINES = 100_000


def record_path(plan: Plan, arm: str) -> Path:
    return plan.fill_dir() / f"{arm}.r1.json"


def load_record(plan: Plan, arm: str) -> dict[str, Any] | None:
    path = record_path(plan, arm)
    if not path.exists():
        return None
    record = evidence.read_json(path)
    shaped = (
        isinstance(record, dict) and record.get("arm") == arm
        and isinstance(record.get("detected"), list) and isinstance(record.get("groups"), list)
        and all(isinstance(name, str) for name in record["detected"]) and len(record["groups"]) <= 1000
    )
    if not shaped:
        raise Stop("fill-record", f"{path} is damaged; repair it by hand")
    assert isinstance(record, dict)
    for group in record["groups"]:
        if not _group_ok(group, arm):
            raise Stop("fill-record", f"{path} holds a damaged group; repair it by hand")
    return record


def _group_ok(group: Any, arm: str) -> bool:
    if not isinstance(group, dict) or not isinstance(group.get("out"), str):
        return False
    attempts, tasks, instances = group.get("attempts"), group.get("tasks"), group.get("instances")
    return (
        group["out"] == f"{arm}{RERUN_TAG}{attempts}" and type(attempts) is int and attempts >= 1
        and isinstance(tasks, str) and tasks.startswith("/") and isinstance(instances, list)
        and all(isinstance(name, str) for name in instances)
    )


def detected(plan: Plan, arm: str) -> frozenset[str]:
    record = load_record(plan, arm)
    return frozenset(str(name) for name in record["detected"]) if record else frozenset()


def run_detector(system: System, plan: Plan, arm_dir: Path, owner: int) -> list[str]:
    if not plan.detector:
        return []
    if not arm_dir.is_dir():
        return []
    argv = [render(arg, {"arm_dir": str(arm_dir)}) for arg in plan.detector]
    code, text = system.capture(argv, plan.detector_timeout_seconds, owner)
    if code != 0:
        raise Stop("detector", f"the detector ended with {code} on {arm_dir}")
    names: list[str] = []
    lines = text.splitlines()
    if len(lines) > MAX_DETECTOR_LINES:
        raise Stop("detector", f"the detector printed more than {MAX_DETECTOR_LINES} lines")
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except (ValueError, RecursionError):
            raise Stop("detector", f"the detector printed invalid JSON ({len(line.encode('utf-8'))} bytes)") from None
        trial = item.get("trial") if isinstance(item, dict) else None
        if trial is None:
            continue
        if not isinstance(trial, str) or not evidence.is_trial(arm_dir / trial):
            raise Stop("detector", f"the detector printed a bad trial name ({len(line.encode('utf-8'))} bytes)")
        names.append(trial)
    return sorted(set(names))


def groups_for(plan: Plan, arm: Arm, found: frozenset[str]) -> list[dict[str, Any]]:
    counts = evidence.folder_counts(plan.output_root / arm.name, found)["measured_by_instance"]
    by_missing: dict[int, list[str]] = {}
    for name in evidence.task_instances(arm.tasks):
        missing = arm.attempts - counts.get(name, 0)
        if missing > 0:
            by_missing.setdefault(missing, []).append(name)
    stage = plan.state_root / "fill-tasks" / f"{arm.name}.r1"
    return [
        {"out": f"{arm.name}{RERUN_TAG}{missing}", "attempts": missing,
         "tasks": str(stage / f"m{missing}"), "instances": names}
        for missing, names in sorted(by_missing.items())
    ]


def stage_tasks(arm: Arm, groups: list[dict[str, Any]]) -> None:
    single = (arm.tasks / "task.toml").is_file()
    for group in groups:
        target = Path(group["tasks"])
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for name in group["instances"]:
            source = arm.tasks if single else arm.tasks / name
            shutil.copytree(source, target / name, symlinks=True)


def plan_record(plan: Plan, system: System, owner: int, arm: Arm) -> dict[str, Any]:
    found = run_detector(system, plan, plan.output_root / arm.name, owner)
    groups = groups_for(plan, arm, frozenset(found))
    return {"arm": arm.name, "plan_sha256": plan.sha256, "planned_utc": system.utc(),
            "detected": found, "groups": groups}


def eligible(plan: Plan, arm: Arm) -> str:
    if arm.pilot:
        return "a pilot arm is never rerun"
    if evidence.rerun_folders(plan.output_root, arm.name):
        return "it has a rerun folder but no rerun record; there is 1 rerun round only"
    try:
        state = ledger.state(plan.ledger, plan.id).get(arm.name)
    except ledger.LedgerError as error:
        raise Stop("ledger", str(error)) from error
    if state is not None and state.open:
        return "its first run has no settle line yet"
    never = state is None or all(status == "spawn-failed" for status in state.statuses)
    if never and not (plan.output_root / arm.name).exists():
        return "its first run has not started"
    return ""


def expand_rerun(runner: Runner, run: LaneRun, arm_name: str) -> list[Step]:
    plan = runner.plan
    arm = plan.arm(arm_name)
    record = load_record(plan, arm.name)
    if record is None:
        reason = eligible(plan, arm)
        if reason:
            runner.say("SKIP", run.lane.name, f"{arm.name}: {reason}")
            return []
        record = plan_record(plan, runner.system, runner.owner, arm)
        try:
            stage_tasks(arm, record["groups"])
            write_atomic(record_path(plan, arm.name), json.dumps(record, indent=1) + "\n")
        except OSError as error:
            raise Stop("fill-record", f"cannot stage the rerun of {arm.name}: {error}") from error
    steps = [
        Step(group["out"], arm.name, Path(group["tasks"]), group["attempts"], arm.rerun_reserve_usd, False)
        for group in record["groups"]
    ]
    shown = ", ".join(f"{step.out} ({step.attempts} attempts)" for step in steps) or "nothing to rerun"
    runner.say("PLAN", run.lane.name, f"{arm.name}: rerun round 1: {shown}; "
                                      f"{len(record['detected'])} trials listed by the detector")
    return steps


def dry_run(plan: Plan, system: System, owner: int, lanes: list[Lane]) -> list[str]:
    lines = []
    for lane in lanes:
        for name in lane.arms:
            arm = plan.arm(name)
            record = load_record(plan, name)
            reason = "" if record is not None else eligible(plan, arm)
            if reason:
                lines.append(f"{name}: skip: {reason}")
                continue
            source = "recorded" if record is not None else "planned now, nothing written"
            record = record or plan_record(plan, system, owner, arm)
            groups = ", ".join(
                f"{group['out']} {len(group['instances'])} tasks x {group['attempts']}" for group in record["groups"]
            )
            lines.append(f"{name}: {source}; detector listed {len(record['detected'])}; "
                         f"rerun: {groups or 'none'}")
    return lines
