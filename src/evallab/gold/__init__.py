"""Action-labelled views of the existing IssueBank and canonical trajectory IR."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

from evallab.failure_library.bank import BankContext, IssueBank, IssueItem
from evallab.gold.exclusions import source_exclusion
from evallab.trace_anatomy import RefusedRun, load_atif_file
from evallab.trajectory_ir import TrajectoryIR, build_trajectory_ir

SOURCES = ("goodfire-action-v1", "impossible-task-final", "goodfire-action-v2",
           "terminal-wrench-action-v2", "cheatbench-action-v2", "c1c5-action-v2")


def load_rows(repo_root: Path, state_root: Path | None = None) -> list[IssueItem]:
    """Return source/protocol-specific rows, including attempted and unjudged rows."""
    for source in SOURCES:
        exclusion = source_exclusion(source)
        if exclusion:
            raise ValueError(f"Excluded gold source {exclusion['name']}: {exclusion['reason']}")
    bank = IssueBank(repo_root, state_root=state_root)
    rows = list(bank.iter_items(SOURCES))
    ctx = BankContext(repo_root, state_root=bank.state_root)
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if row.protocol is None and row.source in {"goodfire-action-v1", "impossible-task-final"}:
            row.protocol = "action-v1"
        if row.protocol is None:
            raise ValueError(f"Gold row has no action protocol: {row.source}/{row.subject_id}")
        key = (row.source, row.subject_id)
        if key in seen:
            raise ValueError(f"Duplicate gold run: {key}")
        seen.add(key)
        row.split = ctx.strict_split(
            row.split, item_id=row.subject_id, task_id=row.task_id, task_digest=row.task_digest
        )
    return rows


def load_trajectory(row: IssueItem, *, repo_root: Path) -> TrajectoryIR:
    """Load the hash-bound actor view and mark only resolved source action steps."""
    exclusion = row.gold_exclusion()
    if exclusion:
        raise ValueError(f"Excluded gold source {exclusion['name']}: {exclusion['reason']}")
    split = BankContext(repo_root).strict_split(
        row.split, item_id=row.subject_id, task_id=row.task_id, task_digest=row.task_digest
    )
    if split != "DEV":
        raise RefusedRun("Gold trajectory is not admitted DEV")
    if not row.trajectory_available or not row.trajectory_ref or not row.trajectory_digest:
        raise ValueError(f"Trajectory unavailable: {row.subject_id}")
    path = Path(row.trajectory_ref)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != row.trajectory_digest:
        raise ValueError(f"Trajectory hash differs: {row.subject_id}")
    loaded = load_atif_file(path)
    ir = build_trajectory_ir(dict(loaded.doc), source_path=str(path), source_sha256=digest)
    cheating: set[int] = set()
    for ref in row.step_locators:
        if ref.event_kind == "artifact":
            continue
        if ref not in ir.source_event_map:
            raise ValueError(f"Citation absent from trajectory map: {ref.source_event_id}")
        if ref.normalized_step_id is not None and row.label_state == "executed-positive":
            step = next(step for step in ir.steps if step.step_id == ref.normalized_step_id)
            if not step.tool_calls or ref.resolution == "unmatched-result":
                raise ValueError(f"Citation is not an action: {ref.source_event_id}")
            cheating.add(step.step_id)
    if row.label_state == "executed-positive" and not cheating:
        raise ValueError(f"Positive has no resolved cheating step: {row.subject_id}")
    return replace(ir, trajectory_id=row.subject_id,
                   steps=tuple(replace(step, is_cheating=step.step_id in cheating) for step in ir.steps))
