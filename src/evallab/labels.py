"""Unified human, heuristic, and model behavior labels.

Labels share one versioned envelope regardless of target granularity.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from evallab.schemas import (
    BehaviorLabel,
)
from evallab.traj import (
    TrajectoryError,
    TrajectoryOutline,
)

TRAJECTORY_BEHAVIOR_TAXONOMY = "trajectory_behavior/v1"


def _label_id(
    *, target_type: str, target_id: str, taxonomy: str, provenance: str, author: str
) -> str:
    value = "\0".join((target_type, target_id, taxonomy, provenance, author)).encode()
    return f"sha256:{hashlib.sha256(value).hexdigest()}"
def _normalize_digest(value: str | None) -> str | None:
    if not value:
        return None
    raw = str(value)
    if raw.startswith("sha256:"):
        raw = raw[7:]
    if len(raw) != 64 or any(char not in "0123456789abcdef" for char in raw.lower()):
        raise TrajectoryError(f"invalid sha256 digest: {value!r}")
    return f"sha256:{raw.lower()}"


def propose_heuristic_label(outline: TrajectoryOutline) -> BehaviorLabel:
    label = "unclassified"
    rationale = "No specific heuristic triggered"
    if outline.status != "featured":
        label = "missing_data"
        rationale = f"Trajectory unavailable: {outline.unavailable_reason}"
    elif outline.loop_suspicion.detected:
        label = "tool_use_loop"
        rationale = (
            f"Loop suspicion detected ({outline.loop_suspicion.score:.2f}): "
            f"{', '.join(outline.loop_suspicion.reasons)}"
        )
    elif outline.agent_steps == 0:
        label = "setup_failure"
        rationale = "Zero agent execution steps observed"
    elif outline.step_to_first_edit is None and outline.total_tool_calls > 0:
        label = "planning_no_edit"
        rationale = "Tool calls occurred but zero file modifications were performed"
    elif outline.total_errors > 0 and outline.recovery_count > 0 and outline.primary_reward == 1.0:
        label = "recovered_success"
        rationale = f"Recovered from {outline.total_errors} error(s) and completed task"
    elif outline.total_errors > 0 and outline.primary_reward == 0.0:
        label = "unrecovered_error"
        rationale = f"Encountered {outline.total_errors} error(s) without successful completion"
    elif outline.primary_reward == 1.0:
        label = "clean_success"
        rationale = "Task passed without notable errors or loops"
    else:
        label = "failed_verification"
        rationale = "Execution completed but verifier scored zero"
    author = "evallab-heuristic-v1"
    return BehaviorLabel(
        label_id=_label_id(
            target_type="trajectory", target_id=outline.trial_id,
            taxonomy=TRAJECTORY_BEHAVIOR_TAXONOMY,
            provenance="heuristic", author=author,
        ),
        target_type="trajectory",
        target_id=outline.trial_id,
        job_id=outline.job_id,
        trial_id=outline.trial_id,
        trial_name=outline.trial_name,
        task_name=outline.task_name,
        taxonomy=TRAJECTORY_BEHAVIOR_TAXONOMY,
        label=label,
        rationale=rationale,
        provenance="heuristic",
        author=author,
        created_at=datetime.now(UTC),
        source_sha256=_normalize_digest(outline.source_sha256),
    )

