from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from evallab.failure_library.bank import BankContext, IssueItem
from evallab.failure_library.bank.sources import library_findings
from evallab.failure_library.records import Evidence, Finding, TaskRef, make_finding_id
from evallab.failure_library.store import LibraryStore

REVISION = "0123456789abcdef0123456789abcdef01234567"
DIGEST = "sha256:" + "ab" * 32

EVIDENCE: dict[str, Evidence] = {
    "executed": Evidence(kind="executed", receipt_ref="receipt-1"),
    "upstream_fix": Evidence(kind="upstream_fix", fix_revision="abc123"),
    "human_label": Evidence(kind="human_label", label_source="annotators"),
    "construction": Evidence(kind="construction", note="injected by construction"),
    "judge_label": Evidence(kind="judge_label", label_source="judge"),
    "static_finding": Evidence(kind="static_finding", note="read the grader"),
}
TIER = {
    "executed": "V3_executed",
    "upstream_fix": "V2_upstream_fixed",
    "human_label": "V1_external_label",
    "construction": "V1_external_label",
    "judge_label": "V0_unverified",
    "static_finding": "V0_unverified",
}


def _finding(
    task_id: str = "task-a",
    *,
    collection: str = "coll-a",
    kinds: tuple[str, ...] = ("judge_label",),
    locator: str = "loc-1",
    direction: str = "wrong_pass",
    defect_class: str = "TS04",
    split: str = "DEV",
    **overrides: Any,
) -> Finding:
    tiers = [TIER[kind] for kind in kinds]
    order = ["V3_executed", "V2_upstream_fixed", "V1_external_label", "V0_unverified"]
    payload: dict[str, Any] = {
        "finding_id": make_finding_id("src-a", task_id, REVISION, defect_class, locator),
        "collection": collection,
        "locator": locator,
        "task": TaskRef(
            source_id="src-a",
            task_id=task_id,
            upstream_repo="https://example.test/repo",
            upstream_revision=REVISION,
            task_version_digest=DIGEST,
        ),
        "defect_class": defect_class,
        "direction": direction,
        "summary": "The tests accept a wrong answer.",
        "evidence": [EVIDENCE[kind] for kind in kinds],
        "tier": min(tiers, key=order.index),
        "finder": "someone",
        "source_url": "https://example.test/finding",
        "observed_at": date(2026, 10, 28),
        "split": split,
        "license": "mit",
    }
    payload.update(overrides)
    return Finding.model_validate(payload)


@dataclass
class StubContext(BankContext):
    """Split authority replaced by a dict: sealed task ids are TEST, nothing else tightens."""

    sealed: frozenset[str] = frozenset()
    calls: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.calls = []

    def strict_split(  # type: ignore[override]
        self,
        declared: str,
        *,
        key_id: str | None = None,
        item_id: str | None = None,
        task_id: str | None = None,
        task_digest: str | None = None,
    ) -> Any:
        self.calls.append({"declared": declared, "task_id": task_id, "task_digest": task_digest})
        if declared != "DEV":
            return declared
        return "TEST" if task_id in self.sealed else "DEV"


def _ctx(tmp_path: Path, findings: dict[str, list[Finding]], **kwargs: Any) -> StubContext:
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    for collection, rows in findings.items():
        store.write_findings(collection, rows)
    return StubContext(repo_root=tmp_path, state_root=tmp_path / "state", **kwargs)


def _build(ctx: BankContext) -> list[IssueItem]:
    return list(library_findings.build(ctx))


def test_one_item_per_finding_across_collections_with_provenance(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path,
        {
            "coll-a": [_finding("t1"), _finding("t2", locator="loc-2")],
            "coll-b": [_finding("t3", collection="coll-b")],
        },
    )
    items = _build(ctx)
    assert len(items) == 3
    assert len({item.item_id for item in items}) == 3
    store = LibraryStore.for_repo(tmp_path, state_root=tmp_path / "state")
    by_task = {item.task_id: item for item in items}
    first = by_task["t1"]
    assert first.source == "library-findings"
    assert first.subject_type == "task"
    assert first.subject_id == f"src-a/t1@{REVISION}"
    assert first.locator.startswith("fl-")
    assert first.provenance.store == "failure-library:findings/coll-a"
    assert first.provenance.ref == first.locator
    expected = hashlib.sha256(store.findings_path("coll-a").read_bytes()).hexdigest()
    assert first.provenance.digest == expected
    assert by_task["t3"].provenance.store == "failure-library:findings/coll-b"
    assert (first.actor_transcript, first.harness_selector, first.granularity) == (
        None,
        None,
        "task",
    )
    assert first.license == "mit"


@pytest.mark.parametrize(
    ("direction", "kind"),
    [
        ("wrong_pass", "grader_wrong_pass"),
        ("unfair_fail", "grader_unfair_fail"),
        ("other", "task_defect"),
    ],
)
def test_direction_maps_to_kind(tmp_path: Path, direction: str, kind: str) -> None:
    ctx = _ctx(tmp_path, {"coll-a": [_finding(direction=direction)]})
    [item] = _build(ctx)
    assert (item.kind, item.polarity) == (kind, "positive")


@pytest.mark.parametrize(
    ("kinds", "label_type"),
    [
        (("judge_label", "executed"), "executed"),
        (("static_finding", "upstream_fix"), "upstream_fix"),
        (("construction", "human_label"), "human"),
        (("construction", "judge_label"), "construction"),
        (("static_finding", "judge_label"), "judge"),
        (("static_finding",), "static"),
    ],
)
def test_label_type_is_the_strongest_evidence(
    tmp_path: Path, kinds: tuple[str, ...], label_type: str
) -> None:
    ctx = _ctx(tmp_path, {"coll-a": [_finding(kinds=kinds)]})
    [item] = _build(ctx)
    assert item.label_type == label_type
    assert item.label == f"TS04 {_finding(kinds=kinds).tier}"


def test_source_refuted_claim_is_a_negative_item(tmp_path: Path) -> None:
    refuted = _finding(
        "t-ref",
        summary="Source-native historical issue outcome: refuted; not independently confirmed.",
    )
    confirmed = _finding(
        "t-conf",
        summary=(
            "Source-native historical issue outcome: confirmed; "
            "not independently confirmed or refuted by this importer."
        ),
    )
    ctx = _ctx(tmp_path, {"coll-a": [refuted, confirmed]})
    polarity = {item.task_id: item.polarity for item in _build(ctx)}
    assert polarity == {"t-ref": "negative", "t-conf": "positive"}


def test_fixed_upstream_finding_stays_positive(tmp_path: Path) -> None:
    fixed = _finding(kinds=("upstream_fix",), status_now="fixed_upstream", fixed_in_revision="abc")
    ctx = _ctx(tmp_path, {"coll-a": [fixed]})
    [item] = _build(ctx)
    assert (item.polarity, item.label_type) == ("positive", "upstream_fix")


def test_split_is_tightened_never_upgraded_and_declared_split_kept(tmp_path: Path) -> None:
    rows = [
        _finding("sealed", split="DEV"),
        _finding("open", split="DEV", locator="loc-2"),
        _finding("held", split="heldout", locator="loc-3"),
        _finding("cross", split="cross_test", locator="loc-4"),
        _finding("unset", split="unassigned", locator="loc-5"),
    ]
    ctx = _ctx(tmp_path, {"coll-a": rows}, sealed=frozenset({"sealed"}))
    items = {item.task_id: item for item in _build(ctx)}
    assert items["sealed"].declared_split == "DEV"
    assert items["sealed"].split == "TEST"  # a TEST-sealed task is never emitted as DEV
    assert items["open"].split == "DEV"
    assert (items["held"].declared_split, items["held"].split) == ("heldout", "heldout")
    assert items["cross"].split == "cross_test"
    assert items["unset"].split == "unassigned"
    assert not [item for item in items.values() if item.task_id == "sealed" and item.split == "DEV"]
    call = next(c for c in ctx.calls if c["task_id"] == "open")
    assert call["task_digest"] == DIGEST


def test_locator_naming_a_test_is_test_granularity(tmp_path: Path) -> None:
    named = _finding("t1", locator="tests/test_x.py::test_foo")
    plain = _finding("t2", locator="tests-summary")
    ctx = _ctx(tmp_path, {"coll-a": [named, plain]})
    granularity = {item.task_id: item.granularity for item in _build(ctx)}
    assert granularity == {"t1": "test", "t2": "task"}


def test_same_task_multiple_findings_are_separate_items(tmp_path: Path) -> None:
    rows = [_finding("t1", locator="a"), _finding("t1", locator="b", defect_class="TS07")]
    ctx = _ctx(tmp_path, {"coll-a": rows})
    items = _build(ctx)
    assert len(items) == 2
    assert {item.subject_id for item in items} == {f"src-a/t1@{REVISION}"}
    assert len({item.item_id for item in items}) == 2
