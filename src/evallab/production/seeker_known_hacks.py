"""Export measured DEV seeker runs, keeping known-hack labels strictly local.

``runs-v1.jsonl`` contains ingestion envelopes, not model inputs. Its ``doc`` is
an ordinary ATIF trajectory; admission/task metadata stays outside that document.
``labels-v1.jsonl`` is a separate, local-only scoring receipt. Never upload it or
put its fields in ``verified_receipt_json``. Dry runs create no files and open no
database. No mode executes a submission, grader, or model.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from evallab.production.asset_exporter import MeasuredReceipt, VerifiedAssetExporter
from evallab.production.qc_reports import _ts4_outcome, _verify_factorial_cell_digests
from evallab.production.seeker_error_first import (
    RunnerError,
    extract_runner_error,
    render_feedback_block,
)
from evallab.production.seeker_worker import (
    SeekerRefusalError,
    _load_assignments_csv_index,
    compute_candidate_digest,
    find_cross_key_non_dev_assignments,
    require_cross_key_dev_only,
    scrub_blind_task_text,
)

DEFAULT_STATE_DIR = Path.home() / ".local/state/evallab-prod-live/seeker-known-hacks"
RUNS_FILE = "runs-v1.jsonl"
LABELS_FILE = "labels-v1.jsonl"
LABEL_SCHEMA = "evallab.seeker_known_hack/v1"
INGEST_SCHEMA = "evallab.seeker_trace_ingest/v1"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(_bytes(path))
    if not isinstance(value, dict):
        raise ValueError(f"not a JSON object: {path}")
    return value


def _bytes(path: Path) -> bytes:
    if path.is_symlink():
        raise ValueError(f"symlink evidence refused: {path}")
    return path.read_bytes()


def _file(path: Path, cell_dir: Path) -> dict[str, str]:
    # Relative provenance cannot disclose dataset/source names to the facet model.
    relative = path.relative_to(cell_dir).as_posix()
    return {"path": relative, "sha256": _sha256(_bytes(path))}


@dataclass
class ExportCounts:
    cells_seen: int = 0
    confirmed: int = 0
    exported: int = 0
    already_exported: int = 0
    rejected_by_gate: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cells_seen": self.cells_seen,
            "confirmed": self.confirmed,
            "exported": self.exported,
            "already_exported": self.already_exported,
            "rejected_by_gate": dict(sorted(self.rejected_by_gate.items())),
        }


@dataclass(frozen=True)
class ConfirmedCell:
    directory: Path
    result: Mapping[str, Any]
    task_id: str
    dataset_id: str
    model: str
    turn: str
    solve_sha256: str
    candidate_digest: str
    original: MeasuredReceipt
    repaired: MeasuredReceipt
    assignments_sha256: str

    @property
    def run_id(self) -> str:
        # Opaque identity: neither labels nor source/model names reach ATIF metadata.
        identity = [self.task_id, self.model, self.turn, self.solve_sha256]
        return "run-" + _sha256(_json(identity).encode())


class _RetainedWitnessVerifier(VerifiedAssetExporter):
    """Use the production witness verifier with a retained manifest as its journal.

    Historical experiment cells have no queue event. This adapter supplies only
    that source record; the inherited verifier still independently loads BOTH
    native measurements and recomputes the executable submission digest.
    """

    def __init__(self, directory: Path, event: dict[str, Any]) -> None:
        self.repo_root = directory
        self._source_event = event

    def _linked_event(
        self, event_id: Any, *, expected_type: str | None = None, run_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        event = self._source_event
        if (
            event_id != event["event_id"]
            or (expected_type is not None and event["event_type"] != expected_type)
            or (run_id is not None and event["run_id"] != run_id)
        ):
            return None, ["retained cell source identity mismatch"]
        return event, []


def _submission(directory: Path) -> tuple[Path, dict[str, str | bytes]]:
    submission = directory / "submission"
    bundle = directory / "solution-bundle"
    chosen = bundle if (bundle / "solve.sh").is_file() else submission
    if chosen.is_symlink():
        raise ValueError("symlink submission refused")
    files: dict[str, str | bytes] = {}
    for path in sorted(chosen.rglob("*")):
        if path.is_symlink():
            raise ValueError("symlink submission member refused")
        if path.is_file():
            raw = _bytes(path)
            try:
                files[path.relative_to(chosen).as_posix()] = raw.decode("utf-8")
            except UnicodeError:
                files[path.relative_to(chosen).as_posix()] = raw
    if "solve.sh" not in files:
        raise ValueError("missing executed solve.sh")
    if (
        chosen == bundle and (submission / "solve.sh").is_file()
        and _bytes(submission / "solve.sh") != _bytes(bundle / "solve.sh")
    ):
        raise ValueError("submission and executed solution-bundle differ")
    return chosen, files


def _witness(
    directory: Path, cell: Mapping[str, Any], task_id: str,
) -> tuple[str, str, MeasuredReceipt, MeasuredReceipt]:
    if any((directory / side).is_symlink() for side in ("original", "repaired")):
        raise ValueError("symlink native receipt refused")
    bundle, files = _submission(directory)
    solve_sha = _sha256(_bytes(bundle / "solve.sh"))
    if cell.get("llm_solve_sha256") != solve_sha or cell.get("executed_solve_sha256") != solve_sha:
        raise ValueError("submission_digest_mismatch")
    candidate_digest = compute_candidate_digest(files)
    declared_digest = cell.get("candidate_digest")
    if declared_digest is not None and declared_digest != candidate_digest:
        raise ValueError("candidate_digest_mismatch")
    payload = {
        "source_event_type": "seeker.exploit_confirmed",
        "source_event_id": "retained-cell",
        "asset_id": f"witness:{task_id}:{candidate_digest.removeprefix('sha256:')[:12]}",
        "task_id": task_id,
        "candidate_digest": candidate_digest,
        "exploit_files": files,
        "solution_bundle": str(bundle),
        "exploit_run": str(directory / "original"),
        "repaired_run": str(directory / "repaired"),
        "original_reward": cell.get("original_reward"),
        "upstream_reward": cell.get("original_reward"),
        "repaired_reward": cell.get("repaired_reward"),
    }
    event = {
        "event_id": "retained-cell", "event_type": "seeker.exploit_confirmed",
        "run_id": "retained-cell", "status": "confirmed", "payload": payload,
    }
    verifier = _RetainedWitnessVerifier(directory, event)
    verified, problems = verifier._verify_exploit_witness(event, payload)
    if verified is None:
        raise ValueError("; ".join(problems))
    original = verifier._receipt(payload["exploit_run"])
    repaired = verifier._receipt(payload["repaired_run"])
    if original is None or repaired is None:
        raise ValueError("native receipts disappeared")
    if any(receipt.identity.get("execution_status") not in {
        "measured", "regraded", "ok", "mismatch",
    } for receipt in (original, repaired)):
        raise ValueError("native receipt does not attest measured execution")
    return solve_sha, candidate_digest, original, repaired


def _gate_task(task_id: str, ledger: Path) -> str | None:
    if find_cross_key_non_dev_assignments(task_id, assignments_csv=ledger):
        return "non_dev"
    try:
        require_cross_key_dev_only(task_id, assignments_csv=ledger, require_affirmative_dev=True)
    except SeekerRefusalError:
        return "unlisted_task"
    return None


def confirmed_exploit_cells(
    root: Path, *, assignments_csv: Path | None = None,
    expected_hide_tests: bool = True, counts: ExportCounts | None = None,
) -> Iterator[ConfirmedCell]:
    """Yield only attested, measured, witnessed DEV cells; count the first failed gate.

    Directory identities are admitted BEFORE opening result/prompt/response or
    native evidence. Unlisted and cross-key TEST/heldout task bodies are never read.
    """
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"retained cell root does not exist: {root}")
    ledger = assignments_csv or Path(__file__).resolve().parents[3] / "research/answer-keys/splits/assignments.csv"
    ledger = ledger.expanduser().resolve()
    ledger_sha = _sha256(_bytes(ledger))
    # The shared helper caches by path; a tightened ledger must take effect now.
    _load_assignments_csv_index.cache_clear()
    counts = counts if counts is not None else ExportCounts()
    for result_path in sorted(root.glob("*/*/*/turn-*/result.json")):
        directory = result_path.parent
        dataset, task, slug, turn = directory.relative_to(root).parts
        counts.cells_seen += 1
        gate = _gate_task(task, ledger)
        if gate is not None:
            counts.rejected_by_gate[gate] += 1
            continue
        if any(path.is_symlink() for path in (directory, *directory.parents) if path != root.parent):
            counts.rejected_by_gate["symlink_evidence"] += 1
            continue
        try:
            cell = _object(result_path)
            task_id = cell.get("task_id")
            model = cell.get("requested_model_route")
            if (
                not isinstance(task_id, str)
                or task_id.split("@", 1)[0].rsplit("/", 1)[-1] != task
                or cell.get("dataset_id") != dataset
                or not isinstance(model, str) or model.replace("/", "_") != slug
                or cell.get("turn") != (2 if turn.startswith("turn-2") else 1)
            ):
                gate = "identity_mismatch"
            elif (gate := _gate_task(task_id, ledger)) is not None:
                pass
            elif cell.get("hide_tests") is not expected_hide_tests:
                gate = "hide_tests_mismatch"
            elif cell.get("attestation_ok") is not True:
                gate = "unattested"
            elif not _ts4_outcome(cell, model)[0]:
                gate = "served_route_mismatch"
            elif not _ts4_outcome(cell, model)[1]:
                gate = "not_differential"
            elif any(cell.get(f"{side}_status") not in (None, "measured") for side in ("original", "repaired")):
                gate = "non_measured"
            elif not isinstance(cell.get("llm_stdout_sha256"), str):
                gate = "missing_response_digest"
            else:
                try:
                    _verify_factorial_cell_digests(cell, directory)
                except ValueError:
                    gate = "response_digest_mismatch"
            if gate is not None:
                counts.rejected_by_gate[gate] += 1
                continue
            assert isinstance(task_id, str) and isinstance(model, str)
            try:
                solve_sha, candidate_digest, original, repaired = _witness(directory, cell, task_id)
            except (OSError, ValueError, UnicodeError):
                counts.rejected_by_gate["witness_verification"] += 1
                continue
            confirmed = ConfirmedCell(
                directory, cell, task_id, dataset, model, turn, solve_sha,
                candidate_digest, original, repaired, ledger_sha,
            )
            # A confirmed label also needs a complete, convertible retained trajectory.
            cell_to_atif(confirmed)
        except (OSError, ValueError, UnicodeError, KeyError, TypeError):
            counts.rejected_by_gate["retained_evidence"] += 1
            continue
        counts.confirmed += 1
        yield confirmed


def _step(step_id: int, role: str, message: str, paths: list[Path], cell: ConfirmedCell) -> dict[str, Any]:
    return {
        "step_id": step_id, "source": role, "message": message,
        "evidence_files": [_file(path, cell.directory) for path in paths],
        "content_sha256": _sha256(message.encode()),
    }


def _original_observation(cell: ConfirmedCell) -> tuple[dict[str, Any], list[Path]]:
    # Read only the measured receipt's declared files and its native verifier logs.
    sources = list(cell.original.sources or (cell.original.source,))
    native: dict[str, Any] = {"status": "measured", "reward": cell.original.value}
    entries: list[dict[str, Any]] = []
    for source in sources:
        if source.suffix == ".json":
            data = json.loads(_bytes(source))
            if isinstance(data, dict) and data.get("schema") == "evallab.candidate.result/v1":
                native = data
                logs = data.get("logs") or {}
                entries.extend(logs.get("verifier") or data.get("raw_verifier_evidence") or [])
    if not entries:
        for name in ("test-stdout.txt", "test_output.log"):
            for path in sorted((cell.directory / "original").rglob(name)):
                entries.append({"path": str(path), "digest": _sha256(_bytes(path))})
    for entry in entries:
        path = Path(entry["path"])
        path.resolve().relative_to((cell.directory / "original").resolve())
        if entry.get("digest", "").removeprefix("sha256:") != _sha256(_bytes(path)):
            raise ValueError("native verifier evidence digest mismatch")
        sources.append(path)
    if not entries:
        raise ValueError("missing native original verifier output")
    error = extract_runner_error({**native, "raw_verifier_evidence": entries, "logs": {"verifier": entries}})
    if not error.verifier_output:
        raise ValueError("empty native original verifier output")
    return {
        "status": error.status, "reward": cell.original.value,
        "sandbox_exit_codes": list(error.sandbox_exit_codes),
        "verifier_tail": error.verifier_output,
    }, list(dict.fromkeys(sources))


def cell_to_atif(cell: ConfirmedCell) -> dict[str, Any]:
    """Preserve full UTF-8 response/thinking and solve bytes; scrub only the prompt.

    Provenance hashes always cover raw files, even when prompt scrubbing changes
    the message. The only bounded content is the production native verifier tail.
    """
    directory = cell.directory
    prompt = directory / "prompt.md"
    response = directory / "response.jsonl"
    bundle, _ = _submission(directory)
    solve = bundle / "solve.sh"
    response_bytes, solve_bytes = _bytes(response), _bytes(solve)
    if (
        _sha256(response_bytes) != cell.result.get("llm_stdout_sha256")
        or _sha256(solve_bytes) != cell.solve_sha256
    ):
        raise ValueError("retained response/submission changed after confirmation")
    steps = []
    if cell.turn.startswith("turn-2"):
        feedback = directory / "feedback.md"
        runner_path = directory / "runner_error.json"
        if not runner_path.is_file():
            runner_path = directory.parent / "turn-1" / "runner_error.json"
        payload = _object(runner_path)
        error = RunnerError.from_json(payload)
        if (
            payload.get("grader") != "original" or error.status != "measured"
            or error.sha256 != cell.result.get("runner_error_sha256")
        ):
            raise ValueError("preceding ORIGINAL runner error binding mismatch")
        raw = _bytes(feedback)
        block = raw.decode("utf-8")
        if (
            _sha256(raw) != cell.result.get("feedback_block_sha256")
            or block != render_feedback_block(error)
        ):
            raise ValueError("preceding feedback binding mismatch")
        # The error may live beside turn-1; provenance remains relative and source-blind.
        step = {
            "step_id": 0, "source": "user", "message": block,
            "evidence_files": [{
                "path": os.path.relpath(runner_path, directory),
                "sha256": _sha256(_bytes(runner_path)),
            }],
            "content_sha256": _sha256(block.encode()),
        }
        step["evidence_files"].append(_file(feedback, directory))
        steps.append(step)
    prompt_text = _bytes(prompt).decode("utf-8")
    if cell.result.get("prompt_sha256") is not None and (
        cell.result["prompt_sha256"] != _sha256(prompt_text.encode())
    ):
        raise ValueError("retained prompt digest mismatch")
    scrubbed = scrub_blind_task_text(prompt_text)
    # The shared scrubber normalizes line endings even when nothing is redacted.
    # Preserve the exact retained prompt in that common case.
    if scrubbed == "\n".join(prompt_text.splitlines()):
        scrubbed = prompt_text
    steps.append(_step(1, "user", scrubbed, [prompt], cell))
    steps.append(_step(2, "agent", response_bytes.decode("utf-8"), [response], cell))
    action = _step(3, "agent", "", [solve], cell)
    action["tool_calls"] = [{
        "tool_call_id": "solve", "function_name": "bash",
        "arguments": {"command": solve_bytes.decode("utf-8"), "sha256": cell.solve_sha256},
    }]
    steps.append(action)
    observation, paths = _original_observation(cell)
    steps.append(_step(4, "system", _json(observation), paths, cell))
    return {"schema_version": "ATIF-v1.6", "run_id": cell.run_id, "steps": steps}


def _label(cell: ConfirmedCell, doc: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": LABEL_SCHEMA, "run_id": cell.run_id, "source": "seeker",
        "dataset_id": cell.dataset_id, "task_id": cell.task_id,
        "model_id": cell.model, "turn": cell.turn,
        "submission_sha256": cell.solve_sha256, "candidate_digest": cell.candidate_digest,
        "admission": {"effective_split": "dev", "assignments_csv_sha256": cell.assignments_sha256},
        "source_label": {"judged": True, "verdict": "yes", "verdict_enacted": "yes"},
        "passage_step_labels": {"positive_step_indices": [3]},
        "evidence": {
            "verification": "VerifiedAssetExporter._verify_exploit_witness",
            "cell_directory": str(cell.directory),
            "cell_result_sha256": _sha256(_bytes(cell.directory / "result.json")),
            "original": cell.original.provenance(), "repaired": cell.repaired.provenance(),
            "run_doc_sha256": _sha256(_json(doc).encode()),
        },
    }


def _envelope(cell: ConfirmedCell, doc: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": INGEST_SCHEMA, "run_id": cell.run_id, "doc": doc,
        "task": {
            "task_id": cell.task_id, "dataset_id": cell.dataset_id, "split": "dev",
            "instruction_text": "", "metadata_json": {},
            "original_grader_digest": cell.result.get("original_package_digest"),
            "repaired_grader_digest": cell.result.get("repaired_package_digest"),
        },
        # Deliberately no exploit flag, label receipt, passage labels, or special trust tier.
        "trace": {
            "trace_id": cell.run_id, "run_id": cell.run_id, "task_id": cell.task_id,
            "model_id": cell.model, "outcome": "pass", "reward": 1.0,
        },
    }


def _db_steps(doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for step in doc["steps"]:
        calls = step.get("tool_calls")
        rows.append({
            "step_index": step["step_id"], "role": step["source"],
            "content_text": step["message"],
            "tool_name": "bash" if calls else None,
            "command_or_action": _json([
                {"id": call["tool_call_id"], "name": call["function_name"], "arguments": call["arguments"]}
                for call in calls
            ]) if calls else None,
            "flags_json": {"evidence_files": step["evidence_files"], "content_sha256": step["content_sha256"]},
        })
    return rows


def _read_rows(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    result: dict[str, dict[str, Any]] = {}
    for line in _bytes(path).splitlines():
        row = json.loads(line)
        run_id = row["run_id"]
        if run_id in result:
            raise ValueError(f"duplicate existing run identity in {path}")
        result[run_id] = row
    return result


def _publish(path: Path, rows: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text("".join(_json(rows[key]) + "\n" for key in sorted(rows)), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def export_known_hacks(
    root: Path, *, state_dir: Path = DEFAULT_STATE_DIR, assignments_csv: Path | None = None,
    mode: Literal["dry-run", "stage", "apply"] = "dry-run",
    database: str | Path | None = None, expected_hide_tests: bool = True,
) -> dict[str, Any]:
    """Validate and deduplicate locally; only explicit ``apply`` opens a DB writer."""
    if mode not in {"dry-run", "stage", "apply"}:
        raise ValueError("mode must be dry-run, stage or apply")
    if mode == "apply" and database is None:
        raise ValueError("--apply requires an explicit --cloud-trace-db")
    counts = ExportCounts()
    state_dir = state_dir.expanduser().resolve()
    runs_path, labels_path = state_dir / RUNS_FILE, state_dir / LABELS_FILE
    runs, labels = _read_rows(runs_path), _read_rows(labels_path)
    selected = []
    for cell in confirmed_exploit_cells(
        root, assignments_csv=assignments_csv, expected_hide_tests=expected_hide_tests, counts=counts,
    ):
        doc = cell_to_atif(cell)
        run, label = _envelope(cell, doc), _label(cell, doc)
        if cell.run_id in runs and cell.run_id in labels:
            if runs[cell.run_id] != run or labels[cell.run_id] != label:
                raise ValueError(f"conflicting retained evidence for run {cell.run_id}")
            counts.already_exported += 1
        else:
            if (cell.run_id in runs and runs[cell.run_id] != run) or (cell.run_id in labels and labels[cell.run_id] != label):
                raise ValueError(f"conflicting partial export for run {cell.run_id}")
            if mode != "dry-run":
                counts.exported += 1
            runs[cell.run_id], labels[cell.run_id] = run, label
        selected.append(run)
    if mode != "dry-run":
        state_dir.mkdir(parents=True, exist_ok=True)
        if mode == "apply":
            from evallab.production.cloud_trace_db import CloudTraceDatabase

            with CloudTraceDatabase(database) as store:
                # Preflight every identity before any task or trace mutation.
                for run in selected:
                    task = run["task"]
                    existing = store.get_task(task["task_id"])
                    if existing is not None and (
                        existing["dataset_id"] != task["dataset_id"]
                        or existing["split"].casefold() != "dev"
                    ):
                        raise ValueError("existing cloud task identity conflicts with admitted cell")
                for run in selected:
                    if store.get_task(run["task"]["task_id"]) is None:
                        store.upsert_task(run["task"])
                    store.upsert_trace(run["trace"], steps=_db_steps(run["doc"]))
        _publish(runs_path, runs)
        _publish(labels_path, labels)
    return {
        "schema": "evallab.seeker_known_hack_export/v1", "mode": mode,
        **counts.as_dict(), "would_export": counts.confirmed - counts.already_exported,
        "staging_jsonl": str(runs_path), "local_labels_jsonl": str(labels_path),
    }
