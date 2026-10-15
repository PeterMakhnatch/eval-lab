"""Behavioural tests for the shared QA contract (`evallab.qa`)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evallab.qa import (
    AnswerKeyItem,
    Finding,
    FindingSubject,
    KeyPair,
    append_scoreboard,
    load_answer_key,
    load_findings_dir,
    score_findings,
)
from evallab.qa.answer_key import AnswerKey, import_envcheck_task_audit
from evallab.qa.score import SCOREBOARD_COLUMNS


def _finding(
    task_id: str,
    *,
    verdict: str = "fail",
    category: str = "weak",
    trial_id: str | None = None,
    kind: str = "task",
    cost: float = 0.5,
    measurement: str = "measured",
) -> Finding:
    return Finding.model_validate(
        {
            "schema": "qa.finding/v1",
            "subject": {"kind": kind, "task_id": task_id, "trial_id": trial_id},
            "stage": "S2",
            "verdict": verdict,
            "category": category,
            "severity": "high",
            "fault": "eval",
            "evidence_refs": ["evidence/log.txt"],
            "reviewer": {"agent": "qa-test", "model": "m", "tokens": 100, "cost_usd": cost},
            "measurement": measurement,
        }
    )


def _item(
    task_id: str,
    *,
    verdict: str = "fail",
    category: str | None = "weak",
    split: str = "dev",
    trial_id: str | None = None,
    kind: str = "task",
) -> AnswerKeyItem:
    return AnswerKeyItem(
        subject=FindingSubject(kind=kind, task_id=task_id, trial_id=trial_id),  # type: ignore[arg-type]
        expected_category=category,  # type: ignore[arg-type]
        expected_verdict=verdict,  # type: ignore[arg-type]
        label_source="synthetic test key",
        split=split,
    )


def _key(items: list[AnswerKeyItem], pairs: list[KeyPair] | None = None) -> AnswerKey:
    return AnswerKey(
        key_id="synthetic", items=tuple(items), directory=Path("."), pairs=tuple(pairs or [])
    )


def _row(rows, *, split: str = "all", category: str = "all"):
    return next(r for r in rows if r.split == split and r.category == category)


def test_finding_not_run_cannot_pass() -> None:
    with pytest.raises(ValidationError):
        _finding("t1", verdict="pass", measurement="not_run")


def test_finding_rejects_unknown_category() -> None:
    with pytest.raises(ValidationError):
        _finding("t1", category="nonsense")


def test_fail_item_requires_category() -> None:
    with pytest.raises(ValidationError):
        _item("t1", verdict="fail", category=None)


def test_score_math_recall_root_cause_false_alarm_paired_cost() -> None:
    items = [
        _item("pos-hit"),
        _item("pos-wrong-category"),
        _item("pos-missed"),
        _item("neg-flagged", verdict="pass", category=None),
        _item("neg-clean", verdict="pass", category=None),
        _item("pair-faulty", kind="trace", trial_id="t-f", category="reward_hacking"),
        _item("pair-clean", verdict="pass", category=None, kind="trace", trial_id="t-c"),
    ]
    pairs = [KeyPair(parent="synthetic:trace:pair-clean#t-c", child="synthetic:trace:pair-faulty#t-f")]
    findings = [
        _finding("pos-hit"),
        _finding("pos-wrong-category", category="broken"),
        _finding("pos-missed", verdict="pass"),
        _finding("neg-flagged"),
        _finding("neg-clean", verdict="pass"),
        _finding("pair-faulty", kind="trace", trial_id="t-f", category="reward_hacking"),
        _finding("pair-clean", kind="trace", trial_id="t-c", verdict="pass"),
        _finding("outside-key"),
    ]
    rows = score_findings(_key(items, pairs), findings, scored_at="2026-10-14T00:00:00Z")
    row = _row(rows)
    assert (row.n_items, row.n_measured) == (7, 7)
    # positives: pos-hit, pos-wrong-category, pos-missed, pair-faulty -> 3 flagged
    assert row.recall == pytest.approx(0.75)
    # flagged positives in the expected category: pos-hit, pair-faulty (2 of 3)
    assert row.root_cause_match == pytest.approx(2 / 3, abs=1e-4)
    assert row.false_alarm_rate == pytest.approx(1 / 3, abs=1e-4)
    assert row.paired_discrimination == pytest.approx(1.0)
    assert (row.n_pairs, row.n_pairs_measured) == (1, 1)
    assert row.status == "complete"
    assert "1 finding(s) outside the key" in row.notes
    assert row.cost_per_item_usd == pytest.approx(0.5)
    assert row.agent == "qa-test"
    assert row.system == "m"


def test_unmeasured_items_are_never_passes_or_misses() -> None:
    items = [_item("a"), _item("b"), _item("c", verdict="pass", category=None)]
    findings = [
        _finding("a"),
        _finding("b", verdict="unknown"),  # abstention: not measured
        _finding("c", verdict="pass", measurement="ran_without_usable_result"),
    ]
    row = _row(score_findings(_key(items), findings, scored_at="t"))
    assert row.n_measured == 1
    assert row.recall == 1.0
    assert row.false_alarm_rate is None
    assert row.status == "partial: 1 of 3 items measured"
    assert "unmeasured" in row.notes


def test_pair_needs_both_sides_measured() -> None:
    items = [
        _item("child", kind="trace", trial_id="c", category="reward_hacking"),
        _item("parent", verdict="pass", category=None, kind="trace", trial_id="p"),
    ]
    pairs = [KeyPair(parent="synthetic:trace:parent#p", child="synthetic:trace:child#c")]
    row = _row(
        score_findings(
            _key(items, pairs),
            [_finding("child", kind="trace", trial_id="c", category="reward_hacking")],
            scored_at="t",
        )
    )
    assert (row.n_pairs, row.n_pairs_measured) == (1, 0)
    assert row.paired_discrimination is None


def test_rows_cover_split_and_category_slices_and_append(tmp_path: Path) -> None:
    items = [
        _item("a", split="dev", category="weak"),
        _item("b", split="test", category="broken"),
    ]
    rows = score_findings(_key(items), [_finding("a"), _finding("b", verdict="pass")], scored_at="t")
    assert _row(rows, split="dev").recall == 1.0
    assert _row(rows, split="test").recall == 0.0
    assert _row(rows, split="all", category="broken").recall == 0.0
    assert _row(rows, split="dev", category="weak").n_items == 1
    board = tmp_path / "scoreboard.csv"
    append_scoreboard(rows, board)
    append_scoreboard(rows, board)
    with board.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames or ()) == SCOREBOARD_COLUMNS
        read = list(reader)
    assert len(read) == 2 * len(rows)
    assert read[0]["key_id"] == "synthetic"


def test_load_answer_key_requires_readme(tmp_path: Path) -> None:
    (tmp_path / "items.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="README"):
        load_answer_key(tmp_path)


def test_load_answer_key_keeps_extra_columns_and_pairs(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# k\n", encoding="utf-8")
    base = {
        "schema": "qa.answer_key/v1",
        "label_source": "src",
        "split": "dev",
        "subject": {"kind": "trace", "task_id": "t", "trial_id": "x"},
        "cheat_prompted": True,
        "pair_ids": ["k:pair:x"],
    }
    rows = [
        {**base, "item_id": "k:child", "expected_verdict": "fail",
         "expected_category": "reward_hacking", "role": "child"},
        {**base, "item_id": "k:parent", "expected_verdict": "pass",
         "expected_category": "reward_hacking", "role": "parent",
         "subject": {"kind": "trace", "task_id": "t", "trial_id": "y"}},
    ]
    (tmp_path / "items.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    (tmp_path / "pairs.jsonl").write_text(
        json.dumps({"pair_id": "k:pair:x", "parent": "k:parent", "child": "k:child"}) + "\n",
        encoding="utf-8",
    )
    key = load_answer_key(tmp_path)
    assert len(key.items) == 2 and len(key.pairs) == 1
    (tmp_path / "pairs.jsonl").write_text(
        json.dumps({"parent": "k:parent", "child": "k:missing"}) + "\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="unknown items"):
        load_answer_key(tmp_path)


def test_envcheck_import_maps_labels(tmp_path: Path) -> None:
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "task": "t-defect",
                        "tb_version": "v4.0.0",
                        "label": "defect",
                        "pattern": "task_writable_grader",
                        "finding_slugs": ["slug-a"],
                    }
                ),
                json.dumps(
                    {
                        "task": "t-clean",
                        "tb_version": "v4.0.0",
                        "label": "weak_negative",
                        "pattern": None,
                        "finding_slugs": [],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    splits = {"t-defect@v4.0.0": "heldout", "t-clean@v4.0.0": "dev"}
    key = import_envcheck_task_audit(
        chunks, tmp_path / "key", source_commit="abc123", splits=splits
    )
    assert len(key.items) == 2
    defect = next(i for i in key.items if i.expected_verdict == "fail")
    assert defect.expected_category == "tamperable"
    assert defect.subject.task_id == "t-defect@v4.0.0"
    assert "abc123" in defect.label_source
    assert {i.subject.task_id: i.split for i in key.items} == splits
    with pytest.raises(ValueError, match="no split"):
        import_envcheck_task_audit(
            chunks, tmp_path / "key2", source_commit="abc123", splits={"t-defect@v4.0.0": "dev"}
        )
    assert (tmp_path / "key" / "README.md").is_file()


def test_qa_cli_score_appends_scoreboard_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from evallab.cli import parser
    from evallab.qa.models import dump_finding

    key_dir = tmp_path / "research" / "answer-keys" / "synthetic"
    key_dir.mkdir(parents=True)
    (key_dir / "README.md").write_text("# synthetic\n", encoding="utf-8")
    (key_dir / "items.jsonl").write_text(
        _item("pos").model_dump_json(by_alias=True) + "\n", encoding="utf-8"
    )
    findings_dir = tmp_path / "findings"
    findings_dir.mkdir()
    dump_finding(_finding("pos"), findings_dir / "f1.json")
    args = parser().parse_args(
        ["qa", "score", "--key", "synthetic", "--findings", str(findings_dir), "--json"]
    )
    assert args.func(args, tmp_path) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["recall"] == 1.0
    board = tmp_path / "research" / "qa-scoreboard" / "scoreboard.csv"
    with board.open(newline="", encoding="utf-8") as handle:
        assert len(list(csv.DictReader(handle))) == len(payload)
    assert len(load_findings_dir(findings_dir)) == 1
