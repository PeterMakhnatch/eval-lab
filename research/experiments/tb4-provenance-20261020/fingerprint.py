"""Static public-package qualification for the frozen pC-SELF-1 experiment.

This downloads original packages only. It never starts an environment or agent.
Historical locks are bound by exact native task digest resolution AND a freshly
computed Harbor Packager digest, not by the current value of a dataset alias.
Full inventories and byte diffs deliberately include non-verifier support files.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import os
import stat
import tempfile
import tomllib
import warnings
from collections import Counter
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from evallab.hub_trials import (
    DATASET_DIGEST,
    HubError,
    IntegrityError,
    NativeHarbor,
    _inside,
    _safe,
)

SCHEMA = "evallab.tb4_fingerprints/v1"
UPSTREAM_REF = "452bf305c6daa62fc59061d22133a7cbc7c1572e"
UPSTREAM_ROOT = Path(
    "/Users/petermakhnatch/Developer/eval-lab-data/tb4-20261020/"
    "upstream-git-v4.0.0/terminal-bench"
)
IN2_ROOT = Path(
    "/Users/petermakhnatch/Developer/eval-lab/.worktrees/"
    "infra-tb4-hub-trials-20261020/runs/in2-hub-discovery"
)
PRIMARY_TASKS = (
    "atrx-vep-crispr", "batched-eval-parity", "biped-contact-dynamics",
    "cumulative-layout-shift", "data-anonymization", "distributed-dedup",
    "vf2-speedup-networkx", "wal-recovery-ordering",
)
CATEGORIES = ("instruction", "tests", "environment", "config", "reference", "support")
SOURCES = {
    "terminal-bench/terminal-bench": ("canonical-dataset", DATASET_DIGEST),
    "ryanmarten/terminal-bench-4-no-prebuild": ("no-prebuild-discovery", "latest"),
}


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package_key(source: str, digest: str, task_name: str) -> str:
    return sha256(json.dumps([source, digest, task_name], separators=(",", ":")).encode())


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def immutable_write(path: Path, data: bytes) -> None:
    """Never adopt, repair, or replace an existing original or receipt."""
    _safe(path.parent)
    if path.is_symlink():
        raise IntegrityError(f"Symlink evidence destination: {path}")
    if path.exists():
        if not path.is_file() or path.read_bytes() != data:
            raise IntegrityError(f"Existing evidence differs: {path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(data)


def inventory(root: Path) -> dict[str, dict[str, Any]]:
    """Inventory all paths, retaining link metadata separately from file bytes.

    A contained file symlink has the target file's byte hash plus its own target
    text and mode. External, dangling, or directory links have unmeasured bytes;
    their targets are never traversed. Empty directories are retained too.
    """
    if root.is_symlink() or not root.is_dir():
        raise IntegrityError(f"Package root is not an ordinary directory: {root}")
    root = root.resolve()
    result = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        entry: dict[str, Any] = {"mode": stat.S_IMODE(info.st_mode)}
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(path)
            resolved = path.resolve()
            entry.update(
                kind="symlink", symlink=target, symlink_sha256=sha256(os.fsencode(target)),
                size_bytes=None, sha256=None,
            )
            if resolved.is_relative_to(root) and resolved.is_file():
                entry.update(size_bytes=resolved.stat().st_size, sha256=file_hash(resolved))
        elif stat.S_ISREG(info.st_mode):
            entry.update(kind="file", symlink=None, size_bytes=info.st_size, sha256=file_hash(path))
        elif stat.S_ISDIR(info.st_mode):
            entry.update(kind="directory", symlink=None, size_bytes=0, sha256=None)
        else:
            raise IntegrityError(f"Unsupported package path kind: {path}")
        result[relative] = entry
    return result


def category(path: str) -> str:
    parts = Path(path).parts
    if path == "instruction.md" or (parts[0] == "steps" and parts[-1] == "instruction.md"):
        return "instruction"
    if parts[0] == "tests" or (parts[0] == "steps" and "tests" in parts):
        return "tests"
    if parts[0] == "environment":
        return "environment"
    if path == "task.toml":
        return "config"
    if parts[0] in {"solution", "reference", "references"} or (
        parts[0] == "steps" and "solution" in parts
    ):
        return "reference"
    return "support"


def content_hash_coverage(
    exported: dict[str, dict], upstream: dict[str, dict] | None,
    native_hash_paths: list[str] | None,
) -> dict:
    selected = set(native_hash_paths or [])
    scope_known = native_hash_paths is not None
    groups = {}
    for group in CATEGORIES:
        paths = {
            path for path, info in exported.items()
            if category(path) == group and info["kind"] != "directory"
        }
        upstream_paths = {
            path for path, info in (upstream or {}).items()
            if category(path) == group and info["kind"] != "directory"
        }
        excluded = paths - selected if scope_known else set()
        unknown = {path for path in paths if exported[path]["sha256"] is None}
        missing = upstream_paths - paths
        groups[group] = {
            "native_hash_included_paths": sorted(paths & selected),
            "exported_content_paths_not_hashed": sorted(excluded),
            "unmeasured_exported_content_paths": sorted(unknown),
            "upstream_content_paths_missing_from_export": sorted(missing),
            "all_exported_content_paths_hashed": (
                not excluded and not unknown if scope_known and paths else None
            ),
            "full_compared_content_paths_digest_covered": (
                not excluded and not unknown and not missing
                if scope_known and upstream is not None and (paths or upstream_paths) else None
            ),
        }
    return groups


def byte_identity(entry: dict[str, Any]) -> tuple[Any, ...]:
    kind = "file" if entry["kind"] == "symlink" and entry["sha256"] is not None else entry["kind"]
    return kind, entry["sha256"], entry["size_bytes"]


def compare_inventories(
    original: dict[str, dict[str, Any]], upstream: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Separate full path/byte identity from modes and symlink metadata.

    Equality is not runtime equivalence. Categories with no measured files in
    either package, or unmeasured symlink bytes, are never vacuously equal.
    """
    common = original.keys() & upstream.keys()
    unmeasured = {
        path for files in (original, upstream) for path, entry in files.items()
        if entry["kind"] == "symlink" and entry["sha256"] is None
    }
    changed = sorted(path for path in common - unmeasured if byte_identity(original[path]) != byte_identity(upstream[path]))
    metadata = sorted(
        path for path in common
        if any(original[path][field] != upstream[path][field] for field in ("kind", "mode", "symlink"))
    )
    missing_original = sorted(upstream.keys() - original.keys())
    missing_upstream = sorted(original.keys() - upstream.keys())
    categories = {}
    for group in CATEGORIES:
        left = {path for path in original if category(path) == group}
        right = {path for path in upstream if category(path) == group}
        differences = sorted(path for path in changed if category(path) == group)
        missing_left = sorted(path for path in missing_original if category(path) == group)
        missing_right = sorted(path for path in missing_upstream if category(path) == group)
        unknown = sorted(path for path in unmeasured if category(path) == group)
        present = any(
            category(path) == group and entry["kind"] != "directory"
            for files in (original, upstream) for path, entry in files.items()
        ) or group == "support"
        equal = False if differences or missing_left or missing_right else (
            None if unknown or not present else True
        )
        categories[group] = {
            "equal": equal, "changed_paths": differences,
            "missing_from_package": missing_left, "missing_from_upstream": missing_right,
            "unmeasured_paths": unknown,
            "metadata_changed_paths": sorted(path for path in metadata if category(path) == group),
            "package_path_count": len(left), "upstream_path_count": len(right),
        }
    return {
        "byte_and_path_equal": False if changed or missing_original or missing_upstream else (
            None if unmeasured else True
        ),
        "metadata_equal": not (metadata or missing_original or missing_upstream),
        "changed_paths": changed, "missing_from_package": missing_original,
        "missing_from_upstream": missing_upstream, "unmeasured_paths": sorted(unmeasured),
        "metadata_changed_paths": metadata, "categories": categories,
    }


def binding_status(expected: str | None, resolved: str | None, computed: str | None) -> str:
    """A declaration or mutable-alias match without measured bytes is insufficient."""
    if not isinstance(expected, str) or not expected.startswith("sha256:"):
        return "unmeasured"
    digest = expected.removeprefix("sha256:")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return "unmeasured"
    return "bound" if resolved == digest == computed else "unmeasured"


def qualified_equalities(binding: str, comparison: dict[str, Any] | None) -> dict[str, bool | None]:
    return {
        f"{group}_equal": comparison["categories"][group]["equal"]
        if binding == "bound" and comparison is not None else None
        for group in CATEGORIES
    }


def text_diff(original: Path, upstream: Path, comparison: dict[str, Any]) -> bytes:
    """Keep complete UTF-8 unified diffs; binary/type changes remain hash-inventoried."""
    chunks = []
    paths = sorted(set(comparison["changed_paths"]) | set(comparison["missing_from_package"]) | set(comparison["missing_from_upstream"]))
    for relative in paths:
        left, right = upstream / relative, original / relative
        # Inspect ancestors root-first: a leaf below a directory link is not
        # itself a symlink, but its bytes were deliberately not inventoried.
        if any(
            any(parent.is_symlink() for parent in reversed((path, *path.parents)) if parent.is_relative_to(root))
            or (path.exists() and not path.is_file())
            for path, root in ((left, upstream), (right, original))
        ):
            chunks.append(f"Type or directory change: {relative}\n")
            continue
        old = left.read_bytes() if left.exists() else b""
        new = right.read_bytes() if right.exists() else b""
        try:
            before, after = old.decode("utf-8"), new.decode("utf-8")
        except UnicodeDecodeError:
            chunks.append(f"Binary change: {relative}; see full inventories and original files.\n")
            continue
        if "\x00" in before or "\x00" in after:
            chunks.append(f"Binary change: {relative}; see full inventories and original files.\n")
            continue
        chunks.extend(difflib.unified_diff(
            before.splitlines(keepends=True), after.splitlines(keepends=True),
            fromfile=f"upstream/{relative}", tofile=f"package/{relative}",
        ))
    return "".join(chunks).encode()


def dataset_receipt(root: Path, source: str, native: NativeHarbor) -> tuple[dict, dict]:
    root = _safe(root)
    label, ref = SOURCES[source]
    stdout = _safe(root / "bootstrap" / f"{label}.stdout")
    receipt_path = _safe(stdout.with_suffix(".receipt.json"))
    args = ["version", "show", f"{source}@{ref}", "--tasks", "--json"]
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_bytes())
        if receipt.get("args") != args or not stdout.is_file() or file_hash(stdout) != receipt.get("sha256"):
            raise IntegrityError(f"Dataset receipt changed: {receipt_path}")
        if stdout.stat().st_size != receipt.get("size_bytes"):
            raise IntegrityError(f"Dataset receipt byte count changed: {receipt_path}")
        raw = stdout.read_bytes()
    else:
        if stdout.exists():
            raise IntegrityError(f"Unreceipted native metadata: {stdout}")
        raw = native.json(args[:-1])
        immutable_write(stdout, raw)
        receipt = {
            "args": args, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "path": str(stdout.relative_to(root)), "sha256": sha256(raw),
            "size_bytes": len(raw), "status": "complete",
        }
        immutable_write(receipt_path, json_bytes(receipt))
    payload = json.loads(raw)
    if payload.get("package") != source or payload.get("type") != "dataset" or payload.get("visibility") != "public":
        raise IntegrityError(f"Unexpected/nonpublic dataset identity: {source}")
    if ref.startswith("sha256:") and payload.get("content_hash") != ref:
        raise IntegrityError(f"Native dataset pin did not resolve exactly: {source}")
    return payload, {**receipt, "receipt_path": str(receipt_path.relative_to(root))}


def bootstrap_packages(root: Path, metadata: dict[str, dict]) -> tuple[list[dict], dict]:
    """Start actual byte collection without waiting for the native census worker."""
    canonical = metadata["terminal-bench/terminal-bench"]
    expected = {
        row["task_version"]["package"]["name"]: "sha256:" + row["task_version"]["content_hash"]
        for row in canonical["tasks"] if row.get("task_version")
    }
    packages = {}
    for name, digest in expected.items():
        source, task_name = "terminal-bench/terminal-bench", f"terminal-bench/{name}"
        key = package_key(source, digest, task_name)
        packages[key] = {
            "key": key, "source": source, "digest": digest, "task_name": task_name,
            "canonical_digest": digest,
            "dataset_refs": [{"name": source, "ref": DATASET_DIGEST}],
        }
    originals = []
    for filename in ("gemini-3-8-flash-lock-survey.json", "board-survey.json"):
        path = IN2_ROOT / filename
        raw = path.read_bytes()
        originals.append({"path": str(path), "sha256": sha256(raw), "size_bytes": len(raw)})
        for row in json.loads(raw):
            source, digest, task_name = row.get("lock_source"), row.get("lock_digest"), row.get("task")
            if not all(isinstance(value, str) and value for value in (source, digest, task_name)):
                continue
            key = package_key(source, digest, task_name)
            packages.setdefault(key, {
                "key": key, "source": source, "digest": digest, "task_name": task_name,
                "canonical_digest": expected.get(task_name.rsplit("/", 1)[-1]),
                "dataset_refs": row.get("job_dataset", []),
            })
    payload = {"schema": "evallab.tb4_bootstrap_package_locks/v1", "inputs": originals, "packages": list(packages.values())}
    path = root / "bootstrap-locks.json"
    immutable_write(path, json_bytes(payload))
    return payload["packages"], {"path": str(path), "sha256": file_hash(path), "size_bytes": path.stat().st_size, "bootstrap_only": True}


def download_package(root: Path, pin: str, native: NativeHarbor) -> tuple[Path | None, dict]:
    from harbor.publisher.packager import Packager

    root = _safe(root)
    index = _safe(root / "download-index" / f"{sha256(pin.encode())}.json")
    if index.exists():
        pointer = json.loads(index.read_bytes())
        receipt_path = _inside(root, pointer["receipt_path"])
        if file_hash(receipt_path) != pointer["sha256"]:
            raise IntegrityError(f"Native download receipt changed: {receipt_path}")
        directory = receipt_path.parent
        receipt = json.loads(receipt_path.read_bytes())
        if receipt.get("pin") != pin:
            raise IntegrityError(f"Native download receipt identity changed: {receipt_path}")
        if receipt.get("status") != "complete":
            return None, receipt
        task_root = _inside(directory, receipt["task_relative_path"])
        if inventory(task_root) != receipt["inventory"]:
            raise IntegrityError(f"Native original package changed: {task_root}")
        stdout = _safe(directory / "download.stdout")
        if not stdout.is_file() or file_hash(stdout) != receipt["stdout_sha256"]:
            raise IntegrityError(f"Native download stdout changed: {stdout}")
        return task_root, receipt
    downloads = _safe(root / "downloads")
    downloads.mkdir(parents=True, exist_ok=True)
    # A crashed/unreceipted attempt is never adopted or overwritten. A fresh
    # directory preserves those original bytes while permitting a new attempt.
    directory = Path(tempfile.mkdtemp(prefix=f"{sha256(pin.encode())}-", dir=downloads))
    receipt_path = directory / "receipt.json"
    export = directory / "original"
    args = ["task", "download", pin, "--export", "--output-dir", str(export)]
    receipt: dict[str, Any] = {
        "pin": pin, "args": args, "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "receipt_path": str(receipt_path.relative_to(root)),
    }
    task_root = None
    try:
        stdout = native._run(args)
        immutable_write(directory / "download.stdout", stdout)
        task_root = export / pin.split("@", 1)[0].split("/", 1)[1]
        files = inventory(task_root)
        for relative, entry in files.items():
            if entry["kind"] == "symlink" and not (task_root / relative).resolve().is_relative_to(task_root):
                raise IntegrityError(f"Package symlink escapes package root: {relative}")
        computed, selected = Packager.compute_content_hash(task_root)
        receipt.update(
            status="complete", task_relative_path=str(task_root.relative_to(directory)),
            stdout_path=str((directory / "download.stdout").relative_to(root)),
            stdout_sha256=sha256(stdout), stdout_size_bytes=len(stdout),
            computed_native_digest=computed, native_hash_paths=[str(path.relative_to(task_root)) for path in selected],
            inventory=files,
        )
    except HubError as exc:
        receipt.update(status="failed", error=str(exc))
        task_root = None
    immutable_write(receipt_path, json_bytes(receipt))
    immutable_write(index, json_bytes({
        "receipt_path": str(receipt_path.relative_to(root)), "sha256": file_hash(receipt_path),
    }))
    return task_root, receipt


def edge_task_name(source: str, edge: dict) -> str | None:
    package = edge.get("task_version", {}).get("package", {})
    name = package.get("name", "")
    if source == "terminal-bench/terminal-bench":
        return name if package.get("org", {}).get("name") == "terminal-bench" else None
    if source == "ryanmarten/terminal-bench-4-no-prebuild":
        prefix = "terminal-bench-4-no-prebuild-"
        if package.get("org", {}).get("name") == "ryanmarten" and name.startswith(prefix):
            return name[len(prefix):]
    return None


def summarize(packages: list[dict]) -> dict:
    measured = [entry for entry in packages if entry["status"] == "measured"]
    primary = [entry for entry in packages if entry["task_name"].rsplit("/", 1)[-1] in PRIMARY_TASKS]
    return {
        "distinct_packages_requested": len(packages), "distinct_packages_measured": len(measured),
        "distinct_packages_unmeasured": len(packages) - len(measured),
        "primary_distinct_packages_requested": len(primary),
        "primary_distinct_packages_measured": sum(entry["status"] == "measured" for entry in primary),
        "source_families": dict(Counter(entry["source"] for entry in packages)),
        "binding_statuses": dict(Counter(entry["binding_status"] for entry in packages)),
        "declared_source_bindings": dict(Counter(entry["declared_source_binding"] for entry in packages)),
        "source_membership_unproven_among_digest_bound": sum(
            entry["binding_status"] == "bound" and entry["declared_source_binding"] == "unproven"
            for entry in packages
        ),
        "canonical_dataset_eligible_among_measured": sum(
            entry["canonical_dataset_eligible"] for entry in measured
        ),
        "distinct_task_names_requested": len({entry["task_name"] for entry in packages}),
        "distinct_task_names_measured": len({entry["task_name"] for entry in measured}),
        "unmeasured_reasons": dict(Counter(
            entry["unmeasured_reason"] for entry in packages if entry["status"] != "measured"
        )),
        "by_source": {
            source: {
                "requested": sum(entry["source"] == source for entry in packages),
                "measured": sum(entry["source"] == source for entry in measured),
                "declared_source_unproven": sum(
                    entry["source"] == source and entry["declared_source_binding"] == "unproven"
                    for entry in packages
                ),
            }
            for source in sorted({entry["source"] for entry in packages})
        },
        "equal_categories_among_measured": {
            group: sum(entry[f"{group}_equal"] is True for entry in measured) for group in CATEGORIES
        },
        "full_dimension_digest_content_coverage_among_measured": {
            group: {
                "complete": sum(
                    entry["content_hash_coverage"][group]["full_compared_content_paths_digest_covered"] is True
                    for entry in measured
                ),
                "incomplete": sum(
                    entry["content_hash_coverage"][group]["full_compared_content_paths_digest_covered"] is False
                    for entry in measured
                ),
                "unmeasured": sum(
                    entry["content_hash_coverage"][group]["full_compared_content_paths_digest_covered"] is None
                    for entry in measured
                ),
            }
            for group in CATEGORIES
        },
        "different_categories_among_measured": {
            group: sum(entry[f"{group}_equal"] is False for entry in measured) for group in CATEGORIES
        },
        "full_byte_and_path_equal_among_measured": sum(entry["comparison"]["byte_and_path_equal"] is True for entry in measured),
    }


def qualify(
    output: Path, upstream_root: Path, packages: list[dict], metadata: dict[str, dict],
    dataset_receipts: dict[str, dict], input_receipt: dict, native: NativeHarbor,
) -> dict:
    entries = []
    upstream_inventories = {}
    refused = None
    canonical_task_digests = {
        edge_task_name("terminal-bench/terminal-bench", row): "sha256:" + row["task_version"]["content_hash"]
        for row in metadata.get("terminal-bench/terminal-bench", {}).get("tasks", [])
        if row.get("task_version")
    }
    packages = sorted(packages, key=lambda item: (item["task_name"].rsplit("/", 1)[-1] not in PRIMARY_TASKS, item["task_name"], item["source"], item["digest"] or ""))
    for specification in packages:
        source, digest, task_name = (specification.get(field) for field in ("source", "digest", "task_name"))
        if specification.get("key") != package_key(source, digest, task_name):
            raise IntegrityError("Package key does not bind the exact source/digest/task_name tuple")
        short_name = task_name.rsplit("/", 1)[-1]
        if short_name in {"", ".", ".."} or "/" in short_name or "\\" in short_name:
            raise IntegrityError("Unsafe upstream task name")
        upstream_path = upstream_root / short_name
        entry = {
            **specification, "status": "unmeasured", "binding_status": "unmeasured",
            "binding_scope": "native_packager_digest_bytes_not_source_membership",
            "declared_source_binding": "unproven",
            "declared_source_binding_is_historical_attestation": False,
            "recorded_source_matches_actual_export_source": None,
            "actual_export_source": None, "actual_export_ref": None,
            "actual_export_dataset_ref": None, "actual_export_receipt": None,
            "historical_full_inventory_binding": "unmeasured",
            "native_hash_scope": None, "content_hash_coverage": None,
            "canonical_dataset_eligible": (
                source == "terminal-bench/terminal-bench"
                and task_name == f"terminal-bench/{short_name}"
                and digest == canonical_task_digests.get(short_name)
            ),
            "upstream": {"path": str(upstream_path), "ref": UPSTREAM_REF},
            "download": None, "comparison": None, "unmeasured_reason": None,
        }
        entry.update(qualified_equalities("unmeasured", None))
        edges = [
            (source, row) for row in metadata.get(source, {}).get("tasks", [])
            if row.get("task_version") and edge_task_name(source, row) == short_name
            and "sha256:" + row["task_version"]["content_hash"] == digest
        ]
        if not edges:
            # Content-addressed recovery is evidence about native hash bytes,
            # never about membership in the source recorded by the trial lock.
            edges = [
                (export_source, row)
                for export_source, dataset in metadata.items() for row in dataset.get("tasks", [])
                if row.get("task_version") and edge_task_name(export_source, row) == short_name
                and "sha256:" + row["task_version"]["content_hash"] == digest
            ]
        if len(edges) != 1:
            entry["unmeasured_reason"] = "No unique public native dataset edge with the recorded historical task digest"
        elif refused is not None:
            entry["unmeasured_reason"] = "Native acquisition stopped after refusal; no automatic retry"
        else:
            export_source, edge = edges[0]
            package = edge["task_version"]["package"]
            pin = f"{package['org']['name']}/{package['name']}@{digest}"
            entry["version"] = {
                "task_version_id": edge["task_version_id"], "task_package": pin.split("@", 1)[0],
                "content_hash": edge["task_version"]["content_hash"], "dataset_source": export_source,
                "dataset_resolved_digest": metadata[export_source]["content_hash"],
                "dataset_requested_ref": SOURCES[export_source][1], "dataset_receipt": dataset_receipts[export_source],
                "historical_dataset_ref_claim": False,
            }
            try:
                original, receipt = download_package(output, pin, native)
                entry["download"] = receipt
                entry["binding_status"] = binding_status(digest, edge["task_version"]["content_hash"], receipt.get("computed_native_digest"))
                if original is not None:
                    entry.update(
                        actual_export_source=export_source, actual_export_ref=pin,
                        actual_export_dataset_ref=f"{export_source}@{metadata[export_source]['content_hash']}",
                        actual_export_receipt={
                            "path": receipt["receipt_path"], "sha256": sha256(json_bytes(receipt)),
                        },
                        recorded_source_matches_actual_export_source=source == export_source,
                    )
                    if source == export_source:
                        entry["declared_source_binding"] = (
                            "observed_in_pinned_canonical_dataset_edge"
                            if source == "terminal-bench/terminal-bench"
                            else "observed_in_public_export_dataset_edge_only"
                        )
                    selected = set(receipt.get("native_hash_paths", []))
                    entry["native_hash_scope"] = {
                        "status": "measured" if "native_hash_paths" in receipt else "unmeasured",
                        "included_paths": sorted(selected),
                        "not_hashed_paths": sorted(
                            path for path, info in receipt["inventory"].items()
                            if "native_hash_paths" in receipt
                            and info["kind"] != "directory" and path not in selected
                        ),
                        "mode_and_symlink_metadata_hashed": False,
                    }
                    entry["content_hash_coverage"] = content_hash_coverage(
                        receipt["inventory"], None, receipt.get("native_hash_paths"),
                    )
                if original is None:
                    refused = receipt.get("error", "Native acquisition failed")
                    entry["unmeasured_reason"] = refused
                elif entry["binding_status"] != "bound":
                    entry["unmeasured_reason"] = "Downloaded bytes did not reproduce the recorded historical native task digest"
                elif not upstream_path.is_dir() or upstream_path.is_symlink():
                    entry["unmeasured_reason"] = "Exact upstream task directory unavailable"
                else:
                    if short_name not in upstream_inventories:
                        upstream_inventories[short_name] = inventory(upstream_path)
                    upstream_files = upstream_inventories[short_name]
                    entry["upstream"]["inventory"] = upstream_files
                    entry["content_hash_coverage"] = content_hash_coverage(
                        receipt["inventory"], upstream_files, receipt.get("native_hash_paths"),
                    )
                    comparison = compare_inventories(receipt["inventory"], upstream_files)
                    comparison_dir = output / "comparisons" / specification["key"] / sha256(json_bytes(comparison))
                    immutable_write(comparison_dir / "comparison.json", json_bytes(comparison))
                    delta = text_diff(original, upstream_path, comparison)
                    immutable_write(comparison_dir / "bytes.diff", delta)
                    entry.update(status="measured", comparison=comparison)
                    entry.update(qualified_equalities(entry["binding_status"], comparison))
                    entry["diff"] = {"path": str((comparison_dir / "bytes.diff").relative_to(output)), "sha256": sha256(delta), "size_bytes": len(delta)}
            except IntegrityError as exc:
                entry["unmeasured_reason"] = str(exc)
        entries.append(entry)
    payload = {
        "schema": SCHEMA, "input": input_receipt, "packages": entries,
        "coverage": summarize(entries), "native_refusal": refused,
        "boundaries": {
            "runtime_equivalence": "not_measured_or_claimed",
            "historical_identity": "recorded lock digest reproduced for native Packager-selected bytes, not source membership or historical full inventory",
            "source_membership": "observed native dataset edges are recorded separately; cross-source digest recovery never proves recorded-source membership",
            "canonical_eligibility": "recorded source AND task digest must match the pinned canonical dataset; static content equality cannot confer eligibility",
            "native_digest_scope": "Harbor publishable file selection only; excluded bytes and all modes/link metadata lack historical digest binding; complete static inventories retained",
            "category_equalities": "all paths and file bytes in each category, not only common selected verifier files; modes and symlink metadata separate",
            "dimension_hash_coverage": "per-dimension coverage describes native hash inclusion of compared exported content paths, not equality, source membership, metadata or runtime attestation",
            "mode_scope": "native exported filesystem modes, not unretained tar headers",
            "alias_scope": "dataset alias observations discover digest edges only; aliases are not historical dataset identity",
            "trial_outcomes": "not_read_or_relabelled_by_this_static_comparison",
            "upstream_originals": "retained unchanged at the exact supplied Git export root",
        },
    }
    destination = output / "fingerprints.json"
    temporary = destination.with_suffix(".json.tmp")
    with temporary.open("xb") as handle:
        handle.write(json_bytes(payload))
    temporary.replace(destination)
    return payload


def in1_full_inventory_hash(records: dict[str, dict[str, Any]]) -> str:
    """Frozen IN-1 full file hash; unlike native Packager, no ignore filtering."""
    content = "".join(
        f"{path}\0{records[path]['sha256']}\n"
        for path in sorted(records, key=lambda relative: Path(relative).parts)
        if records[path].get("kind") != "directory"
    )
    return sha256(content.encode())


def verifier_difference_reconciliation(output: Path, fingerprints_path: Path, in1_root: Path, native_input: Path) -> dict[str, Any]:
    """Reconcile package exclusions against the frozen full IN-1 inventory, offline."""
    manifest_raw = (in1_root / "source-manifest.json").read_bytes()
    files_raw = (in1_root / "file-manifest.jsonl").read_bytes()
    manifest = json.loads(manifest_raw)
    if manifest["authoritative_source"]["commit"] != UPSTREAM_REF:
        raise IntegrityError("IN-1 inventory is not tied to the frozen exact Git commit")
    frozen: dict[str, dict[str, dict[str, Any]]] = {"git": {}, "hub": {}}
    for line in files_raw.splitlines():
        record = json.loads(line)
        frozen[record["source"]].setdefault(record["task"], {})[record["path"]] = record
    family_raw = fingerprints_path.read_bytes()
    canonical = {entry["task_name"].rsplit("/", 1)[-1]: entry for entry in json.loads(family_raw)["packages"] if entry["source"] == "terminal-bench/terminal-bench" and entry["canonical_dataset_eligible"] is True}
    if canonical.keys() != frozen["git"].keys():
        raise IntegrityError("Reconciliation requires every exact canonical task, not a partial or alternate-source inventory")
    native_raw = native_input.read_bytes()
    native = json.loads(native_raw)
    row_models = {row["id"]: row["model_label"] for row in native["rows"]}
    comparisons, unequal = [], []
    task_hashes = {}
    for task, entry in sorted(canonical.items()):
        current_git = {path: info for path, info in inventory(Path(entry["upstream"]["path"])).items() if info["kind"] != "directory"}
        current_hub = {path: info for path, info in entry["download"]["inventory"].items() if info["kind"] != "directory"}
        sides = {}
        for source_name, current in (("git", current_git), ("hub", current_hub)):
            recorded = frozen[source_name][task]
            common = current.keys() & recorded.keys()
            sides[source_name] = {
                "frozen_file_count": len(recorded), "observed_file_count": len(current),
                "added_paths": sorted(current.keys() - recorded.keys()),
                "missing_paths": sorted(recorded.keys() - current.keys()),
                "content_changed_paths": sorted(path for path in common if (recorded[path]["sha256"], recorded[path]["size_bytes"]) != (current[path]["sha256"], current[path]["size_bytes"])),
                "metadata_changed_paths": sorted(path for path in common if (recorded[path]["mode"], recorded[path]["symlink"]) != (current[path]["mode"], current[path]["symlink"])),
            }
        task_hashes[task] = in1_full_inventory_hash(current_git)
        sides["git"].update({
            "observed_all_files_sha256": task_hashes[task],
            "frozen_all_files_sha256": manifest["byte_comparison"]["tasks"][task]["git_all_files_content_sha256"],
        })
        comparisons.append({"task": task, **sides})
        if entry["instruction_equal"] is not False and entry["tests_equal"] is not False:
            continue
        members = [trial for trial in native["trials"] if trial["primary_labelled"] and trial["package_key"] == entry["key"]]
        row_counts = Counter(row_id for trial in members for row_id in trial["row_ids"])
        missing = entry["comparison"]["categories"]["tests"]["missing_from_package"]
        files = [path for path in missing if entry["upstream"]["inventory"][path]["kind"] != "directory"]
        frozen_omitted = {item["path"]: item for item in manifest["byte_comparison"]["tasks"][task]["git_only_files"] if item["path"].startswith("tests/")}
        unequal.append({
            "task": task, "package_key": entry["key"], "source": entry["source"], "digest": entry["digest"],
            "strict_instruction_equal": entry["instruction_equal"], "strict_tests_equal": entry["tests_equal"],
            "difference_kind": "package_excludes_Git_test_support_paths_not_common_file_edits",
            "common_instruction_or_test_content_changes": [path for path in entry["comparison"]["changed_paths"] if category(path) in {"instruction", "tests"}],
            "missing_test_support_files": [{"path": path, **entry["upstream"]["inventory"][path], "frozen_IN1_record": frozen_omitted.get(path)} for path in files],
            "missing_test_support_directories": [path for path in missing if path not in files],
            "primary_native_unique_trial_count": len(members),
            "primary_native_trial_ids": sorted(trial["hub_trial_id"] for trial in members),
            "row_memberships": [{"row_id": row_id, "model_label": row_models[row_id], "trial_count": count} for row_id, count in sorted(row_counts.items())],
            "byte_diff": entry["diff"],
        })
    payload = {
        "schema": "evallab.tb4_verifier_difference_reconciliation/v1",
        "baseline": {"Git_ref": UPSTREAM_REF, "IN1_manifest_path": str(in1_root / "source-manifest.json"), "IN1_manifest_sha256": sha256(manifest_raw), "IN1_file_manifest_path": str(in1_root / "file-manifest.jsonl"), "IN1_file_manifest_sha256": sha256(files_raw)},
        "native_primary_input": {"path": str(native_input.resolve()), "sha256": sha256(native_raw)},
        "source_family_input": {"path": str(fingerprints_path.resolve()), "sha256": sha256(family_raw)},
        "frozen_IN1_common_file_claim": manifest["byte_comparison"]["common_tests_and_solution_file_content"],
        "observed_Git_all_task_content_sha256": sha256("".join(f"{task}\0{digest}\n" for task, digest in sorted(task_hashes.items())).encode()),
        "frozen_Git_all_task_content_sha256": manifest["authoritative_source"]["all_task_content_sha256"],
        "source_tree_drift_since_frozen_IN1": any(side["git"]["added_paths"] or side["git"]["missing_paths"] or side["git"]["content_changed_paths"] for side in comparisons),
        "canonical_export_content_drift_since_frozen_IN1": any(side["hub"]["added_paths"] or side["hub"]["missing_paths"] or side["hub"]["content_changed_paths"] for side in comparisons),
        "source_comparisons": comparisons, "unequal_primary_groups": unequal,
        "unequal_primary_native_trial_count": sum(group["primary_native_unique_trial_count"] for group in unequal),
        "boundaries": {
            "strict_unequal_flags": "preserved; exclusions are not normalized away",
            "formatting": "inspect the retained common-file change lists; missing paths are not formatting changes",
            "behavior": "impact of omitted files is not executed, inferred or adjudicated",
            "baseline": "frozen IN-1 full file inventory tied to the exact supplied Git commit; no shared source files changed",
            "runtime_equivalence": "not_measured_or_claimed",
            "Git_observation": "fresh full inventory of the supplied exact upstream root",
            "Hub_observation": "retained qualified native-export inventory, not a fresh mutable alias lookup",
            "drift_scope": "all frozen file/symlink content paths; IN-1 does not inventory empty directories; mode/link metadata differences are reported separately",
        },
    }
    immutable_write(output / "reconciliation-inputs/source-manifest.json", manifest_raw)
    immutable_write(output / "reconciliation-inputs/file-manifest.jsonl", files_raw)
    destination = output / "verifier-difference-reconciliation.json"
    previous = destination.read_bytes() if destination.exists() else None
    if previous is not None:
        immutable_write(output / "reconciliation-stages" / f"{sha256(previous)}.json", previous)
    temporary = destination.with_suffix(".json.tmp")
    with temporary.open("xb") as handle:
        handle.write(json_bytes(payload))
    temporary.replace(destination)
    return payload


def json_value(value: Any) -> Any:
    """Preserve TOML-only scalar types explicitly; raw bytes remain authoritative."""
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_value(item) for item in value]
    if isinstance(value, (datetime, date, time)):
        return {"toml_type": type(value).__name__, "value": value.isoformat()}
    if isinstance(value, float) and not math.isfinite(value):
        return {"toml_type": "float", "value": repr(value)}
    return value


def field_changes(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """Type-sensitive, presence-sensitive JSON-pointer differences without normalization."""
    def pointer(parent: str, key: str) -> str:
        return parent + "/" + key.replace("~", "~0").replace("/", "~1")

    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for key in sorted(before.keys() | after.keys()):
            child = pointer(path, key)
            if key not in before or key not in after:
                changes.append({
                    "path": child, "before_present": key in before, "after_present": key in after,
                    **({"before": json_value(before[key]), "before_type": type(before[key]).__name__} if key in before else {}),
                    **({"after": json_value(after[key]), "after_type": type(after[key]).__name__} if key in after else {}),
                })
            else:
                changes.extend(field_changes(before[key], after[key], child))
        return changes
    if isinstance(before, list) and isinstance(after, list):
        return field_changes({str(index): item for index, item in enumerate(before)}, {str(index): item for index, item in enumerate(after)}, path)
    if type(before) is type(after) and (before == after or isinstance(before, float) and math.isnan(before) and math.isnan(after)):
        return []
    return [{
        "path": path or "/", "before_present": True, "after_present": True,
        "before": json_value(before), "after": json_value(after),
        "before_type": type(before).__name__, "after_type": type(after).__name__,
    }]


def unknown_configuration_fields(raw: dict[str, Any], model: Any, path: str = "") -> dict[str, Any]:
    """Retain fields ignored by the pinned model, including nested package metadata."""
    unknown = {}
    fields = type(model).model_fields
    aliases = {field.alias: name for name, field in fields.items() if isinstance(field.alias, str)}
    legacy = {"version"} if type(model).__name__ == "TaskConfig" else {"memory", "storage"} if type(model).__name__ == "EnvironmentConfig" else set()
    for key, value in raw.items():
        child_path = path + "/" + key.replace("~", "~0").replace("/", "~1")
        field_name = aliases.get(key, key)
        if field_name not in fields:
            if key not in legacy:
                unknown[child_path] = {"value": json_value(value), "type": type(value).__name__}
            continue
        child = getattr(model, field_name)
        if isinstance(value, dict) and hasattr(type(child), "model_fields"):
            unknown.update(unknown_configuration_fields(value, child, child_path))
        elif isinstance(value, list) and isinstance(child, list):
            for index, (raw_item, model_item) in enumerate(zip(value, child)):
                if isinstance(raw_item, dict) and hasattr(type(model_item), "model_fields"):
                    unknown.update(unknown_configuration_fields(raw_item, model_item, f"{child_path}/{index}"))
    return unknown


def declaration_projection(config: Any) -> dict[str, Any]:
    """Resolve documented declaration inheritance, never job/provider/runtime overrides."""
    def environment(value: Any) -> dict[str, Any]:
        result = value.model_dump(mode="json")
        result.pop("network_mode", None)
        result.pop("allowed_hosts", None)
        result["network_policy"] = value.resolve_baseline().model_dump(mode="json")
        return result

    def phase(value: Any, *, verifier: bool) -> dict[str, Any]:
        result = value.model_dump(mode="json")
        baseline = config.environment
        if verifier:
            baseline = value.environment or baseline
            result["environment_mode"] = value.environment_mode or ("separate" if value.environment is not None else "shared")
        result["environment"] = environment(baseline)
        result.pop("network_mode", None)
        result.pop("allowed_hosts", None)
        result["network_policy"] = (value.explicit_phase_policy() or baseline.resolve_baseline()).model_dump(mode="json")
        return result

    steps = None
    if config.steps is not None:
        steps = []
        for step in config.steps:
            item = step.model_dump(mode="json")
            item["agent"] = phase(step.agent, verifier=False)
            item["verifier"] = phase(step.verifier, verifier=True)
            steps.append(item)
    return {
        "agent": phase(config.agent, verifier=False),
        "verifier": phase(config.verifier, verifier=True),
        "solution": config.solution.model_dump(mode="json"),
        "artifacts": config.model_dump(mode="json")["artifacts"],
        "multi_step_reward_strategy": config.multi_step_reward_strategy,
        "steps": steps,
    }


def parse_configuration(raw: bytes) -> dict[str, Any]:
    from harbor.models.task.config import TaskConfig

    result: dict[str, Any] = {"raw_sha256": sha256(raw), "size_bytes": len(raw)}
    captured = []
    try:
        text = raw.decode("utf-8")
        values = tomllib.loads(text)
        result["raw_values"] = json_value(values)
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            model = TaskConfig.model_validate_toml(text)
        result.update({
            "sdk_declarations": model.model_dump(mode="json"),
            "unknown_raw_fields": unknown_configuration_fields(values, model),
            "inherited_declaration_projection": declaration_projection(model),
            "verifier_environment_origin": "explicit_verifier_environment" if model.verifier.environment is not None else "top_level_environment",
            "error": None,
        })
    except Exception as exc:
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
    result["warnings"] = [{"type": warning.category.__name__, "message": str(warning.message)} for warning in captured]
    return result


def configuration_dimensions(changes: list[dict[str, Any]]) -> list[str]:
    dimensions = set()
    for change in changes:
        parts = set(change["path"].split("/"))
        if parts & {"cpus"}:
            dimensions.add("cpu")
        if parts & {"memory", "memory_mb"}:
            dimensions.add("ram")
        if parts & {"gpus", "gpu_types", "tpu"}:
            dimensions.add("gpu_tpu")
        if parts & {"storage", "storage_mb"}:
            dimensions.add("storage")
        if parts & {"timeout_sec", "build_timeout_sec"}:
            dimensions.add("timeouts")
        if parts & {"network_policy", "network_mode", "allowed_hosts", "allow_internet"}:
            dimensions.add("network")
        if parts & {"docker_image", "image", "build"}:
            dimensions.add("images_prebuild")
        if "verifier" in parts:
            dimensions.add("grader_configuration")
    return sorted(dimensions)


def configuration_classification(raw_equal: bool, raw_changes: list, sdk_changes: list, projected_changes: list, unknown_changes: list) -> str:
    if raw_equal:
        return "raw_byte_identical"
    if not raw_changes:
        return "raw_syntax_or_comment_change_sdk_declarations_equal"
    if projected_changes:
        return "sdk_interpreted_runtime_declaration_changes"
    if sdk_changes and all(change["path"].split("/")[1] in {"task", "metadata", "source", "schema_version"} for change in sdk_changes):
        return "sdk_package_metadata_only"
    if sdk_changes:
        return "sdk_declaration_shape_changes_inherited_projection_equal"
    if unknown_changes:
        return "raw_unknown_fields_changed_sdk_declarations_equal"
    return "raw_field_normalization_sdk_declarations_equal"


def configuration_comparison(output: Path, fingerprints_path: Path) -> dict[str, Any]:
    """Offline analysis of retained original exports; never instantiate a native client."""
    from importlib.metadata import version

    import harbor.models.task.config as config_module
    import yaml
    sdk_version = version("harbor")
    if sdk_version != "0.24.0":
        raise IntegrityError(f"Configuration qualification requires pinned Harbor 0.24.0, got {sdk_version}")

    raw_input = fingerprints_path.read_bytes()
    source = json.loads(raw_input)
    entries, failures = [], []

    def original(root: Path, relative: str, expected: dict[str, Any]) -> bytes:
        path = root / relative
        if not path.resolve().is_relative_to(root.resolve()):
            raise IntegrityError("configuration resolves outside its original task root")
        raw = path.read_bytes()
        if sha256(raw) != expected["sha256"]:
            raise IntegrityError("configuration bytes differ from their retained qualified inventory")
        return raw

    def raw_diff(key: str, relative: str, before: bytes, after: bytes) -> str:
        combined_hash = sha256(before + b"\0" + after)
        destination = output / "configuration-diffs" / key / combined_hash / f"{sha256(relative.encode())[:12]}.diff"
        try:
            diff = "".join(difflib.unified_diff(before.decode("utf-8").splitlines(keepends=True), after.decode("utf-8").splitlines(keepends=True), fromfile=f"pinned-git/{relative}", tofile=f"native-export/{relative}"))
        except UnicodeDecodeError:
            diff = "Non-UTF8 original configuration; inspect retained original files and their SHA-256 digests.\n"
        immutable_write(destination, diff.encode("utf-8"))
        return str(destination.relative_to(output))

    for package in source["packages"]:
        entry = {key: package[key] for key in ("key", "source", "digest", "task_name")}
        entry.update({
            "actual_export_source": package.get("actual_export_source"),
            "actual_export_ref": package.get("actual_export_ref"),
            "binding_status": package.get("binding_status"),
            "declared_source_binding": package.get("declared_source_binding"),
            "instruction_equal": package.get("instruction_equal"),
            "tests_equal": package.get("tests_equal"),
            "static_configuration_equal": package.get("config_equal"),
            "status": "unmeasured", "errors": [],
        })
        try:
            if package.get("binding_status") != "bound":
                raise IntegrityError("configuration export is not bound to its recorded digest")
            download = package["download"]
            package_root = output / Path(download["receipt_path"]).parent / download["task_relative_path"]
            if not package_root.resolve().is_relative_to(output.resolve()):
                raise IntegrityError("configuration export root is outside the retained packages directory")
            upstream_root = Path(package["upstream"]["path"])
            before = original(upstream_root, "task.toml", package["upstream"]["inventory"]["task.toml"])
            after = original(package_root, "task.toml", download["inventory"]["task.toml"])
            git_config, exported_config = parse_configuration(before), parse_configuration(after)
            entry.update({
                "upstream": {"path": str(upstream_root / "task.toml"), **git_config},
                "exported": {"path": str(package_root / "task.toml"), **exported_config},
                "native_hash_covers_exported_task_toml": "task.toml" in package["native_hash_scope"]["included_paths"],
                "raw_bytes_equal": before == after,
                "raw_diff": raw_diff(package["key"], "task.toml", before, after),
            })
            for side, parsed in (("upstream", git_config), ("exported", exported_config)):
                if parsed["error"]:
                    entry["errors"].append({"scope": side + "/task.toml", **parsed["error"]})
            if not entry["errors"]:
                raw_changes = field_changes(tomllib.loads(before.decode("utf-8")), tomllib.loads(after.decode("utf-8")))
                sdk_changes = field_changes(git_config["sdk_declarations"], exported_config["sdk_declarations"])
                projected = field_changes(git_config["inherited_declaration_projection"], exported_config["inherited_declaration_projection"])
                unknown_changes = field_changes(git_config["unknown_raw_fields"], exported_config["unknown_raw_fields"])
                entry.update({
                    "status": "measured",
                    "raw_field_changes": raw_changes, "sdk_field_changes": sdk_changes,
                    "inherited_declaration_projection_changes": projected,
                    "unknown_raw_field_changes": unknown_changes,
                    "sdk_field_change_dimensions": configuration_dimensions(sdk_changes),
                    "inherited_projection_change_dimensions": configuration_dimensions(projected),
                    "classification": configuration_classification(before == after, raw_changes, sdk_changes, projected, unknown_changes),
                })
            compose = []
            compose_names = {"docker-compose.yaml", "docker-compose.yml", "compose.yaml", "compose.yml"}
            paths = set(package["upstream"]["inventory"]) | set(download["inventory"])
            for relative in sorted(path for path in paths if category(path) == "environment" and Path(path).name in compose_names):
                item: dict[str, Any] = {"path": relative, "errors": []}
                values = {}
                originals = {}
                for side, task_root, records in (("upstream", upstream_root, package["upstream"]["inventory"]), ("exported", package_root, download["inventory"])):
                    if relative not in records:
                        item[side] = {"present": False}
                        continue
                    content = original(task_root, relative, records[relative])
                    originals[side] = content
                    item[side] = {"present": True, "path": str(task_root / relative), "sha256": sha256(content), "size_bytes": len(content)}
                    try:
                        values[side] = yaml.safe_load(content)
                        item[side]["values"] = json_value(values[side])
                    except Exception as exc:
                        item["errors"].append({"scope": side + "/" + relative, "type": type(exc).__name__, "message": str(exc)})
                item["raw_bytes_equal"] = originals.get("upstream") == originals.get("exported") if len(originals) == 2 else False
                item["raw_diff"] = raw_diff(package["key"], relative, originals.get("upstream", b""), originals.get("exported", b""))
                if not item["errors"]:
                    item["field_changes"] = field_changes(values.get("upstream"), values.get("exported"))
                    item["changed_dimensions"] = configuration_dimensions(item["field_changes"])
                entry["errors"].extend(item["errors"])
                compose.append(item)
            entry["compose_comparisons"] = compose
        except Exception as exc:
            entry["errors"].append({"scope": "configuration_inventory", "type": type(exc).__name__, "message": str(exc)})
        if entry["errors"]:
            failures.extend({"key": package["key"], **error} for error in entry["errors"])
        entries.append(entry)

    dimensions = ("cpu", "ram", "gpu_tpu", "storage", "timeouts", "network", "images_prebuild", "grader_configuration")
    def summary(selected: list[dict[str, Any]]) -> dict[str, Any]:
        measured = [entry for entry in selected if entry["status"] == "measured"]
        return {
            "task_packages": len(selected), "sdk_task_pairs_measured": len(measured),
            "sdk_task_pairs_unmeasured": len(selected) - len(measured),
            "distinct_upstream_task_toml_files": len({entry["upstream"]["path"] for entry in measured}),
            "distinct_exported_task_toml_files": len({entry["exported"]["path"] for entry in measured}),
            "exported_task_toml_digest_content_covered": sum(entry["native_hash_covers_exported_task_toml"] for entry in measured),
            "instruction_tests_content_equal_with_projected_declaration_changes": sum(entry["instruction_equal"] is True and entry["tests_equal"] is True and bool(entry["inherited_declaration_projection_changes"]) for entry in measured),
            "instruction_tests_content_equal_without_projected_declaration_changes": sum(entry["instruction_equal"] is True and entry["tests_equal"] is True and not entry["inherited_declaration_projection_changes"] for entry in measured),
            "classifications": dict(sorted(Counter(entry.get("classification", "unmeasured") for entry in selected).items())),
            "sdk_field_change_dimensions": {dimension: sum(dimension in entry["sdk_field_change_dimensions"] for entry in measured) for dimension in dimensions},
            "inherited_projection_change_dimensions": {dimension: sum(dimension in entry["inherited_projection_change_dimensions"] for entry in measured) for dimension in dimensions},
            "packages_with_unknown_raw_fields": sum(bool(entry["upstream"].get("unknown_raw_fields") or entry["exported"].get("unknown_raw_fields")) for entry in measured),
            "compose_file_pairs": sum(len(entry.get("compose_comparisons", [])) for entry in selected),
            "compose_pairs_with_raw_byte_changes": sum(not item["raw_bytes_equal"] for entry in selected for item in entry.get("compose_comparisons", [])),
        }
    coverage = summary(entries)
    coverage["errors"] = len(failures)
    coverage["by_source"] = {source_name: summary([entry for entry in entries if entry["source"] == source_name]) for source_name in sorted({entry["source"] for entry in entries})}
    reconciliation_path = output / "verifier-difference-reconciliation.json"
    payload = {
        "schema": "evallab.tb4_configuration_comparison/v1",
        "input": {"path": str(fingerprints_path.resolve()), "sha256": sha256(raw_input), "scope": source["input"].get("scope")},
        "sdk": {"distribution": "harbor", "version": sdk_version, "parser": "harbor.models.task.config.TaskConfig.model_validate_toml", "source_path": config_module.__file__, "source_sha256": file_hash(Path(config_module.__file__)), "compose_parser": "yaml.safe_load", "pyyaml_version": yaml.__version__},
        "coverage": coverage, "packages": entries, "errors": failures,
        "examples": {classification: [{"key": entry["key"], "task_name": entry["task_name"], "source": entry["source"], "changed_paths": [change["path"] for change in entry.get("inherited_declaration_projection_changes", [])]} for entry in entries if entry.get("classification") == classification][:3] for classification in sorted({entry.get("classification", "unmeasured") for entry in entries})},
        "verifier_difference_reconciliation": {"path": str(reconciliation_path), "sha256": file_hash(reconciliation_path)} if reconciliation_path.exists() else None,
        "boundaries": {
            "values": "SDK-interpreted package declarations, never historically applied runtime values",
            "raw_data": "retained originals and lossless raw byte hashes/diffs remain authoritative, including ignored or unknown TOML fields",
            "projection": "documented TaskConfig inheritance only: an absent verifier environment inherits top-level declarations; absent environment mode means separate with an explicit verifier environment, shared otherwise; baseline/phase network normalization uses the pinned SDK",
            "projection_exclusions": "no job, provider, prebuild execution, template/environment-variable resolution, hardware allocation or historical runtime attestation",
            "resource_presence": "SDK raw shape differences and inherited declaration projection differences are distinct; explicitly copying inherited resource declarations is not a projected CPU/RAM change",
            "compose": "safe YAML parsing of retained environment Compose declarations only; images/build fields are not pulled or executed",
            "equality": "normalization classifications never change strict package byte-inequality flags or imply behavior/runtime equivalence",
        },
    }
    destination = output / "configuration-comparison.json"
    temporary = destination.with_suffix(".json.tmp")
    with temporary.open("xb") as handle:
        handle.write(json_bytes(payload))
    temporary.replace(destination)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("runs/pc-self1-provenance/packages"))
    parser.add_argument("--upstream", type=Path, default=UPSTREAM_ROOT)
    parser.add_argument("--locks", type=Path, default=Path("runs/pc-self1-provenance/native/package-locks.json"))
    parser.add_argument("--bootstrap", action="store_true", help="Use frozen IN-2 lock facts while native metadata is still collecting")
    parser.add_argument("--harbor", default=str(Path(".venv/bin/harbor").resolve()))
    parser.add_argument("--configuration", action="store_true", help="Compare retained configuration declarations offline; make no native requests")
    parser.add_argument("--fingerprints", type=Path, help="Configuration input; defaults to source-family-fingerprints.json under --output")
    parser.add_argument("--reconcile", action="store_true", help="Reconcile retained package exclusions with frozen IN-1 inventories offline")
    parser.add_argument("--in1", type=Path, default=Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/infra-tb4-upstream-grader-20261020/runs/in1-tb4-inputs"))
    parser.add_argument("--native-input", type=Path, default=Path("runs/pc-self1-provenance/native/input-manifest.json"))
    args = parser.parse_args()
    args.output = _safe(args.output)
    args.output.mkdir(parents=True, exist_ok=True)
    if args.reconcile:
        if args.bootstrap or args.configuration:
            parser.error("--reconcile cannot be combined with --bootstrap or --configuration")
        result = verifier_difference_reconciliation(args.output, args.fingerprints or args.output / "source-family-fingerprints.json", args.in1, args.native_input)
        print(json.dumps({key: result[key] for key in ("source_tree_drift_since_frozen_IN1", "canonical_export_content_drift_since_frozen_IN1", "unequal_primary_native_trial_count")}, sort_keys=True, indent=2))
        return 0 if not result["source_tree_drift_since_frozen_IN1"] and not result["canonical_export_content_drift_since_frozen_IN1"] else 2
    if args.configuration:
        if args.bootstrap:
            parser.error("--configuration cannot be combined with --bootstrap")
        result = configuration_comparison(args.output, args.fingerprints or args.output / "source-family-fingerprints.json")
        print(json.dumps(result["coverage"], sort_keys=True, indent=2))
        return 0 if not result["errors"] and result["coverage"]["sdk_task_pairs_unmeasured"] == 0 else 2
    native = NativeHarbor(args.harbor)
    metadata, receipts = {}, {}
    for source in SOURCES:
        metadata[source], receipts[source] = dataset_receipt(args.output, source, native)
    if args.bootstrap:
        packages, input_receipt = bootstrap_packages(args.output, metadata)
    else:
        raw = args.locks.read_bytes()
        lock_payload = json.loads(raw)
        packages = lock_payload["packages"]
        input_receipt = {
            "path": str(args.locks.resolve()), "sha256": sha256(raw), "size_bytes": len(raw),
            "bootstrap_only": False, "scope": lock_payload.get("scope"),
            "input_schema": lock_payload.get("schema"),
        }
    result = qualify(args.output, args.upstream, packages, metadata, receipts, input_receipt, native)
    print(json.dumps(result["coverage"], sort_keys=True, indent=2))
    return 0 if result["coverage"]["distinct_packages_unmeasured"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
