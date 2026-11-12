"""Harbor metadata projections of the Python task ledger (HAR-172).

These pure helpers read historical evidence labels, not new task grades.
Copied and infrastructure outcomes remain unknown, not solve failures.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

LEDGER = Path("research/experiments/python-task-ledger/ledger.csv")
LOCKED_NOP = Path("research/experiments/har122-egress-lock/har146-locked-nop.csv")
HISTORY = Path("research/experiments/python-task-ledger/task_history.csv")
SCHEMA = "evallab.task_health_tags/v1"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_TASK_ID = re.compile(r"format-code-task-[0-9]{6}\Z")
_OUTCOMES = ("clean_pass", "copied_pass", "fail", "infra")
_PROBE_VERDICTS = {"cracked", "leak-found-not-cracked", "clean", "unscored"}
KNOWN_TAGS = frozenset(
    {
        "health:sound",
        "health:cracked",
        "health:leak-found",
        "health:repaired",
        "health:broken-environment",
        "health:grader-suspect",
        "health:review",
        "health:discarded",
        "health:unchecked",
        "solve:unknown",
        "solve:never-run",
        "solve:unscored",
        "solve:0-of-n",
        "solve:always",
        "solve:mixed",
        "verdict:keep",
        "verdict:fix",
        "verdict:discard",
        "verdict:unknown",
    }
)


_VERDICTS = ("keep", "fix", "discard")


def verdict_tag(ledger: Mapping[str, str]) -> str:
    """HAR-177 keep/fix/discard projection of the ledger verdict column."""
    verdict = ledger.get("verdict")
    if verdict not in _VERDICTS:
        raise ValueError(f"ledger: unsupported verdict {verdict!r}")
    return f"verdict:{verdict}"


def _path(root: Path, value: Path) -> Path:
    return value if value.is_absolute() else root / value


def _csv_rows(raw: bytes, *, required: set[str], label: str) -> dict[str, dict[str, str]]:
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    if not required <= set(reader.fieldnames or []):
        raise ValueError(
            f"{label}: missing columns {sorted(required - set(reader.fieldnames or []))}"
        )
    rows = {}
    for row in reader:
        task_id = row.get("task_id", "")
        if not _TASK_ID.fullmatch(task_id) or task_id in rows:
            raise ValueError(f"{label}: invalid or duplicate task_id {task_id!r}")
        if any(row.get(key) is None for key in required):
            raise ValueError(f"{label}: incomplete row for {task_id}")
        rows[task_id] = row
    return rows


def solve_summary(history: Mapping[str, str] | None) -> dict[str, Any]:
    """Classify the #703 history's clean-pass/fail denominator, not raw passes.

    History is task-level, across its recorded packages/models/harnesses. It is
    not a claim of a current-package pass rate or a new counts adjudication.
    """
    if history is None:
        return {"tag": "solve:unknown", "reason": "no history row"}
    counts = {}
    for key in ("runs", *_OUTCOMES):
        value = history.get(key, "")
        if not isinstance(value, str) or not value.isdecimal():
            raise ValueError(f"history: {key} must be a nonnegative integer")
        counts[key] = int(value)
    if counts["runs"] != sum(counts[key] for key in _OUTCOMES):
        raise ValueError("history: runs does not equal the outcome counts")
    known = counts["clean_pass"] + counts["fail"]
    if counts["runs"] == 0:
        tag = "solve:never-run"
    elif known == 0:
        tag = "solve:unscored"
    elif counts["clean_pass"] == 0:
        tag = "solve:0-of-n"
    elif counts["fail"] == 0:
        tag = "solve:always"
    else:
        tag = "solve:mixed"
    return {
        "tag": tag,
        **counts,
        "known_attempts": known,
        "last_run": history.get("last_run", ""),
        "scope": "task history; copied and infra excluded; not current-package capability",
    }


def health_tag(
    ledger: Mapping[str, str],
    locked_nop: Mapping[str, str] | None,
    *,
    exploit_verdict: str | None = None,
) -> str:
    """Adverse bound evidence wins; a validated repair supersedes an old nop.

    ``sound`` means ledger-usable with a sound locked nop, not proof against
    every exploit. Absence of a probe is never translated into a clean verdict.
    """
    status = ledger["status"]
    if status not in {"usable", "review", "discarded", "unchecked"}:
        raise ValueError(f"ledger: unsupported status {status!r}")
    if exploit_verdict not in _PROBE_VERDICTS | {None}:
        raise ValueError(f"exploit: unsupported verdict {exploit_verdict!r}")
    if exploit_verdict == "cracked":
        return "health:cracked"
    # RUN_DEFECTS emits this diagnosis for an image-bound leak. Do not confuse
    # a closed PyPI channel or a merely available package with an observed leak.
    image_leak = ledger.get("reason", "").casefold().startswith("image leaks the fix:")
    open_leak = status == "review" and "with no leak-closed variant" in ledger.get("reason", "")
    if exploit_verdict == "leak-found-not-cracked" or image_leak or open_leak:
        return "health:leak-found"
    if status != "usable":
        return f"health:{status}"
    if ledger.get("run") == "repair" and ledger.get("run_variant_status") == "validated":
        # A HAR-146 row explicitly naming this repair is current adverse
        # evidence; it must not be hidden by the earlier validation status.
        if locked_nop and locked_nop.get("note") == f"repaired:{ledger['run_digest'][7:19]}":
            label = locked_nop.get("locked_nop", "")
            if label in {"broken_environment", "grader_suspect"}:
                return f"health:{label.replace('_', '-')}"
        return "health:repaired"
    if locked_nop is None:
        return "health:unchecked"
    repaired = locked_nop.get("note", "")
    if repaired.startswith("repaired:") and not ledger["run_digest"].removeprefix(
        "sha256:"
    ).startswith(repaired.removeprefix("repaired:")):
        return "health:unchecked"
    label = locked_nop.get("locked_nop", "")
    if label == "sound":
        return "health:sound"
    if label in {"broken_environment", "grader_suspect"}:
        return f"health:{label.replace('_', '-')}"
    return "health:unchecked"


def _probe_binding(row: Mapping[str, Any], *, input_dir: Path) -> dict[str, str | None]:
    """Bind native HAR-161 output via Lab provenance or its native task lock."""
    digest = row.get("task_package_digest")
    if digest is not None:
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError("exploit: invalid task_package_digest")
        return {"package": digest, "harbor": None}
    trial_value = row.get("trial")
    if not isinstance(trial_value, str) or not trial_value:
        return {"package": None, "harbor": None}
    trial = _path(input_dir, Path(trial_value))
    metadata_path = trial.parent / "lab-metadata.json"
    if metadata_path.is_file():
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        digest = (payload.get("task_staging") or {}).get("source_package_digest")
        if isinstance(digest, str) and _DIGEST.fullmatch(digest):
            return {"package": digest, "harbor": None}
    # Native jobs can lack the Lab sidecar that the published copy retains.
    # A task ref matched to this trial in the immutable lock still identifies
    # the executed Harbor package. Do not guess from a job-name suffix.
    config_path = trial / "config.json"
    if not config_path.is_file():
        return {"package": None, "harbor": None}
    config = json.loads(config_path.read_text(encoding="utf-8"))
    task_path = (config.get("task") or {}).get("path")
    if not isinstance(task_path, str):
        return {"package": None, "harbor": None}
    for lock_path in (trial / "lock.json", trial.parent / "lock.json"):
        if not lock_path.is_file():
            continue
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        refs = [lock] if "task" in lock else lock.get("trials", [])
        digests = {
            ref["task"]["digest"]
            for ref in refs
            if isinstance(ref, dict)
            and isinstance(ref.get("task"), dict)
            and ref["task"].get("path") == task_path
            and isinstance(ref["task"].get("digest"), str)
            and _DIGEST.fullmatch(ref["task"]["digest"])
        }
        if len(digests) == 1:
            return {"package": None, "harbor": next(iter(digests))}
    return {"package": None, "harbor": None}


