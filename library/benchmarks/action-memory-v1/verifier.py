#!/usr/bin/env python3
import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _record(reward_dir: Path, result: dict[str, Any]) -> None:
    (reward_dir / "reward.txt").write_text("1.0\n" if result["reward"] == 1.0 else "0.0\n", encoding="utf-8")
    (reward_dir / "result.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _failure(reward_dir: Path, reason: str, truth_digest: str, **details: Any) -> dict[str, Any]:
    result = {"reward": 0.0, "reason": reason, "truth_digest": truth_digest, **details}
    _record(reward_dir, result)
    return result


def _load_truth(task_dir: Path) -> tuple[dict[str, Any], bytes]:
    spec_path = task_dir / "fixtures" / "target_spec.json"
    if not spec_path.exists():
        spec_path = task_dir / "target_spec.json"
    if spec_path.exists():
        raw = spec_path.read_bytes()
        return json.loads(raw.decode("utf-8")), raw
    scenario_path = task_dir / "scenario.json"
    if not scenario_path.exists():
        raise FileNotFoundError("missing_target_spec_file")
    raw = scenario_path.read_bytes()
    scenario = json.loads(raw.decode("utf-8"))
    return {
        "target_entity": scenario["target_entity"],
        "target_attribute": scenario["target_attribute"],
        "expected_bound_value": scenario["latest_value"],
        "required_chunk_ids": [chunk["chunk_id"] for chunk in scenario["chunks"]],
    }, raw


def _successful_value(event: dict[str, Any]) -> dict[str, Any] | None:
    result = event.get("result")
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    value = result.get("value")
    return value if isinstance(value, dict) else None


def _load_runtime_events(
    events_file: Path, reward_dir: Path, truth_digest: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    if not events_file.exists():
        return [], _failure(reward_dir, "missing_runtime_evidence", truth_digest)
    events: list[dict[str, Any]] = []
    allowed_tools = {
        "list_context_chunks",
        "get_context_chunk",
        "get_context_chunks",
        "execute_mutation",
    }
    for expected_ordinal, line in enumerate(
        (line for line in events_file.read_text(encoding="utf-8").splitlines() if line.strip()),
        start=1,
    ):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return [], _failure(reward_dir, "corrupt_runtime_evidence", truth_digest)
        if not isinstance(event, dict) or event.get("schema_version") != "mcp-tool-event-v1":
            return [], _failure(reward_dir, "noncanonical_runtime_evidence", truth_digest)
        if event.get("event_ordinal") != expected_ordinal:
            return [], _failure(reward_dir, "non_contiguous_runtime_event_ordinals", truth_digest)
        if event.get("tool_name") not in allowed_tools or not isinstance(event.get("arguments"), dict):
            return [], _failure(reward_dir, "noncanonical_runtime_evidence", truth_digest)
        if event.get("event_type") not in {"tool_call_success", "tool_call_error"} or not isinstance(
            event.get("is_error"), bool
        ):
            return [], _failure(reward_dir, "noncanonical_runtime_evidence", truth_digest)
        if (event["event_type"] == "tool_call_success") != (event["is_error"] is False):
            return [], _failure(reward_dir, "inconsistent_runtime_event_error_state", truth_digest)
        events.append(event)
    if not events:
        return [], _failure(reward_dir, "missing_runtime_evidence", truth_digest)
    return events, None


def _flatten_read_ids(
    read_events: list[dict[str, Any]], required_ids: list[str]
) -> list[str] | None:
    """Flatten an ordered sequence of single/batch reads into retrieved chunk IDs."""
    flattened: list[str] = []
    for event in read_events:
        tool = event.get("tool_name")
        args = event.get("arguments") or {}
        val = _successful_value(event)
        if val is None:
            return None
        if tool == "get_context_chunk":
            cid = args.get("chunk_id")
            if not isinstance(cid, str) or cid not in required_ids:
                return None
            flattened.append(cid)
        elif tool == "get_context_chunks":
            chunk_ids = args.get("chunk_ids")
            rng = args.get("range")
            if isinstance(chunk_ids, list) and chunk_ids and all(isinstance(c, str) for c in chunk_ids):
                if any(c not in required_ids for c in chunk_ids):
                    return None
                flattened.extend(chunk_ids)
            elif isinstance(rng, dict) and isinstance(rng.get("start"), int) and isinstance(rng.get("end"), int):
                start, end = rng["start"], rng["end"]
                if start < 0 or end < start or end >= len(required_ids):
                    return None
                flattened.extend(required_ids[start : end + 1])
            else:
                return None
        else:
            return None
    return flattened


def _validate_retrieval_path(
    events: list[dict[str, Any]],
    required_ids: list[str],
    reward_dir: Path,
    truth_digest: str,
    representation: str | None = None,
) -> dict[str, Any] | None:
    first = events[0]
    first_val = _successful_value(first)
    if (
        first["tool_name"] != "list_context_chunks"
        or first["arguments"]
        or first_val is None
        or first_val.get("chunk_ids") != required_ids
    ):
        return _failure(reward_dir, "missing_initial_context_listing", truth_digest)

    # Validate handle-representation binding when declared
    if representation is not None:
        if first_val.get("representation") != representation:
            return _failure(reward_dir, "representation_mismatch_in_runtime_events", truth_digest)
        if representation == "range_batch":
            expected_range = {
                "start": 0,
                "end": max(0, len(required_ids) - 1),
                "unit": "chunk",
            }
            if first_val.get("range") != expected_range:
                return _failure(reward_dir, "missing_range_reference_descriptor", truth_digest)
            reads = [
                e
                for e in events
                if e["tool_name"] in ("get_context_chunk", "get_context_chunks")
            ]
            flattened = _flatten_read_ids(reads, required_ids)
            if flattened is None:
                return _failure(
                    reward_dir, "undeclared_or_mixed_handle_reference_mode", truth_digest
                )
            if flattened != required_ids:
                return _failure(
                    reward_dir,
                    "incomplete_or_reordered_context_retrieval",
                    truth_digest,
                    expected_reads=len(required_ids),
                    observed_reads=len(flattened),
                )
        else:
            # opaque or indexed: single get_context_chunk reads only
            if any(e["tool_name"] == "get_context_chunks" for e in events):
                return _failure(
                    reward_dir, "undeclared_or_mixed_handle_reference_mode", truth_digest
                )
            reads = [e for e in events if e["tool_name"] == "get_context_chunk"]
            if [e["arguments"].get("chunk_id") for e in reads] != required_ids:
                return _failure(
                    reward_dir,
                    "incomplete_or_reordered_context_retrieval",
                    truth_digest,
                    expected_reads=len(required_ids),
                    observed_reads=len(reads),
                )
            for event, chunk_id in zip(reads, required_ids, strict=True):
                value = _successful_value(event)
                if (
                    value is None
                    or value.get("chunk_id") != chunk_id
                    or not isinstance(value.get("content"), str)
                ):
                    return _failure(
                        reward_dir, "unsuccessful_or_malformed_context_read", truth_digest
                    )
    else:
        # Legacy mode (no representation declaration in target_spec)
        reads = [event for event in events if event["tool_name"] == "get_context_chunk"]
        if [event["arguments"].get("chunk_id") for event in reads] != required_ids:
            return _failure(
                reward_dir,
                "incomplete_or_reordered_context_retrieval",
                truth_digest,
                expected_reads=len(required_ids),
                observed_reads=len(reads),
            )
        for event, chunk_id in zip(reads, required_ids, strict=True):
            value = _successful_value(event)
            if (
                value is None
                or value.get("chunk_id") != chunk_id
                or not isinstance(value.get("content"), str)
            ):
                return _failure(
                    reward_dir, "unsuccessful_or_malformed_context_read", truth_digest
                )

    mutations = [event for event in events if event["tool_name"] == "execute_mutation"]
    if (
        len(mutations) != 1
        or events[-1] is not mutations[0]
        or _successful_value(mutations[0]) is None
    ):
        return _failure(reward_dir, "mutation_not_after_complete_retrieval", truth_digest)
    if any(event["is_error"] for event in events):
        return _failure(reward_dir, "runtime_tool_error_observed", truth_digest)
    return None


def verify(task_dir: Path, evidence_dir: Path, reward_dir: Path | None = None) -> dict[str, Any]:
    reward_dir = reward_dir or Path("/logs/verifier")
    reward_dir.mkdir(parents=True, exist_ok=True)
    try:
        spec, spec_bytes = _load_truth(task_dir)
    except FileNotFoundError:
        return _failure(reward_dir, "missing_target_spec_file", "sha256:missing")
    truth_digest = f"sha256:{hashlib.sha256(spec_bytes).hexdigest()}"
    required_ids = spec.get("required_chunk_ids")
    if (
        not isinstance(required_ids, list)
        or not required_ids
        or not all(isinstance(item, str) for item in required_ids)
    ):
        return _failure(reward_dir, "missing_required_retrieval_manifest", truth_digest)

    representation = spec.get("representation")
    if representation is not None and representation not in {"opaque", "indexed", "range_batch"}:
        return _failure(reward_dir, "undeclared_handle_representation", truth_digest)

    final_file = evidence_dir / "final-state.json"
    if not final_file.exists():
        return _failure(reward_dir, "missing_final_state_evidence", truth_digest)
    try:
        final_state = json.loads(final_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _failure(reward_dir, f"corrupt_final_state: {exc}", truth_digest)

    events, failure = _load_runtime_events(
        evidence_dir / "benchmark-events.jsonl", reward_dir, truth_digest
    )
    if failure is not None:
        return failure
    failure = _validate_retrieval_path(events, required_ids, reward_dir, truth_digest, representation)
    if failure is not None:
        return failure
    expected = (
        spec.get("target_entity"),
        spec.get("target_attribute"),
        spec.get("expected_bound_value"),
    )
    observed = (
        final_state.get("target_entity"),
        final_state.get("target_attribute"),
        final_state.get("bound_value"),
    )
    if observed != expected:
        return _failure(reward_dir, "mismatch", truth_digest)
    result = {
        "reward": 1.0,
        "reason": "exact_latest_value_bound_after_complete_retrieval",
        "truth_digest": truth_digest,
        "events_validated": len(events),
        "read_events": len(required_ids),
        "mutation_events": 1,
    }
    _record(reward_dir, result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Action-memory verifier entrypoint")
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--reward-dir", type=Path, default=None)
    args = parser.parse_args()
    raise SystemExit(
        0 if verify(args.task_dir, args.evidence_dir, args.reward_dir)["reward"] == 1.0 else 1
    )
