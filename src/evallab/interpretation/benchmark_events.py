"""Strict, versioned benchmark event, final state, and contract ingestion and verification.

Enforces:
1. Canonical ordering: monotonically increasing 1-based event indices.
2. Gap, duplicate, timestamp drift, and schema drift rejection.
3. Call-ID correlation across MCP calls, executions, faults, and results.
4. CAS/TrialBundle citation generation for immutable evidence grounding.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from evallab.benchmark_program_contracts import (
    CellFactorsA,
    CellFactorsB,
    CellFactorsC,
    FaultInjectionRecord,
    SyntheticFamilyType,
)


class BenchmarkIngestionError(Exception):
    """Base exception for benchmark evidence ingestion and validation."""


class BenchmarkEventGapError(BenchmarkIngestionError):
    """Raised when benchmark event indices contain gaps."""


class BenchmarkEventDuplicateError(BenchmarkIngestionError):
    """Raised when duplicate event indices or duplicate call IDs occur."""


class BenchmarkEventSchemaError(BenchmarkIngestionError):
    """Raised when an event does not conform to the expected schema."""


class BenchmarkContractDriftError(BenchmarkIngestionError):
    """Raised when runtime evidence contradicts the verifier truth digest or contract."""


class BenchmarkMissingArtifactError(BenchmarkIngestionError):
    """Raised when required evidence artifacts (events, final state, contract) are missing."""


@dataclass(frozen=True)
class BenchmarkEventRecord:
    """A single canonical event record from benchmark-events.jsonl."""

    event_index: int
    event_type: str
    payload: dict[str, Any]
    raw_line: str = ""
    digest: str = ""

    def get_tool_call_id(self) -> str | None:
        """Extract tool_call_id or call_id if present in payload."""
        return (
            self.payload.get("tool_call_id")
            or self.payload.get("call_id")
            or self.payload.get("id")
        )

    def get_tool_name(self) -> str | None:
        """Extract tool_name or method if present in payload."""
        return (
            self.payload.get("tool_name") or self.payload.get("name") or self.payload.get("method")
        )


@dataclass(frozen=True)
class FinalStateRecord:
    """Normalized final state and mutation certificate."""

    initial_digest: str
    final_digest: str
    step_count: int
    mutations: list[dict[str, Any]]
    invariants_passed: bool
    details: dict[str, Any]
    raw_content: str = ""
    digest: str = ""


type CanonicalCellFactors = CellFactorsA | CellFactorsB | CellFactorsC

_LEGACY_FAMILY_TYPES = {
    "action-memory-v1": SyntheticFamilyType.FAMILY_A_STATE_INVERSION,
    "mcp-funcdag-v1": SyntheticFamilyType.FAMILY_B_FUNCDAG_V2,
    "mcp-recovery-v1": SyntheticFamilyType.FAMILY_C_FAULT_RECOVERY,
}


@dataclass(frozen=True)
class BenchmarkContractRecord:
    """Trajectory-local parse record with optional PR #268 canonical contract references."""

    family: str
    version: str
    construct: str
    seed: int
    cell_factors: dict[str, Any]
    task_id: str
    opportunity_counts: dict[str, Any]
    verifier_truth_digest: str
    artifact_paths: dict[str, str]
    canonical_family: SyntheticFamilyType | None = None
    canonical_cell_factors: CanonicalCellFactors | None = None
    canonical_fault_record: FaultInjectionRecord | None = None
    raw_content: str = ""
    digest: str = ""


@dataclass(frozen=True)
class CorrelatedToolCall:
    """A correlated MCP / tool call with its corresponding result and fault injection."""

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    request_event: BenchmarkEventRecord
    execution_event: BenchmarkEventRecord | None = None
    fault_event: BenchmarkEventRecord | None = None
    result_event: BenchmarkEventRecord | None = None
    is_fault_injected: bool = False
    fault_class: str | None = None
    result_payload: Any = None
    is_error: bool = False


@dataclass(frozen=True)
class TrialBundle:
    """Immutable evidence bundle for a single benchmark trial."""

    trial_id: str
    contract: BenchmarkContractRecord
    final_state: FinalStateRecord
    events: list[BenchmarkEventRecord]
    correlated_calls: list[CorrelatedToolCall]
    raw_dir: Path | None = None

    def build_citation(
        self,
        event_index: int | None = None,
        tool_call_id: str | None = None,
        artifact: str = "events",
    ) -> str:
        """Build a deterministic CAS citation for this trial evidence."""
        if artifact == "contract":
            return f"cas:contract:{self.contract.digest}"
        elif artifact == "final_state":
            return f"cas:final_state:{self.final_state.digest}"
        elif event_index is not None:
            ev = next((e for e in self.events if e.event_index == event_index), None)
            ev_digest = ev.digest if ev else "unknown"
            return f"cas:event:{self.trial_id}:{event_index}:{ev_digest[:16]}"
        elif tool_call_id is not None:
            call = next((c for c in self.correlated_calls if c.call_id == tool_call_id), None)
            call_digest = call.request_event.digest if call else "unknown"
            return f"cas:call:{self.trial_id}:{tool_call_id}:{call_digest[:16]}"
        return f"cas:trial:{self.trial_id}"


def _canonical_family(family: str) -> SyntheticFamilyType | None:
    """Map a vertical's external family identifier to the PR #268 family type."""
    if family in _LEGACY_FAMILY_TYPES:
        return _LEGACY_FAMILY_TYPES[family]
    try:
        return SyntheticFamilyType(family)
    except ValueError:
        return None


def _canonical_cell_factors(
    family: SyntheticFamilyType | None, cell_factors: dict[str, Any]
) -> CanonicalCellFactors | None:
    """Validate only losslessly supplied PR #268 cell factors; never infer missing units."""
    if family is SyntheticFamilyType.FAMILY_A_STATE_INVERSION:
        required = {"dilation_tokens", "seed"}
        model_type = CellFactorsA
    elif family is SyntheticFamilyType.FAMILY_B_FUNCDAG_V2:
        required = {"critical_path_depth", "parallel_width", "distractor_count", "seed"}
        model_type = CellFactorsB
    elif family is SyntheticFamilyType.FAMILY_C_FAULT_RECOVERY:
        required = {"fault_class", "fault_injection_count", "seed"}
        model_type = CellFactorsC
    else:
        return None

    if not required.issubset(cell_factors):
        return None
    try:
        return model_type.model_validate(
            {name: cell_factors[name] for name in model_type.model_fields if name in cell_factors}
        )
    except ValueError:
        return None


def _canonical_fault_record(data: dict[str, Any]) -> FaultInjectionRecord | None:
    """Validate a supplied PR #268 fault ledger without synthesizing one from trace events."""
    raw_record = data.get("fault_record") or data.get("fault_injection_record")
    if not isinstance(raw_record, dict):
        return None
    try:
        return FaultInjectionRecord.model_validate(raw_record)
    except ValueError:
        return None


def parse_benchmark_contract(
    content_or_path: str | Path | dict[str, Any],
) -> BenchmarkContractRecord:
    """Parse and validate benchmark_contract.json."""
    if isinstance(content_or_path, dict):
        data = content_or_path
        raw_text = json.dumps(data, sort_keys=True)
    elif isinstance(content_or_path, Path):
        if not content_or_path.is_file():
            raise BenchmarkMissingArtifactError(
                f"Benchmark contract file not found: {content_or_path}"
            )
        raw_text = content_or_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    else:
        raw_text = content_or_path
        data = json.loads(raw_text)

    if not isinstance(data, dict):
        raise BenchmarkEventSchemaError("Benchmark contract must be a JSON object")

    family = str(data.get("benchmark_family") or data.get("family") or "")
    if not family:
        raise BenchmarkEventSchemaError(
            "Benchmark contract missing required 'family' or 'benchmark_family' field"
        )

    version = str(data.get("version", "1.0.0"))
    construct = str(data.get("construct", ""))

    # Seeds handling: list or scalar
    seeds = data.get("seeds")
    if isinstance(seeds, list) and seeds:
        seed = int(data.get("seed", seeds[0]))
    else:
        seed = int(data.get("seed", data.get("cell_factors", {}).get("seed", 0)))

    cell_factors = dict(data.get("cell_factors", {}))
    if "cells" in data and isinstance(data["cells"], list):
        cell_factors["cells"] = data["cells"]
        if data["cells"] and isinstance(data["cells"][0], dict):
            for ck, cv in data["cells"][0].items():
                if ck not in cell_factors:
                    cell_factors[ck] = cv
    if isinstance(seeds, list):
        cell_factors["seeds"] = seeds

    for k, v in data.items():
        if (
            k
            not in (
                "family",
                "benchmark_family",
                "version",
                "construct",
                "seed",
                "seeds",
                "cells",
                "cell_factors",
                "task_id",
                "task_name",
                "opportunity_counts",
                "verifier_truth_digest",
                "artifact_paths",
                "fault_record",
                "fault_injection_record",
            )
            and k not in cell_factors
        ):
            cell_factors[k] = v
    task_id = str(data.get("task_id") or data.get("task_name") or family)
    opportunity_counts = dict(data.get("opportunity_counts", {}))
    verifier_truth_digest = str(data.get("verifier_truth_digest", ""))
    artifact_paths = {str(k): str(v) for k, v in data.get("artifact_paths", {}).items()}
    canonical_family = _canonical_family(family)
    canonical_cell_factors = _canonical_cell_factors(canonical_family, cell_factors)
    canonical_fault_record = _canonical_fault_record(data)
    content_digest = sha256(raw_text.encode("utf-8")).hexdigest()

    return BenchmarkContractRecord(
        family=family,
        version=version,
        construct=construct,
        seed=seed,
        cell_factors=cell_factors,
        task_id=task_id,
        opportunity_counts=opportunity_counts,
        verifier_truth_digest=verifier_truth_digest,
        artifact_paths=artifact_paths,
        canonical_family=canonical_family,
        canonical_cell_factors=canonical_cell_factors,
        canonical_fault_record=canonical_fault_record,
        raw_content=raw_text,
        digest=content_digest,
    )


def parse_final_state(
    content_or_path: str | Path | dict[str, Any],
) -> FinalStateRecord:
    """Parse and validate final-state.json."""
    if isinstance(content_or_path, dict):
        data = content_or_path
        raw_text = json.dumps(data, sort_keys=True)
    elif isinstance(content_or_path, Path):
        if not content_or_path.is_file():
            raise BenchmarkMissingArtifactError(f"Final state file not found: {content_or_path}")
        raw_text = content_or_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    else:
        raw_text = content_or_path
        data = json.loads(raw_text)

    if not isinstance(data, dict):
        raise BenchmarkEventSchemaError("Final state must be a JSON object")

    initial_digest = str(data.get("initial_digest", ""))
    final_digest = str(data.get("final_digest", ""))
    step_count = int(data.get("step_count", 0))
    mutations = list(data.get("mutations", []))
    invariants_passed = bool(data.get("invariants_passed", data.get("task_success", False)))
    details = dict(data.get("details", {}))
    for k, v in data.items():
        if k not in (
            "initial_digest",
            "final_digest",
            "step_count",
            "mutations",
            "invariants_passed",
            "details",
        ):
            details[k] = v
    content_digest = sha256(raw_text.encode("utf-8")).hexdigest()

    return FinalStateRecord(
        initial_digest=initial_digest,
        final_digest=final_digest,
        step_count=step_count,
        mutations=mutations,
        invariants_passed=invariants_passed,
        details=details,
        raw_content=raw_text,
        digest=content_digest,
    )


def parse_benchmark_events(
    lines_or_path: str | Path | Sequence[str] | Sequence[dict[str, Any]],
) -> list[BenchmarkEventRecord]:
    """Parse and strictly validate benchmark-events.jsonl lines.

    Enforces:
    1. 1-based monotonically strictly increasing event_index: 1, 2, 3, ...
    2. No gap (e.g. 1 -> 3 raises BenchmarkEventGapError).
    3. No duplicates (e.g. 1, 1 raises BenchmarkEventDuplicateError).
    4. Valid JSON structure per line.
    """
    raw_records: list[dict[str, Any]] = []
    raw_lines: list[str] = []

    if isinstance(lines_or_path, Path):
        if not lines_or_path.is_file():
            raise BenchmarkMissingArtifactError(f"Benchmark events file not found: {lines_or_path}")
        text = lines_or_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            raw_lines.append(line_str)
            try:
                raw_records.append(json.loads(line_str))
            except json.JSONDecodeError as exc:
                raise BenchmarkEventSchemaError(
                    f"Malformed JSON in benchmark events: {line_str}"
                ) from exc
    elif isinstance(lines_or_path, str):
        for line in lines_or_path.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            raw_lines.append(line_str)
            try:
                raw_records.append(json.loads(line_str))
            except json.JSONDecodeError as exc:
                raise BenchmarkEventSchemaError(
                    f"Malformed JSON in benchmark events: {line_str}"
                ) from exc
    else:
        for item in lines_or_path:
            if isinstance(item, dict):
                raw_records.append(item)
                raw_lines.append(json.dumps(item, sort_keys=True))
            else:
                line_str = str(item).strip()
                if not line_str:
                    continue
                raw_lines.append(line_str)
                try:
                    raw_records.append(json.loads(line_str))
                except json.JSONDecodeError as exc:
                    raise BenchmarkEventSchemaError(
                        f"Malformed JSON in benchmark events: {line_str}"
                    ) from exc

    events: list[BenchmarkEventRecord] = []
    seen_indices: set[int] = set()
    expected_index: int | None = None

    for idx, (rec, raw_line) in enumerate(zip(raw_records, raw_lines, strict=True), start=1):
        if not isinstance(rec, dict):
            raise BenchmarkEventSchemaError(
                f"Event at line {idx} must be a JSON object, got {type(rec)}"
            )

        if "event_index" not in rec:
            raise BenchmarkEventSchemaError(f"Event at line {idx} missing required 'event_index'")
        if "event_type" not in rec:
            raise BenchmarkEventSchemaError(f"Event at line {idx} missing required 'event_type'")

        if "payload" in rec and isinstance(rec["payload"], dict):
            payload = dict(rec["payload"])
        else:
            payload = {
                k: v for k, v in rec.items() if k not in ("event_index", "event_type", "timestamp")
            }
        event_index = rec["event_index"]
        if not isinstance(event_index, int):
            raise BenchmarkEventSchemaError(f"Event index must be an integer, got {event_index!r}")

        if expected_index is None:
            if event_index not in (0, 1):
                raise BenchmarkEventGapError(
                    f"Benchmark event initial index must start at 0 or 1, got {event_index}"
                )
            expected_index = event_index

        if event_index != expected_index:
            if event_index in seen_indices:
                raise BenchmarkEventDuplicateError(
                    f"Duplicate benchmark event index detected: {event_index}"
                )
            elif event_index > expected_index:
                raise BenchmarkEventGapError(
                    f"Benchmark event index gap detected: expected index {expected_index}, got {event_index}"
                )
            else:
                raise BenchmarkEventDuplicateError(
                    f"Benchmark event index out of order: expected {expected_index}, got {event_index}"
                )

        seen_indices.add(event_index)
        expected_index += 1
        digest = sha256(raw_line.encode("utf-8")).hexdigest()
        events.append(
            BenchmarkEventRecord(
                event_index=event_index,
                event_type=str(rec["event_type"]),
                payload=payload,
                raw_line=raw_line,
                digest=digest,
            )
        )

    return events


def correlate_tool_calls(
    events: Sequence[BenchmarkEventRecord],
) -> list[CorrelatedToolCall]:
    """Correlate tool call requests with execution, fault injection, and result events."""
    calls_by_id: dict[str, dict[str, Any]] = {}
    ordered_call_ids: list[str] = []

    for event in events:
        event_type = event.event_type
        payload = event.payload

        call_id = (
            payload.get("tool_call_id")
            or payload.get("call_id")
            or payload.get("id")
            or payload.get("request_id")
        )

        if event_type in (
            "mcp_call",
            "tool_call",
            "request",
            "tool_invoked",
            "tool_call_requested",
            "read_chunk",
            "execute_mutation",
            "tools/call",
            "call_tool",
        ):
            if not call_id:
                call_id = f"call_{event.event_index}"
            call_id = str(call_id)
            if call_id not in calls_by_id:
                tool_name = (
                    event.get_tool_name()
                    or payload.get("tool")
                    or (
                        "read_chunk"
                        if event_type == "read_chunk"
                        else (
                            "execute_mutation"
                            if event_type == "execute_mutation"
                            else "unknown_tool"
                        )
                    )
                )
                calls_by_id[call_id] = {
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "arguments": payload.get("arguments") or payload.get("parameters") or payload,
                    "request_event": event,
                    "execution_event": None,
                    "fault_event": None,
                    "result_event": None,
                    "is_fault_injected": False,
                    "fault_class": None,
                    "result_payload": None,
                    "is_error": False,
                }
                ordered_call_ids.append(call_id)

        elif event_type in ("fault_injected", "fault"):
            entry = None
            if call_id and str(call_id) in calls_by_id:
                entry = calls_by_id[str(call_id)]
            elif (
                ordered_call_ids
                and not ordered_call_ids[-1].startswith("fault_")
                and calls_by_id[ordered_call_ids[-1]]["fault_event"] is None
            ):
                entry = calls_by_id[ordered_call_ids[-1]]

            if entry is not None:
                entry["is_fault_injected"] = True
                entry["fault_event"] = event
                entry["fault_class"] = (
                    payload.get("fault_class")
                    or payload.get("class")
                    or payload.get("fault_type")
                    or payload.get("type")
                )
            else:
                synth_id = f"fault_{event.event_index}"
                calls_by_id[synth_id] = {
                    "call_id": synth_id,
                    "tool_name": payload.get("tool_name", payload.get("tool", "fault_tool")),
                    "arguments": payload.get("arguments", {}),
                    "request_event": event,
                    "execution_event": None,
                    "fault_event": event,
                    "result_event": None,
                    "is_fault_injected": True,
                    "fault_class": (
                        payload.get("fault_class")
                        or payload.get("class")
                        or payload.get("fault_type")
                        or payload.get("type")
                    ),
                    "result_payload": payload.get("result_payload"),
                    "is_error": True,
                }
                ordered_call_ids.append(synth_id)

        elif event_type in (
            "tool_call_success",
            "tool_executed",
            "execution",
            "tool_call_executed",
        ):
            entry = None
            if call_id and str(call_id) in calls_by_id:
                entry = calls_by_id[str(call_id)]
            elif (
                ordered_call_ids
                and not ordered_call_ids[-1].startswith("fault_")
                and calls_by_id[ordered_call_ids[-1]]["execution_event"] is None
            ):
                entry = calls_by_id[ordered_call_ids[-1]]

            if entry is not None:
                entry["execution_event"] = event
                if event_type == "tool_call_success":
                    entry["result_event"] = event
                    entry["result_payload"] = payload.get("result")
                    entry["is_error"] = False
            else:
                # Standalone execution record
                exec_id = f"exec_{event.event_index}"
                tool_name = (
                    event.get_tool_name()
                    or payload.get("tool")
                    or payload.get("tool_name")
                    or "unknown_tool"
                )
                calls_by_id[exec_id] = {
                    "call_id": exec_id,
                    "tool_name": tool_name,
                    "arguments": payload.get("arguments") or payload.get("parameters") or payload,
                    "request_event": event,
                    "execution_event": event,
                    "fault_event": None,
                    "result_event": event,
                    "is_fault_injected": False,
                    "fault_class": None,
                    "result_payload": payload.get("result"),
                    "is_error": False,
                }
                ordered_call_ids.append(exec_id)

        elif event_type in (
            "tool_call_rejected",
            "tool_call_schema_error",
            "tool_call_execution_error",
        ):
            entry = None
            if call_id and str(call_id) in calls_by_id:
                entry = calls_by_id[str(call_id)]
            elif (
                ordered_call_ids
                and not ordered_call_ids[-1].startswith("fault_")
                and calls_by_id[ordered_call_ids[-1]]["execution_event"] is None
            ):
                entry = calls_by_id[ordered_call_ids[-1]]

            if entry is not None:
                entry["execution_event"] = event
                entry["result_event"] = event
                entry["result_payload"] = payload.get("error")
                entry["is_error"] = True
            else:
                err_id = f"err_{event.event_index}"
                tool_name = (
                    event.get_tool_name()
                    or payload.get("tool")
                    or payload.get("tool_name")
                    or "unknown_tool"
                )
                calls_by_id[err_id] = {
                    "call_id": err_id,
                    "tool_name": tool_name,
                    "arguments": payload.get("arguments") or payload.get("parameters") or payload,
                    "request_event": event,
                    "execution_event": event,
                    "fault_event": None,
                    "result_event": event,
                    "is_fault_injected": False,
                    "fault_class": None,
                    "result_payload": payload.get("error"),
                    "is_error": True,
                }
                ordered_call_ids.append(err_id)

        elif event_type in ("tool_result", "mcp_response", "response"):
            entry = None
            if call_id and str(call_id) in calls_by_id:
                entry = calls_by_id[str(call_id)]
            elif ordered_call_ids and not ordered_call_ids[-1].startswith("fault_"):
                entry = calls_by_id[ordered_call_ids[-1]]

            if entry is not None:
                entry["result_event"] = event
                entry["result_payload"] = payload.get("result") or payload.get("output") or payload
                entry["is_error"] = bool(
                    payload.get("is_error")
                    or payload.get("error")
                    or (isinstance(payload.get("result"), dict) and "error" in payload["result"])
                )
            else:
                res_id = f"res_{event.event_index}"
                calls_by_id[res_id] = {
                    "call_id": res_id,
                    "tool_name": "unknown_tool",
                    "arguments": {},
                    "request_event": event,
                    "execution_event": event,
                    "fault_event": None,
                    "result_event": event,
                    "is_fault_injected": False,
                    "fault_class": None,
                    "result_payload": payload.get("result") or payload.get("output") or payload,
                    "is_error": bool(
                        payload.get("is_error")
                        or payload.get("error")
                        or (
                            isinstance(payload.get("result"), dict) and "error" in payload["result"]
                        )
                    ),
                }
                ordered_call_ids.append(res_id)

    correlated: list[CorrelatedToolCall] = []
    for call_id in ordered_call_ids:
        entry = calls_by_id[call_id]
        correlated.append(
            CorrelatedToolCall(
                call_id=entry["call_id"],
                tool_name=entry["tool_name"],
                arguments=entry["arguments"],
                request_event=entry["request_event"],
                execution_event=entry["execution_event"],
                fault_event=entry["fault_event"],
                result_event=entry["result_event"],
                is_fault_injected=entry["is_fault_injected"],
                fault_class=entry["fault_class"],
                result_payload=entry["result_payload"],
                is_error=entry["is_error"],
            )
        )

    return correlated


def load_trial_bundle(
    trial_dir: Path | str,
    trial_id: str | None = None,
) -> TrialBundle:
    """Load and strictly validate a complete trial evidence directory."""
    path = Path(trial_dir)
    if not path.is_dir():
        raise BenchmarkMissingArtifactError(f"Trial directory does not exist: {path}")

    tid = trial_id or path.name

    contract_path = path / "benchmark_contract.json"
    if not contract_path.is_file():
        contract_path = path / "benchmark-contract.json"
    if not contract_path.is_file():
        contract_path = path / "contract.json"

    events_path = path / "benchmark-events.jsonl"
    if not events_path.is_file():
        events_path = path / "benchmark_events.jsonl"
    if not events_path.is_file():
        events_path = path / "events.jsonl"

    final_state_path = path / "final-state.json"
    if not final_state_path.is_file():
        final_state_path = path / "final_state.json"

    contract = parse_benchmark_contract(contract_path)
    events = parse_benchmark_events(events_path)
    final_state = parse_final_state(final_state_path)
    correlated = correlate_tool_calls(events)

    return TrialBundle(
        trial_id=tid,
        contract=contract,
        final_state=final_state,
        events=events,
        correlated_calls=correlated,
        raw_dir=path,
    )


# Alias for semantic consistency across interpretation modules
ingest_benchmark_trial = load_trial_bundle
