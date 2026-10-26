"""Deterministic lints for training datasets.

The audit harness's *training-data* subject: leads are deterministic lints and
confirmation is scripts only, no model. Every check here is free, offline, and
reproducible, and each one names the measured issue it catches:

- ``dedup`` — a dataset's row count is not its size. The seeker corpus's 90
  conversations are 39 distinct ``(task, body)`` pairs, and 30 of the 90 are one
  repeated no-op body.
- ``split_leak`` — a task in a held-out or TEST split must never appear in
  training data. The seeker-gym registry's ``tb4-dev`` family holds 41
  ``tb4-audit-v1`` TEST tasks.
- ``coverage`` — what the dataset can teach: classes, positive/negative balance,
  and how much of the assistant content is execution rather than description.

Bodies are read from whichever shape a row uses: a ``messages`` list with
``write_file`` tool calls (the seeker SFT corpus), a ``files`` mapping (a gym
candidate), or a ``submission`` mapping (a replay record).
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

SCHEMA = "evallab.data_lint/v1"
CHECKS = ("dedup", "split_leak", "coverage")
DEFAULT_BODY_PATH = "submission/solve.sh"


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _body_from_messages(row: Mapping[str, Any], body_path: str) -> str | None:
    for message in row.get("messages") or []:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            if function.get("name") != "write_file":
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except ValueError:
                continue
            if str(args.get("path") or "").endswith(body_path):
                return str(args.get("content") or "")
    return None


def body_of(row: Mapping[str, Any], body_path: str = DEFAULT_BODY_PATH) -> str | None:
    """The candidate body a row carries, from any of the three known shapes."""
    for key in ("files", "submission"):
        mapping = row.get(key)
        if isinstance(mapping, Mapping):
            for name, content in mapping.items():
                if str(name).endswith(body_path):
                    return str(content)
    return _body_from_messages(row, body_path)


def task_of(row: Mapping[str, Any]) -> str | None:
    for key in ("task_id", "task", "pair_id"):
        value = row.get(key)
        if isinstance(value, str) and value:
            return value.rsplit("/", 1)[-1]
    return None


def _digest(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()[:12]


def _finding(check: str, severity: str, subject: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {
        "check": check,
        "severity": severity,
        "subject": subject,
        "detail": detail,
        **extra,
    }


def lint_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    body_path: str = DEFAULT_BODY_PATH,
    checks: Iterable[str] = CHECKS,
) -> list[dict[str, Any]]:
    """Run the dataset-local checks (everything except split leakage)."""
    findings: list[dict[str, Any]] = []
    wanted = set(checks)
    bodies = [(task_of(row), body_of(row, body_path), row) for row in rows]

    if "dedup" in wanted:
        pairs = {(task, _digest(body)) for task, body, _ in bodies if body is not None}
        digests = Counter(_digest(body) for _, body, _ in bodies if body is not None)
        duplicated = {digest: count for digest, count in digests.items() if count > 1}
        if len(pairs) < len(rows):
            findings.append(
                _finding(
                    "dedup",
                    "high",
                    "dataset",
                    f"{len(rows)} rows carry {len(pairs)} distinct (task, body) pairs",
                    rows=len(rows),
                    distinct_task_body_pairs=len(pairs),
                    distinct_bodies=len(digests),
                    duplicated_bodies=len(duplicated),
                    extra_copies=sum(count - 1 for count in duplicated.values()),
                )
            )
        for digest, count in sorted(duplicated.items(), key=lambda item: -item[1])[:5]:
            tasks = sorted(
                {
                    task
                    for task, body, _ in bodies
                    if body is not None and task is not None and _digest(body) == digest
                }
            )
            findings.append(
                _finding(
                    "dedup",
                    "medium" if count > 2 else "low",
                    f"body:{digest}",
                    f"one body appears {count} times across {len(tasks)} tasks",
                    copies=count,
                    tasks=tasks[:8],
                )
            )

    if "coverage" in wanted:
        classes = Counter(
            str(row.get("mechanism_task_class") or row.get("class") or "unknown") for row in rows
        )
        positives = sum(1 for row in rows if row.get("exploit_found") is True)
        negatives = sum(1 for row in rows if row.get("exploit_found") is False)
        findings.append(
            _finding(
                "coverage",
                "info",
                "dataset",
                f"{len(rows)} rows, {positives} positive, {negatives} negative",
                classes=dict(classes),
                positives=positives,
                negatives=negatives,
            )
        )
    return findings


def lint_split_leak(
    rows: Sequence[Mapping[str, Any]],
    answer_keys: Path,
    *,
    held_out_splits: Sequence[str] = ("test",),
) -> list[dict[str, Any]]:
    """Tasks in the dataset that belong to a held-out split."""
    keys = _load_jsonl(answer_keys)
    held = {
        str(row["task"]).rsplit("/", 1)[-1]
        for row in keys
        if row.get("split") in set(held_out_splits)
    }
    tasks = {task for task in (task_of(row) for row in rows) if task}
    leaked = sorted(tasks & held)
    if not leaked:
        return []
    return [
        _finding(
            "split_leak",
            "high",
            "dataset",
            f"{len(leaked)} of {len(tasks)} tasks are in a held-out split",
            held_out_splits=list(held_out_splits),
            leaked_tasks=leaked,
            answer_keys=answer_keys.as_posix(),
        )
    ]


def lint_registry_split_leak(
    registry: Path,
    answer_keys: Path,
    *,
    dev_families: Sequence[str] = ("tb4-dev",),
    held_out_splits: Sequence[str] = ("test",),
) -> list[dict[str, Any]]:
    """A registry family that claims DEV but holds held-out tasks."""
    keys = _load_jsonl(answer_keys)
    held = {
        str(row["task"]).rsplit("/", 1)[-1]
        for row in keys
        if row.get("split") in set(held_out_splits)
    }
    dev = {str(row["task"]).rsplit("/", 1)[-1] for row in keys if row.get("split") == "dev"}
    registry_rows = json.loads(registry.read_text(encoding="utf-8"))
    pairs = registry_rows.get("pairs") or []
    findings: list[dict[str, Any]] = []
    for family in dev_families:
        members = [pair for pair in pairs if pair.get("family") == family]
        leaked = sorted({str(pair.get("task")) for pair in members if pair.get("task") in held})
        if not leaked:
            continue
        findings.append(
            _finding(
                "split_leak",
                "high",
                f"registry:{family}",
                f"{len(leaked)} of {len(members)} '{family}' pairs are held-out tasks",
                held_out_splits=list(held_out_splits),
                leaked_tasks=leaked,
                dev_tasks=sorted(
                    {str(pair.get("task")) for pair in members if pair.get("task") in dev}
                ),
                registry=registry.as_posix(),
                answer_keys=answer_keys.as_posix(),
            )
        )
    return findings


def lint_dataset(
    dataset: Path,
    *,
    answer_keys: Path | None = None,
    registry: Path | None = None,
    body_path: str = DEFAULT_BODY_PATH,
    checks: Iterable[str] = CHECKS,
) -> dict[str, Any]:
    rows = _load_jsonl(dataset)
    findings = lint_rows(rows, body_path=body_path, checks=checks)
    if answer_keys is not None and "split_leak" in set(checks):
        findings += lint_split_leak(rows, answer_keys)
    if registry is not None and answer_keys is not None and "split_leak" in set(checks):
        findings += lint_registry_split_leak(registry, answer_keys)
    return {
        "schema": SCHEMA,
        "dataset": dataset.as_posix(),
        "rows": len(rows),
        "checks": sorted(set(checks)),
        "findings": findings,
        "status": "issues"
        if any(f["severity"] in {"high", "medium"} for f in findings)
        else "clean",
    }
