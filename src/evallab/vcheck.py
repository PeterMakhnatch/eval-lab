"""VerifierCheck (``evallab vcheck``): red/blue verifier-audit orchestration.

Faithful rebuild of Tokenless EnvCheck's red/blue verifier-audit architecture:
per-task red agents read the grader, launch hinted blue-solver probes, and
construct wrong submissions that the task's own verifier scores. Harness-owned
controls (baseline / honest-legit / broken-negative), frozen requirement maps,
a blind adjudicator input (promoted hypotheses), hash-pinned manifests, and
cross-wave broadcast hints.

All agents run GLM 5.3 (red may run Flash) at temperature 0.1, max_tokens 4096,
reasoning_effort low (:data:`MODEL_DEFAULTS`); red loops are bounded by
:data:`RED_BUDGETS`.

Only the orchestrator (:func:`run_wave`, via :func:`run_controls`) grades the
harness controls. Red agents NEVER call :func:`run_controls` directly; they
receive a caller-supplied ``grade_fn`` closure instead.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

SubmissionKind = Literal["patch", "file", "json"]
SubmissionBase = Literal["oracle", "environment"]
GradeStatus = Literal["ok", "mismatch", "infra"]
ControlVerdict = Literal["ok", "task_broken"]

#: Red-loop budgets: at most 30 agent steps, 25 graded submissions, 6 blue probes.

#: Shared model defaults for every vcheck agent.


# --------------------------------------------------------------------------- #
# Submissions and grades
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Submission:
    """One candidate input for the task's own verifier.

    ``kind`` selects how ``content`` is applied to ``path`` (absolute container
    path): ``patch`` applies a unified diff, ``file``/``json`` write the exact
    bytes. ``base`` selects what the submission builds on: ``oracle`` runs the
    reference solution first, ``environment`` grades the untouched environment.
    Empty ``content`` is a no-op on either base.
    """

    kind: SubmissionKind
    path: str
    content: bytes
    base: SubmissionBase


def oracle_submission() -> Submission:
    """The reference solution as-is (baseline control input)."""
    return Submission(kind="patch", path="", content=b"", base="oracle")


def blank_submission() -> Submission:
    """An empty submission on the untouched environment (negative control input)."""
    return Submission(kind="patch", path="", content=b"", base="environment")


@dataclass
class GradeResult:
    """The task's own verifier scored one submission."""

    reward: float | None
    status: GradeStatus
    verifier_outputs: dict[str, Any]
    job_dir: str | None
    script: str


#: Grades one submission with the task's verifier: ``(package, submission)``.
#: Production calls go through ``grade_submission``. The default is free local
#: Docker and no model; Modal requires a CloudSpendGate. Tests inject fakes.
GradeFn = Callable[[str | Path, Submission], GradeResult]


def grade_submission(
    package: str | Path,
    submission: Submission,
    *,
    repo_root: str | Path,
    run_dir: str | Path,
    timeout_seconds: int = 1_800,
    environment: str = "docker",
    docker_platform: str | None = None,
    spend_gate: object | None = None,
    prelude: str = "",
    modal_app_name: str | None = None,
    modal_resource_policy: Literal["limit"] | None = None,
    modal_sandbox_timeout_seconds: int | None = None,
    modal_sandbox_create_max_attempts: Literal[1] | None = None,
) -> GradeResult:
    """Grade one submission with the task's own verifier (re-export).

    The implementation lives in :mod:`evallab.verifier_mutation` next to the
    solution-override machinery it reuses; this lazy re-export keeps the
    ``evallab.vcheck`` surface complete without a module-level import cycle.
    """
    from evallab.verifier_mutation import grade_submission as _grade

    return _grade(
        package,
        submission,
        repo_root=repo_root,
        run_dir=run_dir,
        timeout_seconds=timeout_seconds,
        environment=environment,
        docker_platform=docker_platform,
        spend_gate=spend_gate,
        prelude=prelude,
        modal_app_name=modal_app_name,
        modal_resource_policy=modal_resource_policy,
        modal_sandbox_timeout_seconds=modal_sandbox_timeout_seconds,
        modal_sandbox_create_max_attempts=modal_sandbox_create_max_attempts,
    )


# --------------------------------------------------------------------------- #
# Frozen requirements and campaign manifests
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Hypothesis lifecycle
# --------------------------------------------------------------------------- #


_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"promoted", "attached", "merged", "rejected", "inconclusive"})
}


# --------------------------------------------------------------------------- #
# Harness controls (orchestrator-only)
# --------------------------------------------------------------------------- #


@dataclass
class ControlReport:
    """Harness-owned controls for one task."""

    task: str
    baseline: GradeResult
    legit: GradeResult
    negative: GradeResult
    verdict: ControlVerdict
    reason: str


def run_controls(
    package: str | Path,
    grade_fn: GradeFn,
    honest: Submission | None = None,
    task: str | None = None,
) -> ControlReport:
    """Grade the harness controls: baseline (oracle, expect 1), legit (honest
    solve where available, else oracle), negative (empty submission, expect 0).

    Returns ``task_broken`` unless the oracle scores 1 and the empty
    submission scores 0; infra/unscored controls are also ``task_broken``.
    Orchestrator-only: red agents NEVER call this directly.
    """
    name = task if task is not None else Path(package).name
    baseline = grade_fn(package, oracle_submission())
    legit = grade_fn(package, honest) if honest is not None else baseline
    negative = grade_fn(package, blank_submission())

    verdict: ControlVerdict = "ok"
    if baseline.status == "infra" or baseline.reward is None:
        verdict = "task_broken"
        reason = f"baseline unscored (status={baseline.status}); verifier gave no signal"
    elif negative.status == "infra" or negative.reward is None:
        verdict = "task_broken"
        reason = f"negative control unscored (status={negative.status}); verifier gave no signal"
    elif (baseline.reward or 0) < 1:
        verdict = "task_broken"
        reason = f"oracle earned {baseline.reward:g}, expected 1; task or verifier is broken"
    elif (negative.reward or 0) > 0:
        verdict = "task_broken"
        reason = f"empty submission earned {negative.reward:g}, expected 0; verifier is broken"
    else:
        legit_reward = "unscored" if legit.reward is None else f"{legit.reward:g}"
        reason = f"oracle=1, empty=0; legit={legit_reward}"
    return ControlReport(
        task=name,
        baseline=baseline,
        legit=legit,
        negative=negative,
        verdict=verdict,
        reason=reason,
    )


# --------------------------------------------------------------------------- #
# Wave orchestration and broadcast synthesis
# --------------------------------------------------------------------------- #


#: Per-task red audit: ``(task_id, broadcast_hints) -> hypotheses``. The
#: factory closes over whatever the agent needs (client, budget, grade_fn).


