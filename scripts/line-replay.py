#!/usr/bin/env python3
"""Run the frozen, zero-spend standard line replay gate.

Usage: ``uv run python scripts/line-replay.py [--set PATH]``. The set defaults
to EVALLAB_LINE_REPLAY_SET or ~/Developer/eval-lab-results/line/replay-v1.
Missing required local data exits 77 (SKIP); invalid data exits 2. Replay's
0/pass, 1/regression and 2/invalid statuses are preserved. This tool never
refreshes the baseline or writes Judge Station state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from evallab.line.replay import replay

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SCHEMA = "evallab.line_replay_manifest/v1"


def _object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def _digest(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
            size += len(chunk)
    return hasher.hexdigest(), size


def _count(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a nonnegative integer")
    return value


def _sha256(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError(f"{field} must be a SHA-256 hex digest")
    return value.lower()


def _input_path(root: Path, value: Any) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("manifest path must be nonempty relative POSIX text")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise ValueError(f"manifest path escapes set: {value!r}")
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f"manifest path escapes set: {value!r}")
    return path


def _validate_set(root: Path) -> dict[str, Any]:
    manifest_path = root / "MANIFEST.json"
    manifest = _object(manifest_path)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ValueError("unsupported manifest schema")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("manifest files must be a nonempty list")
    if _count(manifest.get("input_files"), "manifest input_files") != len(files):
        raise ValueError("manifest input_files does not match files count")
    run_count = _count(manifest.get("run_count"), "manifest run_count")
    corpora = manifest.get("corpora")
    if not isinstance(corpora, dict) or any(
        not isinstance(name, str) or not name for name in corpora
    ):
        raise ValueError("manifest corpora must map corpus names to run counts")
    if sum(_count(n, f"manifest corpora.{name}") for name, n in corpora.items()) != run_count:
        raise ValueError("manifest corpus counts do not match run_count")
    seen: set[Path] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("manifest file entries must be objects")
        path = _input_path(root, entry.get("path"))
        if path in seen:
            raise ValueError(f"duplicate manifest input: {entry['path']}")
        seen.add(path)
        expected_hash = _sha256(entry.get("sha256"), f"manifest {entry['path']} sha256")
        expected_size = _count(entry.get("size"), f"manifest {entry['path']} size")
        if not isinstance(entry.get("kind"), str) or not entry["kind"]:
            raise ValueError(f"manifest {entry['path']} needs a kind")
        if "source" in entry and not isinstance(entry["source"], str):
            raise ValueError(f"manifest {entry['path']} source must be text")
        if not path.is_file():
            raise ValueError(f"manifest input missing or not a file: {entry['path']}")
        actual_hash, actual_size = _digest(path)
        if actual_size != expected_size or actual_hash != expected_hash:
            raise ValueError(f"manifest input hash/size mismatch: {entry['path']}")
    if (root / "spec.json").resolve() not in seen:
        raise ValueError("manifest does not include spec.json")
    _object(root / "spec.json")
    baseline = _object(root / "baseline.json")
    provenance_path = root / "baseline-provenance.json"
    if provenance_path.exists() or provenance_path.is_symlink():
        provenance = _object(provenance_path)
        for field, path in (
            ("baseline_sha256", root / "baseline.json"),
            ("manifest_sha256", manifest_path),
        ):
            expected = _sha256(provenance.get(field), f"baseline provenance {field}")
            if _digest(path)[0] != expected:
                raise ValueError(f"baseline provenance {field} mismatch")
    return baseline


def _rate(group: dict[str, Any], metric: str, denominator: str) -> str:
    value = group["metrics"][metric]
    k = _count(value["k"], f"{metric}.k")
    n = _count(value["n"], f"{metric}.n")
    if n != _count(group[denominator], denominator) or k > n:
        raise ValueError(f"invalid {metric} denominator")
    return f"{k}/{n} ({100 * k / n:.1f}%)" if n else "0/0 (n/a)"


def _summary(result: dict[str, Any], baseline: dict[str, Any], status: int) -> None:
    print(f"Verdict: { {0: 'PASS', 1: 'REGRESSION', 2: 'INVALID'}[status] }")
    if result.get("error"):
        print(f"Replay error: {result['error']}", file=sys.stderr)
    batch_value = result.get("batch")
    if not isinstance(batch_value, str) or not batch_value:
        raise ValueError("replay result has no batch path")
    batch = Path(batch_value)
    print(f"Batch: {batch}")
    changes = result.get("changed_runs")
    if not isinstance(changes, list) or any(not isinstance(row, dict) for row in changes):
        raise ValueError("replay changed_runs must be a list of objects")
    print(f"Changed decisions: {len(changes)}")
    if changes:
        report = batch / "changed-decisions.json"
        report.write_text(
            json.dumps(changes, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"Changed decision report: {report}")
    previous = baseline["scoreboard"]["by_corpus"]
    current = _object(batch / "scoreboard.json")["by_corpus"]
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise ValueError("scoreboards need by_corpus objects")
    for corpus in sorted(previous.keys() | current.keys()):
        before, after = previous[corpus], current[corpus]
        recall = " -> ".join(
            _rate(group, "drop_or_review_recall", "cheats") for group in (before, after)
        )
        alarms = " -> ".join(
            _rate(group, "clean_false_alarm_rate", "clean") for group in (before, after)
        )
        print(f"{corpus}: recall {recall}; false alarms {alarms}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", dest="replay_set", type=Path, help="frozen replay set directory")
    args = parser.parse_args(argv)
    try:
        root = (
            (
                args.replay_set
                if args.replay_set is not None
                else Path(
                    os.environ.get(
                        "EVALLAB_LINE_REPLAY_SET",
                        str(Path.home() / "Developer/eval-lab-results/line/replay-v1"),
                    )
                )
            )
            .expanduser()
            .resolve()
        )
        required = [
            root,
            *(root / name for name in ("spec.json", "MANIFEST.json", "baseline.json")),
        ]
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            print(f"SKIP: standard line replay data missing: {', '.join(missing)}", file=sys.stderr)
            return 77
        baseline = _validate_set(root)
        result, status = replay(root / "spec.json", root / "baseline.json", REPO_ROOT)
        if isinstance(status, bool) or not isinstance(status, int) or status not in (0, 1, 2):
            raise ValueError("replay returned an invalid exit status")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"Invalid standard line replay data: {exc}", file=sys.stderr)
        return 2
    try:
        if not isinstance(result, dict):
            raise ValueError("replay result must be an object")
        _summary(result, baseline, status)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"Replay summary unavailable: {exc}", file=sys.stderr)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
