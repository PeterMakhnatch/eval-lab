#!/usr/bin/env python3
"""Recompute the provable wrong-pass differential count from retained evidence bytes.

A task counts only when a wrong candidate scored the ORIGINAL grader 1.0 and the SAME
candidate scored the REPAIRED grader 0.0 in two retained, executed grades, and the task
is affirmatively dev/train in ``assignments.csv`` with no non-dev row under any key.

The ledger records where each grade lives. This tool re-reads those bytes, so the number
never rests on a summary column: it re-hashes every cited file, resolves every cited
JSON pointer, and applies the production cross-key predicate. It runs nothing: no
container, no cloud, no model.

    verify_differentials.py LEDGER.json --assignments assignments.csv \
        [--root R=/path ...] [--database cloud-trace-db.sqlite] [--output report.json]

Exit status: 0 all entries verified, 1 any rejected, 2 any unverifiable (a cited file
is missing on this machine). Unverifiable entries are never counted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evallab.production.seeker_worker import (
    find_cross_key_dev_assignments,
    find_cross_key_non_dev_assignments,
)

LEDGER_SCHEMA = "evallab.verified_differentials/v1"
REPORT_SCHEMA = "evallab.verified_differentials_report/v1"

# A host-side grade is not a container grade; "unstated" is kept visible in the counts.
REJECTED_BACKENDS = frozenset({"host"})
LEVELS = frozenset({"native_differential_single", "native_differential_with_controls"})
# exact_digest: one recorded digest binds both grades. source_bound_projection: the source itself
# binds the arms through a declared projection of the patch. named_candidate: bound by name only.
BINDINGS = frozenset({"exact_digest", "source_bound_projection", "named_candidate"})
# Repeat controls required before an entry may claim the stronger level.
REFERENCE_CONTROLS = [1, 1, 1]
NOP_CONTROLS = [0, 0, 0]

_MISSING = object()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_pointer(document: Any, pointer: str) -> Any:
    """RFC 6901 lookup; returns ``_MISSING`` when any step is absent."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        return _MISSING
    node = document
    for raw in pointer[1:].split("/"):
        key = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, Mapping):
            if key not in node:
                return _MISSING
            node = node[key]
        elif isinstance(node, Sequence) and not isinstance(node, str):
            if not key.isdigit() or int(key) >= len(node):
                return _MISSING
            node = node[int(key)]
        else:
            return _MISSING
    return node


def bare_task(task_id: str) -> str:
    return str(task_id or "").strip().split("@", 1)[0].rsplit("/", 1)[-1]


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_arm(
    name: str, arm: Mapping[str, Any], want: float, roots: Mapping[str, Path]
) -> tuple[list[str], list[str]]:
    """Return (problems, absent): absent lists cited files missing on this machine."""
    problems: list[str] = []
    source = arm.get("source")
    if not isinstance(source, Mapping) or source.get("root") not in roots:
        return [f"{name}: source root missing or unknown"], []
    relative = str(source.get("path") or "")
    if not relative or Path(relative).is_absolute() or ".." in Path(relative).parts:
        return [f"{name}: source path must be relative to its root"], []
    path = roots[source["root"]] / relative
    if not path.is_file():
        return [], [f"{name}: cited file is absent on this machine"]
    if arm.get("reward") != want or not _is_number(arm.get("reward")):
        problems.append(f"{name}: recorded reward must be {want}")
    if arm.get("executed") is not True:
        problems.append(f"{name}: not recorded as an executed grade")
    if str(arm.get("backend") or "") in REJECTED_BACKENDS:
        problems.append(f"{name}: host-side grade is not a container grade")
    if not arm.get("backend"):
        problems.append(f"{name}: backend must be stated (use 'unstated')")
    recorded = str(arm.get("sha256") or "")
    if sha256_file(path) != recorded:
        problems.append(f"{name}: sha256 differs from the retained bytes")
        return problems, []
    try:
        document = json.loads(path.read_bytes())
    except ValueError:
        return [*problems, f"{name}: cited file is not JSON"], []
    value = resolve_pointer(document, str(arm.get("reward_pointer") or "/missing"))
    if value is _MISSING or not _is_number(value) or value != want:
        problems.append(f"{name}: reward pointer does not resolve to {want}")
    pointer = arm.get("exception_pointer")
    if pointer is not None:
        found = resolve_pointer(document, str(pointer))
        if found is not _MISSING and found is not None:
            problems.append(f"{name}: arm recorded an infrastructure exception")
    return problems, []


def _verify_entry(
    entry: Mapping[str, Any], roots: Mapping[str, Path], assignments_csv: Path
) -> dict[str, Any]:
    task_id = str(entry.get("task_id") or "")
    problems: list[str] = []
    absent: list[str] = []
    for name, want in (("original", 1.0), ("repaired", 0.0)):
        arm = entry.get(name)
        if not isinstance(arm, Mapping):
            problems.append(f"{name}: arm missing")
            continue
        found, missing = _check_arm(name, arm, want, roots)
        problems.extend(found)
        absent.extend(missing)
    level = entry.get("level")
    if level not in LEVELS:
        problems.append("level is not a recognised evidence level")
    controls = entry.get("controls") or {}
    if level == "native_differential_with_controls" and (
        controls.get("reference") != REFERENCE_CONTROLS or controls.get("nop") != NOP_CONTROLS
    ):
        problems.append("with-controls level needs reference [1,1,1] and nop [0,0,0]")
    binding = (entry.get("candidate_identity") or {}).get("binding")
    if binding not in BINDINGS:
        problems.append("candidate binding is not a recognised kind")
    non_dev = find_cross_key_non_dev_assignments(task_id, assignments_csv=assignments_csv)
    dev = find_cross_key_dev_assignments(task_id, assignments_csv=assignments_csv)
    if non_dev:
        problems.append(f"non-dev assignment under another key: {[list(row) for row in non_dev]}")
    if not dev:
        problems.append("no affirmative dev/train assignment")
    if problems:
        status = "rejected"
    elif absent:
        status = "unverifiable"
    else:
        status = "verified"
    return {
        "task_id": task_id,
        "status": status,
        "problems": problems + absent,
        "level": level,
        "binding": binding,
        "backend": [
            str((entry.get(name) or {}).get("backend") or "") for name in ("original", "repaired")
        ],
        "dev_rows": [list(row) for row in dev],
    }


def _database_tier1(database: Path) -> set[str]:
    uri = f"file:{database}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            "SELECT DISTINCT task_id FROM task_issues WHERE trust_tier = 'tier1_container_verified' "
            "AND status = 'confirmed' AND direction = 'wrong_pass' "
            "AND original_reward = 1.0 AND repaired_reward = 0.0"
        ).fetchall()
    return {bare_task(row[0]) for row in rows}


def verify_ledger(
    ledger: Mapping[str, Any],
    *,
    assignments_csv: Path,
    roots: Mapping[str, Path],
    database: Path | None = None,
) -> dict[str, Any]:
    if ledger.get("schema") != LEDGER_SCHEMA:
        raise ValueError(f"ledger schema must be {LEDGER_SCHEMA}")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in ledger.get("entries", []):
        result = _verify_entry(entry, roots, assignments_csv)
        key = bare_task(result["task_id"])
        if key in seen:
            result["problems"].append("task appears more than once")
            result["status"] = "rejected"
        seen.add(key)
        results.append(result)
    verified = [row for row in results if row["status"] == "verified"]
    counts: dict[str, Any] = {
        "entries": len(results),
        "verified": len(verified),
        "rejected": sum(row["status"] == "rejected" for row in results),
        "unverifiable": sum(row["status"] == "unverifiable" for row in results),
        "by_level": dict(Counter(row["level"] for row in verified)),
        "by_binding": dict(Counter(row["binding"] for row in verified)),
        "by_backend": dict(Counter("/".join(row["backend"]) for row in verified)),
    }
    if database is not None:
        baseline = _database_tier1(database)
        counts["database_tier1_tasks"] = len(baseline)
        counts["verified_in_database_tier1"] = sum(
            bare_task(row["task_id"]) in baseline for row in verified
        )
        counts["verified_beyond_database_tier1"] = sum(
            bare_task(row["task_id"]) not in baseline for row in verified
        )
    return {
        "schema": REPORT_SCHEMA,
        "assignments_sha256": sha256_file(assignments_csv),
        "counts": counts,
        "entries": results,
    }


def _parse_roots(values: Sequence[str]) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for value in values:
        name, separator, location = value.partition("=")
        if not separator or not name or not location:
            raise ValueError(f"--root must look like NAME=PATH, got {value!r}")
        roots[name] = Path(location).expanduser().resolve()
    return roots


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ledger", type=Path)
    parser.add_argument("--assignments", type=Path, required=True)
    parser.add_argument(
        "--root", action="append", default=[], help="override a ledger root: NAME=PATH"
    )
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    ledger = json.loads(args.ledger.read_text(encoding="utf-8"))
    roots = {name: Path(path).expanduser() for name, path in ledger.get("roots", {}).items()}
    roots.update(_parse_roots(args.root))
    report = verify_ledger(
        ledger, assignments_csv=args.assignments, roots=roots, database=args.database
    )
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(text, encoding="utf-8")
    json.dump(report["counts"], sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    counts = report["counts"]
    return 1 if counts["rejected"] else 2 if counts["unverifiable"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
