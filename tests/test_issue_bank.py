from __future__ import annotations

import json
import types
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

import evallab.failure_library.bank as bank_module
from evallab.failure_library.bank import (
    BankContext,
    IssueBank,
    IssueItem,
    Provenance,
    SourceSummary,
    new_item,
)


def _item(subject: str = "run-1", **overrides: object) -> IssueItem:
    fields: dict[str, object] = {
        "source": "fake",
        "kind": "cheat",
        "polarity": "positive",
        "label_type": "human",
        "label": "reward_hacked",
        "granularity": "run",
        "subject_type": "run",
        "subject_id": subject,
        "declared_split": "dev",
        "split": "DEV",
        "provenance": Provenance(store="fake", ref=subject),
    }
    fields.update(overrides)
    return new_item(**fields)


class _Registry:
    """Stand-in for CorpusRegistry: a TEST-sealed task name and one keyed TEST item."""

    split_assignments_digest = "sha256:" + "0" * 64

    def __init__(self, *, authority: bool = True) -> None:
        self.authority = authority

    def has_assignment_authority(self) -> bool:
        return self.authority

    def has_split_assignments(self, key_id: str) -> bool:
        return key_id == "key-a"

    def assigned_split(self, key_id: str, *, item_id=None, task_id=None) -> str | None:
        return "test" if item_id == "7" else "dev"

    def sealed_identity(self, *, item_id=None, task_id=None, task_digest=None) -> str | None:
        return "task name" if task_id == "sealed-task" else None

    def assigned_test_anywhere(self, *, item_id=None, task_id=None) -> bool:
        return item_id == "other-key-test-item" or task_id == "test-elsewhere"


def _ctx(tmp_path: Path, registry: _Registry | None = None) -> BankContext:
    ctx = BankContext(repo_root=tmp_path, state_root=tmp_path / "state")
    ctx.__dict__["registry"] = registry or _Registry()
    return ctx


def test_item_id_is_deterministic_and_checked() -> None:
    item = _item()
    assert item == _item()
    assert item.item_id != _item(locator="step-3").item_id
    with pytest.raises(ValidationError, match="not deterministic"):
        IssueItem(**{**item.model_dump(), "item_id": "ib-0000000000000000"})


@pytest.mark.parametrize(
    ("declared", "identities", "expected"),
    [
        ("dev", {"task_id": "open-task"}, "DEV"),
        ("dev", {"task_id": "sealed-task"}, "TEST"),
        ("dev", {"task_id": "test-elsewhere"}, "TEST"),
        ("dev", {"key_id": "key-a", "item_id": "7"}, "TEST"),
        ("dev", {"item_id": "other-key-test-item"}, "TEST"),
        ("cross_test", {"task_id": "open-task"}, "cross_test"),
        ("heldout", {"task_id": "open-task"}, "heldout"),
    ],
)
def test_strict_split_only_tightens(
    tmp_path: Path, declared: str, identities: dict[str, str], expected: str
) -> None:
    assert _ctx(tmp_path).strict_split(declared, **identities) == expected


def test_bare_numeric_item_ids_do_not_seal_across_keys(tmp_path: Path) -> None:
    class Collides(_Registry):
        def assigned_test_anywhere(self, *, item_id=None, task_id=None) -> bool:
            return item_id == "7"

    # "7" is TEST in some other key; as a bare row index it is not distinctive.
    assert _ctx(tmp_path, Collides()).strict_split("dev", item_id="7") == "DEV"


def test_nothing_is_dev_without_assignment_authority(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, _Registry(authority=False))
    assert ctx.strict_split("dev", task_id="open-task") == "unassigned"


def _install(monkeypatch: pytest.MonkeyPatch, builders: dict[str, object]) -> None:
    modules = {
        slug: types.SimpleNamespace(SOURCE=slug, build=build) for slug, build in builders.items()
    }
    monkeypatch.setattr(bank_module, "available_sources", lambda: {slug: slug for slug in modules})
    monkeypatch.setattr(bank_module, "_load_source", lambda name: modules[name])


def test_build_writes_items_locally_and_only_counts_to_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake(_ctx: BankContext) -> Iterator[IssueItem]:
        yield _item("run-1")
        yield _item("run-2", polarity="negative", label="clean")
        yield _item("run-1")  # an identical repeat is one item

    def forbidden(_ctx: BankContext) -> Iterator[SourceSummary]:
        yield SourceSummary(
            source="forbidden",
            kind="cheat",
            granularity="command",
            split="cross_test",
            license="CC-BY-NC-4.0",
            actor_transcript=True,
            counts={"positive": 169, "negative": 0},
            reason="no rehosting",
            provenance=Provenance(store="cross-test:forbidden", ref="registry"),
        )

    _install(monkeypatch, {"fake": fake, "forbidden": forbidden})
    bank = IssueBank(tmp_path, state_root=tmp_path / "state")
    manifest = bank.build(_ctx(tmp_path))

    assert sorted(item.subject_id for item in bank.iter_items()) == ["run-1", "run-2"]
    assert not bank.items_path("forbidden").exists()
    entry = manifest["sources"]["fake"]
    assert entry["count"] == 2
    assert entry["by_kind_polarity_split"] == {"cheat|negative|DEV": 1, "cheat|positive|DEV": 1}
    assert manifest["sources"]["forbidden"]["itemized"] is False
    assert manifest["totals"]["by_polarity_split"]["positive|cross_test(summary)"] == 169
    committed = (tmp_path / bank_module.MANIFEST_PATH).read_text(encoding="utf-8")
    assert "run-1" not in committed and _item("run-1").item_id not in committed
    assert bank.verify() == []
    assert list(bank.iter_items(split="TEST")) == []
    assert len(list(bank.iter_items(split="DEV", kind="cheat"))) == 2


def test_rebuilding_one_source_keeps_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"a": 0, "b": 0}

    def builder(slug: str):
        def build(_ctx: BankContext) -> Iterator[IssueItem]:
            calls[slug] += 1
            yield _item(f"{slug}-run", source=slug)

        return build

    _install(monkeypatch, {"a": builder("a"), "b": builder("b")})
    bank = IssueBank(tmp_path, state_root=tmp_path / "state")
    bank.build(_ctx(tmp_path))
    manifest = bank.build(_ctx(tmp_path), ["a"])
    assert calls == {"a": 2, "b": 1}
    assert set(manifest["sources"]) == {"a", "b"}
    assert manifest["totals"]["items"] == 2


def test_conflicting_duplicates_and_foreign_sources_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def conflicting(_ctx: BankContext) -> Iterator[IssueItem]:
        yield _item("run-1")
        yield _item("run-1", label="different label")

    def foreign(_ctx: BankContext) -> Iterator[IssueItem]:
        yield _item("run-1", source="elsewhere")

    _install(monkeypatch, {"fake": conflicting, "other": foreign})
    bank = IssueBank(tmp_path, state_root=tmp_path / "state")
    with pytest.raises(ValueError, match="conflicting duplicate"):
        bank.build(_ctx(tmp_path), ["fake"])
    with pytest.raises(ValueError, match="names source elsewhere"):
        bank.build(_ctx(tmp_path), ["other"])
    with pytest.raises(ValueError, match="unknown bank sources"):
        bank.build(_ctx(tmp_path), ["missing"])


def test_verify_reports_changed_local_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, {"fake": lambda _ctx: iter([_item()])})
    bank = IssueBank(tmp_path, state_root=tmp_path / "state")
    bank.build(_ctx(tmp_path))
    path = bank.items_path("fake")
    path.write_text(path.read_text(encoding="utf-8") + json.dumps({}) + "\n", encoding="utf-8")
    assert bank.verify() and "differs from manifest" in bank.verify()[0]
