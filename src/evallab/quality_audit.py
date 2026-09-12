"""HAR-25 harness-first cohort quality audit.

Read-only audit over a pinned cohort snapshot (for example
``research/analysis/harness-first-quality/cohort.json``). The audit keeps four
questions separate instead of collapsing them into one verdict:

- ``static_screening``: what ``inspect_candidate`` diagnostics say about the
  task package. Harness limitations (``unsupported`` codes) are never folded
  into ``invalid``.
- ``semantic_validity``: only ever ``oracle_nop_only`` when control evidence
  shows ``oracle.reward == 1`` with ``nop.reward == 0``, else ``unknown``.
  Oracle/nop controls test task and harness validity; they never certify the
  task's semantics, so no ``certified`` status exists.
- ``difficulty``: always ``unknown``; a cohort freeze carries no difficulty
  evidence.
- ``training_utility``: always ``unknown``; a declared ``allowed_uses`` entry
  and oracle/nop control rewards cannot establish training utility.

The audit never upgrades a cohort's declared ``allowed_uses`` into an audited
allowance: the declaration is reported verbatim as ``declared_allowed_uses``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

from evallab.task_workbench import (
    CandidateSource,
    Diagnostic,
    ProvenanceZone,
    inspect_candidate,
)

SCHEMA_VERSION = 1

StaticScreeningStatus = str  # "pass" | "unsupported" | "invalid" | "unknown"

# Error-severity diagnostic code markers that name a concrete task-side safety
# or integrity failure. A code carrying one of these markers is classified
# "invalid" even when it also contains "unsupported": a member that both fails
# a safety check and trips a harness limitation is invalid, not merely
# unsupported. Pure "unsupported" codes (harness cannot prove isolation for
# this shape) never map to "invalid".
_INVALID_CODE_MARKERS = ("safety", "invalid", "unauthorized", "secret", "path_escape")

_DIFFICULTY_UNKNOWN_REASON = (
    "a pinned cohort freeze carries no difficulty evidence, so difficulty is unknown"
)

_TRAINING_UTILITY_UNKNOWN_REASON = (
    "declared allowed_uses entries and oracle/nop control rewards cannot "
    "establish training utility, so training utility is unknown"
)

_SEMANTIC_UNKNOWN_REASON = (
    "control evidence does not show oracle reward 1 with nop reward 0"
)


def audit_cohort(repo_root: Path, cohort_path: Path) -> dict[str, Any]:
    """Audit a pinned cohort file and return the four-field member records.

    The audit is read-only: it reads the cohort bytes (digesting them), runs
    static ``inspect_candidate`` screening against each member's task path, and
    classifies the declared control evidence. It executes no controls and
    writes nothing.
    """
    repo_root = repo_root.resolve()
    raw = cohort_path.read_bytes()
    cohort_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    try:
        cohort = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"cohort file {cohort_path} is not valid JSON: {exc}") from exc
    if not isinstance(cohort, Mapping):
        raise ValueError(f"cohort file {cohort_path} must contain a JSON object")
    cohort_id = cohort.get("cohort_id")
    if not isinstance(cohort_id, str) or not cohort_id.strip():
        raise ValueError(f"cohort file {cohort_path} must declare a non-empty cohort_id")
    raw_members = cohort.get("members")
    if not isinstance(raw_members, list):
        raise ValueError(f"cohort file {cohort_path} must declare a members list")

    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "harness_first_quality_audit",
        "cohort_id": cohort_id,
        "cohort_digest": cohort_digest,
        "members": [_audit_member(repo_root, member) for member in raw_members],
    }


def _audit_member(repo_root: Path, member: Any) -> dict[str, Any]:
    if not isinstance(member, Mapping):
        return {
            "task_id": "",
            "task_path": None,
            "source": None,
            "static_screening": _unknown_static("member record is not a JSON object"),
            "semantic_validity": _semantic_validity(member),
            "difficulty": {"status": "unknown", "reason": _DIFFICULTY_UNKNOWN_REASON},
            "training_utility": {
                "status": "unknown",
                "reason": _TRAINING_UTILITY_UNKNOWN_REASON,
            },
            "declared_allowed_uses": [],
        }

    source = _member_source(member)
    raw_path = member.get("task_path")
    return {
        "task_id": str(member.get("task_id") or ""),
        "task_path": raw_path if isinstance(raw_path, str) and raw_path.strip() else None,
        "source": source.to_dict(),
        "static_screening": _static_screening(repo_root, raw_path, source),
        "semantic_validity": _semantic_validity(member),
        "difficulty": {"status": "unknown", "reason": _DIFFICULTY_UNKNOWN_REASON},
        "training_utility": {"status": "unknown", "reason": _TRAINING_UTILITY_UNKNOWN_REASON},
        "declared_allowed_uses": _declared_allowed_uses(member),
    }


def _member_source(member: Mapping[str, Any]) -> CandidateSource:
    zone_raw = member.get("provenance_zone")
    zone = zone_raw if isinstance(zone_raw, str) and zone_raw.strip() else "03-synthetic"
    source_ref = member.get("source_ref")
    return CandidateSource(
        source_uri=str(member.get("source_uri") or ""),
        source_ref=source_ref if isinstance(source_ref, str) else "",
        license=str(member.get("license") or ""),
        provenance_zone=cast(ProvenanceZone, zone),
    )


def _static_screening(
    repo_root: Path, raw_path: Any, source: CandidateSource
) -> dict[str, Any]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return _unknown_static("member declares no task_path, so no static screening ran")
    try:
        inspection = inspect_candidate(
            repo_root=repo_root, task_path=Path(raw_path), source=source
        )
    except Exception as exc:  # inspect failure degrades to unknown, never crashes the audit
        return _unknown_static(
            f"inspect_candidate failed: {type(exc).__name__}: {exc}",
        )
    diagnostics: tuple[Diagnostic, ...] = inspection.diagnostics
    errors = [item for item in diagnostics if item.severity == "error"]
    record: dict[str, Any] = {
        "codes": sorted({item.code for item in errors}),
        "diagnostics": [item.to_dict() for item in diagnostics],
    }
    if not errors:
        return {"status": "pass", **record}
    classes = {_classify_error_code(item.code) for item in errors}
    # "invalid" wins only when a marker-class code is actually present; a
    # member whose errors are all harness limitations stays "unsupported".
    status = "invalid" if "invalid" in classes else "unsupported"
    return {"status": status, **record}


def _classify_error_code(code: str) -> str:
    if any(marker in code for marker in _INVALID_CODE_MARKERS):
        return "invalid"
    if "unsupported" in code:
        return "unsupported"
    # Remaining error diagnostics are task-side defects (missing metadata,
    # unpinned refs, non-executable scripts, ...): the candidate is invalid,
    # not merely unsupported by the harness.
    return "invalid"


def _unknown_static(reason: str) -> dict[str, Any]:
    return {"status": "unknown", "codes": [], "diagnostics": [], "reason": reason}


def _semantic_validity(member: Any) -> dict[str, Any]:
    evidence = member.get("control_evidence") if isinstance(member, Mapping) else None
    controls = evidence if isinstance(evidence, Mapping) else {}
    oracle_reward = _control_reward(controls.get("oracle"))
    nop_reward = _control_reward(controls.get("nop"))
    if oracle_reward == 1.0 and nop_reward == 0.0:
        return {
            "status": "oracle_nop_only",
            "certified": False,
            "oracle_reward": oracle_reward,
            "nop_reward": nop_reward,
        }
    return {"status": "unknown", "certified": False, "reason": _SEMANTIC_UNKNOWN_REASON}


def _control_reward(control: Any) -> float | None:
    if not isinstance(control, Mapping):
        return None
    value = control.get("reward")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _declared_allowed_uses(member: Mapping[str, Any]) -> list[str]:
    raw = member.get("allowed_uses")
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]
