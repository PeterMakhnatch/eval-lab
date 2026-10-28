"""The cross-test issue-bank source: polarity policy, metadata, HackBench summary-only, split safety."""

from __future__ import annotations

import json
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from evallab.failure_library.bank import BankContext, IssueItem, SourceSummary
from evallab.failure_library.bank.sources import cross_test as source
from evallab.failure_library.sources import normalize_split

DIGEST = "ab" * 32


@dataclass
class _Ctx(BankContext):
    """A dict-backed split authority: a DEV declaration on a sealed task becomes TEST."""

    sealed: frozenset[str] = frozenset()
    calls: list[tuple[str, str | None, str | None]] = field(default_factory=list)

    def strict_split(  # type: ignore[override]
        self,
        declared: str,
        *,
        key_id: str | None = None,
        item_id: str | None = None,
        task_id: str | None = None,
        task_digest: str | None = None,
    ) -> Any:
        self.calls.append((declared, item_id, task_id))
        split = normalize_split(declared)
        if split != "DEV":
            return split
        return "TEST" if task_id in self.sealed else "DEV"


def _entry(tmp_path: Path, slug: str, basis: str, licence: str = "mit", **extra: Any) -> dict[str, Any]:
    bundle = tmp_path / "bundles" / slug
    bundle.mkdir(parents=True, exist_ok=True)
    return {
        "id": slug,
        "revision": "0123456789abcdef" * 2 + "01234567",
        "bundle_path": str(bundle),
        "manifest": {"sha256": DIGEST},
        "licence": {"text": licence},
        "labels": {"counts": {}, "false_alarm_basis": {"negative_basis": basis}},
        **extra,
    }


def _context(tmp_path: Path, entries: list[dict[str, Any]], **registry: Any) -> _Ctx:
    path = tmp_path / "registry.json"
    path.write_text(json.dumps({"schema_version": "cross-test-registry/v1", "entries": entries, **registry}))
    return _Ctx(repo_root=tmp_path / "repo", state_root=tmp_path / "state", cross_test_registry=path)


def _adapter(tmp_path: Path, relative: str, body: str) -> None:
    path = tmp_path / "repo" / relative / "tools" / "adapter.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body))


def _items(ctx: BankContext) -> list[IssueItem]:
    out = list(source.build(ctx))
    assert all(isinstance(item, IssueItem) for item in out)
    return out  # type: ignore[return-value]


def _agent(text: str | None) -> dict[str, Any]:
    return {"step_id": 0, "source": "agent", "message": text} if text else {"step_id": 0, "source": "system"}


def test_harness_set_maps_polarity_and_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        ("staged:agent-cheat-index:aaaa", {"_steps": [_agent("ran tests")]}, True),
        ("staged:agent-cheat-index:bbbb", {"_steps": [_agent(None)]}, False),
        ("staged:agent-cheat-index:cccc", {"_steps": [_agent("x")]}, None),  # ambiguous: not an item
    ]
    seen: list[tuple[str, Path, Path]] = []

    def fake_rows(slug: str, bundle: Path, repo_root: Path) -> Iterator[tuple[str, dict[str, Any], bool | None]]:
        seen.append((slug, bundle, repo_root))
        return iter(rows)

    monkeypatch.setattr(source, "_staged_rows", fake_rows)
    ctx = _context(tmp_path, [_entry(tmp_path, "agent-cheat-index", "mixed", "apache-2.0")])

    positive, negative = _items(ctx)

    assert seen == [("agent-cheat-index", tmp_path / "bundles" / "agent-cheat-index", tmp_path / "repo")]
    assert (positive.polarity, negative.polarity) == ("positive", "negative")
    assert (positive.subject_id, negative.subject_id) == ("agent-cheat-index:aaaa", "agent-cheat-index:bbbb")
    assert positive.item_id != negative.item_id
    for item in (positive, negative):
        assert (item.source, item.kind, item.granularity, item.subject_type) == ("cross-test", "cheat", "run", "run")
        assert (item.declared_split, item.split) == ("cross_test", "cross_test")
        assert item.license == "apache-2.0"
        assert item.harness_selector == "staged:agent-cheat-index"
        assert item.provenance.store == "cross-test:agent-cheat-index"
        assert item.provenance.digest == DIGEST
    assert (positive.provenance.ref, negative.provenance.ref) == ("aaaa", "bbbb")
    # actor transcript comes from the run's own steps, never assumed
    assert (positive.actor_transcript, negative.actor_transcript) == (True, False)


def test_task_qa_unchanged_tasks_are_not_negatives(tmp_path: Path) -> None:
    _adapter(
        tmp_path,
        source._LB11 + "/parsewave__automationbench-verified",
        """
        def specs(bundle):
            for name, state, group in (("t1", "defective_verified", "Attribute mismatch"),
                                       ("t2", "unchanged_not_certified", None),
                                       ("t3", "defective_verified", "Text-matching gaps")):
                yield {"labels": {"label_state": state, "issue_group": group}, "identity": {"task_name": name}}
        """,
    )
    ctx = _context(tmp_path, [_entry(tmp_path, "automationbench-verified", "none", "cc-by-4.0")])

    items = _items(ctx)

    assert [(i.subject_id, i.polarity, i.task_id) for i in items] == [
        ("automationbench-verified:t1", "positive", "t1"),
        ("automationbench-verified:t3", "positive", "t3"),
    ]
    assert items[0].kind == "task_defect" and items[0].granularity == "task"
    assert items[0].label == "defective_verified: Attribute mismatch"
    assert items[0].harness_selector is None and items[0].actor_transcript is None


def test_paichecker_has_real_negatives_keyed_by_sub_corpus(tmp_path: Path) -> None:
    _adapter(
        tmp_path,
        source._LB11 + "/manyi-w__paichecker",
        """
        def specs(bundle):
            yield {"identity": {"sub_corpus": "gym", "instance_id": "i-1", "repo": "r"},
                   "gold": {"positive": True, "families": ["DP", "SC"]}}
            yield {"identity": {"sub_corpus": "gym", "instance_id": "i-2", "repo": "r"},
                   "gold": {"positive": False, "families": []}}
            yield {"identity": {"sub_corpus": "multi", "instance_id": "i-1", "repo": "r"},
                   "gold": {"positive": False, "families": []}}
        """,
    )
    ctx = _context(tmp_path, [_entry(tmp_path, "paichecker", "established")])

    items = _items(ctx)

    assert [(i.polarity, i.label, i.label_type) for i in items] == [
        ("positive", "misalignment: DP+SC", "human"),
        ("negative", "No Misalignment", "human"),
        ("negative", "No Misalignment", "human"),
    ]
    assert len({i.item_id for i in items}) == 3  # the same instance id in two sub-corpora stays distinct
    assert all(i.task_id in {"i-1", "i-2"} for i in items)


def test_unflagged_negatives_are_dropped_when_basis_is_no_flag_fired(tmp_path: Path) -> None:
    _adapter(
        tmp_path,
        source._PQ2 + "/ameek__mistrace",
        """
        def specs(bundle):
            for key, state in (("r1", "positive_success"), ("r2", "negative"),
                               ("r3", "positive_attempt"), ("r4", "ambiguous")):
                yield {"gold": {"label_state": state}, "identity": {"run_key": key},
                       "_steps": [{"step_id": 0, "source": "agent", "message": "hi"}]}
        """,
    )
    ctx = _context(tmp_path, [_entry(tmp_path, "mistrace-reward-hacking", "no_flag_fired", "cc-by-4.0")])

    items = _items(ctx)

    assert [(i.polarity, i.label_type) for i in items] == [("positive", "consensus"), ("positive", "judge")]
    assert all(i.actor_transcript is True and i.harness_selector is None for i in items)


def test_segment_construction_labels_keep_both_arms(tmp_path: Path) -> None:
    _adapter(
        tmp_path,
        source._PQ1 + "/safety-research__sleight-bench",
        """
        def specs(bundle):
            yield {"trace_key": "k-attack", "positive": True, "_steps": [{"step_id": 0, "source": "agent", "message": "x"}]}
            yield {"trace_key": "k-benign", "positive": False, "_steps": [{"step_id": 0, "source": "user", "message": "y"}]}
        """,
    )
    ctx = _context(tmp_path, [_entry(tmp_path, "sleight-bench", "established")])

    attack, benign = _items(ctx)

    assert (attack.polarity, benign.polarity) == ("positive", "negative")
    assert {attack.granularity, benign.granularity} == {"segment"}
    assert {attack.label_type, benign.label_type} == {"construction"}
    assert (attack.actor_transcript, benign.actor_transcript) == (True, False)


def test_hackbench_yields_only_a_summary_and_never_opens_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("HackBench rows must never be loaded")

    monkeypatch.setattr(source, "_staged_rows", forbidden)
    monkeypatch.setattr(source, "_load_adapter", forbidden)
    entry = _entry(tmp_path, "hackbench", "none", "cc-by-nc-4.0")
    entry["bundle_path"] = str(tmp_path / "absent")  # not even a directory
    entry["labels"]["counts"] = {"hack_positive_records": 169, "clean_negative_records": 0, "natural_records": 41}
    ctx = _context(tmp_path, [entry])

    out = list(source.build(ctx))

    assert len(out) == 1 and isinstance(out[0], SourceSummary)
    summary = out[0]
    assert summary.counts == {"positive": 169, "negative": 0}
    assert (summary.source, summary.split, summary.license) == ("cross-test", "cross_test", "cc-by-nc-4.0")
    assert "41 natural" in summary.reason
    assert summary.provenance.store == "cross-test:hackbench" and summary.provenance.digest == DIGEST


def test_test_sealed_identity_is_never_dev(tmp_path: Path) -> None:
    _adapter(
        tmp_path,
        source._LB11 + "/manyi-w__paichecker",
        """
        def specs(bundle):
            yield {"identity": {"sub_corpus": "s", "instance_id": "sealed-task", "repo": "r"},
                   "gold": {"positive": True, "families": ["SC"]}}
            yield {"identity": {"sub_corpus": "s", "instance_id": "open-task", "repo": "r"},
                   "gold": {"positive": False, "families": []}}
        """,
    )
    ctx = _context(tmp_path, [_entry(tmp_path, "paichecker", "established")])
    ctx.sealed = frozenset({"sealed-task"})

    items = _items(ctx)

    # the identities are passed to the strict check, and the stub proves a sealed DEV claim would be TEST
    assert {(call[0], call[2]) for call in ctx.calls} == {("cross_test", "sealed-task"), ("cross_test", "open-task")}
    assert ctx.strict_split("DEV", task_id="sealed-task") == "TEST"
    assert [i.split for i in items] == ["cross_test", "cross_test"]
    assert all(i.declared_split == "cross_test" and i.split != "DEV" for i in items)


def test_unmapped_registry_entry_and_missing_bundle_fail_loudly(tmp_path: Path) -> None:
    unknown = _context(tmp_path, [_entry(tmp_path, "brand-new-set", "established")])
    with pytest.raises(ValueError, match="no issue-bank mapping"):
        list(source.build(unknown))

    entry = _entry(tmp_path, "paichecker", "established")
    entry["bundle_path"] = str(tmp_path / "gone")
    with pytest.raises(FileNotFoundError, match="not on this machine"):
        list(source.build(_context(tmp_path, [entry])))


def test_duplicate_native_id_is_refused(tmp_path: Path) -> None:
    _adapter(
        tmp_path,
        source._PQ1 + "/safety-research__sleight-bench",
        """
        def specs(bundle):
            yield {"trace_key": "same", "positive": True}
            yield {"trace_key": "same", "positive": False}
        """,
    )
    ctx = _context(tmp_path, [_entry(tmp_path, "sleight-bench", "established")])
    with pytest.raises(ValueError, match="duplicate row id"):
        list(source.build(ctx))


def test_skipped_and_held_sets_are_reported(tmp_path: Path) -> None:
    hackbench = _entry(tmp_path, "hackbench", "none", "cc-by-nc-4.0")
    hackbench["labels"]["counts"] = {"hack_positive_records": 1, "clean_negative_records": 0, "natural_records": 0}
    ctx = _context(
        tmp_path,
        [_entry(tmp_path, "apollo-insider-trading", "no_flag_fired"), hackbench],
        hold_access=[{"id": "metr-evals/malt", "status": "needs_peter", "why_held": "one-click gate"}],
        hold_licence_not_staged=[{"id": "owner/no-licence", "why": "licence is null"}],
    )

    assert set(source.not_itemized(ctx)) == {"apollo-insider-trading", "hackbench"}
    assert source.held_sets(ctx) == {"metr-evals/malt": "one-click gate", "owner/no-licence": "licence is null"}
    # a skipped entry contributes nothing; HackBench contributes its summary alone
    assert [type(x) for x in source.build(ctx)] == [SourceSummary]
