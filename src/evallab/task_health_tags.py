"""Harbor metadata projections of the Python task ledger (HAR-172).

These pure helpers read historical evidence labels, not new task grades.
Copied and infrastructure outcomes remain unknown, not solve failures.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import tomlkit

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


def metadata_bytes(
    original: bytes, tags: Sequence[str], *, description: str | None = None
) -> bytes:
    """Replace only our tag namespaces while preserving other TOML fields.

    ``description`` sets ``[metadata] description`` (the one-line ledger
    reason ``harbor view --tasks`` shows for an actionable task); ``None``
    preserves whatever the parent package carries.
    """
    before = tomllib.loads(original.decode("utf-8"))
    document = tomlkit.parse(original.decode("utf-8"))
    if "metadata" not in document:
        document["metadata"] = tomlkit.table()
    metadata = document["metadata"]
    if not isinstance(metadata, Mapping):
        raise ValueError("task.toml metadata must be a table")
    existing = metadata.get("tags", [])
    if not isinstance(existing, list) or any(not isinstance(tag, str) for tag in existing):
        raise ValueError("task.toml metadata.tags must be a string array")
    for stale in ("health:", "solve:", "verdict:"):
        existing = [tag for tag in existing if not tag.startswith(stale)]
    combined = list(dict.fromkeys([*existing, *tags]))
    document["metadata"]["tags"] = combined
    if description is not None:
        if not description or not description.strip():
            raise ValueError("description must be a non-empty one-line reason")
        document["metadata"]["description"] = " ".join(description.split())
    output = tomlkit.dumps(document).encode("utf-8")
    after = tomllib.loads(output.decode("utf-8"))
    expected_metadata = {**before.get("metadata", {}), "tags": combined}
    if description is not None:
        expected_metadata["description"] = " ".join(description.split())
    expected = dict(before)
    expected["metadata"] = expected_metadata
    if after != expected:
        raise ValueError("health tags changed non-tag task configuration")
    return output




