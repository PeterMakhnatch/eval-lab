"""Deterministic empirical sequence and transition analysis over ATIF/event rows.

Provides ordering, typed transition edge extraction, exact observable motif
detection (repeated tool failure, recovery after failure, verification after action,
post-terminal action leakage), cohort-keyed aggregation with strict opportunity denominators,
explicit preservation of unknown/unexposed evidence, deterministic PyArrow schemas,
and atomic Parquet projections.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq

Outcome = Literal["success", "error", "unknown"]
ActionIntent = Literal["mutation", "verification", "wait", "poll", "other", "unknown"]

MUTATION_FAMILIES = frozenset({"edit", "write", "patch", "modify", "insert", "delete"})
VERIFICATION_FAMILIES = frozenset({"test", "inspect", "search", "verify", "check", "view", "read", "diff"})
TERMINAL_FUNCTIONS = frozenset({
    "submit",
    "finish",
    "complete",
    "terminate",
    "exit_conversation",
    "end_turn",
    "conclude",
})


class TrajectorySequenceError(ValueError):
    """Raised when sequence rows violate schema invariants or trial isolation constraints."""


def _stable_hash(*parts: object) -> str:
    """Deterministic sha256 digest over null-byte separated parts."""
    return hashlib.sha256("\0".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def deterministic_edge_id(
    trial_id: str,
    source_action_id: str,
    target_action_id: str,
    transition_type: str,
) -> str:
    """Generate a deterministic primary key for an action transition edge."""
    return _stable_hash(trial_id, source_action_id, target_action_id, transition_type)


def deterministic_motif_id(
    trial_id: str,
    motif_type: str,
    action_ids: Sequence[str],
) -> str:
    """Generate a deterministic primary key for an observable motif occurrence."""
    return _stable_hash(trial_id, motif_type, ",".join(action_ids))


def deterministic_summary_id(
    cohort_keys_json: str,
    motif_type: str,
) -> str:
    """Generate a deterministic primary key for a motif summary."""
    return _stable_hash(cohort_keys_json, motif_type)


TRAJECTORY_SEQUENCE_SCHEMAS: dict[str, pa.Schema] = {
    "action_transition_edges": pa.schema([
        pa.field("edge_id", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("source_action_id", pa.string(), nullable=False),
        pa.field("source_step_id", pa.int64(), nullable=True),
        pa.field("target_action_id", pa.string(), nullable=False),
        pa.field("target_step_id", pa.int64(), nullable=True),
        pa.field("from_type", pa.string(), nullable=False),
        pa.field("to_type", pa.string(), nullable=False),
        pa.field("transition_type", pa.string(), nullable=False),
        pa.field("source_outcome", pa.string(), nullable=False),
        pa.field("target_outcome", pa.string(), nullable=False),
        pa.field("cohort_keys_json", pa.string(), nullable=False),
        pa.field("provenance_json", pa.string(), nullable=False),
    ]),
    "observable_motifs": pa.schema([
        pa.field("motif_id", pa.string(), nullable=False),
        pa.field("motif_type", pa.string(), nullable=False),
        pa.field("trial_id", pa.string(), nullable=False),
        pa.field("step_ids_json", pa.string(), nullable=False),
        pa.field("action_ids_json", pa.string(), nullable=False),
        pa.field("details_json", pa.string(), nullable=False),
        pa.field("cohort_keys_json", pa.string(), nullable=False),
        pa.field("provenance_json", pa.string(), nullable=False),
    ]),
    "motif_summaries": pa.schema([
        pa.field("summary_id", pa.string(), nullable=False),
        pa.field("cohort_keys_json", pa.string(), nullable=False),
        pa.field("motif_type", pa.string(), nullable=False),
        pa.field("occurrences", pa.int64(), nullable=False),
        pa.field("opportunities", pa.int64(), nullable=False),
        pa.field("rate", pa.float64(), nullable=True),
        pa.field("unknown_evidence_count", pa.int64(), nullable=False),
    ]),
}


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class NormalizedAction:
    """Normalized empirical action representation extracted from row-like inputs."""

    trial_id: str
    action_id: str
    step_id: int | None = None
    ordinal: int | None = None
    timestamp: datetime | None = None
    action_type: str = "other"
    action_family: str = "other"
    function_name: str = "unknown"
    outcome: Outcome = "unknown"
    exit_code: int | None = None
    intent: ActionIntent = "unknown"
    is_terminal: bool = False
    cohort_keys: tuple[tuple[str, str], ...] = ()
    provenance: tuple[tuple[str, str], ...] = ()

    @classmethod
    def from_dict(
        cls,
        data: Mapping[str, Any],
        *,
        cohort_fields: Sequence[str] = (),
        provenance_fields: Sequence[str] = ("document_id", "source_path", "job_id"),
    ) -> NormalizedAction:
        """Create a NormalizedAction from any dict/row mapping, enforcing strict identity validation."""
        raw_trial = data.get("trial_id") if "trial_id" in data else data.get("trial")
        if raw_trial is None or not str(raw_trial).strip():
            raise TrajectorySequenceError("Row is missing required trial identity ('trial_id' or 'trial').")
        trial_id = str(raw_trial).strip()

        step_id_raw = data.get("step_id")
        step_id = int(step_id_raw) if step_id_raw is not None else None

        raw_action_id = (
            data.get("action_id")
            if "action_id" in data and data["action_id"] is not None
            else data.get("event_id")
            if "event_id" in data and data["event_id"] is not None
            else data.get("tool_call_id")
            if "tool_call_id" in data and data["tool_call_id"] is not None
            else None
        )

        if raw_action_id is not None and str(raw_action_id).strip():
            action_id = str(raw_action_id).strip()
        elif step_id is not None:
            action_id = f"step_{step_id}"
        else:
            raise TrajectorySequenceError(
                f"Row in trial '{trial_id}' lacks action identity (action_id/event_id/tool_call_id or explicit step_id)."
            )

        seq_raw = data.get("ordinal") if "ordinal" in data else data.get("sequence")
        ordinal = int(seq_raw) if seq_raw is not None else None

        ts = _parse_timestamp(data.get("timestamp") or data.get("time"))

        function_name = str(data.get("function_name") or data.get("tool_name") or "unknown").strip() or "unknown"
        action_family = str(data.get("action_family") or data.get("family") or "other").strip() or "other"
        action_type = str(data.get("action_type") or action_family or function_name or "other").strip() or "other"

        raw_outcome = data.get("outcome")
        outcome: Outcome
        if raw_outcome in ("success", "error", "unknown"):
            outcome = raw_outcome
        elif raw_outcome is not None:
            norm_out = str(raw_outcome).lower().strip()
            if norm_out in ("success", "ok", "passed", "true", "0"):
                outcome = "success"
            elif norm_out in ("error", "fail", "failed", "exception", "nonzero"):
                outcome = "error"
            else:
                outcome = "unknown"
        else:
            exit_code_val = data.get("exit_code")
            if exit_code_val is not None:
                outcome = "success" if int(exit_code_val) == 0 else "error"
            else:
                outcome = "unknown"

        exit_code_raw = data.get("exit_code")
        exit_code = int(exit_code_raw) if exit_code_raw is not None else None

        raw_intent = data.get("intent")
        intent: ActionIntent
        if raw_intent in ("mutation", "verification", "wait", "poll", "other", "unknown"):
            intent = raw_intent
        elif raw_intent is not None:
            norm_intent = str(raw_intent).lower().strip()
            intent = norm_intent if norm_intent in ("mutation", "verification", "wait", "poll", "other") else "unknown"  # type: ignore[assignment]
        else:
            if action_family in MUTATION_FAMILIES or function_name.lower() in MUTATION_FAMILIES:
                intent = "mutation"
            elif action_family in VERIFICATION_FAMILIES or function_name.lower() in VERIFICATION_FAMILIES:
                intent = "verification"
            else:
                intent = "unknown"

        raw_is_terminal = data.get("is_terminal")
        if raw_is_terminal is not None:
            is_terminal = bool(raw_is_terminal)
        else:
            is_terminal = (
                function_name.lower() in TERMINAL_FUNCTIONS
                or str(data.get("event_type", "")).lower() in ("terminal", "stop", "end")
            )

        cohort_kvs = []
        for k in sorted(cohort_fields):
            if k in data and data[k] is not None:
                cohort_kvs.append((k, str(data[k])))

        prov_kvs = []
        for k in sorted(provenance_fields):
            if k in data and data[k] is not None:
                prov_kvs.append((k, str(data[k])))

        return cls(
            trial_id=trial_id,
            action_id=action_id,
            step_id=step_id,
            ordinal=ordinal,
            timestamp=ts,
            action_type=action_type,
            action_family=action_family,
            function_name=function_name,
            outcome=outcome,
            exit_code=exit_code,
            intent=intent,
            is_terminal=is_terminal,
            cohort_keys=tuple(cohort_kvs),
            provenance=tuple(prov_kvs),
        )


def order_actions(
    actions: Iterable[NormalizedAction | Mapping[str, Any]],
    *,
    cohort_fields: Sequence[str] = (),
) -> list[NormalizedAction]:
    """Order actions deterministically within each trial."""
    normalized: list[NormalizedAction] = [
        a if isinstance(a, NormalizedAction) else NormalizedAction.from_dict(a, cohort_fields=cohort_fields)
        for a in actions
    ]

    by_trial: dict[str, list[NormalizedAction]] = defaultdict(list)
    for act in normalized:
        by_trial[act.trial_id].append(act)

    ordered_result: list[NormalizedAction] = []
    for trial_id in sorted(by_trial.keys()):
        trial_actions = by_trial[trial_id]

        seen_action_ids: set[str] = set()
        for act in trial_actions:
            if act.action_id in seen_action_ids:
                raise TrajectorySequenceError(
                    f"Duplicate action_id '{act.action_id}' in trial '{trial_id}'."
                )
            seen_action_ids.add(act.action_id)

        first_cohort = trial_actions[0].cohort_keys
        for act in trial_actions[1:]:
            if act.cohort_keys != first_cohort:
                raise TrajectorySequenceError(
                    f"Inconsistent cohort_keys within trial '{trial_id}': expected {first_cohort}, got {act.cohort_keys}."
                )

        seen_order_keys: set[tuple[int, int, str]] = set()

        def sort_key(act: NormalizedAction) -> tuple[int, int, str, str]:
            has_ord = 0 if act.ordinal is not None else (1 if act.step_id is not None else 2)
            ord_val = act.ordinal if act.ordinal is not None else (act.step_id if act.step_id is not None else 0)
            ts_val = act.timestamp.isoformat() if act.timestamp is not None else ""
            return (has_ord, ord_val, ts_val, act.action_id)

        for act in trial_actions:
            if act.ordinal is not None or act.step_id is not None:
                has_ord = 0 if act.ordinal is not None else 1
                ord_val = act.ordinal if act.ordinal is not None else (act.step_id or 0)
                ts_val = act.timestamp.isoformat() if act.timestamp is not None else ""
                key = (has_ord, ord_val, ts_val)
                if key in seen_order_keys:
                    raise TrajectorySequenceError(
                        f"Conflicting duplicate order key {key} in trial '{trial_id}' for action '{act.action_id}'."
                    )
                seen_order_keys.add(key)

        sorted_trial = sorted(trial_actions, key=sort_key)
        ordered_result.extend(sorted_trial)

    return ordered_result


@dataclass(frozen=True)
class TransitionEdge:
    """Typed directed edge between two consecutive actions within the same trial."""

    edge_id: str
    trial_id: str
    source_action_id: str
    source_step_id: int | None
    target_action_id: str
    target_step_id: int | None
    from_type: str
    to_type: str
    transition_type: str
    source_outcome: Outcome
    target_outcome: Outcome
    cohort_keys: tuple[tuple[str, str], ...] = ()
    provenance: tuple[tuple[str, str], ...] = ()


def extract_transition_edges(
    actions: Iterable[NormalizedAction | Mapping[str, Any]],
    *,
    cohort_fields: Sequence[str] = (),
    type_field: Literal["action_type", "action_family", "function_name"] = "action_family",
) -> list[TransitionEdge]:
    """Extract transition edges in chronological order strictly within each trial."""
    ordered = order_actions(actions, cohort_fields=cohort_fields)
    by_trial: dict[str, list[NormalizedAction]] = defaultdict(list)
    for act in ordered:
        by_trial[act.trial_id].append(act)

    edges: list[TransitionEdge] = []
    for trial_id in sorted(by_trial.keys()):
        trial_acts = by_trial[trial_id]
        for i in range(len(trial_acts) - 1):
            src = trial_acts[i]
            tgt = trial_acts[i + 1]

            from_type = getattr(src, type_field, src.action_family)
            to_type = getattr(tgt, type_field, tgt.action_family)
            transition_type = f"{from_type}->{to_type}"

            prov_dict = dict(src.provenance)
            for k, v in tgt.provenance:
                if k not in prov_dict:
                    prov_dict[k] = v

            e_id = deterministic_edge_id(trial_id, src.action_id, tgt.action_id, transition_type)

            edges.append(
                TransitionEdge(
                    edge_id=e_id,
                    trial_id=trial_id,
                    source_action_id=src.action_id,
                    source_step_id=src.step_id,
                    target_action_id=tgt.action_id,
                    target_step_id=tgt.step_id,
                    from_type=from_type,
                    to_type=to_type,
                    transition_type=transition_type,
                    source_outcome=src.outcome,
                    target_outcome=tgt.outcome,
                    cohort_keys=src.cohort_keys,
                    provenance=tuple(sorted(prov_dict.items())),
                )
            )

    return edges


@dataclass(frozen=True)
class TransitionAggregation:
    """Aggregated transition rate metrics for a specific (cohort, from_type, to_type)."""

    cohort_keys: tuple[tuple[str, str], ...]
    from_type: str
    to_type: str
    transition_type: str
    count: int
    opportunities: int
    rate: float | None
    unexposed_source_count: int = 0
    unexposed_target_count: int = 0


def aggregate_transitions(
    edges: Iterable[TransitionEdge],
) -> list[TransitionAggregation]:
    """Aggregate transition counts, eligible opportunities, and rates grouped by cohort and from_type."""
    trans_counts: dict[tuple[tuple[tuple[str, str], ...], str, str], int] = defaultdict(int)
    from_opportunities: dict[tuple[tuple[tuple[str, str], ...], str], int] = defaultdict(int)
    unexposed_source: dict[tuple[tuple[tuple[str, str], ...], str, str], int] = defaultdict(int)
    unexposed_target: dict[tuple[tuple[tuple[str, str], ...], str, str], int] = defaultdict(int)

    for edge in edges:
        c_key = edge.cohort_keys
        f_type = edge.from_type
        t_type = edge.to_type
        k = (c_key, f_type, t_type)

        trans_counts[k] += 1
        from_opportunities[(c_key, f_type)] += 1

        if edge.source_outcome == "unknown":
            unexposed_source[k] += 1
        if edge.target_outcome == "unknown":
            unexposed_target[k] += 1

    results: list[TransitionAggregation] = []
    for (c_key, f_type, t_type), count in sorted(trans_counts.items(), key=lambda x: (x[0][0], x[0][1], x[0][2])):
        opp = from_opportunities[(c_key, f_type)]
        rate = (count / opp) if opp > 0 else None
        results.append(
            TransitionAggregation(
                cohort_keys=c_key,
                from_type=f_type,
                to_type=t_type,
                transition_type=f"{f_type}->{t_type}",
                count=count,
                opportunities=opp,
                rate=rate,
                unexposed_source_count=unexposed_source[(c_key, f_type, t_type)],
                unexposed_target_count=unexposed_target[(c_key, f_type, t_type)],
            )
        )

    return results


MotifType = Literal[
    "repeated_tool_failure",
    "recovery_after_failure",
    "verification_after_action",
    "post_terminal_action",
]


@dataclass(frozen=True)
class ObservableMotif:
    """Exact observable sequence motif occurrence."""

    motif_id: str
    motif_type: MotifType
    trial_id: str
    step_ids: tuple[int | None, ...]
    action_ids: tuple[str, ...]
    details: tuple[tuple[str, str], ...] = ()
    cohort_keys: tuple[tuple[str, str], ...] = ()
    provenance: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class MotifSummary:
    """Aggregated motif occurrences, eligible opportunity denominator, and rate per cohort."""

    summary_id: str
    cohort_keys: tuple[tuple[str, str], ...]
    motif_type: MotifType
    occurrences: int
    opportunities: int
    rate: float | None
    unknown_evidence_count: int = 0


def detect_observable_motifs(
    actions: Iterable[NormalizedAction | Mapping[str, Any]],
    *,
    cohort_fields: Sequence[str] = (),
) -> tuple[list[ObservableMotif], list[MotifSummary]]:
    """Detect exact observable motifs in chronological order and compute non-tautological opportunity denominators."""
    ordered = order_actions(actions, cohort_fields=cohort_fields)
    by_trial: dict[str, list[NormalizedAction]] = defaultdict(list)
    for act in ordered:
        by_trial[act.trial_id].append(act)

    motifs: list[ObservableMotif] = []

    cohort_opps: dict[tuple[tuple[tuple[str, str], ...], MotifType], int] = defaultdict(int)
    cohort_occs: dict[tuple[tuple[tuple[str, str], ...], MotifType], int] = defaultdict(int)
    cohort_unknowns: dict[tuple[tuple[tuple[str, str], ...], MotifType], int] = defaultdict(int)
    seen_cohorts: set[tuple[tuple[str, str], ...]] = set()

    for trial_id in sorted(by_trial.keys()):
        trial_acts = by_trial[trial_id]
        n = len(trial_acts)
        if not trial_acts:
            continue

        trial_cohort = trial_acts[0].cohort_keys
        seen_cohorts.add(trial_cohort)

        first_terminal_idx: int | None = None
        for idx, act in enumerate(trial_acts):
            if act.is_terminal:
                first_terminal_idx = idx
                break

        if first_terminal_idx is not None:
            cohort_opps[(trial_cohort, "post_terminal_action")] += 1
            post_term_acts = trial_acts[first_terminal_idx + 1 :]
            if post_term_acts:
                cohort_occs[(trial_cohort, "post_terminal_action")] += 1
                p_act_ids = tuple(p.action_id for p in post_term_acts)
                m_id = deterministic_motif_id(trial_id, "post_terminal_action", p_act_ids)
                motifs.append(
                    ObservableMotif(
                        motif_id=m_id,
                        motif_type="post_terminal_action",
                        trial_id=trial_id,
                        step_ids=tuple(p.step_id for p in post_term_acts),
                        action_ids=p_act_ids,
                        details=(
                            ("terminal_action_id", trial_acts[first_terminal_idx].action_id),
                            ("terminal_step_id", str(trial_acts[first_terminal_idx].step_id)),
                            ("leaked_action_count", str(len(post_term_acts))),
                        ),
                        cohort_keys=trial_cohort,
                        provenance=trial_acts[first_terminal_idx].provenance,
                    )
                )

        for i in range(n):
            act = trial_acts[i]
            if act.outcome == "unknown":
                cohort_unknowns[(trial_cohort, "recovery_after_failure")] += 1
                cohort_unknowns[(trial_cohort, "repeated_tool_failure")] += 1

            if i + 1 < n:
                next_act = trial_acts[i + 1]

                if act.outcome == "error":
                    cohort_opps[(trial_cohort, "recovery_after_failure")] += 1

                    if next_act.outcome == "success":
                        cohort_occs[(trial_cohort, "recovery_after_failure")] += 1
                        m_id = deterministic_motif_id(
                            trial_id, "recovery_after_failure", (act.action_id, next_act.action_id)
                        )
                        motifs.append(
                            ObservableMotif(
                                motif_id=m_id,
                                motif_type="recovery_after_failure",
                                trial_id=trial_id,
                                step_ids=(act.step_id, next_act.step_id),
                                action_ids=(act.action_id, next_act.action_id),
                                details=(
                                    ("failed_function", act.function_name),
                                    ("recovered_function", next_act.function_name),
                                ),
                                cohort_keys=trial_cohort,
                                provenance=act.provenance,
                            )
                        )

                    if act.function_name == next_act.function_name and act.function_name != "unknown":
                        cohort_opps[(trial_cohort, "repeated_tool_failure")] += 1
                        if next_act.outcome == "error":
                            cohort_occs[(trial_cohort, "repeated_tool_failure")] += 1
                            m_id = deterministic_motif_id(
                                trial_id, "repeated_tool_failure", (act.action_id, next_act.action_id)
                            )
                            motifs.append(
                                ObservableMotif(
                                    motif_id=m_id,
                                    motif_type="repeated_tool_failure",
                                    trial_id=trial_id,
                                    step_ids=(act.step_id, next_act.step_id),
                                    action_ids=(act.action_id, next_act.action_id),
                                    details=(
                                        ("function_name", act.function_name),
                                        ("first_exit_code", str(act.exit_code)),
                                        ("second_exit_code", str(next_act.exit_code)),
                                    ),
                                    cohort_keys=trial_cohort,
                                    provenance=act.provenance,
                                )
                            )

                is_mutation = (
                    act.intent == "mutation"
                    or act.action_family in MUTATION_FAMILIES
                    or act.function_name.lower() in MUTATION_FAMILIES
                )
                if is_mutation:
                    cohort_opps[(trial_cohort, "verification_after_action")] += 1
                    is_verification = (
                        next_act.intent == "verification"
                        or next_act.action_family in VERIFICATION_FAMILIES
                        or next_act.function_name.lower() in VERIFICATION_FAMILIES
                    )
                    if is_verification:
                        cohort_occs[(trial_cohort, "verification_after_action")] += 1
                        m_id = deterministic_motif_id(
                            trial_id, "verification_after_action", (act.action_id, next_act.action_id)
                        )
                        motifs.append(
                            ObservableMotif(
                                motif_id=m_id,
                                motif_type="verification_after_action",
                                trial_id=trial_id,
                                step_ids=(act.step_id, next_act.step_id),
                                action_ids=(act.action_id, next_act.action_id),
                                details=(
                                    ("mutation_function", act.function_name),
                                    ("verification_function", next_act.function_name),
                                ),
                                cohort_keys=trial_cohort,
                                provenance=act.provenance,
                            )
                        )

    all_motif_types: tuple[MotifType, ...] = (
        "repeated_tool_failure",
        "recovery_after_failure",
        "verification_after_action",
        "post_terminal_action",
    )

    summaries: list[MotifSummary] = []
    for c_key in sorted(seen_cohorts):
        cohort_json = json.dumps(dict(c_key), sort_keys=True)
        for m_type in all_motif_types:
            opps = cohort_opps[(c_key, m_type)]
            occs = cohort_occs[(c_key, m_type)]
            unknowns = cohort_unknowns[(c_key, m_type)]
            rate = (occs / opps) if opps > 0 else None
            s_id = deterministic_summary_id(cohort_json, m_type)
            summaries.append(
                MotifSummary(
                    summary_id=s_id,
                    cohort_keys=c_key,
                    motif_type=m_type,
                    occurrences=occs,
                    opportunities=opps,
                    rate=rate,
                    unknown_evidence_count=unknowns,
                )
            )

    return motifs, summaries


# --- Parquet Projection and Storage ------------------------------------------


@contextmanager
def _table_lock(path: Path, *, exclusive: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".trajectory_sequence.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_parquet_atomic(
    path: Path,
    table: pa.Table,
) -> None:
    """Atomically write a PyArrow Table to a Parquet file with deterministic options."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
    ) as staged:
        temporary = Path(staged.name)
    try:
        pq.write_table(
            table,
            temporary,
            compression="zstd",
            use_dictionary=False,
            write_statistics=True,
        )
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        with suppress(FileNotFoundError):
            temporary.unlink()


def project_trajectory_sequence_tables(
    actions: Iterable[NormalizedAction | Mapping[str, Any]],
    *,
    output_dir: Path | str,
    cohort_fields: Sequence[str] = (),
    type_field: Literal["action_type", "action_family", "function_name"] = "action_family",
) -> dict[str, Path]:
    """Deterministically project action sequences into atomic relational Parquet tables.

    Produces:
    - action_transition_edges.parquet
    - observable_motifs.parquet
    - motif_summaries.parquet

    Rows inside each Parquet table are sorted by primary key to guarantee byte-level stability.
    """
    out_path = Path(output_dir)
    edges = extract_transition_edges(actions, cohort_fields=cohort_fields, type_field=type_field)
    motifs, summaries = detect_observable_motifs(actions, cohort_fields=cohort_fields)

    # 1. Action Transition Edges Table (sorted by edge_id for deterministic byte stability)
    edge_rows = sorted(
        [
            {
                "edge_id": e.edge_id,
                "trial_id": e.trial_id,
                "source_action_id": e.source_action_id,
                "source_step_id": e.source_step_id,
                "target_action_id": e.target_action_id,
                "target_step_id": e.target_step_id,
                "from_type": e.from_type,
                "to_type": e.to_type,
                "transition_type": e.transition_type,
                "source_outcome": e.source_outcome,
                "target_outcome": e.target_outcome,
                "cohort_keys_json": json.dumps(dict(e.cohort_keys), sort_keys=True),
                "provenance_json": json.dumps(dict(e.provenance), sort_keys=True),
            }
            for e in edges
        ],
        key=lambda r: r["edge_id"],
    )
    edges_table = pa.Table.from_pylist(
        edge_rows,
        schema=TRAJECTORY_SEQUENCE_SCHEMAS["action_transition_edges"],
    )

    # 2. Observable Motifs Table (sorted by motif_id for deterministic byte stability)
    motif_rows = sorted(
        [
            {
                "motif_id": m.motif_id,
                "motif_type": m.motif_type,
                "trial_id": m.trial_id,
                "step_ids_json": json.dumps(list(m.step_ids)),
                "action_ids_json": json.dumps(list(m.action_ids)),
                "details_json": json.dumps(dict(m.details), sort_keys=True),
                "cohort_keys_json": json.dumps(dict(m.cohort_keys), sort_keys=True),
                "provenance_json": json.dumps(dict(m.provenance), sort_keys=True),
            }
            for m in motifs
        ],
        key=lambda r: r["motif_id"],
    )
    motifs_table = pa.Table.from_pylist(
        motif_rows,
        schema=TRAJECTORY_SEQUENCE_SCHEMAS["observable_motifs"],
    )

    # 3. Motif Summaries Table (sorted by summary_id for deterministic byte stability)
    summary_rows = sorted(
        [
            {
                "summary_id": s.summary_id,
                "cohort_keys_json": json.dumps(dict(s.cohort_keys), sort_keys=True),
                "motif_type": s.motif_type,
                "occurrences": s.occurrences,
                "opportunities": s.opportunities,
                "rate": s.rate,
                "unknown_evidence_count": s.unknown_evidence_count,
            }
            for s in summaries
        ],
        key=lambda r: r["summary_id"],
    )
    summaries_table = pa.Table.from_pylist(
        summary_rows,
        schema=TRAJECTORY_SEQUENCE_SCHEMAS["motif_summaries"],
    )

    edges_file = out_path / "action_transition_edges.parquet"
    motifs_file = out_path / "observable_motifs.parquet"
    summaries_file = out_path / "motif_summaries.parquet"

    with _table_lock(edges_file, exclusive=True):
        _write_parquet_atomic(edges_file, edges_table)
        _write_parquet_atomic(motifs_file, motifs_table)
        _write_parquet_atomic(summaries_file, summaries_table)

    return {
        "action_transition_edges": edges_file,
        "observable_motifs": motifs_file,
        "motif_summaries": summaries_file,
    }


def load_trajectory_sequence_table(path: Path | str) -> pa.Table:
    """Load a trajectory sequence table with shared read locking."""
    p = Path(path)
    with _table_lock(p, exclusive=False):
        return pq.read_table(p)
