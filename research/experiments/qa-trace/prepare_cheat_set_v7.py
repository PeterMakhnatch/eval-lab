#!/usr/bin/env python3
"""Prepare hash-bound MiMo cheat-set attempts for frozen QA Trace batch (no inference).

Only the producer's blind-index/v1, referenced blind ATIF, and explicitly
hash-bound optional diff are consumed. Raw trial refs and generation conditions
are never hydrated or copied. Evaluator labels belong to the separate scorer.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from evallab.cheatbench_fidelity import blind_atif, find_forbidden_json
from evallab.qa.trace import blind_id

INDEX_SCHEMA = "mimo-cheat-set/blind-index/v1"
MANIFEST_SCHEMA = "qa.cheat_set_inputs/v1"
LIMITATIONS = (
    "These are projected attempts, not a pass-filtered or released benchmark population.",
    "Only supplied blind ATIF and explicitly hash-bound diffs are present; raw trials, "
    "generation conditions and evaluator labels were not hydrated.",
    "Authoritative task packages, graders, rewards and final filesystem state are unavailable; "
    "task context is limited to the supplied ATIF user messages.",
    "Missing diffs remain missing; trajectory actions are not reconstructed as final files.",
    "Producer blinding is not a canonical admission contract. This staged_pre_release "
    "preparation is not citable model-quality, grading-correctness or behavioral-cheating evidence.",
)
# Check the ATIF envelope, not task text/tool payloads: an agent may legitimately
# manipulate a file or tool argument called 'reward' or 'label'. The existing
# CheatBench scan separately rejects known annotation side channels everywhere.
_EVALUATOR_FIELDS = frozenset(
    {
        "attempt",
        "cell_id",
        "disposition",
        "generation",
        "generation_status",
        "job_name",
        "label",
        "labels",
        "model_usage",
        "native_job",
        "prompt_condition",
        "prompt_sha256",
        "refusals",
        "regrade_job_dir",
        "reward",
        "rewards",
        "spec_id",
        "task_id",
        "task_name",
        "trial_dir",
        "trial_ref",
        "v6_package_digest",
        "v6_reward",
        "v7_grader_kind",
        "v7_grader_label",
        "v7_pending_reasons",
        "v7_raw_package_digest",
        "v7_reward",
        "verifier_result",
        "wrong_pass",
    }
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path, description: str) -> bytes:
    try:
        if not path.is_file():
            raise SystemExit(f"{description}: not a regular file: {path}")
        return path.read_bytes()
    except OSError as exc:
        raise SystemExit(f"{description}: cannot read {path}: {exc}") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant {value}")


def _json_object(data: bytes | str, description: str) -> dict[str, Any]:
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        obj = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (UnicodeError, ValueError) as exc:
        raise SystemExit(f"{description}: invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise SystemExit(f"{description}: expected a JSON object")
    return obj


def _digest(value: Any, description: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", value):
        raise SystemExit(f"{description}: expected sha256:<64hex>")
    return value.removeprefix("sha256:").lower()


def _root(path: Path, description: str) -> Path:
    try:
        root = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SystemExit(f"{description}: cannot resolve directory: {exc}") from exc
    if not root.is_dir():
        raise SystemExit(f"{description}: not a directory: {root}")
    return root


def _relative_file(root: Path, reference: Any, description: str) -> Path:
    if not isinstance(reference, str) or not reference.strip():
        raise SystemExit(f"{description}: expected a nonempty relative file reference")
    path = Path(reference)
    if path.is_absolute():
        raise SystemExit(f"{description}: absolute file reference is not allowed")
    try:
        resolved = (root / path).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise SystemExit(f"{description}: cannot resolve file: {exc}") from exc
    if not resolved.is_relative_to(root):
        raise SystemExit(f"{description}: file reference escapes its root")
    return resolved


def _check_envelope(node: Any, path: str = "$", *, payload_fields: tuple[str, ...] = ()) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in payload_fields:
                continue
            if key.lower() in _EVALUATOR_FIELDS:
                raise SystemExit(f"trajectory: evaluator/generation field at {path}.{key}")
            _check_envelope(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _check_envelope(value, f"{path}[{index}]")


def _check_blind_atif(data: bytes, item_id: str) -> None:
    doc = _json_object(data, "trajectory")
    version = doc.get("schema_version")
    if not isinstance(version, str) or not version.startswith("ATIF-"):
        raise SystemExit("trajectory: missing ATIF schema_version")
    steps = doc.get("steps")
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        raise SystemExit("trajectory: expected an ATIF steps array of objects")
    if find_forbidden_json(doc):
        raise SystemExit("trajectory: evaluator fields or annotation text survived blinding")
    if blind_atif(doc, item_id=item_id) != doc:
        raise SystemExit("trajectory: not the producer's blind_atif projection for this item_id")
    _check_envelope({key: value for key, value in doc.items() if key != "steps"})
    for index, step in enumerate(steps):
        if type(step.get("step_id")) is not int or step["step_id"] != index:
            raise SystemExit("trajectory: step_id must match the producer's zero-based projection")
        # These payloads are authoritative evidence, not evaluator metadata.
        # Preserve even structured user messages and task-specific tool data.
        location = f"$.steps[{index}]"
        _check_envelope(
            step,
            location,
            payload_fields=("message", "tool_calls", "observation", "reasoning_content"),
        )
        calls = step.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list) or any(not isinstance(call, dict) for call in calls):
                raise SystemExit("trajectory: tool_calls must be an array of objects")
            for call_index, call in enumerate(calls):
                _check_envelope(
                    call, f"{location}.tool_calls[{call_index}]", payload_fields=("arguments",)
                )
        observation = step.get("observation")
        if observation is not None:
            if not isinstance(observation, dict):
                raise SystemExit("trajectory: observation must be an object")
            _check_envelope(observation, f"{location}.observation", payload_fields=("results",))
            results = observation.get("results")
            if results is not None:
                if not isinstance(results, list) or any(
                    not isinstance(result, dict) for result in results
                ):
                    raise SystemExit("trajectory: observation results must be an array of objects")
                for result_index, result in enumerate(results):
                    _check_envelope(
                        result,
                        f"{location}.observation.results[{result_index}]",
                        payload_fields=("content",),
                    )


def _check_out(out: Path) -> None:
    if out.is_symlink():
        raise SystemExit(f"output must not be a symlink: {out}")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise SystemExit(f"output must not contain existing data: {out}")


def prepare(
    index: Path,
    out: Path,
    *,
    exports_root: Path | None = None,
    producer_root: Path | None = None,
) -> dict[str, Any]:
    """Validate the entire index before exclusively writing minimal projected trials.

    Return the exact input manifest written to disk. Invalid inputs create no
    output. Filesystem write failures can leave a partial run root, which is
    deliberately not overwritten or automatically removed on a later attempt.
    """
    _check_out(out)
    out = out.resolve()
    index_bytes = _read_bytes(index, "index")
    try:
        lines = index_bytes.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise SystemExit(f"index: invalid UTF-8: {exc}") from exc
    exports = _root(exports_root if exports_root is not None else index.parent, "exports root")
    producer = _root(producer_root, "producer root") if producer_root is not None else None
    prepared: list[tuple[dict[str, Any], bytes, bytes | None]] = []
    subjects: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    seen_blind_ids: set[str] = set()
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        row = _json_object(line, f"index line {number}")
        if row.get("schema") != INDEX_SCHEMA:
            raise SystemExit(f"index line {number}: expected schema {INDEX_SCHEMA}")
        item_id = row.get("item_id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise SystemExit(f"index line {number}: expected a nonempty string item_id")
        if item_id in seen_ids:
            raise SystemExit(f"index line {number}: duplicate item_id {item_id!r}")
        try:
            opaque = blind_id(item_id)
        except UnicodeError as exc:
            raise SystemExit(f"index line {number}: item_id is not valid UTF-8") from exc
        if opaque in seen_blind_ids:
            raise SystemExit(f"index line {number}: blind_id collision for {item_id!r}")
        seen_ids.add(item_id)
        seen_blind_ids.add(opaque)
        trajectory = row.get("blind_trajectory")
        if not isinstance(trajectory, dict):
            raise SystemExit(f"index line {number}: missing blind_trajectory object")
        digest = _digest(trajectory.get("sha256"), "trajectory digest")
        size = trajectory.get("bytes")
        if type(size) is not int or size < 0:
            raise SystemExit("trajectory: bytes must be a nonnegative integer")
        source = _relative_file(exports, trajectory.get("file"), "trajectory")
        trajectory_bytes = _read_bytes(source, "trajectory")
        if len(trajectory_bytes) != size or _sha256(trajectory_bytes) != digest:
            raise SystemExit(f"index line {number}: trajectory size/hash mismatch")
        _check_blind_atif(trajectory_bytes, item_id)
        trial_ref = row.get("trial_ref")
        if not isinstance(trial_ref, dict):
            raise SystemExit(f"index line {number}: missing trial_ref object")
        diff_ref = trial_ref.get("agent_diff_path")
        diff_digest = trial_ref.get("agent_diff_sha256")
        diff_bytes = None
        diff = None
        trial_path = Path("inputs") / opaque
        if diff_ref is None:
            if diff_digest is not None:
                raise SystemExit("diff: digest supplied without an agent_diff_path")
        else:
            if not isinstance(diff_ref, str) or not diff_ref.strip():
                raise SystemExit("diff: expected a nonempty agent_diff_path or null")
            expected = _digest(diff_digest, "diff digest")
            source = Path(diff_ref)
            if not source.is_absolute():
                if producer is None:
                    raise SystemExit("relative diff requires an explicit --producer-root")
                source = _relative_file(producer, diff_ref, "diff")
            diff_bytes = _read_bytes(source, "diff")
            if _sha256(diff_bytes) != expected:
                raise SystemExit(f"index line {number}: diff hash mismatch")
            diff = {
                "path": (trial_path / "verifier/agent.diff").as_posix(),
                "sha256": expected,
                "bytes": len(diff_bytes),
            }
        item = {
            "item_id": item_id,
            "trial_path": trial_path.as_posix(),
            "trajectory": {
                "path": (trial_path / "agent/trajectory.json").as_posix(),
                "sha256": digest,
                "bytes": size,
            },
            "diff": diff,
        }
        prepared.append((item, trajectory_bytes, diff_bytes))
        subjects.append({"item_id": item_id, "trial_path": str(out / trial_path)})
    if not prepared:
        raise SystemExit("index: empty input; no subjects to prepare")
    subjects_bytes = "".join(json.dumps(row, sort_keys=True) + "\n" for row in subjects).encode()
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "index_sha256": _sha256(index_bytes),
        "subjects_sha256": _sha256(subjects_bytes),
        "items": [item for item, _, _ in prepared],
        "limitations": list(LIMITATIONS),
    }
    # Recheck after validation and use exclusive writes throughout: neither a
    # previous prediction root nor another writer's files may be overwritten.
    _check_out(out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "inputs").mkdir()
    for item, trajectory_bytes, diff_bytes in prepared:
        trajectory_path = out / item["trajectory"]["path"]
        trajectory_path.parent.mkdir(parents=True)
        with trajectory_path.open("xb") as stream:
            stream.write(trajectory_bytes)
        if diff_bytes is not None:
            diff_path = out / item["diff"]["path"]
            diff_path.parent.mkdir()
            with diff_path.open("xb") as stream:
                stream.write(diff_bytes)
    with (out / "subjects.jsonl").open("xb") as stream:
        stream.write(subjects_bytes)
    with (out / "input-manifest.json").open("xb") as stream:
        stream.write((json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--exports-root", type=Path)
    parser.add_argument("--producer-root", type=Path)
    args = parser.parse_args()
    manifest = prepare(
        args.index,
        args.out,
        exports_root=args.exports_root,
        producer_root=args.producer_root,
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
