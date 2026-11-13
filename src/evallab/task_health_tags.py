"""Harbor metadata projections of the Python task ledger (HAR-172).

These pure helpers read historical evidence labels, not new task grades.
Copied and infrastructure outcomes remain unknown, not solve failures.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

LEDGER = Path("research/experiments/python-task-ledger/ledger.csv")
LOCKED_NOP = Path("research/experiments/har122-egress-lock/har146-locked-nop.csv")
HISTORY = Path("research/experiments/python-task-ledger/task_history.csv")
SCHEMA = "evallab.task_health_tags/v1"
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
