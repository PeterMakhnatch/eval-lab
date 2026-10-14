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
    append_scoreboard,
    load_answer_key,
    load_findings_dir,
    score_findings,
)
from evallab.qa.answer_key import AnswerKey, import_envcheck_task_audit


def _finding(
    task_id: str,
    *,
    verdict: str = "fail",
    category: str = "weak",
    trial_id: str | None = None,
    kind: str = "task",
    cost: float = 0.5,
    tokens: int | None = 100,
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
            "reviewer": {"agent": "qa-test", "model": "m", "tokens": tokens, "cost_usd": cost},
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
    pair_id: str | None = None,
) -> AnswerKeyItem:
    return AnswerKeyItem(
        subject=FindingSubject(kind=kind, task_id=task_id, trial_id=trial_id),  # type: ignore[arg-type]
        expected_category=category,  # type: ignore[arg-type]
        expected_verdict=verdict,  # type: ignore[arg-type]
        label_source="synthetic test key",
        split=split,
        pair_id=pair_id,
    )


def _key(items: list[AnswerKeyItem]) -> AnswerKey:
    return AnswerKey(key_id="synthetic", items=tuple(items), directory=Path("."))


def test_finding_not_run_cannot_pass() -> None:
    with pytest.raises(ValidationError):
        Finding.model_validate(
            {
                "subject": {"kind": "task", "task_id": "t1"},
                "stage": "S0",
                "verdict": "pass",
                "category": "broken",
                "severity": "low",
                "fault": "eval",
                "reviewer": {"agent": "qa"},
                "measurement": "not_run",
            }
        )


def test_finding_rejects_unknown_fields_and_categories() -> None:
    with pytest.raises(ValidationError):
        Finding.model_validate(
            {
                "subject": {"kind": "task", "task_id": "t1"},
                "stage": "S0",
                "verdict": "fail",
                "category": "nonsense",
                "severity": "low",
                "fault": "eval",
                "reviewer": {"agent": "qa"},
            }
        )


def test_score_math_recall_root_cause_false_alarm_paired_cost() -> None:
    items = [
        _item("pos-hit", pair_id=None),
        _item("pos-wrong-category"),
        _item("pos-missed"),
        _item("neg-flagged", verdict="pass", category=None),
        _item("neg-clean", verdict="pass", category=None),
        _item("unk", verdict="unknown", category=None),
        _item("pair-faulty", kind="trace", trial_id="t-f", pair_id="p1", category="reward_hacking"),
        _item(
            "pair-clean",
            verdict="pass",
            category=None,
            kind="trace",
            trial_id="t-c",
            pair_id="p1",
        ),
    ]
    findings = [
        _finding("pos-hit"),
        _finding("pos-wrong-category", category="broken"),
        _finding("pos-missed", verdict="pass"),
        _finding("neg-flagged"),
        _finding("pair-faulty", kind="trace", trial_id="t-f", category="reward_hacking"),
        _finding("outside-key"),
    ]
    rows = score_findings(_key(items), findings, scored_at="2026-10-14T00:00:00+00:00")
    assert len(rows) == 1
    row = rows[0]
    assert row.items == 8
    assert row.positives == 4
    assert row.negatives == 3
    assert row.unknown_items == 1
    assert row.recall == pytest.approx(3 / 4)
    assert row.root_cause_match == pytest.approx(2 / 4)
    assert row.false_alarm_rate == pytest.approx(1 / 3)
    assert row.paired_discrimination == pytest.approx(1.0)
    assert row.unmatched_findings == 1
    # 5 matched findings x $0.5 over 8 items.
    assert row.cost_per_item == pytest.approx(2.5 / 8)
    assert row.agent == "qa-test"


def test_score_rows_are_per_split_and_append_scoreboard(tmp_path: Path) -> None:
    items = [_item("a", split="dev"), _item("b", split="held_out")]
    rows = score_findings(_key(items), [_finding("a")], scored_at="t")
    assert [row.split for row in rows] == ["dev", "held_out"]
    assert rows[0].recall == 1.0
    assert rows[1].recall == 0.0
    board = tmp_path / "scoreboard.csv"
    append_scoreboard(rows, board)
    append_scoreboard(rows, board)
    with board.open(newline="", encoding="utf-8") as handle:
        read = list(csv.DictReader(handle))
    assert len(read) == 4
    assert read[0]["key_id"] == "synthetic"
    assert read[0]["recall"] == "1.0"


def test_load_answer_key_requires_readme(tmp_path: Path) -> None:
    (tmp_path / "items.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="README"):
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
    key = import_envcheck_task_audit(chunks, tmp_path / "key", source_commit="abc123")
    assert len(key.items) == 2
    defect = next(i for i in key.items if i.expected_verdict == "fail")
    assert defect.expected_category == "tamperable"
    assert defect.subject.task_id == "t-defect@v4.0.0"
    assert "abc123" in defect.label_source
    assert (tmp_path / "key" / "README.md").is_file()


def test_qa_cli_score_writes_scoreboard(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
    assert board.is_file()
    # Round-trip: the findings loader reads back what dump wrote.
    assert len(load_findings_dir(findings_dir)) == 1
