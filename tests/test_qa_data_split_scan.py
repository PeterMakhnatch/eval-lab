"""The ledger-side split lookup answers one word; ``qa-data scan`` fails closed without a runtime and never reports a size."""

from __future__ import annotations

import hashlib
import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest

from evallab.production import ledger_split, qa_data_scan, qa_data_split
from evallab.production.cloud_trace_db import CloudTraceDatabase, CloudTraceRetriever
from evallab.production.corpus_queue import CorpusRegistry, _identity
from evallab.production.qa_data import UNAVAILABLE, Budget, QaContext, QaDataError, estimate_tokens

KEY, OTHER = "synthetic-key", "other-key"
LEDGER_TEXT = (
    "key_id,item_id,task,unit,split,expected_verdict,exposures\n"
    f"{KEY},item-dev,suite/dev-task,task,dev,SECRET-VERDICT,SECRET-EXPOSURE\n"
    f"{KEY},item-test,suite/test-task,task,test,SECRET-VERDICT,SECRET-EXPOSURE\n"
    f"{KEY},item-locked,suite/locked-task,task,dev,SECRET-VERDICT,SECRET-EXPOSURE\n"
    f"{OTHER},item-locked,suite/locked-task,task,test,SECRET-VERDICT,SECRET-EXPOSURE\n"
)


class LedgerRegistry(CorpusRegistry):
    """A populated split ledger without the real answer-key indexes."""

    def __init__(self) -> None:
        self._assignments = {
            (_identity(KEY), _identity("item-dev")): "dev",
            (_identity(KEY), _identity("item-test")): "test",
            (_identity(KEY), _identity("item-locked")): "dev",
            (_identity(OTHER), _identity("item-locked")): "test",
        }
        self._assignment_keys = frozenset(key for key, _item in self._assignments)
        self._assignment_names = {
            (_identity(KEY), self.task_name("suite/dev-task")): "dev",
            (_identity(KEY), self.task_name("suite/test-task")): "test",
            (_identity(KEY), self.task_name("suite/locked-task")): "dev",
            (_identity(OTHER), self.task_name("suite/locked-task")): "test",
        }
        self._assigned_test_ids = {_identity("item-test"), _identity("item-locked")}
        self._assigned_test_names = {self.task_name("suite/test-task"), self.task_name("suite/locked-task")}
        self._sealed_names = {self.task_name("suite/sealed-task")}
        self._sealed_digests: set[str] = set()
        self._sealed_ids: set[str] = set()
        self._items = {}
        self._index_names = {}
        self._external = set()
        self._token = None
        self.split_assignments_digest = "sha256:" + hashlib.sha256(LEDGER_TEXT.encode()).hexdigest()


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ledger = tmp_path / "research/answer-keys/splits/assignments.csv"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(LEDGER_TEXT, encoding="utf-8")
    monkeypatch.setattr(ledger_split, "CorpusRegistry", lambda _root: LedgerRegistry())
    return tmp_path


@pytest.mark.parametrize(("task", "expected"), [
    ("suite/dev-task", "DEV"), ("dev-task", "DEV"),  # namespaced or bare
    ("suite/test-task", "TEST"), ("test-task", "TEST"),
    ("suite/locked-task", "TEST"),  # DEV under its own key, TEST under another
    ("suite/sealed-task", "TEST"), ("sealed-task", "TEST"),
    ("suite/never-seen", "UNKNOWN"),
])
def test_task_split_is_one_word_and_test_wins_over_a_dev_label(root: Path, task: str, expected: str) -> None:
    assert ledger_split.split(root, "task", [task]) == {"split": expected}


@pytest.mark.parametrize(("key", "item", "expected"), [
    (KEY, "item-dev", "DEV"), (KEY.upper(), " ITEM-DEV ", "DEV"),
    (KEY, "item-test", "TEST"), (KEY, "item-locked", "TEST"), (KEY, "unregistered-item", "UNKNOWN"),
])
def test_item_split_uses_the_pair_and_any_key_lock(root: Path, key: str, item: str, expected: str) -> None:
    assert ledger_split.split(root, "item", [key, item]) == {"split": expected}


def test_ledger_split_never_returns_ledger_labels_and_refuses_bad_usage(root: Path, tmp_path: Path) -> None:
    answer = ledger_split.split(root, "item", [KEY, "item-test"])
    assert "SECRET" not in repr(answer) and set(answer) == {"split"}
    for mode, ids in (("task", []), ("task", ["a", "b"]), ("item", ["only-one"])):
        with pytest.raises(ValueError, match="usage"):
            ledger_split.split(root, mode, ids)
    assert ledger_split.split(tmp_path / "elsewhere", "task", ["suite/dev-task"]) == {"split": "UNKNOWN"}


def test_scan_refuses_unknown_scanners_missing_files_and_a_missing_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcripts = tmp_path / "transcripts.jsonl"
    transcripts.write_text("{}\n", encoding="utf-8")
    with pytest.raises(QaDataError, match="unknown scanner"):
        qa_data_scan.scan(transcripts, ["rule_not_a_text_scanner"], Budget(1000))
    with pytest.raises(QaDataError, match="pass the transcripts.jsonl"):
        qa_data_scan.scan(tmp_path / "missing.jsonl", None, Budget(1000))
    if importlib.util.find_spec("inspect_scout") is None:
        monkeypatch.setattr(qa_data_scan, "RUNTIME", tmp_path / "no-runtime")
        with pytest.raises(QaDataError, match="no scout-watch runtime"):
            qa_data_scan.scan(transcripts, None, Budget(1000))


def test_scan_rows_beyond_the_budget_are_counted_not_refused() -> None:
    rows = [
        {"blind_id": f"run-{n}", "messages": 3, "scanner": "repeated_assistant_message", "results": 1,
         "values": [0], "references": 0}
        for n in range(100)
    ]
    fitted = qa_data_scan._fit({"transcripts": 100, "rows": rows, "model_calls": 0}, Budget(400))
    assert 0 < len(fitted["rows"]) < 100
    assert fitted["omitted_rows"] == 100 - len(fitted["rows"]) and fitted["truncated"] is True
    assert fitted["rows"] == rows[: len(fitted["rows"])]  # whole rows, in order


@pytest.mark.parametrize("limit", [200, 250, 300, 600, 1000, 4000])
def test_scan_reply_with_its_fixed_fields_never_exceeds_the_budget(limit: int) -> None:
    rows = [
        {"blind_id": f"run-{n:032x}", "messages": 296, "scanner": "harness_parse_errors", "results": 4,
         "values": [0, 0, 0, 0], "references": 0}
        for n in range(212)
    ]
    budget = Budget(limit - 120)
    fitted = qa_data_scan._fit({"transcripts": 53, "rows": rows, "inspect_scout": "0.5.4", "model_calls": 0}, budget)
    assert estimate_tokens(fitted) <= limit - 120
    assert len(fitted["rows"]) + fitted["omitted_rows"] == 212 and fitted["model_calls"] == 0


@pytest.fixture
def ctx(root: Path, tmp_path: Path) -> Iterator[QaContext]:
    registry = LedgerRegistry()
    with CloudTraceDatabase(tmp_path / "qa.sqlite", sqlite_fts=False) as db:
        for task_id in ("suite/dev-task", "suite/test-task", "suite/locked-task"):
            db.upsert_task({
                "task_id": task_id, "dataset_id": "suite", "split": "dev", "instruction_text": "Repair output",
            })
        yield QaContext(
            database=db, retriever=CloudTraceRetriever(db, registry=registry),
            repo_root=root, state_dir=tmp_path / "state", budget=Budget(10_000),
        )


@pytest.mark.parametrize("ids", [
    ("task", ["suite/dev-task"]), ("task", ["dev-task"]), ("item", [KEY, "item-dev"]),
    ("item", [KEY.upper(), " ITEM-DEV "]),
])
def test_qa_data_split_confirms_only_admitted_dev_ids(ctx: QaContext, ids: tuple[str, list[str]]) -> None:
    assert qa_data_split.split(ctx, *ids) == {"split": "DEV"}


@pytest.mark.parametrize("ids", [
    ("task", ["suite/test-task"]), ("task", ["suite/locked-task"]),  # TEST; DEV under one key, TEST under another
    ("task", ["suite/sealed-task"]), ("task", ["never-seen"]),
    ("item", [KEY, "item-test"]), ("item", [KEY, "item-locked"]), ("item", [KEY, "unregistered-item"]),
    ("item", ["no-such-key", "item-dev"]),
])
def test_qa_data_split_gives_every_other_id_the_one_refusal(ctx: QaContext, ids: tuple[str, list[str]]) -> None:
    with pytest.raises(QaDataError) as error:
        qa_data_split.split(ctx, *ids)
    assert str(error.value) == UNAVAILABLE


def test_qa_data_split_usage_error_does_not_depend_on_the_ids(ctx: QaContext) -> None:
    for mode, ids in (("task", []), ("task", ["a", "b"]), ("item", ["only-one"])):
        with pytest.raises(QaDataError, match="usage"):
            qa_data_split.split(ctx, mode, ids)
