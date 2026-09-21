"""Deterministic, resume-safe repeated attempt matrix for TB4 campaigns.

Provides:
- Deterministic attempt identity derived from sha256(task_id + attempt_index) as Crockford ULID.
- Repeated attempt matrix compilation from a compiled TB4 plan JSON.
- Per-attempt limits defaulting to Z.ai OpenAPI ceilings (max_cost_usd=$2.50, 8h timeout).
- Explicit spend gate: preparing 66x2 authorizes zero executions (authorized=False by default).
- Idempotent resume semantics merging sidecar outcomes without duplicating or hiding.
- Honest charge accounting: failed, timeout, and unknown attempts consume their predicted ceiling.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import ConfigDict, Field

from evallab.campaigns import TrialLimits
from evallab.execution_contracts import new_ulid
from evallab.schemas import ContractModel

# Standard Z.ai OpenAPI per-attempt ceiling defaults
ZAI_OPENAPI_DEFAULT_MAX_REQUESTS: int = 64
ZAI_OPENAPI_DEFAULT_MAX_INPUT_TOKENS: int = 200_000
ZAI_OPENAPI_DEFAULT_MAX_OUTPUT_TOKENS: int = 8_192
ZAI_OPENAPI_DEFAULT_MAX_TOTAL_TOKENS: int = 208_192
ZAI_OPENAPI_DEFAULT_MAX_COST_USD: float = 2.50
DEFAULT_ATTEMPTS_PER_TASK: int = 2

OutcomeStatus = Literal["completed", "failed", "timeout", "unknown"]
ApprovalState = Literal["prepared_not_authorized", "authorized"]


def deterministic_attempt_id(task_id: str, attempt_index: int) -> str:
    """Deterministic, Crockford-encoded 26-char ULID-compatible attempt identity.

    Derived from sha256(f"{task_id}\\0{attempt_index}"):
    - Upper 48 bits map to the timestamp field.
    - Next 80 bits map to the randomness field.
    Guarantees stable, collision-free per-attempt identity across re-preparations
    so partial runs can be resumed idempotently without re-keying attempts.
    """
    if attempt_index < 1:
        raise ValueError(f"attempt_index must be >= 1, got {attempt_index}")
    digest = hashlib.sha256(f"{task_id}\0{attempt_index}".encode()).digest()
    millis = int.from_bytes(digest[:6], "big") & ((1 << 48) - 1)
    randomness = int.from_bytes(digest[6:16], "big") & ((1 << 80) - 1)
    return new_ulid(timestamp_ms=millis, randomness=randomness)


def estimate_attempt_cost(
    model: str,
    timeout_seconds: int,
    limits: TrialLimits | None = None,
) -> float:
    """Predict attempt cost ceiling from the plan's model and timeout (no execution).

    For billable models (e.g. zai/glm-5.3-flash or deepseek/*), this returns the
    per-attempt cost ceiling (limits.max_cost_usd, default $2.50).
    For free control models (e.g. oracle, nop), this returns 0.0.
    """
    del timeout_seconds
    if model in {"oracle", "nop"}:
        return 0.0
    if limits is not None and limits.max_cost_usd > 0:
        return float(limits.max_cost_usd)
    return ZAI_OPENAPI_DEFAULT_MAX_COST_USD


class AttemptOutcome(ContractModel):
    """Retained attempt outcome loaded from an operator sidecar JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str = Field(min_length=1)
    status: OutcomeStatus
    usage: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    timestamp: str | None = None


class RepeatedAttempt(ContractModel):
    """One explicitly numbered attempt of a TB4 task within a repeated matrix."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    task_ref: str = Field(min_length=1)
    attempt_index: int = Field(ge=1)
    task_digest: str = Field(min_length=1)
    model: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    backend: str = Field(min_length=1)
    timeout_seconds: int = Field(ge=1)
    limits: TrialLimits
    predicted_ceiling_usd: float = Field(ge=0.0)
    pins: dict[str, Any] = Field(default_factory=dict)
    approval_state: ApprovalState = "prepared_not_authorized"
    outcome: AttemptOutcome | None = None

    @property
    def predicted_ceiling(self) -> float:
        return self.predicted_ceiling_usd

    @property
    def status(self) -> str:
        return self.outcome.status if self.outcome is not None else "pending"

    @property
    def is_settled(self) -> bool:
        return self.outcome is not None

    @property
    def is_charged(self) -> bool:
        # Completed, failed, timeout, and unknown attempts all consume their predicted ceiling.
        return self.outcome is not None


def _compute_matrix_aggregates(
    attempts: Sequence[RepeatedAttempt],
    task_count: int,
    attempts_per_task: int,
    authorized: bool,
    authorized_by: str | None,
) -> dict[str, Any]:
    total_predicted = round(sum(a.predicted_ceiling_usd for a in attempts), 4)
    # Every settled outcome (completed, failed, timeout, unknown) is charged and consumes its ceiling:
    consumed = round(sum(a.predicted_ceiling_usd for a in attempts if a.outcome is not None), 4)
    remaining = round(max(0.0, total_predicted - consumed), 4)

    summary: dict[str, int] = {
        "pending": sum(1 for a in attempts if a.outcome is None),
        "completed": sum(1 for a in attempts if a.outcome is not None and a.outcome.status == "completed"),
        "failed": sum(1 for a in attempts if a.outcome is not None and a.outcome.status == "failed"),
        "timeout": sum(1 for a in attempts if a.outcome is not None and a.outcome.status == "timeout"),
        "unknown": sum(1 for a in attempts if a.outcome is not None and a.outcome.status == "unknown"),
    }

    if authorized:
        approval_state: ApprovalState = "authorized"
        authorized_executions = summary["pending"]
        gate_statement = (
            f"explicitly authorized by {authorized_by}: {authorized_executions} execution(s) admitted "
            f"(${remaining:.2f} remaining ceiling)"
        )
    else:
        approval_state = "prepared_not_authorized"
        authorized_executions = 0
        gate_statement = (
            f"preparing {task_count}x{attempts_per_task} authorizes zero executions: "
            "paid execution requires explicit operator authorization"
        )

    return {
        "total_predicted_cost_usd": total_predicted,
        "consumed_cost_usd": consumed,
        "remaining_predicted_cost_usd": remaining,
        "authorized": authorized,
        "authorized_executions": authorized_executions,
        "approval_state": approval_state,
        "gate_statement": gate_statement,
        "authorized_by": authorized_by,
        "outcomes_summary": summary,
    }


class RepeatedAttemptMatrix(ContractModel):
    """Immutable, numbered repeated-attempt matrix for a TB4 campaign."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    matrix_version: Literal["repeated-attempt-matrix/v1"] = "repeated-attempt-matrix/v1"
    dataset_ref: str = Field(min_length=1)
    plan_version: str = Field(min_length=1)
    task_count: int = Field(ge=1)
    attempts_per_task: int = Field(ge=1)
    total_attempts: int = Field(ge=1)
    pins: dict[str, Any] = Field(default_factory=dict)
    attempts: tuple[RepeatedAttempt, ...] = Field(min_length=1)
    total_predicted_cost_usd: float = Field(ge=0.0)
    consumed_cost_usd: float = Field(default=0.0, ge=0.0)
    remaining_predicted_cost_usd: float = Field(default=0.0, ge=0.0)
    authorized: bool = False
    authorized_executions: int = 0
    approval_state: ApprovalState = "prepared_not_authorized"
    gate_statement: str = Field(min_length=1)
    authorized_by: str | None = None
    outcomes_summary: dict[str, int] = Field(default_factory=dict)

    def authorize_execution(self, *, operator: str) -> RepeatedAttemptMatrix:
        """Explicit operator authorization flip. Never implicit."""
        if not operator or not operator.strip():
            raise ValueError("Explicit operator name required to authorize executions")
        aggregates = _compute_matrix_aggregates(
            self.attempts,
            self.task_count,
            self.attempts_per_task,
            authorized=True,
            authorized_by=operator.strip(),
        )
        updated_attempts = tuple(
            a.model_copy(update={"approval_state": "authorized"})
            if a.outcome is None
            else a
            for a in self.attempts
        )
        return self.model_copy(
            update={
                "attempts": updated_attempts,
                **aggregates,
            }
        )

    def merge_outcomes(
        self,
        outcomes: Path | str | Sequence[Mapping[str, Any] | AttemptOutcome] | Mapping[str, Any],
    ) -> RepeatedAttemptMatrix:
        """Merge retained attempt outcomes idempotently without duplicating or hiding."""
        loaded_outcomes = load_attempt_outcomes(outcomes)
        attempt_id_set = {a.attempt_id for a in self.attempts}
        for attempt_id in loaded_outcomes:
            if attempt_id not in attempt_id_set:
                raise ValueError(
                    f"Unknown attempt_id {attempt_id!r} in outcomes; does not belong to this matrix"
                )

        updated_attempts: list[RepeatedAttempt] = []
        for attempt in self.attempts:
            if attempt.attempt_id in loaded_outcomes:
                new_outcome = loaded_outcomes[attempt.attempt_id]
                if attempt.outcome is not None and attempt.outcome != new_outcome:
                    raise ValueError(
                        f"Conflicting retained outcome for attempt_id {attempt.attempt_id!r}: "
                        f"existing={attempt.outcome.status}, incoming={new_outcome.status}"
                    )
                updated_attempts.append(attempt.model_copy(update={"outcome": new_outcome}))
            else:
                updated_attempts.append(attempt)

        aggregates = _compute_matrix_aggregates(
            updated_attempts,
            self.task_count,
            self.attempts_per_task,
            authorized=self.authorized,
            authorized_by=self.authorized_by,
        )
        return self.model_copy(
            update={
                "attempts": tuple(updated_attempts),
                **aggregates,
            }
        )


def load_attempt_outcomes(
    source: Path | str | Sequence[Mapping[str, Any] | AttemptOutcome] | Mapping[str, Any],
) -> dict[str, AttemptOutcome]:
    """Load attempt outcome sidecars from a path, directory, or sequence of dicts.

    Fails closed on schema violation or conflicting outcomes.
    """
    raw_list: list[Any] = []
    if isinstance(source, (str, Path)):
        path = Path(source)
        if not path.exists():
            raise FileNotFoundError(f"Outcomes source not found: {path}")
        if path.is_dir():
            for p in sorted(path.glob("*.json")):
                content = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(content, list):
                    raw_list.extend(content)
                elif isinstance(content, dict):
                    if "attempt_id" in content:
                        raw_list.append(content)
                    elif "outcomes" in content and isinstance(content["outcomes"], list):
                        raw_list.extend(content["outcomes"])
                    else:
                        for k, v in content.items():
                            if isinstance(v, dict):
                                raw_list.append({"attempt_id": k, **v})
        else:
            content = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(content, list):
                raw_list.extend(content)
            elif isinstance(content, dict):
                if "attempt_id" in content:
                    raw_list.append(content)
                elif "outcomes" in content and isinstance(content["outcomes"], list):
                    raw_list.extend(content["outcomes"])
                else:
                    for k, v in content.items():
                        if isinstance(v, dict):
                            raw_list.append({"attempt_id": k, **v})
    elif isinstance(source, Mapping):
        if "outcomes" in source and isinstance(source["outcomes"], list):
            raw_list.extend(source["outcomes"])
        elif "attempt_id" in source:
            raw_list.append(source)
        else:
            for k, v in source.items():
                if isinstance(v, dict):
                    raw_list.append({"attempt_id": k, **v})
                elif isinstance(v, AttemptOutcome):
                    raw_list.append(v)
    elif isinstance(source, Sequence):
        raw_list.extend(source)
    else:
        raise TypeError(f"Unsupported outcomes source type: {type(source).__name__}")

    outcomes_by_id: dict[str, AttemptOutcome] = {}
    for item in raw_list:
        outcome = item if isinstance(item, AttemptOutcome) else AttemptOutcome.model_validate(item)
        if outcome.attempt_id in outcomes_by_id:
            existing = outcomes_by_id[outcome.attempt_id]
            if existing != outcome:
                raise ValueError(
                    f"Conflicting retained outcome for attempt_id {outcome.attempt_id!r}: "
                    f"prior={existing.status}, new={outcome.status}"
                )
        else:
            outcomes_by_id[outcome.attempt_id] = outcome

    return outcomes_by_id


def plan_repeated_matrix(
    plan: Path | str | Mapping[str, Any],
    *,
    attempts_per_task: int = DEFAULT_ATTEMPTS_PER_TASK,
    limits: TrialLimits | Mapping[str, Any] | None = None,
    outcomes: Path | str | Sequence[Mapping[str, Any] | AttemptOutcome] | Mapping[str, Any] | None = None,
    authorize: bool = False,
    operator: str | None = None,
    out: Path | str | None = None,
) -> RepeatedAttemptMatrix:
    """Consume a compiled TB4 plan JSON and produce a numbered repeated-attempt matrix.

    - Deterministic attempt IDs per (task_id, attempt_index 1..N).
    - Preserves model, config, harness, and task pins.
    - Limits default to Z.ai OpenAPI ceilings (max_cost_usd=$2.50, max_requests=64, etc.).
    - Backend mapped from task environment (docker for CPU, modal for GPU).
    - Predicted ceiling computed from model and timeout (no execution).
    - Authorization defaults to False (approval_state='prepared_not_authorized').
    - Preparing 66x2 authorizes zero executions unless explicitly authorized with operator.
    - Merges retained outcomes without duplicating or hiding; failed/unknown are charged.
    """
    if attempts_per_task < 1:
        raise ValueError(f"attempts_per_task must be >= 1, got {attempts_per_task}")
    if authorize and (not operator or not operator.strip()):
        raise ValueError("Explicit operator name required to authorize executions")

    if isinstance(plan, (str, Path)):
        plan_path = Path(plan)
        if not plan_path.is_file():
            raise FileNotFoundError(f"Plan file not found: {plan_path}")
        plan_data = json.loads(plan_path.read_text(encoding="utf-8"))
    elif isinstance(plan, Mapping):
        plan_data = dict(plan)
    else:
        raise TypeError(f"Unsupported plan type: {type(plan).__name__}")

    plan_version = plan_data.get("plan_version")
    if plan_version != "tb4-job-plan/1":
        raise ValueError(f"Unsupported or missing plan_version: {plan_version!r} (expected 'tb4-job-plan/1')")

    tasks = plan_data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("Plan contains no tasks")

    dataset_ref = plan_data.get("dataset_ref", "terminal-bench/terminal-bench@4.0.0")
    plan_pins = dict(plan_data.get("pin") or {})
    provider_info = dict(plan_data.get("provider") or {})
    default_agent = provider_info.get("selected_agent") or provider_info.get("agent", "mini-swe-agent")
    default_model = provider_info.get("selected_model", "zai/glm-5.3-flash")

    attempts: list[RepeatedAttempt] = []
    for task in tasks:
        task_id = task["task_id"]
        task_ref = task["task_ref"]
        task_digest = task.get("task_digest", "")
        timeout_seconds = int(task.get("timeout_seconds", plan_data.get("timeout_seconds", 28800)))
        agent = task.get("agent", default_agent)
        model = task.get("model", default_model)
        backend = task.get("environment", "docker")

        if limits is not None:
            attempt_limits = (
                limits
                if isinstance(limits, TrialLimits)
                else TrialLimits.model_validate(limits)
            )
        else:
            attempt_limits = TrialLimits(
                max_requests=ZAI_OPENAPI_DEFAULT_MAX_REQUESTS,
                max_cost_usd=ZAI_OPENAPI_DEFAULT_MAX_COST_USD,
                max_input_tokens=ZAI_OPENAPI_DEFAULT_MAX_INPUT_TOKENS,
                max_output_tokens=ZAI_OPENAPI_DEFAULT_MAX_OUTPUT_TOKENS,
                max_total_tokens=ZAI_OPENAPI_DEFAULT_MAX_TOTAL_TOKENS,
                max_wall_clock_seconds=min(timeout_seconds, 28800),
            )

        predicted_ceiling_usd = estimate_attempt_cost(model, timeout_seconds, attempt_limits)

        task_pins = {
            "dataset_ref": dataset_ref,
            "task_ref": task_ref,
            "task_digest": task_digest,
            "tag": plan_pins.get("tag", "v4.0.0"),
            "commit": plan_pins.get("commit", "452bf30"),
            "license": plan_pins.get("license", "Apache-2.0"),
            "source_identity": plan_data.get("source_identity", "terminal-bench/terminal-bench@4.0.0"),
        }

        for attempt_index in range(1, attempts_per_task + 1):
            attempt_id = deterministic_attempt_id(task_id, attempt_index)
            attempts.append(
                RepeatedAttempt(
                    attempt_id=attempt_id,
                    task_id=task_id,
                    task_ref=task_ref,
                    attempt_index=attempt_index,
                    task_digest=task_digest,
                    model=model,
                    agent=agent,
                    backend=backend,
                    timeout_seconds=timeout_seconds,
                    limits=attempt_limits,
                    predicted_ceiling_usd=predicted_ceiling_usd,
                    pins=task_pins,
                    approval_state="authorized" if authorize else "prepared_not_authorized",
                    outcome=None,
                )
            )

    task_count = len(tasks)
    aggregates = _compute_matrix_aggregates(
        attempts,
        task_count,
        attempts_per_task,
        authorized=authorize,
        authorized_by=operator.strip() if operator else None,
    )

    matrix = RepeatedAttemptMatrix(
        dataset_ref=dataset_ref,
        plan_version=plan_version,
        task_count=task_count,
        attempts_per_task=attempts_per_task,
        total_attempts=len(attempts),
        pins=plan_pins,
        attempts=tuple(attempts),
        **aggregates,
    )

    if outcomes is not None:
        matrix = matrix.merge_outcomes(outcomes)

    if out is not None:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(matrix.model_dump_json(indent=2) + "\n", encoding="utf-8")

    return matrix


def resume_repeated_matrix(
    matrix_or_plan: Path | str | Mapping[str, Any] | RepeatedAttemptMatrix,
    outcomes: Path | str | Sequence[Mapping[str, Any] | AttemptOutcome] | Mapping[str, Any],
    *,
    attempts_per_task: int = DEFAULT_ATTEMPTS_PER_TASK,
    limits: TrialLimits | Mapping[str, Any] | None = None,
    authorize: bool = False,
    operator: str | None = None,
    out: Path | str | None = None,
) -> RepeatedAttemptMatrix:
    """Resume a repeated-attempt matrix by merging retained outcomes.

    If matrix_or_plan is an existing RepeatedAttemptMatrix (or serialized matrix JSON),
    merges outcomes into it.
    If matrix_or_plan is a compiled TB4 plan, compiles the matrix and merges outcomes.
    """
    if isinstance(matrix_or_plan, RepeatedAttemptMatrix):
        matrix = matrix_or_plan
    elif isinstance(matrix_or_plan, Mapping) and matrix_or_plan.get("matrix_version") == "repeated-attempt-matrix/v1":
        matrix = RepeatedAttemptMatrix.model_validate(matrix_or_plan)
    elif isinstance(matrix_or_plan, (str, Path)):
        p = Path(matrix_or_plan)
        if not p.is_file():
            raise FileNotFoundError(f"Matrix or plan file not found: {p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("matrix_version") == "repeated-attempt-matrix/v1":
            matrix = RepeatedAttemptMatrix.model_validate(data)
        else:
            matrix = plan_repeated_matrix(
                data,
                attempts_per_task=attempts_per_task,
                limits=limits,
                authorize=authorize,
                operator=operator,
            )
    else:
        matrix = plan_repeated_matrix(
            matrix_or_plan,
            attempts_per_task=attempts_per_task,
            limits=limits,
            authorize=authorize,
            operator=operator,
        )

    matrix = matrix.merge_outcomes(outcomes)

    if authorize and not matrix.authorized:
        matrix = matrix.authorize_execution(operator=operator or "")

    if out is not None:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(matrix.model_dump_json(indent=2) + "\n", encoding="utf-8")

    return matrix


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.repetition",
        description="Repeated-attempt matrix preparation and resumption",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # prepare subcommand
    prep = subparsers.add_parser("prepare", help="Prepare repeated attempt matrix from TB4 plan")
    prep.add_argument("plan", type=Path, help="Path to compiled TB4 job plan JSON")
    prep.add_argument("--attempts", type=int, default=2, help="Attempts per task (default 2)")
    prep.add_argument("--out", type=Path, help="Output matrix JSON path")
    prep.add_argument("--outcomes", type=Path, help="Retained outcomes JSON/dir sidecar to merge")
    prep.add_argument("--authorize", action="store_true", help="Explicitly authorize execution")
    prep.add_argument("--operator", type=str, help="Operator name (required if --authorize)")
    prep.add_argument("--json", action="store_true", help="Output matrix as JSON to stdout")

    # resume subcommand
    res = subparsers.add_parser("resume", help="Resume matrix with retained attempt outcomes")
    res.add_argument("target", type=Path, help="Path to matrix JSON or TB4 plan JSON")
    res.add_argument("--outcomes", type=Path, required=True, help="Retained outcomes JSON/dir sidecar")
    res.add_argument("--attempts", type=int, default=2, help="Attempts per task if target is plan")
    res.add_argument("--out", type=Path, help="Output updated matrix JSON path")
    res.add_argument("--authorize", action="store_true", help="Explicitly authorize execution")
    res.add_argument("--operator", type=str, help="Operator name (required if --authorize)")
    res.add_argument("--json", action="store_true", help="Output matrix as JSON to stdout")

    args = parser.parse_args(argv)

    if args.command == "prepare":
        matrix = plan_repeated_matrix(
            args.plan,
            attempts_per_task=args.attempts,
            outcomes=args.outcomes,
            authorize=args.authorize,
            operator=args.operator,
            out=args.out,
        )
    elif args.command == "resume":
        matrix = resume_repeated_matrix(
            args.target,
            outcomes=args.outcomes,
            attempts_per_task=args.attempts,
            authorize=args.authorize,
            operator=args.operator,
            out=args.out,
        )
    else:
        parser.print_help()
        return 1

    if args.json:
        print(matrix.model_dump_json(indent=2))
    else:
        print(f"matrix: {matrix.matrix_version} ({matrix.task_count} tasks x {matrix.attempts_per_task} attempts = {matrix.total_attempts} total)")
        print(f"predicted ceiling: ${matrix.total_predicted_cost_usd:.2f} (consumed: ${matrix.consumed_cost_usd:.2f}, remaining: ${matrix.remaining_predicted_cost_usd:.2f})")
        print(f"approval state: {matrix.approval_state} (authorized: {matrix.authorized}, executions: {matrix.authorized_executions})")
        print(f"gate: {matrix.gate_statement}")
        print(f"outcomes: {matrix.outcomes_summary}")
        if args.out:
            print(f"written: {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
