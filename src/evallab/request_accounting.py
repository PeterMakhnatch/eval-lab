"""Native request accounting records for Harbor and Eval Lab.

Implements the contract defined in:
    lanes/harness/evidence/native-lab-20260908/adapter-contract.json

Key rules enforced:
- RequestAttempt is a typed, frozen record capturing physical send attempts.
- cost is ALWAYS None (asserted, never a parameter; no dollar inference).
- Missing token counts stay null/None (no zero-fill, no interpolation).
- Sealed FACET task_000009 must never appear in any admitted attempt.
- Replay rows carry evidence_level="synthetic_http_replay".
- Late receipts update the same attempt record, not a new request.
- ATIF step count is not request count (both numbers explicitly tracked).
- Export writes request_attempts.parquet + deterministic _accounting.json manifest.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field, replace
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from evallab.results import load_job, load_trial

SEALED_TASK_ID = "task_000009"
DEFAULT_CONTRACT_SHA256 = (
    "f15d0eab1afe3947e38fa84efd55c3e13583778998afd4f9c016e859eb98c6b7"
)
PARQUET_DATASET_NAME = "request_attempts.parquet"
MANIFEST_NAME = "_accounting.json"
KNOWN_ROLES = ("actor", "image_controller")

UNKNOWN_OUTCOMES = frozenset(
    {
        "local_timeout_remote_unknown",
        "cancelled_local_remote_unknown",
    }
)


@dataclass(frozen=True)
class RequestAttempt:
    """A physical HTTP request attempt record from native model harness transport.

    Captures raw Anthropic tokens before LiteLLM normalization, transport status,
    local latencies, and outcome tracking.
    """

    source_file: str
    record_path: str
    role: str
    attempt_ordinal: int
    outcome: str
    transport_state: str | None = None
    http_attempts: int = 1
    http_status: int | None = None
    usage_source: str | None = None
    uncached_input_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cached_tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error_type: str | None = None
    late: bool = False
    complete_by_field: dict[str, bool] = field(default_factory=dict)
    trial_reference: str | None = None
    trial_id: str | None = None
    job_id: str | None = None
    unresolved_trial: bool = False
    task_id: str | None = None
    evidence_level: str = "synthetic_http_replay"
    atif_step_count: int | None = None
    cost: None = field(default=None, init=False)

    def __post_init__(self) -> None:
        assert self.cost is None, "cost must always be None; dollars cannot be inferred"
        assert self.role in KNOWN_ROLES, (
            f"role must be one of {KNOWN_ROLES!r}, got {self.role!r}"
        )
        if self.task_id is not None:
            assert SEALED_TASK_ID not in self.task_id.lower(), (
                f"Sealed {SEALED_TASK_ID} must never appear in task_id"
            )
        if self.trial_reference is not None:
            assert SEALED_TASK_ID not in self.trial_reference.lower(), (
                f"Sealed {SEALED_TASK_ID} must never appear in trial_reference"
            )
        if self.trial_id is not None:
            assert SEALED_TASK_ID not in self.trial_id.lower(), (
                f"Sealed {SEALED_TASK_ID} must never appear in trial_id"
            )


@dataclass(frozen=True)
class ExportSummary:
    """Summary of exported request accounting Parquet dataset and manifest."""

    parquet_path: Path
    manifest_path: Path
    row_count: int
    manifest: dict[str, Any]


REQUEST_ATTEMPTS_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("source_file", pa.string(), nullable=False),
        pa.field("record_path", pa.string(), nullable=False),
        pa.field("evidence_level", pa.string(), nullable=False),
        pa.field("trial_reference", pa.string(), nullable=True),
        pa.field("trial_id", pa.string(), nullable=True),
        pa.field("job_id", pa.string(), nullable=True),
        pa.field("unresolved_trial", pa.bool_(), nullable=False),
        pa.field("task_id", pa.string(), nullable=True),
        pa.field("role", pa.string(), nullable=False),
        pa.field("attempt_ordinal", pa.int64(), nullable=False),
        pa.field("outcome", pa.string(), nullable=False),
        pa.field("transport_state", pa.string(), nullable=True),
        pa.field("http_attempts", pa.int64(), nullable=False),
        pa.field("http_status", pa.int64(), nullable=True),
        pa.field("usage_source", pa.string(), nullable=True),
        pa.field("uncached_input_tokens", pa.int64(), nullable=True),
        pa.field("cache_creation_tokens", pa.int64(), nullable=True),
        pa.field("cached_tokens", pa.int64(), nullable=True),
        pa.field("prompt_tokens", pa.int64(), nullable=True),
        pa.field("completion_tokens", pa.int64(), nullable=True),
        pa.field("error_type", pa.string(), nullable=True),
        pa.field("late", pa.bool_(), nullable=False),
        pa.field("complete_by_field_json", pa.string(), nullable=True),
        pa.field("cost", pa.float64(), nullable=True),
        pa.field("atif_step_count", pa.int64(), nullable=True),
    ]
)


def apply_late_receipt(
    attempt: RequestAttempt,
    *,
    http_status: int | None = None,
    usage_source: str | None = None,
    uncached_input_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    cached_tokens: int | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    error_type: str | None = None,
    complete_by_field: dict[str, bool] | None = None,
    transport_state: str = "response_received",
) -> RequestAttempt:
    """Update an existing attempt record with late-arriving provider usage.

    Enforces contract requirement: late receipts update the same attempt record,
    not a second request.
    """
    updated_completeness = (
        dict(complete_by_field)
        if complete_by_field is not None
        else dict(attempt.complete_by_field)
    )
    if complete_by_field is None:
        if prompt_tokens is not None:
            updated_completeness["prompt_tokens"] = True
        if completion_tokens is not None:
            updated_completeness["completion_tokens"] = True
        if cached_tokens is not None:
            updated_completeness["cached_tokens"] = True

    return replace(
        attempt,
        late=True,
        transport_state=transport_state,
        http_status=http_status if http_status is not None else attempt.http_status,
        usage_source=usage_source if usage_source is not None else attempt.usage_source,
        uncached_input_tokens=(
            uncached_input_tokens
            if uncached_input_tokens is not None
            else attempt.uncached_input_tokens
        ),
        cache_creation_tokens=(
            cache_creation_tokens
            if cache_creation_tokens is not None
            else attempt.cache_creation_tokens
        ),
        cached_tokens=(
            cached_tokens if cached_tokens is not None else attempt.cached_tokens
        ),
        prompt_tokens=(
            prompt_tokens if prompt_tokens is not None else attempt.prompt_tokens
        ),
        completion_tokens=(
            completion_tokens
            if completion_tokens is not None
            else attempt.completion_tokens
        ),
        error_type=error_type if error_type is not None else attempt.error_type,
        complete_by_field=updated_completeness,
    )


def load_replay_attempts(path: Path | str) -> list[RequestAttempt]:
    """Load RequestAttempt records from synthetic HTTP transport replay evidence.

    Handles scenarios dict format as emitted by native-runtime-transport-replay.json,
    as well as sidecar calls[] format.
    """
    p = Path(path)
    data = json.loads(p.read_text())
    source_file = str(p)
    evidence_level = data.get("evidence_level", "synthetic_http_replay")

    attempts: list[RequestAttempt] = []

    # 1. Scenarios layout (native-runtime-transport-replay.json)
    if "scenarios" in data and isinstance(data["scenarios"], dict):
        ordinal = 0
        for scen_name, scen in data["scenarios"].items():
            if not isinstance(scen, dict):
                continue
            if "raw_usage" in scen and isinstance(scen["raw_usage"], dict):
                ordinal += 1
                attempts.append(
                    _attempt_from_raw_usage(
                        raw=scen["raw_usage"],
                        source_file=source_file,
                        record_path=f"scenarios.{scen_name}.raw_usage",
                        evidence_level=evidence_level,
                        attempt_ordinal=ordinal,
                        scenario_meta=scen,
                    )
                )
            elif any(
                isinstance(v, dict) and "raw_usage" in v for v in scen.values()
            ):
                for sub_name, sub_val in scen.items():
                    if isinstance(sub_val, dict) and "raw_usage" in sub_val:
                        ordinal += 1
                        attempts.append(
                            _attempt_from_raw_usage(
                                raw=sub_val["raw_usage"],
                                source_file=source_file,
                                record_path=f"scenarios.{scen_name}.{sub_name}.raw_usage",
                                evidence_level=evidence_level,
                                attempt_ordinal=ordinal,
                                scenario_meta=sub_val,
                            )
                        )
            elif "calls" in scen and isinstance(scen["calls"], list):
                for call_idx, call in enumerate(scen["calls"]):
                    ordinal += 1
                    attempts.append(
                        _attempt_from_call_dict(
                            call=call,
                            source_file=source_file,
                            record_path=f"scenarios.{scen_name}.calls[{call_idx}]",
                            evidence_level=evidence_level,
                            attempt_ordinal=ordinal,
                        )
                    )

    # 2. Sidecar layout (request-accounting.json calls[])
    elif "calls" in data and isinstance(data["calls"], list):
        for call_idx, call in enumerate(data["calls"]):
            attempts.append(
                _attempt_from_call_dict(
                    call=call,
                    source_file=source_file,
                    record_path=f"calls[{call_idx}]",
                    evidence_level=evidence_level,
                    attempt_ordinal=call_idx + 1,
                )
            )

    return attempts


def _attempt_from_raw_usage(
    raw: dict[str, Any],
    source_file: str,
    record_path: str,
    evidence_level: str,
    attempt_ordinal: int,
    scenario_meta: dict[str, Any],
) -> RequestAttempt:
    completeness = scenario_meta.get("complete_by_field") or raw.get(
        "complete_by_field"
    )
    if not completeness:
        completeness = {
            "prompt_tokens": raw.get("prompt_tokens") is not None,
            "completion_tokens": raw.get("completion_tokens") is not None,
            "cached_tokens": raw.get("cached_tokens") is not None,
        }
    else:
        completeness = dict(completeness)

    role = raw.get("role") or scenario_meta.get("role") or "actor"

    return RequestAttempt(
        source_file=source_file,
        record_path=record_path,
        role=role,
        attempt_ordinal=attempt_ordinal,
        outcome=str(raw.get("outcome", "unknown")),
        transport_state=raw.get("transport_state"),
        http_attempts=int(raw.get("http_attempts", 1)),
        http_status=raw.get("http_status"),
        usage_source=raw.get("usage_source"),
        uncached_input_tokens=raw.get("uncached_input_tokens"),
        cache_creation_tokens=raw.get("cache_creation_tokens"),
        cached_tokens=raw.get("cached_tokens"),
        prompt_tokens=raw.get("prompt_tokens"),
        completion_tokens=raw.get("completion_tokens"),
        error_type=raw.get("error_type"),
        late=bool(raw.get("late", False)),
        complete_by_field=completeness,
        trial_reference=None,
        trial_id=None,
        job_id=None,
        unresolved_trial=False,
        task_id=None,
        evidence_level=evidence_level,
        atif_step_count=None,
    )


def _attempt_from_call_dict(
    call: dict[str, Any],
    source_file: str,
    record_path: str,
    evidence_level: str,
    attempt_ordinal: int,
) -> RequestAttempt:
    completeness = {
        "prompt_tokens": call.get("prompt_tokens") is not None,
        "completion_tokens": call.get("completion_tokens") is not None,
        "cached_tokens": call.get("cached_tokens") is not None,
    }
    role = call.get("role", "actor")
    return RequestAttempt(
        source_file=source_file,
        record_path=record_path,
        role=role,
        attempt_ordinal=int(call.get("seq", attempt_ordinal)),
        outcome=str(call.get("outcome", "unknown")),
        transport_state=call.get("transport_state"),
        http_attempts=int(call.get("http_attempts", 1)),
        http_status=call.get("http_status"),
        usage_source=call.get("usage_source"),
        uncached_input_tokens=call.get("uncached_input_tokens"),
        cache_creation_tokens=call.get("cache_creation_tokens"),
        cached_tokens=call.get("cached_tokens"),
        prompt_tokens=call.get("prompt_tokens"),
        completion_tokens=call.get("completion_tokens"),
        error_type=call.get("error_type"),
        late=bool(call.get("late", False)),
        complete_by_field=completeness,
        trial_reference=call.get("trial_reference"),
        trial_id=call.get("trial_id"),
        job_id=call.get("job_id"),
        unresolved_trial=bool(call.get("unresolved_trial", False)),
        task_id=call.get("task_id"),
        evidence_level=evidence_level,
        atif_step_count=call.get("atif_step_count"),
    )


def load_development_usage(
    path: Path | str,
    *,
    repo_root: Path | str | None = None,
) -> list[RequestAttempt]:
    """Load RequestAttempt records from development usage shape, resolving trials.

    Each trial_path is read via evallab.results.load_trial to bind trial_id and
    job_id in a strictly read-only manner.
    If a trial cannot be found or read, it yields an attempt with trial_reference=None,
    trial_id=None, job_id=None, and unresolved_trial=True.
    """
    p = Path(path)
    data = json.loads(p.read_text())
    source_file = str(p)
    evidence_level = data.get(
        "evidence_level", "existing_native_model_trials_not_new_experiments"
    )
    rows = data.get("rows", [])
    resolved_root = Path(repo_root) if repo_root is not None else Path.cwd()

    attempts: list[RequestAttempt] = []

    for idx, row in enumerate(rows):
        trial_path_raw = row.get("trial_path")
        resolved_trial = None
        bound_job_id = None
        trial_reference = None
        unresolved = False

        if trial_path_raw:
            candidate = Path(trial_path_raw)
            resolved_dir = None
            if candidate.is_dir() and (candidate / "result.json").is_file():
                resolved_dir = candidate
            else:
                rel_candidate = resolved_root / candidate
                if rel_candidate.is_dir() and (rel_candidate / "result.json").is_file():
                    resolved_dir = rel_candidate
                elif "runs" in candidate.parts:
                    parts = candidate.parts
                    runs_idx = parts.index("runs")
                    from_runs = resolved_root.joinpath(*parts[runs_idx:])
                    if from_runs.is_dir() and (from_runs / "result.json").is_file():
                        resolved_dir = from_runs

            if resolved_dir is not None:
                try:
                    resolved_trial = load_trial(resolved_dir)
                    trial_reference = resolved_dir.as_posix()
                    parent = resolved_dir.parent
                    if (parent / "result.json").is_file():
                        try:
                            job = load_job(parent)
                            bound_job_id = job.id
                        except Exception:
                            # If parent load_job fails, fallback to parent result id
                            try:
                                parent_res = json.loads(
                                    (parent / "result.json").read_text()
                                )
                                bound_job_id = parent_res.get("id")
                            except Exception:
                                bound_job_id = None
                except Exception:
                    unresolved = True
            else:
                unresolved = True
        else:
            unresolved = True

        trial_id = resolved_trial.id if resolved_trial is not None else None
        task = row.get("task")
        if task is not None:
            assert SEALED_TASK_ID not in str(task).lower(), (
                f"Sealed {SEALED_TASK_ID} must never appear"
            )

        reported = row.get("reported_usage") or {}
        atif_metrics = row.get("atif_final_metrics") or {}
        step_count = row.get("step_count") or atif_metrics.get("total_steps")

        prompt_tokens = reported.get("n_input_tokens") or atif_metrics.get(
            "total_prompt_tokens"
        )
        completion_tokens = reported.get("n_output_tokens") or atif_metrics.get(
            "total_completion_tokens"
        )
        cached_tokens = reported.get("n_cache_tokens") or atif_metrics.get(
            "total_cached_tokens"
        )

        uncached_tokens = None
        if prompt_tokens is not None and cached_tokens is not None:
            uncached_tokens = max(0, prompt_tokens - cached_tokens)

        extra = atif_metrics.get("extra") or {}
        cache_creation = extra.get("total_cache_write_input_tokens")

        exception = row.get("exception")
        reward = row.get("reward")
        outcome = "error" if exception else "success" if reward is not None else "unknown"

        completeness = {
            "prompt_tokens": prompt_tokens is not None,
            "completion_tokens": completion_tokens is not None,
            "cached_tokens": cached_tokens is not None,
        }

        attempts.append(
            RequestAttempt(
                source_file=source_file,
                record_path=f"rows[{idx}]",
                role="actor",
                attempt_ordinal=1,
                outcome=outcome,
                transport_state="response_received",
                http_attempts=1,
                http_status=200 if outcome == "success" else None,
                usage_source="reported_usage",
                uncached_input_tokens=uncached_tokens,
                cache_creation_tokens=cache_creation,
                cached_tokens=cached_tokens,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error_type=str(exception) if exception else None,
                late=False,
                complete_by_field=completeness,
                trial_reference=trial_reference if not unresolved else None,
                trial_id=trial_id if not unresolved else None,
                job_id=bound_job_id if not unresolved else None,
                unresolved_trial=unresolved,
                task_id=task,
                evidence_level=evidence_level,
                atif_step_count=step_count,
            )
        )

    return attempts


def role_summary(attempts: Sequence[RequestAttempt]) -> dict[str, Any]:
    """Group attempts by role with outcomes, known token sums, and ATIF note.

    Token sums are computed ONLY over non-null values and labelled known_*.
    The atif_step_count_is_not_request_count note explicitly carries both numbers
    for every bound trial.
    """
    summary: dict[str, Any] = {}
    roles_present = sorted({a.role for a in attempts} | set(KNOWN_ROLES))

    for role in roles_present:
        role_attempts = [a for a in attempts if a.role == role]
        outcomes_counter = Counter(a.outcome for a in role_attempts)

        known_prompt = sum(
            a.prompt_tokens for a in role_attempts if a.prompt_tokens is not None
        )
        known_completion = sum(
            a.completion_tokens
            for a in role_attempts
            if a.completion_tokens is not None
        )
        known_cached = sum(
            a.cached_tokens for a in role_attempts if a.cached_tokens is not None
        )
        known_uncached = sum(
            a.uncached_input_tokens
            for a in role_attempts
            if a.uncached_input_tokens is not None
        )
        known_cache_creation = sum(
            a.cache_creation_tokens
            for a in role_attempts
            if a.cache_creation_tokens is not None
        )

        late_count = sum(1 for a in role_attempts if a.late)
        unknown_outcome_count = sum(
            1
            for a in role_attempts
            if a.outcome in UNKNOWN_OUTCOMES
            or "unknown" in a.outcome.lower()
            or a.transport_state == "sent_remote_unknown"
        )

        # Reconcile bound trial ATIF step count vs request attempts
        bound_trials: list[dict[str, Any]] = []
        trials_seen: dict[str, list[RequestAttempt]] = {}
        for a in role_attempts:
            key = a.trial_id or a.trial_reference
            if key is not None and a.atif_step_count is not None:
                trials_seen.setdefault(key, []).append(a)

        for key, trial_attempts in sorted(trials_seen.items()):
            first = trial_attempts[0]
            req_count = len(trial_attempts)
            atif_steps = first.atif_step_count or 0
            bound_trials.append(
                {
                    "trial_key": key,
                    "trial_id": first.trial_id,
                    "trial_reference": first.trial_reference,
                    "request_attempts": req_count,
                    "atif_step_count": atif_steps,
                    "counts_differ": req_count != atif_steps,
                }
            )

        total_bound_attempts = sum(bt["request_attempts"] for bt in bound_trials)
        total_bound_atif = sum(bt["atif_step_count"] for bt in bound_trials)

        summary[role] = {
            "attempts": len(role_attempts),
            "terminal_outcomes": dict(sorted(outcomes_counter.items())),
            "known_prompt_tokens": known_prompt,
            "known_completion_tokens": known_completion,
            "known_cached_tokens": known_cached,
            "known_uncached_input_tokens": known_uncached,
            "known_cache_creation_tokens": known_cache_creation,
            "late_count": late_count,
            "unknown_outcome_count": unknown_outcome_count,
            "atif_step_count_is_not_request_count": {
                "note": (
                    "ATIF step counts alone are not physical model-request counts. "
                    "Group calls by role for actor/controller accounting."
                ),
                "total_bound_request_attempts": total_bound_attempts,
                "total_bound_atif_steps": total_bound_atif,
                "bound_trials": bound_trials,
            },
        }

    return summary


def export_request_accounting(
    attempts: Sequence[RequestAttempt],
    output_root: Path | str | None = None,
    *,
    contract_sha256: str | None = None,
    contract_path: Path | str | None = None,
) -> ExportSummary:
    """Export RequestAttempt records to ONE Parquet dataset and a manifest.

    Writes:
      <output_root>/request_attempts.parquet
      <output_root>/_accounting.json

    Manifest contains sorted keys, no timestamps, contract sha256, source paths,
    per-role attempt counts, and a null-field inventory.
    Nulls survive the Parquet round-trip as nulls (never zero-filled).
    Cost is None everywhere.
    """
    out_dir = (
        Path(output_root) if output_root is not None else Path("request-accounting")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    # Determine contract SHA256
    sha = contract_sha256
    if sha is None and contract_path is not None:
        cp = Path(contract_path)
        if cp.is_file():
            sha = hashlib.sha256(cp.read_bytes()).hexdigest()
    if sha is None:
        sha = DEFAULT_CONTRACT_SHA256

    rows: list[dict[str, Any]] = []
    null_counts: dict[str, int] = {
        name: 0 for name in REQUEST_ATTEMPTS_PARQUET_SCHEMA.names
    }

    per_role_counts: dict[str, int] = {role: 0 for role in KNOWN_ROLES}
    source_paths_set: set[str] = set()

    for a in attempts:
        assert a.cost is None, "Cost must be None for every exported attempt"
        if a.role in per_role_counts:
            per_role_counts[a.role] += 1
        else:
            per_role_counts[a.role] = 1

        source_paths_set.add(a.source_file)

        row = {
            "source_file": a.source_file,
            "record_path": a.record_path,
            "evidence_level": a.evidence_level,
            "trial_reference": a.trial_reference,
            "trial_id": a.trial_id,
            "job_id": a.job_id,
            "unresolved_trial": a.unresolved_trial,
            "task_id": a.task_id,
            "role": a.role,
            "attempt_ordinal": a.attempt_ordinal,
            "outcome": a.outcome,
            "transport_state": a.transport_state,
            "http_attempts": a.http_attempts,
            "http_status": a.http_status,
            "usage_source": a.usage_source,
            "uncached_input_tokens": a.uncached_input_tokens,
            "cache_creation_tokens": a.cache_creation_tokens,
            "cached_tokens": a.cached_tokens,
            "prompt_tokens": a.prompt_tokens,
            "completion_tokens": a.completion_tokens,
            "error_type": a.error_type,
            "late": a.late,
            "complete_by_field_json": (
                json.dumps(a.complete_by_field, sort_keys=True)
                if a.complete_by_field
                else None
            ),
            "cost": None,
            "atif_step_count": a.atif_step_count,
        }

        for col_name, val in row.items():
            if val is None:
                null_counts[col_name] += 1

        rows.append(row)

    table = (
        pa.Table.from_pylist(rows, schema=REQUEST_ATTEMPTS_PARQUET_SCHEMA)
        if rows
        else REQUEST_ATTEMPTS_PARQUET_SCHEMA.empty_table()
    )

    parquet_file = out_dir / PARQUET_DATASET_NAME
    tmp_parquet = parquet_file.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp_parquet, compression="zstd")
    tmp_parquet.replace(parquet_file)

    manifest_data = {
        "contract_sha256": sha,
        "null_field_inventory": dict(sorted(null_counts.items())),
        "per_role_attempt_counts": dict(sorted(per_role_counts.items())),
        "row_count": len(attempts),
        "source_paths": sorted(source_paths_set),
    }

    manifest_file = out_dir / MANIFEST_NAME
    tmp_manifest = manifest_file.with_suffix(".json.tmp")
    tmp_manifest.write_text(json.dumps(manifest_data, indent=2, sort_keys=True) + "\n")
    tmp_manifest.replace(manifest_file)

    return ExportSummary(
        parquet_path=parquet_file,
        manifest_path=manifest_file,
        row_count=len(attempts),
        manifest=manifest_data,
    )


def load_exported_accounting(output_root: Path | str) -> list[RequestAttempt]:
    """Read back RequestAttempt records from an exported parquet dataset."""
    root = Path(output_root)
    parquet_file = (
        root if root.name.endswith(".parquet") else root / PARQUET_DATASET_NAME
    )
    table = pq.read_table(parquet_file)
    rows = table.to_pylist()

    attempts: list[RequestAttempt] = []
    for r in rows:
        comp_json = r.get("complete_by_field_json")
        comp = json.loads(comp_json) if comp_json else {}
        attempts.append(
            RequestAttempt(
                source_file=r["source_file"],
                record_path=r["record_path"],
                role=r["role"],
                attempt_ordinal=r["attempt_ordinal"],
                outcome=r["outcome"],
                transport_state=r.get("transport_state"),
                http_attempts=r.get("http_attempts", 1),
                http_status=r.get("http_status"),
                usage_source=r.get("usage_source"),
                uncached_input_tokens=r.get("uncached_input_tokens"),
                cache_creation_tokens=r.get("cache_creation_tokens"),
                cached_tokens=r.get("cached_tokens"),
                prompt_tokens=r.get("prompt_tokens"),
                completion_tokens=r.get("completion_tokens"),
                error_type=r.get("error_type"),
                late=bool(r.get("late", False)),
                complete_by_field=comp,
                trial_reference=r.get("trial_reference"),
                trial_id=r.get("trial_id"),
                job_id=r.get("job_id"),
                unresolved_trial=bool(r.get("unresolved_trial", False)),
                task_id=r.get("task_id"),
                evidence_level=r.get("evidence_level", "synthetic_http_replay"),
                atif_step_count=r.get("atif_step_count"),
            )
        )
    return attempts
