#!/usr/bin/env python3
"""Materialize tb2-to-tb21-v1 packages from frozen local git pins.

The archives come from ``git archive`` at the pinned commits. Working-tree
files are never read. Every extracted package is checked with
``evallab.registry.compute_task_digests`` against the answer key.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from evallab.registry import compute_task_digests  # noqa: E402

KEY_ID = "tb2-to-tb21-v1"
KEY_PATH = REPO_ROOT / "research" / "answer-keys" / KEY_ID / "items.jsonl"
SCHEMA = "evallab.tb2_to_tb21.materialization/v1"


@dataclass(frozen=True)
class Pin:
    """One immutable task source."""

    version: str
    repo: Path
    commit: str
    prefix: str

    @property
    def label(self) -> str:
        return f"{self.repo.name}@{self.commit}"


PINS = {
    "tb2": Pin(
        "tb2",
        Path("~/Developer/lab-extras/upstream/terminal-bench-2-tb20").expanduser(),
        "69671fbaac6d67a7ef0dfec016cc38a64ef7a77c",
        "",
    ),
    "tb21": Pin(
        "tb21",
        Path("~/Developer/lab-extras/upstream/terminal-bench-2-1").expanduser(),
        "7131e4375048a0e408a8fb404b5f499d726b695b",
        "tasks",
    ),
}


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_items(path: Path = KEY_PATH) -> list[dict[str, Any]]:
    """Load the 104 key items without depending on the QA scorer."""
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(rows) != 104:
        raise ValueError(f"{path}: expected 104 items, found {len(rows)}")
    return rows


def _repo_rel(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _archive_path(pin: Pin, task_id: str) -> str:
    return f"{pin.prefix}/{task_id}" if pin.prefix else task_id


def _safe_members(payload: bytes) -> list[tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        members = archive.getmembers()
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe archive path: {member.name}")
    return members


def extract_archive(payload: bytes, destination: Path, archive_path: str) -> None:
    """Extract one task directory from a git archive into ``destination``."""
    if not _safe_members(payload):
        raise ValueError(f"{archive_path}: archive is empty")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
        root = Path(temporary)
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
            archive.extractall(root, filter="data")
        extracted = root / archive_path
        if not extracted.is_dir():
            raise FileNotFoundError(f"{archive_path}: task directory missing from archive")
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(extracted, destination)


def _git_archive(pin: Pin, task_id: str) -> bytes:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(pin.repo),
            "archive",
            "--format=tar",
            pin.commit,
            _archive_path(pin, task_id),
        ],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
        raise RuntimeError(detail or f"git archive exited {completed.returncode}")
    if not completed.stdout:
        raise RuntimeError(f"{pin.label}:{_archive_path(pin, task_id)}: empty archive")
    return completed.stdout


def _verify_pin(pin: Pin) -> None:
    if not pin.repo.is_dir():
        raise FileNotFoundError(f"{pin.version} mirror missing: {pin.repo}")
    completed = subprocess.run(
        ["git", "-C", str(pin.repo), "rev-parse", "--verify", "--quiet", f"{pin.commit}^{{commit}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or completed.stdout.strip() != pin.commit:
        raise RuntimeError(f"{pin.version}: commit {pin.commit} is not in {pin.repo}")


def _package_record(
    item: Mapping[str, Any],
    pin: Pin,
    destination: Path,
    *,
    repo_root: Path,
) -> dict[str, Any]:
    version, task_id = str(item["subject"]["task_id"]).split("/", 1)
    expected = str(item["subject"]["task_digest"])
    record: dict[str, Any] = {
        "version": version,
        "task_id": task_id,
        "item_id": item["item_id"],
        "path": _repo_rel(repo_root, destination),
        "source": pin.label,
        "commit": pin.commit,
        "archive_path": _archive_path(pin, task_id),
        "expected_digest": expected,
        "actual_digest": None,
        "digest_match": False,
        "error": None,
    }
    try:
        actual = None
        if destination.is_dir():
            try:
                actual = compute_task_digests(destination).package
            except ValueError:
                actual = None
        if actual == expected:
            record["actual_digest"] = actual
            record["digest_match"] = True
            return record
        payload = _git_archive(pin, task_id)
        extract_archive(payload, destination, _archive_path(pin, task_id))
        actual = compute_task_digests(destination).package
        record["actual_digest"] = actual
        record["digest_match"] = actual == expected
        if actual != expected:
            record["error"] = f"digest mismatch: {actual} != {expected}"
    except (OSError, RuntimeError, ValueError, tarfile.TarError) as exc:
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["digest_match"] = False
    return record


def materialize_packages(
    items: Sequence[Mapping[str, Any]],
    runs_dir: Path,
    *,
    pins: Mapping[str, Pin] = PINS,
    repo_root: Path = REPO_ROOT,
) -> dict[str, Any]:
    """Extract every requested package and record its digest check."""
    for pin in pins.values():
        _verify_pin(pin)
    packages: dict[str, dict[str, Any]] = {}
    for item in items:
        subject = str(item["subject"]["task_id"])
        version, task_id = subject.split("/", 1)
        pin = pins[version]
        destination = runs_dir / "packages" / version / task_id
        packages[subject] = _package_record(item, pin, destination, repo_root=repo_root)
    counts = {
        "items": len(items),
        "materialized": sum(1 for row in packages.values() if row["actual_digest"]),
        "digest_match": {
            version: sum(
                1
                for row in packages.values()
                if row["version"] == version and row["digest_match"]
            )
            for version in pins
        },
        "digest_mismatch": {
            version: sum(
                1
                for row in packages.values()
                if row["version"] == version and row["actual_digest"] and not row["digest_match"]
            )
            for version in pins
        },
        "unmaterialized": {
            version: sum(
                1
                for row in packages.values()
                if row["version"] == version and not row["actual_digest"]
            )
            for version in pins
        },
    }
    report = {
        "schema": SCHEMA,
        "key_id": KEY_ID,
        "recorded_at": _utcnow(),
        "counts": counts,
        "packages": packages,
        "unmaterialized": [
            {
                "task": subject,
                "source": row["source"],
                "archive_path": row["archive_path"],
                "error": row["error"],
            }
            for subject, row in sorted(packages.items())
            if not row["actual_digest"]
        ],
        "mismatches": [
            {
                "task": subject,
                "expected_digest": row["expected_digest"],
                "actual_digest": row["actual_digest"],
            }
            for subject, row in sorted(packages.items())
            if row["actual_digest"] and not row["digest_match"]
        ],
    }
    path = runs_dir / "materialization.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return report


def load_report(runs_dir: Path) -> dict[str, Any]:
    path = runs_dir / "materialization.json"
    if not path.is_file():
        raise FileNotFoundError(f"materialization report missing: {path}")
    report = json.loads(path.read_text(encoding="utf-8"))
    if report.get("schema") != SCHEMA:
        raise ValueError(f"{path}: unexpected schema {report.get('schema')!r}")
    return report
