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

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

SubmissionKind = Literal["patch", "file", "json"]
SubmissionBase = Literal["oracle", "environment"]
GradeStatus = Literal["ok", "mismatch", "infra"]
HypothesisStatus = Literal[
    "candidate", "promoted", "attached", "merged", "rejected", "inconclusive"
]
ControlVerdict = Literal["ok", "task_broken"]

#: Red-loop budgets: at most 30 agent steps, 25 graded submissions, 6 blue probes.
RED_BUDGETS: dict[str, int] = {"max_steps": 30, "max_grades": 25, "max_probes": 6}

#: Shared model defaults for every vcheck agent.
MODEL_DEFAULTS: dict[str, Any] = {
    "family": "glm-5.3",
    "temperature": 0.1,
    "max_tokens": 4096,
    "reasoning_effort": "low",
}


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
#: Production calls go through ``grade_submission`` (free local Docker, no
#: model); tests inject fakes. A package-bound one-arg wrapper is equivalent.
GradeFn = Callable[[str | Path, Submission], GradeResult]


def grade_submission(
    package: str | Path,
    submission: Submission,
    *,
    repo_root: str | Path,
    run_dir: str | Path,
    timeout_seconds: int = 1_800,
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
    )


# --------------------------------------------------------------------------- #
# Frozen requirements and campaign manifests
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RequirementItem:
    """One frozen behavioral requirement of a task's verifier."""

    req_id: str
    text: str
    covered_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RequirementMap:
    """A task's frozen requirement set; ``sha256`` pins its exact content."""

    task: str
    sha256: str
    items: list[RequirementItem]

    @classmethod
    def freeze(cls, task: str, items: Sequence[RequirementItem]) -> RequirementMap:
        """Pin ``items``: any text/id/coverage change yields a new ``sha256``."""
        payload = json.dumps(
            [
                {"req_id": item.req_id, "text": item.text, "covered_by": list(item.covered_by)}
                for item in items
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return cls(task=task, sha256=digest, items=list(items))


@dataclass(frozen=True)
class CampaignManifest:
    """Immutable campaign pin: benchmark, tasks, frozen maps, models, budgets, prompts."""

    campaign_id: str
    benchmark: str
    pin: str
    tasks: list[str]
    requirement_maps: list[RequirementMap]
    models: dict[str, Any]
    budgets: dict[str, Any]
    prompt_shas: dict[str, str]


def manifest_digest(manifest: CampaignManifest) -> str:
    """Hash-pin a manifest; any requirement-map change yields a new digest."""
    payload = json.dumps(
        {
            "campaign_id": manifest.campaign_id,
            "benchmark": manifest.benchmark,
            "pin": manifest.pin,
            "tasks": list(manifest.tasks),
            "requirement_maps": [
                {
                    "task": mapping.task,
                    "sha256": mapping.sha256,
                    "items": [
                        {
                            "req_id": item.req_id,
                            "text": item.text,
                            "covered_by": list(item.covered_by),
                        }
                        for item in mapping.items
                    ],
                }
                for mapping in manifest.requirement_maps
            ],
            "models": manifest.models,
            "budgets": manifest.budgets,
            "prompt_shas": manifest.prompt_shas,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Hypothesis lifecycle
# --------------------------------------------------------------------------- #


_ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "candidate": frozenset({"promoted", "attached", "merged", "rejected", "inconclusive"})
}


@dataclass
class Hypothesis:
    """One suspected grader defect, from candidate to a terminal disposition."""

    id: str
    task: str
    requirement_ids: list[str]
    statement: str
    status: HypothesisStatus = "candidate"
    evidence: list[Any] = field(default_factory=list)
    history: list[dict[str, str]] = field(default_factory=list)


def raise_hypothesis(
    *,
    id: str,
    task: str,
    statement: str,
    requirement_ids: Sequence[str] = (),
    evidence: Sequence[Any] = (),
    note: str = "",
) -> Hypothesis:
    """Raise a new candidate hypothesis with its genesis history entry."""
    return Hypothesis(
        id=id,
        task=task,
        requirement_ids=list(requirement_ids),
        statement=statement,
        status="candidate",
        evidence=list(evidence),
        history=[{"from": "genesis", "to": "candidate", "note": note}],
    )


def transition_hypothesis(
    hypothesis: Hypothesis, to: HypothesisStatus, note: str = ""
) -> Hypothesis:
    """Move a candidate to promoted|attached|merged|rejected|inconclusive.

    Terminal dispositions have no outgoing transitions. Every move appends a
    ``{from, to, note}`` history entry.
    """
    allowed = _ALLOWED_TRANSITIONS.get(hypothesis.status, frozenset())
    if to not in allowed:
        raise ValueError(
            f"hypothesis {hypothesis.id}: cannot transition "
            f"{hypothesis.status} -> {to} (allowed: {sorted(allowed) or 'none'})"
        )
    hypothesis.history.append({"from": hypothesis.status, "to": to, "note": note})
    hypothesis.status = to
    return hypothesis


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
RedFactory = Callable[[str, Sequence[str]], list["Hypothesis"]]


@dataclass
class WaveResult:
    """One orchestrated wave: collected hypotheses, next-wave hints, skips."""

    campaign_id: str
    hypotheses: list[Hypothesis]
    broadcast: list[str]
    skipped_task_broken: list[str]


def _defect_key(evidence: Sequence[Any]) -> tuple[str, str] | None:
    """(family, class) of the first evidence item that carries both, else None."""
    for item in evidence:
        if not isinstance(item, Mapping):
            continue
        defect = item.get("defect")
        node = defect if isinstance(defect, Mapping) else item
        family, cls = node.get("family"), node.get("class")
        if isinstance(family, str) and family and isinstance(cls, str) and cls:
            return (family, cls)
    return None


def synthesize_broadcast(hypotheses: Sequence[Hypothesis]) -> list[str]:
    """Hint strings for the next wave from confirmed (promoted) patterns.

    Hypotheses promoted by the blind adjudicator group by defect
    ``(family, class)`` from their evidence; every class with 2+ confirmations
    emits one hint: ``in this universe, verifiers of family F fail to check C
    -- test whether this task's verifier also fails``.
    """
    grouped: dict[tuple[str, str], int] = {}
    for hypothesis in hypotheses:
        if hypothesis.status != "promoted":
            continue
        key = _defect_key(hypothesis.evidence)
        if key is not None:
            grouped[key] = grouped.get(key, 0) + 1
    return [
        f"in this universe, verifiers of family {family} fail to check {cls} "
        "-- test whether this task's verifier also fails"
        for (family, cls) in sorted(grouped)
        if grouped[(family, cls)] >= 2
    ]


def run_wave(
    manifest: CampaignManifest,
    tasks: Sequence[str],
    red_factory: RedFactory,
    broadcast: Sequence[str] = (),
    grade_fn: GradeFn | None = None,
    packages: Mapping[str, str | Path] | None = None,
) -> WaveResult:
    """Run one wave: controls per task, then that task's red agent, then broadcast.

    Tasks run SEQUENTIALLY (no threads). ``run_matrix`` itself already fans out
    internally (see ``_Audit.execute`` in verifier_mutation.py, which maps
    matrices over a ThreadPoolExecutor), so per-task red-agent loops add no
    parallelism here: red agents carry mutable prompt/grade state that is not
    designed for concurrent use, and the local posture stays at most 2
    concurrent trials. Threads would require proving that shared red-agent
    state -- not just ``run_matrix`` -- is thread-safe; it is not.

    When ``grade_fn`` is given, harness controls gate every task: ``task_broken``
    tasks land in ``skipped_task_broken`` and see no red agent. Without a
    ``grade_fn`` (offline composition), every task is audited.
    """
    hints = list(broadcast)
    collected: list[Hypothesis] = []
    skipped: list[str] = []
    for task_id in tasks:
        if grade_fn is not None:
            if packages is None or task_id not in packages:
                raise ValueError(f"task {task_id}: no package mapping to run harness controls")
            controls = run_controls(packages[task_id], grade_fn, task=task_id)
            if controls.verdict == "task_broken":
                skipped.append(task_id)
                continue
        collected.extend(red_factory(task_id, hints))
    confirmed = [hypothesis for hypothesis in collected if hypothesis.status == "promoted"]
    return WaveResult(
        campaign_id=manifest.campaign_id,
        hypotheses=collected,
        broadcast=synthesize_broadcast(confirmed),
        skipped_task_broken=skipped,
    )
