"""LabEvaluator integration for GEPA optimize_anything and MetaHarnessEngine.

This module provides the concrete evaluation boundary between GEPA's black-box
text optimization loops and Eval Lab's task execution runtime.

Key Invariants:
1. Candidate Safety & Immutability:
   - Candidates are immutable UTF-8 supplementary instruction text (preambles),
     NEVER executable host code.
   - Hash-addressed via full SHA-256 (``sha256:<64-hex>``).
   - Passed to Harbor via ``extra_instruction_path`` and ``extra_instruction_sha256``.
   - Existing candidate files must match content exactly; collisions raise errors and
     are never overwritten.
   - Output and jobs directories must be located inside the repository root; symlinks
     escaping the root are rejected.
2. Dataset Boundaries & Task Integrity:
   - Evaluator accepts only explicitly declared development examples (``split == 'development'``).
   - Incoming examples are compared field-by-field against immutable stored declarations.
   - Tasks must reside within the repository root (repo-relative, no traversal).
   - Task package digests are cryptographically verified against declared values.
3. Execution Policy Boundary:
   - Model-backed targets resolve through the normal Lab profile registry, with
     explicit model pins. Controls use model=None. No lane-local alias registry.
   - Broker-backed targets require explicit per-trial provider ceilings.
   - Local controls execute directly via ``Executor.execute_direct`` with ``RunProvenance``.
   - Newly executed and resumed jobs are both validated against exact recorded native provenance.
   - Model-backed evaluations require a genuine cost estimate (``estimated_cost_usd > 0``),
     submit an exact ``ExperimentSpec`` with hypothesis and elicitation purpose to the
     standing-approvals queue, and NEVER approve it. Broker-backed specs carry
     the same explicit provider ceilings as the ordinary execution path.
   - The DeepSeek target additionally binds recorded provider ceilings when persisted,
     and records observed model identity (matched/unknown/mismatch) without inferring it.
   - Resumption checks ONLY the deterministic job name or retained evaluation receipt;
     no broad directory discovery across unrelated runs.
   - Pending evaluations reuse existing queue specs on resume without duplicate submissions.
   - Review-mode candidates are retained without submission until explicitly allowed.
   - Rejected or failed queue entries are errors, not perpetually pending evaluations.
4. Reward Fidelity & No NaN:
   - When an evaluation is pending, ``EvaluationPending`` is raised to stop orchestration.
   - When an evaluation has missing reward or infra/runtime errors, the failure is recorded
     in ``records`` and on disk with status="error", and ``EvaluationUnavailable`` is raised
     immediately. NaN is NEVER returned.
   - Actual usage is parsed under native ``agent_result``.
5. Comparison Export:
   - ``write_comparison_spec(candidate_ids)`` requires exact full candidate digests without
     prefix ambiguity.
   - Emits an exploratory comparison (``mode="exploratory"``, no causal claim) using
     ``CohortComparisonSpec`` across real completed job paths.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.database import AGENT_STOP_EXCEPTIONS
from evallab.execution_contracts import (
    DEEPSEEK_ALLOWED_MODEL,
    HARBOR_AGENT_IMPORT_PATHS,
)
from evallab.queue import Executor, new_ulid
from evallab.registry import compute_task_digests, task_directory_digest
from evallab.results import JobRecord, TrialRecord, load_job
from evallab.runner import CONTROL_AGENTS, RunRequest, profile_for_request, resolve_harbor_model
from evallab.schemas import CohortComparisonSpec, CohortSelector, ExperimentSpec, RunProvenance
from evallab.toolbox import compute_skill_digest, validate_toolbox_code
from evallab.upstream_fetch import (
    KNOWN_SCORE_RULES,
    UPSTREAM_FETCH_ZERO,
    commands_from_trial,
    detect_upstream_fetch,
    format_fetch_notice,
)

from .budget import AggregateBudget
from .feedback import build_feedback, validate_oracle_reference, validate_prior_run_reference
from .intake import replay_spec_for_candidate

PERMITTED_CONTROLS = frozenset({"oracle", "nop"})
DEEPSEEK_TARGET_AGENT = "mini-swe-agent"
DEEPSEEK_TARGET_IMPORT_PATH = HARBOR_AGENT_IMPORT_PATHS[DEEPSEEK_TARGET_AGENT]

#: Exact keys of a provider_ceilings object, shared with workflow.load_campaign.
PROVIDER_CEILING_FIELDS: tuple[str, ...] = (
    "max_requests",
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "cost_limit_usd",
)

#: GEPA objective rule id scoring from authoritative native process-job counts.
COUNTED_VERDICT = "counted_verdict"

#: Exact digest of the zero-byte stock/no-addendum instruction artifact.
EMPTY_CANDIDATE_SHA256 = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class CandidateReviewRequired(Exception):
    """A proposal is retained but cannot be submitted until explicitly reviewed."""

    def __init__(self, candidate_id: str, candidate_path: Path) -> None:
        super().__init__(f"Review candidate {candidate_id} before evaluating it: {candidate_path}")
        self.candidate_id = candidate_id
        self.candidate_path = candidate_path


@dataclass(frozen=True)
class ProviderCeilings:
    """Explicit per-trial provider ceilings for a model-backed evaluation target.

    All five fields are required and positive; max_total_tokens must not exceed
    max_input_tokens + max_output_tokens, mirroring
    execution_contracts.validate_request. Unknown values stay unknown elsewhere;
    here absence is a refusal, never zero.
    """

    max_requests: int
    max_input_tokens: int
    max_output_tokens: int
    max_total_tokens: int
    cost_limit_usd: float

    def __post_init__(self) -> None:
        for field in (
            "max_requests",
            "max_input_tokens",
            "max_output_tokens",
            "max_total_tokens",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(
                    f"ProviderCeilings.{field} must be a positive integer, got {value!r}"
                )
        cost = self.cost_limit_usd
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or cost <= 0
        ):
            raise ValueError(
                "ProviderCeilings.cost_limit_usd must be a genuine positive finite "
                f"cap, got {cost!r}"
            )
        if self.max_total_tokens > self.max_input_tokens + self.max_output_tokens:
            raise ValueError("ProviderCeilings.max_total_tokens exceeds input plus output ceilings")
        if isinstance(cost, int):
            object.__setattr__(self, "cost_limit_usd", float(cost))

    def to_spec_kwargs(self) -> dict[str, Any]:
        """Ceilings as ExperimentSpec keyword arguments."""
        return {
            "max_requests": self.max_requests,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_total_tokens": self.max_total_tokens,
            "cost_limit_usd": self.cost_limit_usd,
        }

    def expected_usage_limits(self) -> dict[str, int]:
        """Ceilings as the DeepSeek proxy accounting record persists them."""
        return {
            "max_requests": self.max_requests,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_total_tokens": self.max_total_tokens,
            "max_cost_micros": math.ceil(self.cost_limit_usd * 1_000_000),
        }


class EvaluationPending(Exception):
    """Raised when an evaluation is pending in the queue or awaiting execution."""

    def __init__(
        self,
        message: str,
        *,
        spec_id: str | None = None,
        spec_path: Path | None = None,
        candidate_hash: str | None = None,
        task_id: str | None = None,
        decision: Any = None,
    ) -> None:
        super().__init__(message)
        self.spec_id = spec_id
        self.spec_path = spec_path
        self.candidate_hash = candidate_hash
        self.task_id = task_id
        self.decision = decision


class EvaluationUnavailable(Exception):
    """Raised when an evaluation cannot complete due to missing reward, verifier failure, or error."""


class ProvenanceMismatchError(ValueError):
    """Raised when recorded job provenance differs from requested candidate, task, agent, or model."""


class TaskDigestMismatchError(ValueError):
    """Raised when a task's recomputed package digest does not match its declared digest."""


class ExampleDeclarationError(ValueError):
    """Raised when an example violates declaration constraints (sealed split, undeclared, path traversal)."""


@dataclass(frozen=True)
class EvaluationRecord:
    """Retained record of one candidate evaluation on an example task.

    Main consumes:
        candidate_id: 'sha256:' + full 64-char hex digest
        task_id: task identifier
        status: 'completed' for scored native receipts; otherwise 'pending' or 'error'
        score: float | None (primary reward when scored, None for pending/error)
        job_path: str | None (path to completed job directory, None when pending)
    """

    candidate_id: str
    candidate_sha256: str
    candidate_path: str
    task_id: str
    task_path: str
    task_package_digest: str
    agent: str
    model: str | None
    split: str
    job_path: str | None
    receipt_paths: dict[str, str]
    score: float | None
    rewards: dict[str, float | None]
    usage: dict[str, Any]
    status: str
    error: str | None
    trial_id: str | None
    trial_name: str | None
    evaluated_at: str
    candidate_kind: str = "instructions"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def sanitize_cohort_label(full_digest: str) -> str:
    """Sanitize candidate digest for CohortSelector.label (^[a-z0-9][a-z0-9-]+$)."""
    hex_part = full_digest.split(":", 1)[-1] if ":" in full_digest else full_digest
    return f"c-{hex_part[:16]}"


def deterministic_job_name(
    *,
    campaign_path: str,
    agent: str,
    model: str | None,
    task_id: str,
    candidate_sha256: str,
) -> str:
    """Bind a native job to its campaign and complete candidate identity."""
    identity = json.dumps([campaign_path, agent, model, task_id, candidate_sha256])
    identity_tag = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    clean_agent = re.sub(r"[^a-z0-9]+", "-", agent.lower()).strip("-")[:16]
    clean_task = re.sub(r"[^a-z0-9]+", "-", task_id.lower()).strip("-")[:20]
    raw = f"gepa-{clean_agent}-{clean_task}-{identity_tag}"
    return re.sub(r"-+", "-", raw).strip("-")


def _artifact_task_tag(task_id: str) -> str:
    """Safe alphanumeric hash tag for task_id preventing any path traversal in filenames."""
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]


def _validate_in_repo_dir(repo_root: Path, target_dir: Path, label: str) -> Path:
    """Ensure directory is within repository root and does not escape via symlinks."""
    resolved_root = repo_root.resolve()
    resolved_target = target_dir.resolve()

    if target_dir.is_symlink() or any(parent.is_symlink() for parent in target_dir.parents):
        raise ValueError(f"{label} cannot contain symlinks escaping repository root: {target_dir}")

    if resolved_target != resolved_root and resolved_root not in resolved_target.parents:
        raise ValueError(
            f"{label} must be inside the repository root ({resolved_root}): {target_dir}"
        )
    return resolved_target


def validate_example_dict(repo_root: Path, example: dict[str, Any]) -> dict[str, Any]:
    """Validate that an example conforms strictly to the explicit development contract."""
    split = example.get("split")
    if split != "development":
        raise ExampleDeclarationError(
            f"Only development split examples are permitted; split '{split}' is forbidden (sealed tasks must not reach GEPA)"
        )

    task_id = example.get("task_id")
    if not task_id or not isinstance(task_id, str):
        raise ExampleDeclarationError(f"task_id must be a non-empty string, got {task_id!r}")

    task_path_str = example.get("task_path")
    if not task_path_str or not isinstance(task_path_str, str):
        raise ExampleDeclarationError(
            f"task_path must be a non-empty string, got {task_path_str!r}"
        )

    if task_path_str.startswith("/") or ".." in task_path_str.split("/"):
        raise ExampleDeclarationError(
            f"task_path must stay relative to the repository without path traversal: {task_path_str!r}"
        )

    resolved_root = repo_root.resolve()
    abs_task_path = (repo_root / task_path_str).resolve()
    if abs_task_path != resolved_root and resolved_root not in abs_task_path.parents:
        raise ExampleDeclarationError(f"task_path escapes repository root: {task_path_str!r}")

    if not abs_task_path.is_dir():
        raise ExampleDeclarationError(
            f"task directory not found in repository: {task_path_str!r} ({abs_task_path})"
        )

    declared_digest = example.get("task_package_digest")
    if (
        not declared_digest
        or not isinstance(declared_digest, str)
        or not re.match(r"^sha256:[0-9a-f]{64}$", declared_digest)
    ):
        raise ExampleDeclarationError(
            f"task_package_digest must be a sha256:... 64-hex string, got {declared_digest!r}"
        )

    computed_digest = task_directory_digest(abs_task_path)
    if computed_digest != declared_digest:
        raise TaskDigestMismatchError(
            f"Task package digest mismatch for {task_id}: declared {declared_digest}, computed {computed_digest}"
        )

    validated: dict[str, Any] = {
        "task_id": str(task_id),
        "task_path": str(task_path_str),
        "task_package_digest": str(declared_digest),
        "split": "development",
    }
    if "oracle_reference" in example:
        reference = example["oracle_reference"]
        if (
            not isinstance(reference, dict)
            or set(reference) != {"trial_path", "result_sha256", "task_package_digest"}
            or reference.get("task_package_digest") != declared_digest
            or not all(isinstance(value, str) for value in reference.values())
        ):
            raise ExampleDeclarationError("Oracle reference must bind this development package")
        validated["oracle_reference"] = copy.deepcopy(reference)
        validate_oracle_reference(repo_root, Path(task_path_str), reference)
    if "prior_run_reference" in example:
        reference = example["prior_run_reference"]
        if (
            not isinstance(reference, dict)
            or set(reference) != {"trial_path", "result_sha256", "task_package_digest"}
            or reference.get("task_package_digest") != declared_digest
            or not all(isinstance(value, str) for value in reference.values())
        ):
            raise ExampleDeclarationError("Prior run reference must bind this development package")
        validated["prior_run_reference"] = copy.deepcopy(reference)
        validate_prior_run_reference(repo_root, Path(task_path_str), reference)
    return validated


def _check_job_provenance(
    job: JobRecord,
    *,
    expected_agent: str,
    expected_model: str | None,
    expected_task_id: str,
    expected_package_digest: str,
    expected_candidate_sha256: str,
    expected_ceilings: ProviderCeilings | None = None,
    candidate_kind: str = "instructions",
) -> bool:
    """Bind the single native trial to its recorded request and fixed profile."""
    locked_trials = job.lock.get("trials")
    if len(job.trials) != 1 or not isinstance(locked_trials, list) or len(locked_trials) != 1:
        return False
    is_empty_stock = (
        candidate_kind == "instructions" and expected_candidate_sha256 == EMPTY_CANDIDATE_SHA256
    )
    # config.json intentionally omits defaults in Harbor 0.21; locks retain the
    # effective request. Never reconstruct historical settings from defaults.
    for locked in (locked_trials[0], job.trials[0].lock):
        if not isinstance(locked, dict):
            return False
        extras = locked.get("extra_instructions", [])
        if not isinstance(extras, list):
            return False
        if candidate_kind == "instructions":
            if is_empty_stock:
                # Native stock has no addendum: either an explicit empty digest or
                # a native job with zero extra instructions. Both are equivalent
                # ONLY to the exact empty candidate, never to another digest.
                if len(extras) == 0:
                    pass
                elif (
                    len(extras) != 1
                    or not isinstance(extras[0], dict)
                    or extras[0].get("digest") != EMPTY_CANDIDATE_SHA256
                ):
                    return False
            elif (
                len(extras) != 1
                or not isinstance(extras[0], dict)
                or extras[0].get("digest") != expected_candidate_sha256
            ):
                return False
        elif extras:
            return False
        agent = locked.get("agent")
        if not isinstance(agent, dict) or agent.get("model_name") != resolve_harbor_model(
            expected_agent, expected_model
        ):
            return False
        expected_import = HARBOR_AGENT_IMPORT_PATHS.get(expected_agent)
        if expected_import is not None:
            # Harbor's --agent stores an import path in name; explicit
            # AgentConfig.import_path is another valid persisted representation.
            name, import_path = agent.get("name"), agent.get("import_path")
            if import_path is None:
                if name != expected_import:
                    return False
            elif import_path != expected_import or name not in (
                None,
                expected_agent,
                expected_import,
            ):
                return False
        elif agent.get("name") != expected_agent or agent.get("import_path"):
            return False

    exp = job.metadata.get("experiment")
    if not isinstance(exp, dict):
        return False

    if exp.get("task_id") != expected_task_id:
        return False
    if exp.get("package_digest") != expected_package_digest:
        return False
    identity_field = "toolbox_sha256" if candidate_kind == "python_toolbox" else "preamble_sha256"
    recorded_identity = exp.get(identity_field)
    if is_empty_stock:
        # Empty stock must be unaugmented on imports and retained-cache reuse.
        toolbox_meta = job.metadata.get("toolbox")
        if exp.get("toolbox_sha256") is not None or (
            toolbox_meta is not None and toolbox_meta != {}
        ):
            return False
        # Stock equivalence: explicit empty digest or absent preamble (native
        # no-addendum). Any other digest never matches the empty candidate.
        if recorded_identity not in (EMPTY_CANDIDATE_SHA256, None):
            return False
    elif recorded_identity != expected_candidate_sha256:
        return False
    if candidate_kind == "python_toolbox":
        toolbox = job.metadata.get("toolbox", {})
        relative = toolbox.get("artifact_path")
        if not isinstance(relative, str) or Path(relative).is_absolute():
            return False
        artifact = job.path / relative
        if artifact.is_symlink() or not artifact.resolve().is_relative_to(job.path.resolve()):
            return False
        if (
            not artifact.is_file()
            or "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
            != expected_candidate_sha256
            or toolbox.get("sha256") != expected_candidate_sha256
        ):
            return False
        try:
            if compute_skill_digest(artifact.parent) != toolbox.get("skill_digest"):
                return False
        except (OSError, ValueError):
            return False
        for locked in (locked_trials[0], job.trials[0].lock):
            skills = locked.get("skills", [])
            if len(skills) != 1 or not isinstance(skills[0], dict):
                return False
            if skills[0].get("name") != "repl-tools" or skills[0].get("digest") != toolbox.get(
                "skill_digest"
            ):
                return False
    if expected_ceilings is not None:
        # The runner persists the enforced ceilings in the DeepSeek accounting
        # report (metadata provider_usage.limits). A missing or differing
        # record never matches.
        provider_usage = job.metadata.get("provider_usage")
        if not isinstance(provider_usage, dict):
            return False
        limits = provider_usage.get("limits")
        if not isinstance(limits, dict):
            return False
        expected_limits = expected_ceilings.expected_usage_limits()
        if any(limits.get(key) != value for key, value in expected_limits.items()):
            return False
        if provider_usage.get("unresolved_requests") != 0:
            return False
    return True


def _returned_provider_models(job: JobRecord) -> frozenset[str | None]:
    """Return verbatim model identities from native broker response records.

    Only the secret proxy's per-call accounting (``provider_usage.calls[*]``,
    persisted by the runner) is provider-side evidence. Harbor's
    ``agent_info``/``agent_result`` carry the agent's *declared* model and must
    never be read as an observed identity. Calls without a ``returned_model``
    retain None so incomplete identity coverage cannot be reported as matched.
    """
    provider_usage = job.metadata.get("provider_usage")
    if not isinstance(provider_usage, dict):
        return frozenset()
    calls = provider_usage.get("calls")
    if not isinstance(calls, list):
        return frozenset()
    returned: set[str | None] = set()
    for call in calls:
        value = call.get("returned_model") if isinstance(call, dict) else None
        returned.add(value if isinstance(value, str) and value else None)
    return frozenset(returned)

class _CountedUnavailable(ValueError):
    """Internal: authoritative counts cannot score this trial (excluded/missing/malformed)."""


def _base_spec_binding(base_spec: ExperimentSpec | None) -> dict[str, Any] | None:
    """Frozen behavioral binding recorded in every counted receipt."""
    if base_spec is None:
        return None
    return {
        "agent": base_spec.agent,
        "model": base_spec.model,
        "environment": base_spec.environment,
        "attempts": base_spec.attempts,
        "concurrency": base_spec.concurrency,
        "timeout_seconds": base_spec.timeout_seconds,
        "max_requests": base_spec.max_requests,
        "max_input_tokens": base_spec.max_input_tokens,
        "max_output_tokens": base_spec.max_output_tokens,
        "max_total_tokens": base_spec.max_total_tokens,
        "cost_limit_usd": base_spec.cost_limit_usd,
        "harness_tree_sha256": base_spec.harness_tree_sha256,
        "harness_policy": base_spec.harness_policy,
    }


def _check_base_spec_binding(
    job: JobRecord,
    *,
    evaluator_agent: str,
    evaluator_model: str | None,
    evaluator_timeout_seconds: int,
    evaluator_ceilings: ProviderCeilings | None,
    base_spec: ExperimentSpec | None,
) -> bool:
    """Bind the frozen target to native spec evidence, not assumed defaults."""
    if base_spec is None:
        return True
    if (
        evaluator_agent != base_spec.agent
        or evaluator_model != base_spec.model
        or evaluator_timeout_seconds != base_spec.timeout_seconds
    ):
        return False
    if evaluator_ceilings is not None:
        expected = evaluator_ceilings.to_spec_kwargs()
        if any(expected[field] != getattr(base_spec, field) for field in PROVIDER_CEILING_FIELDS):
            return False
    exp = job.metadata.get("experiment")
    if not isinstance(exp, dict) or exp.get("harness_tree_sha256") != base_spec.harness_tree_sha256:
        return False
    spec_path = job.path / "experiment-spec.json"
    if not spec_path.is_file() or spec_path.is_symlink():
        return False
    try:
        recorded = ExperimentSpec.model_validate_json(spec_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if _base_spec_binding(recorded) != _base_spec_binding(base_spec):
        return False
    provider_usage = job.metadata.get("provider_usage")
    if isinstance(provider_usage, dict) and isinstance(provider_usage.get("limits"), dict):
        limits = provider_usage["limits"]
        for key in ("max_requests", "max_input_tokens", "max_output_tokens", "max_total_tokens"):
            value = getattr(base_spec, key)
            if value is not None and limits.get(key) != value:
                return False
        if (
            base_spec.cost_limit_usd is not None
            and limits.get("max_cost_micros") != math.ceil(base_spec.cost_limit_usd * 1_000_000)
        ):
            return False
    return True


def _read_counted_verdict(
    job_dir: Path, trial: TrialRecord, primary_reward: float | None
) -> tuple[dict[str, Any], Path, str]:
    """Read the canonical process-job report and bind its native trial reward."""
    from evallab.counts import COUNTS_SCHEMA

    report_path = job_dir / "processed" / f"trial-{trial.name}.json"
    if (
        not report_path.is_file()
        or report_path.is_symlink()
        or not report_path.resolve().is_relative_to(job_dir.resolve())
    ):
        raise _CountedUnavailable(f"counted_verdict requires retained process-job report {report_path}")
    try:
        raw_bytes = report_path.read_bytes()
        report = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise _CountedUnavailable(f"counted_verdict report {report_path} is unreadable: {exc}") from exc
    if not isinstance(report, dict) or report.get("trial_name") != trial.name:
        raise _CountedUnavailable(f"counted_verdict report {report_path} has a mismatched trial identity")
    counts = report.get("counts")
    if not isinstance(counts, dict) or counts.get("schema") != COUNTS_SCHEMA:
        raise _CountedUnavailable(f"counted_verdict report {report_path} has no canonical counts schema")
    verdict, reasons, reward = counts.get("verdict"), counts.get("reasons"), counts.get("raw_reward")
    if (
        verdict not in ("counted_pass", "counted_fail", "excluded")
        or not isinstance(reasons, list)
        or any(not isinstance(reason, str) or not reason for reason in reasons)
    ):
        raise _CountedUnavailable(f"counted_verdict report {report_path} has malformed verdict/reasons")
    if (
        type(reward) not in (int, float)
        or not math.isfinite(reward)
        or primary_reward is None
        or not math.isfinite(primary_reward)
        or reward != primary_reward
    ):
        raise _CountedUnavailable(f"counted_verdict report {report_path} raw_reward mismatches native reward")
    if verdict == "excluded":
        if not reasons:
            raise _CountedUnavailable(f"counted_verdict report {report_path} excludes without reasons")
    elif (
        counts.get("scored") is not True
        or reasons
        or (reward >= 1.0) != (verdict == "counted_pass")
    ):
        raise _CountedUnavailable(f"counted_verdict report {report_path} has inconsistent scored verdict")
    return counts, report_path, "sha256:" + hashlib.sha256(raw_bytes).hexdigest()


class LabEvaluator:
    """Concrete evaluation boundary for GEPA optimize_anything on Eval Lab."""

    def __init__(
        self,
        repo_root: Path,
        output_dir: Path,
        examples: list[dict[str, Any]],
        agent: str,
        model: str | None = None,
        timeout_seconds: int = 1200,
        estimated_cost_usd: float | None = None,
        *,
        executor: Executor | None = None,
        jobs_dir: Path | None = None,
        ceilings: ProviderCeilings | None = None,
        approved_candidate_ids: frozenset[str] | None = None,
        feedback_max_chars: int = 24000,
        budgets: tuple[AggregateBudget, ...] = (),
        candidate_kind: str = "instructions",
        base_spec: ExperimentSpec | None = None,
        score_rules: tuple[str, ...] = (),
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.output_dir = _validate_in_repo_dir(self.repo_root, Path(output_dir), "output_dir")
        self._campaign_path = self.output_dir.relative_to(self.repo_root).as_posix()
        self.jobs_dir = _validate_in_repo_dir(
            self.repo_root,
            Path(jobs_dir) if jobs_dir else (self.repo_root / "runs"),
            "jobs_dir",
        )

        self.agent = str(agent)
        self.model = str(model) if model is not None else None
        self.timeout_seconds = int(timeout_seconds)
        self.estimated_cost_usd = (
            float(estimated_cost_usd) if estimated_cost_usd is not None else None
        )
        if ceilings is not None and not isinstance(ceilings, ProviderCeilings):
            raise ValueError(f"ceilings must be a ProviderCeilings, got {type(ceilings).__name__}")
        self.ceilings = ceilings
        self.approved_candidate_ids = approved_candidate_ids
        self.feedback_max_chars = feedback_max_chars
        self.budgets = budgets
        if candidate_kind not in {"instructions", "python_toolbox"}:
            raise ValueError("Unsupported candidate_kind")
        self.candidate_kind = candidate_kind
        self.base_spec = base_spec
        if any(not isinstance(rule, str) for rule in score_rules):
            raise ValueError("score_rules must be rule-id strings")
        unknown_rules = [
            rule for rule in score_rules if rule not in KNOWN_SCORE_RULES and rule != COUNTED_VERDICT
        ]
        if unknown_rules:
            raise ValueError(
                f"Unknown score_rules {unknown_rules}; known: {sorted(KNOWN_SCORE_RULES | frozenset({COUNTED_VERDICT}))}"
            )
        if UPSTREAM_FETCH_ZERO in score_rules and COUNTED_VERDICT in score_rules:
            raise ValueError(
                "score_rules counted_verdict and upstream_fetch_zero are mutually exclusive"
            )
        self.score_rules = tuple(score_rules)
        if ceilings is not None and self.agent not in {DEEPSEEK_TARGET_AGENT, "zai-opencode"}:
            raise ValueError(f"the {self.agent} target does not accept provider ceilings")

        # Agent/profile and model validation
        if self.agent in PERMITTED_CONTROLS:
            if self.model is not None:
                raise ValueError(f"the {self.agent} control does not accept a model")
        else:
            if self.model is None:
                raise ValueError("Model-backed evaluations require an explicit registered model")
            profile_for_request(
                RunRequest(
                    task=self.repo_root,
                    agent=self.agent,
                    model=self.model,
                    name="gepa-profile",
                    jobs_dir=self.jobs_dir,
                )
            )
            if self.agent in {DEEPSEEK_TARGET_AGENT, "zai-opencode"} and self.ceilings is None:
                raise ValueError(f"Target '{self.agent}' requires explicit provider ceilings")

        # Validate and store immutable copies of declared development examples
        if not examples:
            raise ValueError(
                "examples list cannot be empty; explicit development tasks are required"
            )

        self._declared_examples: dict[str, dict[str, Any]] = {}
        for ex in examples:
            validated = validate_example_dict(self.repo_root, ex)
            task_id = validated["task_id"]
            if task_id in self._declared_examples:
                raise ExampleDeclarationError(f"Duplicate task_id in declared examples: {task_id}")
            self._declared_examples[task_id] = copy.deepcopy(validated)

        # Executor boundary
        self.executor = executor or Executor.from_repo(self.repo_root)

        # In-memory record retention
        self._records: list[EvaluationRecord] = []

        # Local output subdirectories
        (self.output_dir / "candidates").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "evaluations").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "comparisons").mkdir(parents=True, exist_ok=True)

    @property
    def records(self) -> list[EvaluationRecord]:
        """Expose immutable view of evaluation records."""
        return list(self._records)

    def _ensure_candidate_stored(self, candidate: str) -> tuple[str, Path, str]:
        """Store candidate text addressed by sha256. Fails if existing content differs."""
        if not isinstance(candidate, str):
            raise TypeError(f"Candidate must be a UTF-8 string, got {type(candidate).__name__}")

        candidate_bytes = candidate.encode("utf-8")
        candidate_sha256 = f"sha256:{hashlib.sha256(candidate_bytes).hexdigest()}"
        digest_hex = candidate_sha256.split(":", 1)[-1]

        suffix = ".py" if self.candidate_kind == "python_toolbox" else ".txt"
        local_candidate_file = self.output_dir / "candidates" / f"{digest_hex}{suffix}"
        if local_candidate_file.is_symlink():
            raise ValueError("Candidate files must not be symlinks")
        if local_candidate_file.exists():
            existing_text = local_candidate_file.read_text(encoding="utf-8")
            if existing_text != candidate:
                raise ValueError(
                    f"Immutable candidate file collision: {local_candidate_file} exists but content differs from hash {candidate_sha256}"
                )
        else:
            with local_candidate_file.open("x", encoding="utf-8", newline="") as stream:
                stream.write(candidate)

        rel_candidate_path = local_candidate_file.relative_to(self.repo_root).as_posix()
        return candidate_sha256, local_candidate_file, rel_candidate_path

    def _record_evaluation(
        self,
        *,
        candidate_sha256: str,
        local_candidate_file: Path,
        example: dict[str, Any],
        status: str,
        job_path: str | None,
        receipt_paths: dict[str, str],
        score: float | None,
        rewards: dict[str, float | None],
        usage: dict[str, Any],
        error: str | None,
        trial_id: str | None = None,
        trial_name: str | None = None,
    ) -> EvaluationRecord:
        """Shared constructor: build EvaluationRecord, retain in memory, and persist receipt on disk."""
        if self.ceilings is not None:
            usage = {**usage, "provider_ceilings": self.ceilings.to_spec_kwargs()}
        record = EvaluationRecord(
            candidate_id=candidate_sha256,
            candidate_sha256=candidate_sha256,
            candidate_path=str(local_candidate_file),
            task_id=example["task_id"],
            task_path=example["task_path"],
            task_package_digest=example["task_package_digest"],
            agent=self.agent,
            model=self.model,
            split=example["split"],
            job_path=job_path,
            receipt_paths=receipt_paths,
            score=score,
            rewards=rewards,
            usage=usage,
            status=status,
            error=error,
            trial_id=trial_id,
            trial_name=trial_name,
            evaluated_at=datetime.now(UTC).isoformat(),
            candidate_kind=self.candidate_kind,
        )
        self._records.append(record)

        # Persist receipt artifact to disk under evaluations/
        cand_hex = candidate_sha256.split(":", 1)[-1]
        task_tag = _artifact_task_tag(example["task_id"])
        receipt_file = self.output_dir / "evaluations" / f"{cand_hex}_{task_tag}.json"
        receipt_file.write_text(
            json.dumps(record.to_dict(), indent=2, allow_nan=False) + "\n", encoding="utf-8"
        )
        if job_path is not None:
            identity = deterministic_job_name(
                campaign_path=self._campaign_path,
                agent=self.agent,
                model=self.model,
                task_id=example["task_id"],
                candidate_sha256=candidate_sha256,
            )
            for budget in self.budgets:
                budget.complete(
                    "target",
                    identity,
                    status=status,
                    metadata={"trial_id": trial_id, "candidate_id": candidate_sha256},
                )

        return record

    def _consume_completed_job(
        self,
        job_record: JobRecord,
        example: dict[str, Any],
        candidate_sha256: str,
        local_candidate_file: Path,
    ) -> tuple[float, dict[str, Any]]:
        """Parse completed job and trial records; stop immediately on missing reward or error."""
        job_dir = job_record.path
        trial = job_record.trials[0] if job_record.trials else None

        receipt_paths: dict[str, str] = {
            "job_result": str(job_dir / "result.json"),
            "job_config": str(job_dir / "config.json"),
            "job_lock": str(job_dir / "lock.json"),
        }
        if (job_dir / "lab-metadata.json").is_file():
            receipt_paths["lab_metadata"] = str(job_dir / "lab-metadata.json")
        if trial is not None:
            receipt_paths["trial_result"] = str(trial.path / "result.json")
            receipt_paths["trial_config"] = str(trial.path / "config.json")
            receipt_paths["trial_lock"] = str(trial.path / "lock.json")

        agent_result: dict[str, Any] = {}
        error_info: str | None = None
        if trial is not None:
            raw_ar = trial.result.get("agent_result")
            if isinstance(raw_ar, dict):
                agent_result = raw_ar

            if trial.result.get("exception_info"):
                error_info = str(trial.result["exception_info"])
            elif trial.result.get("error"):
                error_info = str(trial.result["error"])

        usage: dict[str, Any] = {
            "cost_usd": agent_result.get("cost_usd"),
            "n_input_tokens": agent_result.get("n_input_tokens"),
            "n_cache_tokens": agent_result.get("n_cache_tokens"),
            "n_output_tokens": agent_result.get("n_output_tokens"),
            "n_total_tokens": (
                agent_result.get("n_input_tokens", 0) + agent_result.get("n_output_tokens", 0)
                if agent_result.get("n_input_tokens") is not None
                and agent_result.get("n_output_tokens") is not None
                else None
            ),
        }
        if "provider_usage" in job_record.metadata:
            usage["provider_usage"] = job_record.metadata["provider_usage"]
        if self.agent in {DEEPSEEK_TARGET_AGENT, "zai-opencode"}:
            # Observed identity comes only from the proxy's per-call records;
            # an unrecorded model is unknown, never inferred as matched, and
            # calls that disagree with the pin (or each other) are a mismatch.
            expected_model = (
                str(self.model).rsplit("/", 1)[-1]
                if str(self.model).startswith("zai/")
                else (
                    DEEPSEEK_ALLOWED_MODEL
                    if self.agent == DEEPSEEK_TARGET_AGENT
                    else str(self.model).rsplit("/", 1)[-1]
                )
            )
            returned = _returned_provider_models(job_record)
            known = {name for name in returned if name is not None}
            if known - {expected_model}:
                raise ProvenanceMismatchError(
                    f"Broker-backed job at {job_dir} returned model {sorted(known)!r}, "
                    f"expected provider model {expected_model!r}"
                )
            observed = expected_model if known else None
            identity_status = "matched" if returned == {expected_model} else "unknown"
            usage["identity"] = {
                "requested_model": self.model,
                "observed_model": observed,
                "status": identity_status,
            }
        if trial is not None:
            accounting_path = trial.path / "agent" / "request-accounting.json"
            if accounting_path.is_file() and not accounting_path.is_symlink():
                accounting = json.loads(accounting_path.read_text())
                receipt_paths["request_accounting"] = str(accounting_path)
                usage["native_request_totals"] = accounting.get("totals")
                usage["remote_outcomes_pending"] = accounting.get("remote_outcomes_pending")
                usage["role_accounting_receipt"] = str(accounting_path)
            elif self.agent not in PERMITTED_CONTROLS:
                usage["native_request_accounting"] = "missing"

        primary_reward = trial.primary_reward if trial is not None else None
        rewards = {
            name: value if math.isfinite(value) else None
            for name, value in (trial.rewards.items() if trial is not None else ())
        }
        # An agent timeout or trial-ceiling stop ends the agent's run while Harbor still
        # runs the verifier: with a finite reward that is the agent's outcome, not an
        # infrastructure failure, so it is scored like a clean trial.
        exception_info = trial.result.get("exception_info") if trial is not None else None
        if (
            trial is not None
            and error_info
            and isinstance(exception_info, dict)
            and exception_info.get("exception_type") in AGENT_STOP_EXCEPTIONS
            and not trial.result.get("error")
            and primary_reward is not None
            and math.isfinite(float(primary_reward))
        ):
            usage["agent_stop"] = exception_info["exception_type"]
            error_info = None

        # Handle failure cases: persist error record and raise EvaluationUnavailable (never return NaN)
        if error_info:
            self._record_evaluation(
                candidate_sha256=candidate_sha256,
                local_candidate_file=local_candidate_file,
                example=example,
                status="error",
                job_path=str(job_dir),
                receipt_paths=receipt_paths,
                score=None,
                rewards=rewards,
                usage=usage,
                error=error_info,
                trial_id=trial.id if trial else None,
                trial_name=trial.name if trial else None,
            )
            raise EvaluationUnavailable(
                f"Evaluation failed with error on task {example['task_id']}: {error_info}"
            )

        if primary_reward is None or not math.isfinite(float(primary_reward)):
            self._record_evaluation(
                candidate_sha256=candidate_sha256,
                local_candidate_file=local_candidate_file,
                example=example,
                status="error",
                job_path=str(job_dir),
                receipt_paths=receipt_paths,
                score=None,
                rewards=rewards,
                usage=usage,
                error="missing_or_nonfinite_reward",
                trial_id=trial.id if trial else None,
                trial_name=trial.name if trial else None,
            )
            raise EvaluationUnavailable(
                f"Evaluation completed without a finite primary reward on task {example['task_id']}"
            )

        # Successful evaluation: counted_verdict scores from authoritative
        # process-job counts; historic rules score the native reward directly.
        score = float(primary_reward)
        counted_counts: dict[str, Any] | None = None
        counted_digest: str | None = None
        if COUNTED_VERDICT in self.score_rules:
            assert trial is not None  # A finite native reward above requires a trial.
            receipt_paths["counts_report"] = str(
                job_dir / "processed" / f"trial-{trial.name}.json"
            )
            try:
                counted_counts, counted_path, counted_digest = _read_counted_verdict(
                    job_dir, trial, primary_reward
                )
            except _CountedUnavailable as exc:
                usage["counted_verdict"] = {
                    "applied": False,
                    "reason": str(exc),
                    "receipt": receipt_paths["counts_report"],
                }
                self._record_evaluation(
                    candidate_sha256=candidate_sha256,
                    local_candidate_file=local_candidate_file,
                    example=example,
                    status="error",
                    job_path=str(job_dir),
                    receipt_paths=receipt_paths,
                    score=None,
                    rewards=rewards,
                    usage=usage,
                    error=f"counted_verdict_unavailable: {exc}",
                    trial_id=trial.id,
                    trial_name=trial.name,
                )
                raise EvaluationUnavailable(
                    f"counted_verdict unavailable on task {example['task_id']}: {exc}"
                ) from exc
            verdict = counted_counts["verdict"]
            usage["counts"] = counted_counts
            usage["counted_verdict"] = {
                "applied": verdict != "excluded",
                "verdict": verdict,
                "reasons": counted_counts["reasons"],
                "raw_reward": counted_counts["raw_reward"],
                "receipt": str(counted_path),
                "receipt_sha256": counted_digest,
            }
            if self.base_spec is not None:
                usage["base_spec"] = _base_spec_binding(self.base_spec)
            if verdict == "excluded":
                self._record_evaluation(
                    candidate_sha256=candidate_sha256,
                    local_candidate_file=local_candidate_file,
                    example=example,
                    status="error",
                    job_path=str(job_dir),
                    receipt_paths=receipt_paths,
                    score=None,
                    rewards=rewards,
                    usage=usage,
                    error=f"counted_verdict_excluded: {counted_counts['reasons']}",
                    trial_id=trial.id,
                    trial_name=trial.name,
                )
                raise EvaluationUnavailable(
                    f"counted_verdict excluded trial on task {example['task_id']}: {counted_counts['reasons']}"
                )
            score = 1.0 if verdict == "counted_pass" else 0.0
        prior_trial_paths: tuple[Path, ...] = ()
        prior_ref = example.get("prior_run_reference")
        if prior_ref is not None:
            prior_trial_paths = (
                validate_prior_run_reference(self.repo_root, Path(example["task_path"]), prior_ref),
            )
        feedback = (
            build_feedback(
                repo_root=self.repo_root,
                task_path=Path(example["task_path"]),
                trial_path=trial.path,
                max_chars=self.feedback_max_chars,
                oracle_reference=example.get("oracle_reference"),
                prior_trial_paths=prior_trial_paths,
            )
            if trial is not None
            else {"feedback": "No trial evidence available."}
        )
        if counted_counts is not None:
            notice = (
                f"counted_verdict {counted_counts['verdict']} "
                f"(raw_reward={counted_counts['raw_reward']}, reasons={counted_counts['reasons']}) "
                f"from {receipt_paths['counts_report']} ({counted_digest})."
            )
            feedback = {**feedback, "feedback": f"{feedback.get('feedback', '')}\n\n{notice}"}
        # Upstream-fetch leak rule: the recorded verifier reward stands, but a
        # trial that fetched remote content scores 0 for GEPA's objective, with
        # the rule and its findings in the evaluation evidence.
        if trial is not None and UPSTREAM_FETCH_ZERO in self.score_rules:
            fetch_findings = detect_upstream_fetch(commands_from_trial(trial.path))
            if fetch_findings:
                score = 0.0
                usage["score_rules"] = {
                    UPSTREAM_FETCH_ZERO: {
                        "applied": True,
                        "finding_count": len(fetch_findings),
                        "findings": [
                            {
                                "step_index": finding.step_index,
                                "kind": finding.kind,
                                "excerpt": finding.excerpt,
                                "names_task_repo": finding.names_task_repo,
                            }
                            for finding in fetch_findings
                        ],
                    }
                }
                notice = format_fetch_notice(fetch_findings)
                feedback = {
                    **feedback,
                    "feedback": f"{feedback.get('feedback', '')}\n\n{notice}",
                }
        self._record_evaluation(
            candidate_sha256=candidate_sha256,
            local_candidate_file=local_candidate_file,
            example=example,
            status="completed",
            job_path=str(job_dir),
            receipt_paths=receipt_paths,
            score=score,
            rewards=rewards,
            usage=usage,
            error=None,
            trial_id=trial.id if trial else None,
            trial_name=trial.name if trial else None,
        )

        info: dict[str, Any] = {
            "task_id": example["task_id"],
            "candidate_hash": candidate_sha256,
            "candidate_id": candidate_sha256,
            "job_path": str(job_dir),
            "status": "completed",
            "score": score,
            "rewards": rewards,
            "usage": usage,
            "error": None,
            "receipt_paths": receipt_paths,
            **feedback,
        }
        return score, info

    def __call__(self, candidate: str, example: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        """Evaluate one retained instruction or Python toolbox artifact on development data.

        Returns:
            (score, info): Scalar score and diagnostic feedback dict.
        Raises:
            EvaluationPending: If a model-backed evaluation is pending in the queue.
            EvaluationUnavailable: If trial has missing reward or infra failure (never NaN).
            ExampleDeclarationError: If example does not match immutable declared specification.
            TaskDigestMismatchError: If task package digest has mutated.
            ProvenanceMismatchError: If recorded job provenance does not match.
            TypeError: If candidate is not a string.
        """
        candidate_sha256, local_candidate_file, rel_candidate_path = self._ensure_candidate_stored(
            candidate
        )

        task_id = example.get("task_id")
        if not task_id or task_id not in self._declared_examples:
            raise ExampleDeclarationError(
                f"Example task '{task_id}' is not an explicitly declared development task"
            )

        # Compare incoming example fields against immutable declaration
        declared = self._declared_examples[task_id]
        for key in ("task_id", "task_path", "task_package_digest", "split"):
            if example.get(key) != declared[key]:
                raise ExampleDeclarationError(
                    f"Incoming example field '{key}' differs from immutable declared contract: "
                    f"{example.get(key)!r} != {declared[key]!r}"
                )
        if example.get("oracle_reference") != declared.get("oracle_reference"):
            raise ExampleDeclarationError(
                "Oracle reference differs from frozen development declaration"
            )
        if example.get("prior_run_reference") != declared.get("prior_run_reference"):
            raise ExampleDeclarationError(
                "Prior run reference differs from frozen development declaration"
            )

        abs_task_path = (self.repo_root / declared["task_path"]).resolve()
        current_digest = task_directory_digest(abs_task_path)
        if current_digest != declared["task_package_digest"]:
            raise TaskDigestMismatchError(
                f"Current task directory digest for {task_id} has mutated: "
                f"expected {declared['task_package_digest']}, computed {current_digest}"
            )

        if (
            self.approved_candidate_ids is not None
            and candidate_sha256 not in self.approved_candidate_ids
        ):
            raise CandidateReviewRequired(candidate_sha256, local_candidate_file)
        if self.candidate_kind == "python_toolbox":
            try:
                validate_toolbox_code(candidate)
            except ValueError as exc:
                info = {
                    "status": "invalid_candidate",
                    "candidate_id": candidate_sha256,
                    "candidate_path": str(local_candidate_file),
                    "native_trial_executed": False,
                    "feedback": f"Python toolbox static validation failed: {exc}",
                }
                rejection = local_candidate_file.with_suffix(".validation.json")
                rejection.write_text(json.dumps(info, indent=2) + "\n")
                return 0.0, info

        job_name = deterministic_job_name(
            campaign_path=self._campaign_path,
            agent=self.agent,
            model=self.model,
            task_id=task_id,
            candidate_sha256=candidate_sha256,
        )
        exact_job_dir = self.jobs_dir / job_name
        receipt_path = (
            self.output_dir
            / "evaluations"
            / f"{candidate_sha256.split(':', 1)[-1]}_{_artifact_task_tag(task_id)}.json"
        )
        retained: dict[str, Any] = {}
        if receipt_path.is_file():
            retained = json.loads(receipt_path.read_text(encoding="utf-8"))
            if retained.get("status") == "completed":
                if not retained.get("job_path"):
                    raise EvaluationUnavailable("Completed evaluation receipt has no retained job")
                exact_job_dir = _validate_in_repo_dir(
                    self.repo_root, Path(retained["job_path"]), "retained job"
                )
                if not exact_job_dir.is_dir() or not (exact_job_dir / "result.json").is_file():
                    raise EvaluationUnavailable(
                        "Retained completed job is unavailable; restore its evidence, not a new run"
                    )

        # Harbor writes result.json while the job is still running. Only a
        # finished result is consumable; otherwise reuse the exact queued spec.
        result_path = exact_job_dir / "result.json"
        finished = False
        if result_path.is_file():
            finished = bool(json.loads(result_path.read_text()).get("finished_at"))
            if not finished and retained.get("status") != "pending":
                raise EvaluationUnavailable(
                    "Unfinished native job has no retained pending receipt; inspect the queue, not a new run"
                )
        if finished:
            # Retained receipt binding: a cached stock import records its frozen
            # base-spec; changed harness/limits must never silently reuse it.
            if retained.get("status") == "completed" and self.base_spec is not None:
                recorded_binding = (retained.get("usage") or {}).get("base_spec")
                current_binding = _base_spec_binding(self.base_spec)
                if recorded_binding is not None and recorded_binding != current_binding:
                    raise ProvenanceMismatchError(
                        f"Retained evaluation at {receipt_path} belongs to a different frozen harness/limits"
                    )
            job_record = load_job(exact_job_dir)
            if not _check_job_provenance(
                job_record,
                expected_agent=self.agent,
                expected_model=self.model,
                expected_task_id=task_id,
                expected_package_digest=declared["task_package_digest"],
                expected_candidate_sha256=candidate_sha256,
                expected_ceilings=self.ceilings,
                candidate_kind=self.candidate_kind,
            ):
                raise ProvenanceMismatchError(
                    f"Job at {exact_job_dir} exists but does not match exact candidate/task/model provenance"
                )
            if not _check_base_spec_binding(
                job_record,
                evaluator_agent=self.agent,
                evaluator_model=self.model,
                evaluator_timeout_seconds=self.timeout_seconds,
                evaluator_ceilings=self.ceilings,
                base_spec=self.base_spec,
            ):
                raise ProvenanceMismatchError(
                    f"Job at {exact_job_dir} does not match the frozen harness/limits"
                )
            return self._consume_completed_job(
                job_record,
                declared,
                candidate_sha256,
                local_candidate_file,
            )

        # If local control, execute directly via Executor.execute_direct
        if self.agent in CONTROL_AGENTS:
            control_timeout = self.timeout_seconds
            control_attempts = 1
            control_concurrency = 1
            control_environment = "docker"
            if self.base_spec is not None:
                control_timeout = self.base_spec.timeout_seconds
                control_attempts = self.base_spec.attempts
                control_concurrency = self.base_spec.concurrency
                control_environment = self.base_spec.environment
            request = RunRequest(
                task=abs_task_path,
                agent=self.agent,
                name=job_name,
                jobs_dir=self.jobs_dir,
                model=None,
                extra_instruction_path=local_candidate_file
                if self.candidate_kind == "instructions"
                else None,
                toolbox_path=local_candidate_file
                if self.candidate_kind == "python_toolbox"
                else None,
                toolbox_sha256=candidate_sha256
                if self.candidate_kind == "python_toolbox"
                else None,
                timeout_seconds=control_timeout,
                attempts=control_attempts,
                concurrency=control_concurrency,
                environment=control_environment,
                provenance=RunProvenance(
                    spec_id=f"gepa-{new_ulid()}",
                    task=declared["task_path"],
                    task_path=declared["task_path"],
                    task_id=task_id,
                    package_digest=declared["task_package_digest"],
                    preamble_path=rel_candidate_path
                    if self.candidate_kind == "instructions"
                    else None,
                    preamble_sha256=candidate_sha256
                    if self.candidate_kind == "instructions"
                    else None,
                    toolbox_path=rel_candidate_path
                    if self.candidate_kind == "python_toolbox"
                    else None,
                    toolbox_sha256=candidate_sha256
                    if self.candidate_kind == "python_toolbox"
                    else None,
                ),
            )
            job_dir = self.executor.execute_direct(request)
            job_record = load_job(job_dir)
            if not _check_job_provenance(
                job_record,
                expected_agent=self.agent,
                expected_model=self.model,
                expected_task_id=task_id,
                expected_package_digest=declared["task_package_digest"],
                expected_candidate_sha256=candidate_sha256,
                expected_ceilings=self.ceilings,
                candidate_kind=self.candidate_kind,
            ):
                raise ProvenanceMismatchError(
                    f"Executed job at {job_dir} failed provenance verification: recorded metadata does not match requested agent/model/task/candidate"
                )
            return self._consume_completed_job(
                job_record,
                declared,
                candidate_sha256,
                local_candidate_file,
            )

        # Model-backed evaluation: verify estimate, reuse pending spec if already queued
        if self.estimated_cost_usd is None or self.estimated_cost_usd <= 0.0:
            raise ValueError(
                f"Model-backed evaluation for agent '{self.agent}' requires a genuine positive estimated_cost_usd, got {self.estimated_cost_usd!r}"
            )

        cand_hex = candidate_sha256.split(":", 1)[-1]
        task_tag = _artifact_task_tag(task_id)
        eval_artifact_path = self.output_dir / "evaluations" / f"{cand_hex}_{task_tag}.json"

        if eval_artifact_path.is_file():
            artifact_data = json.loads(eval_artifact_path.read_text(encoding="utf-8"))
            if any(
                artifact_data.get(key) != expected
                for key, expected in {
                    "agent": self.agent,
                    "model": self.model,
                    "candidate_id": candidate_sha256,
                    "task_id": task_id,
                    "task_package_digest": declared["task_package_digest"],
                }.items()
            ):
                raise ProvenanceMismatchError("Retained evaluation belongs to a different request")
            existing_spec = artifact_data.get("receipt_paths", {}).get("spec_file")
            if artifact_data.get("status") == "pending" and existing_spec:
                recorded_usage = artifact_data.get("usage")
                if self.ceilings is not None and (
                    not isinstance(recorded_usage, dict)
                    or recorded_usage.get("provider_ceilings") != self.ceilings.to_spec_kwargs()
                ):
                    raise ProvenanceMismatchError(
                        "Pending evaluation does not match exact provider ceilings"
                    )
                retained_spec = Path(existing_spec)
                spec_id = retained_spec.stem.rsplit("-", 1)[-1]
                try:
                    existing_spec_path = self.executor.queue.locate(spec_id)
                except ValueError as exc:
                    raise EvaluationUnavailable(
                        f"Retained evaluation {spec_id} is missing or ambiguous in the queue; "
                        "inspect its native queue record rather than automatically resubmitting"
                    ) from exc
                queue_state = existing_spec_path.parent.name
                if queue_state in {"rejected", "failed", "done"}:
                    raise EvaluationUnavailable(
                        f"Evaluation {spec_id} is {queue_state} without a usable completed job; "
                        "inspect the native queue reason before continuing"
                    )
                self._record_evaluation(
                    candidate_sha256=candidate_sha256,
                    local_candidate_file=local_candidate_file,
                    example=declared,
                    status="pending",
                    job_path=None,
                    receipt_paths={
                        "spec_file": str(existing_spec_path),
                        "evaluation_artifact": str(eval_artifact_path),
                    },
                    score=None,
                    rewards={},
                    usage={"estimated_cost_usd": self.estimated_cost_usd},
                    error=None,
                )
                raise EvaluationPending(
                    f"Model evaluation for candidate {candidate_sha256[:16]} on task '{task_id}' is already pending in queue ({existing_spec_path})",
                    spec_id=existing_spec_path.stem.rsplit("-", 1)[-1],
                    spec_path=existing_spec_path,
                    candidate_hash=candidate_sha256,
                    task_id=task_id,
                )

        # Submit new ExperimentSpec to queue with required name pattern, hypothesis, and elicitation purpose
        clean_spec_name = deterministic_job_name(
            campaign_path=self._campaign_path,
            agent=self.agent,
            model=self.model,
            task_id=task_id,
            candidate_sha256=candidate_sha256,
        )
        relative_jobs = self.jobs_dir.relative_to(self.repo_root).as_posix()
        if self.base_spec is not None:
            spec = replay_spec_for_candidate(
                self.base_spec,
                campaign_name=self._campaign_path,
                candidate_path=Path(rel_candidate_path),
                candidate_sha256=candidate_sha256,
                jobs_dir=relative_jobs,
                candidate_kind=self.candidate_kind,
            )
            # The retained spec was frozen for one task; every task-bound digest must be
            # rebound to the example's task, or the queue refuses the run as tampered.
            verifier_digest = (
                compute_task_digests((self.repo_root / declared["task_path"]).resolve()).verifier
                if spec.verifier_digest is not None
                else None
            )
            spec = spec.model_copy(
                update={
                    "spec_id": new_ulid(),
                    "name": clean_spec_name,
                    "task": declared["task_path"],
                    "task_path": declared["task_path"],
                    "task_id": task_id,
                    "task_package_digest": declared["task_package_digest"],
                    "verifier_digest": verifier_digest,
                }
            )
        else:
            spec_kwargs: dict[str, Any] = {
                "spec_id": new_ulid(),
                "name": clean_spec_name,
                "hypothesis": f"{self.candidate_kind} candidate improves performance on {task_id}",
                "purpose": "elicitation",
                "task": declared["task_path"],
                "task_path": declared["task_path"],
                "task_id": task_id,
                "task_package_digest": declared["task_package_digest"],
                "agent": self.agent,
                "model": self.model,
                "timeout_seconds": self.timeout_seconds,
                "est_cost_usd": self.estimated_cost_usd,
                "submitted_by": "gepa-evaluator",
                "jobs_dir": relative_jobs,
            }
            if self.candidate_kind == "python_toolbox":
                spec_kwargs.update(toolbox_path=rel_candidate_path, toolbox_sha256=candidate_sha256)
            else:
                spec_kwargs.update(
                    extra_instruction_path=rel_candidate_path,
                    extra_instruction_sha256=candidate_sha256,
                )
            if self.ceilings is not None:
                spec_kwargs.update(self.ceilings.to_spec_kwargs())
            spec = ExperimentSpec(**spec_kwargs)
        for budget in self.budgets:
            budget.reserve(
                "target",
                clean_spec_name,
                metadata={
                    "spec_id": spec.spec_id,
                    "task_id": task_id,
                    "candidate_id": candidate_sha256,
                    "campaign": self._campaign_path,
                },
            )

        spec_path, decision = self.executor.submit(spec)

        self._record_evaluation(
            candidate_sha256=candidate_sha256,
            local_candidate_file=local_candidate_file,
            example=declared,
            status="pending",
            job_path=None,
            receipt_paths={
                "spec_file": str(spec_path),
                "evaluation_artifact": str(eval_artifact_path),
            },
            score=None,
            rewards={},
            usage={"estimated_cost_usd": self.estimated_cost_usd},
            error=None,
        )

        raise EvaluationPending(
            f"Model evaluation for candidate {candidate_sha256[:16]} on task '{task_id}' is pending in queue ({spec_path})",
            spec_id=spec.spec_id,
            spec_path=spec_path,
            candidate_hash=candidate_sha256,
            task_id=task_id,
            decision=decision,
        )

    def import_seed_evaluation(
        self, job_dir: Path, example: dict[str, Any]
    ) -> tuple[float, dict[str, Any]]:
        """Import one EXACT stock/no-addendum native single-trial job as seed evidence.

        The native job must already be retained inside this evaluator's
        ``repo_root`` (parent copies it under ``research/evidence/runs``) and is
        never modified: metadata, locks and results stay byte-identical. Only the
        evaluator's own receipt under ``output_dir/evaluations`` is written.

        The empty instruction (``""``, ``sha256:e3b0...``) is the sole
        stock/no-addendum equivalence: a native job with zero
        ``extra_instructions`` (or an explicit empty digest) is accepted ONLY for
        that exact empty candidate, never for another digest. A nonempty
        candidate always requires its exact recorded digest.

        Provenance (agent/model/task/package), finished single-trial shape,
        absent addendum/toolbox, frozen ``base_spec`` harness digest and
        behavioral limits/route are all verified. Counted scoring reads the
        authoritative ``<job>/processed/trial-<trial>.json`` counts
        (``evallab.counts/v1``): pass is 1.0, fail is 0.0, excluded/missing/
        malformed/mismatched is :class:`EvaluationUnavailable` with persisted
        score None, never zero. The deterministic target identity is reserved
        before completion exactly once per existing :class:`AggregateBudget`
        (native evidence reuse, never a new paid launch or ``$0`` imputation).
        """
        if self.candidate_kind != "instructions":
            raise ValueError("import_seed_evaluation supports only the instructions candidate kind")
        if COUNTED_VERDICT not in self.score_rules:
            raise ValueError("import_seed_evaluation requires score_rules=['counted_verdict']")
        task_id = example.get("task_id")
        if not task_id or task_id not in self._declared_examples:
            raise ExampleDeclarationError(
                f"Example task '{task_id}' is not an explicitly declared development task"
            )
        declared = self._declared_examples[task_id]
        for key in ("task_id", "task_path", "task_package_digest", "split"):
            if example.get(key) != declared[key]:
                raise ExampleDeclarationError(
                    f"Incoming example field '{key}' differs from immutable declared contract: "
                    f"{example.get(key)!r} != {declared[key]!r}"
                )
        if example.get("oracle_reference") != declared.get("oracle_reference"):
            raise ExampleDeclarationError(
                "Oracle reference differs from frozen development declaration"
            )
        if example.get("prior_run_reference") != declared.get("prior_run_reference"):
            raise ExampleDeclarationError(
                "Prior run reference differs from frozen development declaration"
            )
        abs_task_path = (self.repo_root / declared["task_path"]).resolve()
        current_digest = task_directory_digest(abs_task_path)
        if current_digest != declared["task_package_digest"]:
            raise TaskDigestMismatchError(
                f"Current task directory digest for {task_id} has mutated: "
                f"expected {declared['task_package_digest']}, computed {current_digest}"
            )
        # The stock candidate is exactly the zero-byte artifact.
        candidate_sha256, local_candidate_file, _rel = self._ensure_candidate_stored("")
        if candidate_sha256 != EMPTY_CANDIDATE_SHA256:
            raise AssertionError("empty candidate digest drifted from sha256:e3b0...")
        resolved_job = _validate_in_repo_dir(self.repo_root, Path(job_dir), "seed job")
        if not resolved_job.is_dir() or not (resolved_job / "result.json").is_file():
            raise EvaluationUnavailable(
                f"Seed job at {job_dir} is not a retained Harbor job directory"
            )
        receipt_path = (
            self.output_dir
            / "evaluations"
            / f"{EMPTY_CANDIDATE_SHA256.split(':', 1)[-1]}_{_artifact_task_tag(task_id)}.json"
        )
        if receipt_path.is_file():
            retained = json.loads(receipt_path.read_text(encoding="utf-8"))
            retained_job = retained.get("job_path")
            if retained_job is not None and Path(retained_job).resolve() != resolved_job:
                raise ProvenanceMismatchError(
                    f"Seed receipt at {receipt_path} is already bound to another native job"
                )
        try:
            finished_flag = bool(
                json.loads((resolved_job / "result.json").read_text(encoding="utf-8")).get(
                    "finished_at"
                )
            )
        except (OSError, ValueError) as exc:
            raise EvaluationUnavailable(
                f"Seed job at {job_dir} has no readable finished result: {exc}"
            ) from exc
        if not finished_flag:
            raise EvaluationUnavailable(
                f"Seed job at {job_dir} is unprocessed (no finished_at); restore finished evidence"
            )
        try:
            job_record = load_job(resolved_job)
        except ValueError as exc:
            raise EvaluationUnavailable(f"Seed job at {job_dir} is not consumable: {exc}") from exc
        if not _check_job_provenance(
            job_record,
            expected_agent=self.agent,
            expected_model=self.model,
            expected_task_id=task_id,
            expected_package_digest=declared["task_package_digest"],
            expected_candidate_sha256=EMPTY_CANDIDATE_SHA256,
            expected_ceilings=self.ceilings,
            candidate_kind=self.candidate_kind,
        ):
            raise ProvenanceMismatchError(
                f"Seed job at {resolved_job} does not match exact stock/task/model provenance"
            )
        if not _check_base_spec_binding(
            job_record,
            evaluator_agent=self.agent,
            evaluator_model=self.model,
            evaluator_timeout_seconds=self.timeout_seconds,
            evaluator_ceilings=self.ceilings,
            base_spec=self.base_spec,
        ):
            raise ProvenanceMismatchError(
                f"Seed job at {resolved_job} does not match the frozen harness/limits"
            )
        # Reserve the deterministic target identity once before completing.
        identity = deterministic_job_name(
            campaign_path=self._campaign_path,
            agent=self.agent,
            model=self.model,
            task_id=task_id,
            candidate_sha256=EMPTY_CANDIDATE_SHA256,
        )
        for budget in self.budgets:
            if not budget.is_reserved("target", identity):
                budget.reserve(
                    "target",
                    identity,
                    metadata={
                        "task_id": task_id,
                        "candidate_id": EMPTY_CANDIDATE_SHA256,
                        "campaign": self._campaign_path,
                        "seed_import": True,
                    },
                )
        # Score through the single counted path (never raw-reward fallback).
        return self._consume_completed_job(
            job_record,
            declared,
            EMPTY_CANDIDATE_SHA256,
            local_candidate_file,
        )

    def write_comparison_spec(self, candidate_ids: list[str]) -> Path:
        """Write an exploratory CohortComparisonSpec comparing two or more candidates across actual job paths.

        Args:
            candidate_ids: List of exact full 'sha256:<64-hex>' candidate digests without prefix ambiguity.

        Returns:
            Path to the written CohortComparisonSpec JSON file.
        """
        if len(candidate_ids) < 2:
            raise ValueError(
                f"CohortComparisonSpec requires at least 2 candidates to compare, got {len(candidate_ids)}"
            )

        cohorts: list[CohortSelector] = []
        coverage_summary: dict[str, Any] = {}
        all_declared_task_ids = set(self._declared_examples.keys())

        for cid in candidate_ids:
            if (
                not isinstance(cid, str)
                or not cid.startswith("sha256:")
                or len(cid) != 71
                or not re.match(r"^sha256:[0-9a-f]{64}$", cid)
            ):
                raise ValueError(
                    f"candidate_ids must be exact full 'sha256:<64-hex>' digests without prefix ambiguity, got {cid!r}"
                )

            matching_records = [r for r in self._records if r.candidate_id == cid]
            if not matching_records:
                raise ValueError(f"No evaluation records found for candidate {cid!r}")

            completed_job_paths: list[str] = []
            covered_task_ids: set[str] = set()

            for r in matching_records:
                if r.status == "completed" and r.job_path is not None and Path(r.job_path).is_dir():
                    covered_task_ids.add(r.task_id)
                    try:
                        rel_path = (
                            Path(r.job_path)
                            .resolve()
                            .relative_to(self.repo_root.resolve())
                            .as_posix()
                        )
                    except ValueError:
                        rel_path = str(r.job_path)
                    if rel_path not in completed_job_paths:
                        completed_job_paths.append(rel_path)

            missing_tasks = sorted(all_declared_task_ids - covered_task_ids)
            coverage_summary[cid] = {
                "covered_tasks": sorted(covered_task_ids),
                "missing_tasks": missing_tasks,
                "coverage_ratio": len(covered_task_ids) / max(1, len(all_declared_task_ids)),
                "completed_job_paths": completed_job_paths,
            }

            if not completed_job_paths:
                raise ValueError(
                    f"Candidate {cid!r} has no completed job paths (coverage: {len(covered_task_ids)}/{len(all_declared_task_ids)} tasks)"
                )

            cohort_label = sanitize_cohort_label(cid)
            cohorts.append(CohortSelector(label=cohort_label, paths=completed_job_paths))

        comparison_id = f"gepa-cmp-{uuid.uuid4().hex[:12]}"
        spec = CohortComparisonSpec(
            schema_version=1,
            comparison_id=comparison_id,
            experiment_id="gepa-optimization",
            declared_variable="toolset_digest"
            if self.candidate_kind == "python_toolbox"
            else "preamble_content_sha256",
            mode="exploratory",  # exploratory, no causal claim
            reward_name="reward",
            pass_threshold=1.0,
            pass_k=[1],
            pairing_key="task_digest",
            cohorts=cohorts,
        )

        spec_file = self.output_dir / "comparisons" / f"{comparison_id}.json"
        spec_file.write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")

        coverage_file = self.output_dir / "comparisons" / f"{comparison_id}_coverage.json"
        coverage_file.write_text(json.dumps(coverage_summary, indent=2) + "\n", encoding="utf-8")

        return spec_file
