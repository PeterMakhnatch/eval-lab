#!/usr/bin/env python3
"""Classify source-bound HAR-191 control receipts and export complete coverage.

This is an offline consumer, not an executor or evidence synthesizer. Successful
extractor statuses certify source-only selection; retained patch bytes and logs
are independently hash-bound here. Verifier failure requires an ordinary
completed process, never a timeout or signal-shaped exit. CSV rows link the
complete receipt. Unknown and unrun observations live only in coverage JSON.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

LABELS = (
    "oracle:pass+nop:fail",
    "oracle:fail-network",
    "oracle:fail",
    "oracle:none",
    "oracle:patch-conflict",
    "nop:pass",
)
CSV_COLUMNS = (
    "task_id",
    "label",
    "fix_commit",
    "patch_tip",
    "how_chosen",
    "evidence_path",
    "run_digest",
)
TASK_ID = re.compile(r"format-code-task-[0-9]{6}\Z")
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
RUN_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
IMAGE_REF = re.compile(r".+@sha256:[0-9a-f]{64}\Z")
EMPTY_PATCH_SHA256 = hashlib.sha256(b"").hexdigest()
SOURCE_FIELDS = ("run_digest", "image", "base", "test_patch_sha256")
ARM_STATUSES = frozenset(
    (
        "complete",
        "timeout",
        "infrastructure-error",
        "budget-stopped",
        "not-run",
    )
)
NONE_STATUSES = frozenset(("no-identifiable-fix", "test-only-fix", "empty-diff"))
OPERATIONAL_EXTRACTION_STATUSES = frozenset(
    (
        "input-error",
        "git-error",
        "git-timeout",
        "unsupported-tree",
        "unsupported-path",
        "internal-error",
        "output-error",
    )
)


def _text(value: Any) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and not any(ord(character) < 32 for character in value)
    )


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _identity_error(record: dict, *, require_base: bool) -> str | None:
    checks = (
        ("task_id", TASK_ID),
        ("run_digest", RUN_DIGEST),
        ("test_patch_sha256", SHA256),
    )
    for field, pattern in checks:
        if not _matches(pattern, record.get(field)):
            return f"invalid {field}"
    if not _text(record.get("image")) or not _matches(IMAGE_REF, record["image"]):
        return "invalid image"
    if (require_base or record.get("base") is not None) and not _matches(SHA40, record.get("base")):
        return "invalid base"
    return None


def _result(label: str | None, state: str, reason: str) -> dict:
    return {"label": label, "state": state, "reason": reason}


def _unknown(state: str, reason: str) -> dict:
    return _result(None, state, reason)


def _classified(label: str, reason: str) -> dict:
    return _result(label, "classified", reason)


def _bound_file_problem(
    path_value: Any,
    digest: Any,
    description: str,
    *,
    nonempty: bool = False,
) -> dict | None:
    if not _text(path_value) or not Path(path_value).is_absolute():
        return _unknown("missing-evidence", f"{description}: missing absolute evidence path")
    if not _matches(SHA256, digest):
        return _unknown("missing-evidence", f"{description}: missing valid evidence SHA256")
    try:
        path = Path(path_value)
        if not path.is_file():
            return _unknown("missing-evidence", f"{description}: evidence file is missing")
        observed = hashlib.sha256()
        has_content = False
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                observed.update(block)
                has_content = has_content or bool(block.strip())
    except OSError:
        return _unknown("missing-evidence", f"{description}: evidence file is inaccessible")
    if observed.hexdigest() != digest:
        return _unknown("source-mismatch", f"{description}: evidence bytes differ from SHA256")
    if nonempty and not has_content:
        return _unknown("missing-evidence", f"{description}: solution patch is empty")
    return None


def _arm_problem(receipt: dict, name: str, patch: str | None) -> dict | None:
    arm = receipt["arms"].get(name)
    if arm is None:
        return _unknown("not-run", f"{name}: missing arm")
    if not isinstance(arm, dict):
        return _unknown("invalid", f"{name}: arm must be an object")
    status = arm.get("status")
    if not isinstance(status, str) or status not in ARM_STATUSES:
        return _unknown("invalid", f"{name}: invalid arm status")
    # A stopped arm need not have been allocated or have source/log metadata.
    # If it does claim source bindings, they must not contradict the receipt.
    for field in SOURCE_FIELDS:
        if field in arm and arm[field] != receipt[field]:
            return _unknown("source-mismatch", f"{name}: {field} differs from receipt")
    if "task_id" in arm and arm["task_id"] != receipt["task_id"]:
        return _unknown("source-mismatch", f"{name}: task_id differs from receipt")
    network = "open" if name == "open_oracle" else "locked"
    if "network_mode" in arm and arm["network_mode"] != network:
        return _unknown("source-mismatch", f"{name}: expected {network} network mode")
    if "solution_patch_sha256" in arm and arm["solution_patch_sha256"] != patch:
        return _unknown("source-mismatch", f"{name}: solution patch differs from extraction")
    if status != "complete":
        return _unknown(status, f"{name}: {status}")
    for field in SOURCE_FIELDS:
        if field not in arm:
            return _unknown("source-mismatch", f"{name}: missing {field} binding")
    if arm.get("network_mode") != network:
        return _unknown("source-mismatch", f"{name}: expected {network} network mode")
    if "solution_patch_sha256" not in arm:
        return _unknown("source-mismatch", f"{name}: missing solution patch binding")
    if not _text(arm.get("sandbox_id")):
        return _unknown("invalid", f"{name}: missing sandbox_id")
    if arm.get("test_patch_applied") is not True:
        return _unknown("infrastructure-error", f"{name}: test patch was not applied")
    if arm.get("solution_patch_applied") is not (patch is not None):
        return _unknown(
            "infrastructure-error", f"{name}: invalid solution patch application evidence"
        )
    # Honor explicit termination metadata as well as conventional timeout and
    # signal-shaped return codes. Error text is never a network diagnosis.
    if arm.get("timed_out") is True:
        return _unknown("timeout", f"{name}: verifier timed out")
    if arm.get("killed") is True or arm.get("oom_killed") is True:
        return _unknown("infrastructure-error", f"{name}: verifier was killed")
    termination = arm.get("termination_reason")
    if termination is not None and termination not in ("exited", "completed"):
        return _unknown("infrastructure-error", f"{name}: abnormal verifier termination")
    code = arm.get("verifier_exit_code")
    if type(code) is not int:
        return _unknown("infrastructure-error", f"{name}: missing integer verifier exit code")
    if code == 124:
        return _unknown("timeout", f"{name}: timeout-shaped verifier exit code 124")
    if code < 0 or code >= 128:
        return _unknown("infrastructure-error", f"{name}: signal-shaped verifier exit code {code}")
    return _bound_file_problem(
        arm.get("evidence_path"),
        arm.get("evidence_sha256"),
        f"{name} log",
    )


def _extraction_problem(receipt: dict, extraction: dict) -> dict | None:
    for field in SOURCE_FIELDS:
        if field in extraction and extraction[field] != receipt[field]:
            return _unknown("source-mismatch", f"extraction: {field} differs from receipt")
    for field in ("task", "task_id"):
        if field in extraction and extraction[field] != receipt["task_id"]:
            return _unknown("source-mismatch", f"extraction: {field} differs from receipt")
    return None


def classify_receipt(receipt: dict) -> dict:
    """Return label/state/reason without inferring missing or operational results.

    Standalone classification binds arms to the receipt. ``write_sweep`` also
    binds the receipt to the frozen manifest and rejects duplicate observations.
    Valid completed locked nop passes take precedence over extraction failures.
    """
    if not isinstance(receipt, dict):
        return _unknown("invalid", "receipt must be an object")
    if type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1:
        return _unknown("invalid", "unsupported receipt schema_version")
    problem = _identity_error(receipt, require_base=False)
    if problem:
        return _unknown("invalid", problem)
    if receipt.get("base") is None:
        state = receipt.get("execution_status")
        if state not in ("budget-stopped", "not-run", "infrastructure-error", "timeout"):
            state = "infrastructure-error"
        reason = receipt.get("execution_reason")
        return _unknown(
            state,
            reason if _text(reason) else "base unavailable; no source-bound verifier observation",
        )
    if not isinstance(receipt.get("arms"), dict):
        return _unknown("not-run", "missing arms object")
    problem = _arm_problem(receipt, "nop", None)
    if problem:
        return problem
    nop = receipt["arms"]["nop"]
    if nop["verifier_exit_code"] == 0:
        return _classified("nop:pass", "completed source-bound locked nop passed")

    extraction = receipt.get("extraction")
    if not isinstance(extraction, dict):
        return _unknown("invalid", "missing extraction object")
    problem = _extraction_problem(receipt, extraction)
    if problem:
        return problem
    status = extraction.get("status")
    if not isinstance(status, str):
        return _unknown("invalid", "extraction: missing status")
    if status in NONE_STATUSES:
        return _classified("oracle:none", f"extractor found no usable source fix: {status}")
    if status == "patch-no-apply":
        return _classified("oracle:patch-conflict", "extractor explicitly reported patch-no-apply")
    if status == "needs-tip-decision":
        return _unknown("ambiguous", "extraction: needs-tip-decision")
    if status in OPERATIONAL_EXTRACTION_STATUSES:
        state = "timeout" if status == "git-timeout" else "infrastructure-error"
        return _unknown(state, f"extraction: {status}")
    if status not in ("ok", "ok-divergent"):
        return _unknown("invalid", f"extraction: unsupported status {status}")
    fix = extraction.get("fix")
    if not isinstance(fix, dict) or not _matches(SHA40, fix.get("sha")):
        return _unknown("invalid", "extraction: missing valid introducing fix SHA")
    tip = extraction.get("tip")
    if tip is not None and not _matches(SHA40, tip):
        return _unknown("invalid", "extraction: invalid patch tip SHA")
    if status == "ok-divergent" and tip is None:
        return _unknown("invalid", "extraction: divergent patch requires a separate tip SHA")
    if not _text(extraction.get("strategy")):
        return _unknown("invalid", "extraction: missing selection strategy")
    patch = extraction.get("solution_patch_sha256")
    if not _matches(SHA256, patch) or patch == EMPTY_PATCH_SHA256:
        return _unknown("invalid", "extraction: missing nonempty solution patch digest")
    if "solution_patch_bytes" in extraction:
        size = extraction["solution_patch_bytes"]
        if type(size) is not int or size <= 0:
            return _unknown("invalid", "extraction: solution patch must be nonempty")
    if extraction.get("apply_check_on_base") is not True:
        return _unknown(
            "infrastructure-error", "extraction: patch was not checked as applicable on base"
        )
    problem = _bound_file_problem(
        extraction.get("solution_patch_path"),
        patch,
        "extraction patch",
        nonempty=True,
    )
    if problem:
        return problem

    problem = _arm_problem(receipt, "oracle", patch)
    if problem:
        return problem
    oracle = receipt["arms"]["oracle"]
    if oracle["sandbox_id"] == nop["sandbox_id"]:
        return _unknown("source-mismatch", "oracle: reused nop sandbox instead of a fresh base")
    if oracle["verifier_exit_code"] == 0:
        return _classified(
            "oracle:pass+nop:fail", "completed locked oracle passed and locked nop failed"
        )

    # Optional confirmation cannot erase an independently observed locked fail.
    # It can only promote that failure on a fully bound completed open success.
    failure = _classified(
        "oracle:fail",
        "applied source patch failed the completed locked verifier",
    )
    if "open_oracle" not in receipt["arms"]:
        failure["network_confirmation"] = {
            "state": "not-run",
            "reason": "optional open_oracle was not supplied",
        }
        return failure
    problem = _arm_problem(receipt, "open_oracle", patch)
    if problem:
        failure["network_confirmation"] = {
            "state": problem["state"],
            "reason": problem["reason"],
        }
        return failure
    opened = receipt["arms"]["open_oracle"]
    if opened["sandbox_id"] in (nop["sandbox_id"], oracle["sandbox_id"]):
        failure["network_confirmation"] = {
            "state": "source-mismatch",
            "reason": "open_oracle: reused sandbox instead of a fresh base",
        }
        return failure
    if opened["verifier_exit_code"] == 0:
        return {
            **_classified(
                "oracle:fail-network",
                "same patch failed locked oracle and passed fresh open oracle",
            ),
            "network_confirmation": {
                "state": "confirmed",
                "reason": "source-bound completed open_oracle passed",
            },
        }
    failure["network_confirmation"] = {
        "state": "failed",
        "reason": "source-bound completed open_oracle also failed",
    }
    return failure


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _stage(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise


def _publish(outputs: list[tuple[Path, bytes]]) -> None:
    """Stage all files first; atomically replace each, rolling back I/O failures.

    Two paths cannot be a crash-atomic filesystem transaction. The coverage
    records the CSV hash so a reader can detect an interrupted publication.
    """
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path | None] = {}
    published: list[Path] = []
    try:
        for path, data in outputs:
            staged[path] = _stage(path, data)
            backups[path] = _stage(path, path.read_bytes()) if path.exists() else None
        for path, _ in outputs:
            os.replace(staged[path], path)
            published.append(path)
    except BaseException:
        for path in reversed(published):
            backup = backups[path]
            if backup is None:
                path.unlink(missing_ok=True)
            else:
                os.replace(backup, path)
        raise
    finally:
        for temporary in (*staged.values(), *backups.values()):
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def write_sweep(
    manifest: dict,
    receipts: list[tuple[Path, dict]],
    csv_path: Path,
    coverage_path: Path,
) -> dict:
    """Validate the frozen cohort, classify observations, and publish both outputs.

    Manifest/receipt identity errors and any duplicate observation raise
    ``ValueError`` before output publication. Arm/extraction/operational unknowns
    retain complete task coverage without entering the scientific CSV. Evidence
    paths in CSV are the source receipt paths, not a single cherry-picked arm.
    """
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] != 1:
        raise ValueError("unsupported manifest schema_version")
    tasks = manifest.get("tasks")
    if not isinstance(tasks, list):
        raise ValueError("manifest tasks must be a list")
    requested: dict[str, dict] = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("manifest task must be an object")
        problem = _identity_error(task, require_base=False)
        if problem:
            raise ValueError(f"manifest: {problem}")
        task_id = task["task_id"]
        if task_id in requested:
            raise ValueError(f"duplicate manifest task: {task_id}")
        requested[task_id] = task

    observed: dict[str, tuple[Path, dict]] = {}
    source_paths: set[Path] = set()
    receipt_hashes: dict[str, str] = {}
    if not isinstance(receipts, list):
        raise ValueError("receipts must be a list of (path, object) observations")
    for observation in receipts:
        if not isinstance(observation, (tuple, list)) or len(observation) != 2:
            raise ValueError("receipt observation must contain a path and object")
        path, receipt = observation
        if not isinstance(path, (str, Path)):
            raise ValueError("receipt path must be a filesystem path")
        path = Path(path).resolve()
        if not isinstance(receipt, dict):
            raise ValueError(f"{path}: receipt must be an object")
        problem = _identity_error(receipt, require_base=False)
        if problem:
            raise ValueError(f"{path}: {problem}")
        if type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1:
            raise ValueError(f"{path}: unsupported receipt schema_version")
        task_id = receipt["task_id"]
        if task_id not in requested:
            raise ValueError(f"{path}: task is absent from manifest: {task_id}")
        if task_id in observed:
            raise ValueError(f"duplicate receipt observation for {task_id}")
        if path in source_paths:
            raise ValueError(f"duplicate receipt source path: {path}")
        for field in ("run_digest", "image", "test_patch_sha256", "base"):
            if field == "base" and receipt.get("base") is None:
                continue
            if field in requested[task_id] and receipt.get(field) != requested[task_id][field]:
                raise ValueError(f"{path}: {field} conflicts with manifest for {task_id}")
        observed[task_id] = (path, receipt)
        source_receipt, source_hash = _load_json(path)
        if _json_bytes(source_receipt) != _json_bytes(receipt):
            raise ValueError(f"{path}: supplied receipt differs from source JSON")
        receipt_hashes[task_id] = source_hash
        source_paths.add(path)
    protected_paths = set(source_paths)
    for _, receipt in observed.values():
        extraction = receipt.get("extraction")
        if isinstance(extraction, dict) and _text(extraction.get("solution_patch_path")):
            protected_paths.add(Path(extraction["solution_patch_path"]).resolve())
        arms = receipt.get("arms")
        if isinstance(arms, dict):
            for arm in arms.values():
                if isinstance(arm, dict) and _text(arm.get("evidence_path")):
                    protected_paths.add(Path(arm["evidence_path"]).resolve())

    csv_path, coverage_path = Path(csv_path).absolute(), Path(coverage_path).absolute()
    if csv_path.resolve() == coverage_path.resolve():
        raise ValueError("CSV and coverage output paths must differ")
    if csv_path.resolve() in protected_paths or coverage_path.resolve() in protected_paths:
        raise ValueError("outputs must not overwrite source receipts, patches, or logs")

    rows: list[dict] = []
    coverage_tasks: list[dict] = []
    source_receipts: list[dict] = []
    for task_id in sorted(requested):
        task = requested[task_id]
        if task_id not in observed:
            classification = _unknown("not-run", "no receipt supplied for requested task")
            coverage_tasks.append(
                {
                    **task,
                    **classification,
                    "receipt_path": None,
                    "evidence_path": None,
                    "evidence_paths": {},
                    "arm_statuses": {},
                }
            )
            continue
        path, receipt = observed[task_id]
        classification = classify_receipt(receipt)
        extraction = receipt.get("extraction")
        extraction = extraction if isinstance(extraction, dict) else {}
        arms = receipt.get("arms")
        arms = arms if isinstance(arms, dict) else {}
        coverage_tasks.append(
            {
                **task,
                **classification,
                "base": receipt.get("base"),
                "execution_status": receipt.get("execution_status"),
                "execution_reason": receipt.get("execution_reason"),
                "receipt_path": str(path),
                "evidence_path": str(path),
                "extraction_status": extraction.get("status"),
                "solution_patch_path": extraction.get("solution_patch_path"),
                "solution_patch_sha256": extraction.get("solution_patch_sha256"),
                "evidence_paths": {
                    name: arm.get("evidence_path")
                    for name, arm in sorted(arms.items())
                    if isinstance(arm, dict)
                },
                "evidence_sha256": {
                    name: arm.get("evidence_sha256")
                    for name, arm in sorted(arms.items())
                    if isinstance(arm, dict)
                },
                "arm_statuses": {
                    name: arm.get("status")
                    for name, arm in sorted(arms.items())
                    if isinstance(arm, dict)
                },
            }
        )
        source_receipts.append(
            {
                "task_id": task_id,
                "path": str(path),
                "content_sha256": _sha256(_json_bytes(receipt)),
                "sha256": receipt_hashes[task_id],
            }
        )
        label = classification["label"]
        if label is None:
            continue
        has_fix = label not in ("oracle:none", "nop:pass")
        fix = extraction.get("fix")
        fix_sha = fix.get("sha") if isinstance(fix, dict) else None
        patch_tip = extraction.get("tip")
        rows.append(
            {
                "task_id": task_id,
                "label": label,
                "fix_commit": fix_sha if has_fix and _matches(SHA40, fix_sha) else "",
                "patch_tip": patch_tip if has_fix and _matches(SHA40, patch_tip) else "",
                "how_chosen": extraction.get("strategy")
                if _text(extraction.get("strategy"))
                else "",
                "evidence_path": str(path),
                "run_digest": task["run_digest"],
            }
        )

    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    csv_bytes = stream.getvalue().encode("utf-8")
    label_counts = Counter(row["label"] for row in rows)
    counts = {
        "requested_count": len(requested),
        "observed_count": len(observed),
        "classified_count": len(rows),
        "unclassified_count": len(requested) - len(rows),
        "counts_per_label": {label: label_counts[label] for label in LABELS},
        "counts_per_state": dict(sorted(Counter(task["state"] for task in coverage_tasks).items())),
        "counts_per_reason": dict(
            sorted(
                Counter(task["reason"] for task in coverage_tasks if task["label"] is None).items()
            )
        ),
    }
    coverage = {
        "schema_version": 1,
        **counts,
        "manifest_metadata": {key: value for key, value in manifest.items() if key != "tasks"},
        "sources": {
            "manifest_content_sha256": _sha256(_json_bytes(manifest)),
            "receipts": source_receipts,
        },
        "csv_path": str(csv_path),
        "csv_sha256": _sha256(csv_bytes),
        "tasks": coverage_tasks,
    }
    coverage_bytes = _json_bytes(coverage)
    _publish([(csv_path, csv_bytes), (coverage_path, coverage_bytes)])
    return {
        **counts,
        "csv_path": str(csv_path),
        "coverage_path": str(coverage_path),
        "csv_sha256": _sha256(csv_bytes),
        "coverage_sha256": _sha256(coverage_bytes),
    }


def _unique_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _load_json(path: Path) -> tuple[dict, str]:
    data = path.read_bytes()
    value = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: JSON root must be an object")
    return value, _sha256(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--receipts",
        type=Path,
        required=True,
        help="dedicated directory of receipt JSON files (searched recursively)",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--coverage", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if not args.receipts.is_dir():
            raise ValueError("--receipts must be an existing directory")
        outputs = (args.output.resolve(), args.coverage.resolve())
        if args.manifest.resolve() in outputs:
            raise ValueError("outputs must not overwrite the manifest")
        if any(path.is_relative_to(args.receipts.resolve()) for path in outputs):
            raise ValueError("outputs must be outside the receipt input directory")
        manifest, manifest_hash = _load_json(args.manifest)
        receipts = []
        sources = []
        for path in sorted(args.receipts.rglob("*.json")):
            receipt, digest = _load_json(path)
            receipts.append((path, receipt))
            sources.append({"path": str(path.resolve()), "sha256": digest})
        summary = write_sweep(manifest, receipts, args.output, args.coverage)
    except (OSError, UnicodeError, ValueError) as exc:
        parser.exit(2, f"leak-oracle results: {exc}\n")
    print(
        json.dumps(
            {
                **summary,
                "sources": {
                    "manifest": {"path": str(args.manifest.resolve()), "sha256": manifest_hash},
                    "receipts": sources,
                },
            },
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
