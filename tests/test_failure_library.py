from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from evallab.failure_library.records import (
    Evidence,
    Finding,
    TaskRef,
    make_finding_id,
)

REVISION = "0123456789abcdef0123456789abcdef01234567"


def _task(task_id: str = "fix-git", revision: str | None = REVISION) -> TaskRef:
    return TaskRef(
        source_id="terminal-bench",
        task_id=task_id,
        upstream_repo="https://github.com/laude-institute/terminal-bench",
        upstream_revision=revision,
    )


def _finding(
    *,
    task_id: str = "fix-git",
    evidence: list[Evidence] | None = None,
    tier: str = "V2_upstream_fixed",
    locator: str = "commit-abc",
    collection: str = "tb-fixes",
    **overrides: object,
) -> Finding:
    evidence = evidence or [Evidence(kind="upstream_fix", fix_revision="abc123")]
    payload: dict[str, object] = {
        "finding_id": make_finding_id("terminal-bench", task_id, REVISION, "TS01", locator),
        "collection": collection,
        "locator": locator,
        "task": _task(task_id),
        "defect_class": "TS01",
        "direction": "wrong_pass",
        "summary": "Tests only check that the output file exists.",
        "evidence": evidence,
        "tier": tier,
        "finder": "upstream maintainers",
        "source_url": "https://github.com/laude-institute/terminal-bench/commit/abc123",
        "observed_at": date(2026, 10, 27),
    }
    payload.update(overrides)
    return Finding.model_validate(payload)


def test_tier_is_the_strongest_evidence_and_cannot_be_overstated() -> None:
    mixed = [
        Evidence(kind="judge_label", label_source="goodfire-3-vote"),
        Evidence(kind="upstream_fix", fix_revision="abc123"),
    ]
    assert _finding(evidence=mixed, tier="V2_upstream_fixed").tier == "V2_upstream_fixed"
    with pytest.raises(ValidationError, match="disagrees with evidence"):
        _finding(evidence=[Evidence(kind="judge_label", label_source="judge")], tier="V3_executed")


def test_finding_requires_a_pinned_revision_and_a_fix_pin_when_fixed() -> None:
    with pytest.raises(ValidationError, match="upstream_revision"):
        Finding.model_validate(
            {**_finding().model_dump(), "task": _task(revision=None).model_dump()}
        )
    with pytest.raises(ValidationError, match="fixed_in_revision"):
        _finding(status_now="fixed_upstream")
    fixed = _finding(status_now="fixed_upstream", fixed_in_revision="abc123")
    assert fixed.fixed_in_revision == "abc123"


def test_finding_id_is_deterministic_and_checked() -> None:
    with pytest.raises(ValidationError, match="deterministic id"):
        _finding(finding_id="fl-0000000000000000")
    assert make_finding_id("a", "b", "c", "TS01", "x") != make_finding_id(
        "a", "b", "d", "TS01", "x"
    )


def test_evidence_kinds_require_their_proof() -> None:
    with pytest.raises(ValidationError, match="receipt_ref or reproducer_ref"):
        Evidence(kind="executed")
    with pytest.raises(ValidationError, match="fix_revision"):
        Evidence(kind="upstream_fix")
    with pytest.raises(ValidationError, match="label_source"):
        Evidence(kind="human_label")


