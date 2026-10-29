from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from evallab.failure_library.bank import IssueItem, Provenance, new_item
from evallab.failure_library.bank.matrix import (
    Identity,
    build_matrix,
    load_run_findings,
    load_task_findings,
    markdown,
    summarize,
    wilson,
    write_outputs,
)

CASCADE = "cascade@v8[kimi-code/k3]"
SCOUT = "scout@s1"
LOOP = "run_health.loop@run-health/4"
GATE = "taskqa_gate@g1#arm-a"


def _item(subject: str, **overrides: Any) -> IssueItem:
    fields: dict[str, Any] = {
        "source": "fake",
        "kind": "cheat",
        "polarity": "positive",
        "label_type": "human",
        "label": "label",
        "granularity": "run",
        "subject_type": "run",
        "subject_id": subject,
        "declared_split": "dev",
        "split": "DEV",
        "actor_transcript": True,
        "harness_selector": subject,
        "provenance": Provenance(store="fake", ref=subject),
    }
    fields.update(overrides)
    return new_item(**fields)


def _finding(
    run_id: str,
    detector: str,
    version: str,
    verdict: str,
    kind: str = "cheat",
    *,
    reader: str | None = None,
    produced_at: str = "2026-10-28T10:00:00Z",
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "detector": detector,
        "detector_version": version,
        "reader_model": reader,
        "verdict": verdict,
        "score": None,
        "kind": kind,
        "evidence_steps": [1],
        "summary": "s",
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_usd": 0.0,
        "latency_ms": 0,
        "produced_at": produced_at,
    }


def _write(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _cells(matrix: Any) -> dict[tuple[str, str], str]:
    return {(c.item.subject_id, c.identity): c.cell for c in matrix.cells}


@pytest.fixture
def scenario(tmp_path: Path) -> tuple[list[IssueItem], Path, Path, Path]:
    items = [
        _item("pos-caught"),
        _item("pos-missed"),
        _item("neg-alarmed", polarity="negative"),
        _item("neg-cleared", polarity="negative"),
        _item("pos-abstain"),
        _item("pos-unknown"),
        _item("pos-notrun"),
        _item("pos-noactor", actor_transcript=False),
        _item("loop-pos", kind="loop", source="run-health"),
        _item("loop-neg", kind="loop", source="run-health", polarity="negative"),
        _item(
            "loop-weak",
            kind="loop",
            source="run-health",
            label_type="detector",
            provenance=Provenance(store="run-health:run-health/4", ref="loop-weak"),
        ),
        # CheatBench: the harness run id is the key-qualified item id; the selector is generic
        _item("cheatbench-actions-v1:abc123", source="answer-keys", harness_selector="cheatbench"),
        _item(
            "task-a",
            subject_type="task",
            granularity="task",
            kind="grader_wrong_pass",
            task_id="org/task-a@1",
            harness_selector=None,
            actor_transcript=None,
        ),
        _item(
            "task-b",
            subject_type="task",
            granularity="task",
            kind="grader_wrong_pass",
            polarity="negative",
            task_id="task-b",
            harness_selector=None,
            actor_transcript=None,
        ),
        _item(
            "task-c",
            subject_type="task",
            granularity="task",
            kind="grader_unfair_fail",
            task_id="task-c",
            harness_selector=None,
            actor_transcript=None,
        ),
    ]
    old = "2026-10-27T10:00:00Z"
    new = "2026-10-28T12:00:00Z"
    first = _write(
        tmp_path / "a.jsonl",
        [
            _finding("pos-caught", "cascade", "v8", "alarm", reader="kimi-code/k3"),
            # latest record wins across files: the newer clear in b.jsonl supersedes this alarm
            _finding(
                "pos-missed", "cascade", "v8", "alarm", reader="kimi-code/k3", produced_at=old
            ),
            _finding("neg-alarmed", "cascade", "v8", "alarm", reader="kimi-code/k3"),
            _finding("neg-cleared", "cascade", "v8", "clear", reader="kimi-code/k3"),
            _finding("pos-abstain", "cascade", "v8", "abstain", reader="kimi-code/k3"),
            _finding("pos-unknown", "cascade", "v8", "unknown", reader="kimi-code/k3"),
            _finding("pos-noactor", "cascade", "v8", "clear", reader="kimi-code/k3"),
            _finding(
                "cheatbench-actions-v1:abc123", "cascade", "v8", "alarm", reader="kimi-code/k3"
            ),
            # scout: a loop alarm is not a cheat catch; it is a loop catch
            _finding("pos-caught", "scout", "s1", "alarm", "loop"),
            _finding("loop-pos", "scout", "s1", "alarm", "loop"),
            _finding("loop-neg", "scout", "s1", "alarm", "cheat"),
        ],
    )
    second = _write(
        tmp_path / "b.jsonl",
        [
            _finding(
                "pos-missed", "cascade", "v8", "clear", reader="kimi-code/k3", produced_at=new
            ),
            _finding("loop-pos", "run_health.loop", "run-health/4", "alarm", "loop"),
            _finding("loop-neg", "run_health.loop", "run-health/4", "clear", "loop"),
            _finding("loop-weak", "run_health.loop", "run-health/4", "alarm", "loop"),
            # an older record in a later file never overrides a newer one
            _finding(
                "neg-cleared", "cascade", "v8", "alarm", reader="kimi-code/k3", produced_at=old
            ),
        ],
    )
    gate = {
        "detector": "taskqa_gate",
        "detector_version": "g1",
        "reader_model": None,
        "arm": "arm-a",
        "score": None,
        "evidence": "e",
        "source_ref": "r",
    }
    tasks = _write(
        tmp_path / "tasks.jsonl",
        [
            {
                **gate,
                "task_id": "task-a",
                "item_ids": [],
                "verdict": "alarm",
                "kind": "grader_wrong_pass",
            },
            {
                **gate,
                "task_id": "task-b",
                "item_ids": [],
                "verdict": "alarm",
                "kind": "grader_wrong_pass",
            },
            # wrong-kind alarm: ran on task-c, raised nothing matching its unfair-fail item
            {
                **gate,
                "task_id": "task-c",
                "item_ids": [],
                "verdict": "alarm",
                "kind": "grader_wrong_pass",
            },
        ],
    )
    return items, first, second, tasks


def test_every_cell_type_and_rules(scenario: tuple[list[IssueItem], Path, Path, Path]) -> None:
    items, first, second, tasks = scenario
    matrix = build_matrix(
        items,
        load_run_findings([first, second]),
        load_task_findings([tasks]),
        unions=[["cascade", "scout"]],
    )
    cells = _cells(matrix)
    assert cells[("pos-caught", CASCADE)] == "catch"
    assert cells[("pos-missed", CASCADE)] == "miss"  # newer clear wins
    assert cells[("neg-alarmed", CASCADE)] == "false_alarm"
    assert cells[("neg-cleared", CASCADE)] == "correct_clear"  # older alarm loses
    assert cells[("pos-abstain", CASCADE)] == "abstain"
    assert cells[("pos-unknown", CASCADE)] == "unknown"
    assert cells[("pos-notrun", CASCADE)] == "not_run"
    assert {c.cell for c in matrix.cells} >= {
        "catch",
        "miss",
        "false_alarm",
        "correct_clear",
        "abstain",
        "unknown",
        "not_run",
    }


def test_no_actor_transcript_is_never_clear(
    scenario: tuple[list[IssueItem], Path, Path, Path],
) -> None:
    items, first, second, _ = scenario
    matrix = build_matrix(items, load_run_findings([first, second]))
    cell = next(
        c for c in matrix.cells if c.item.subject_id == "pos-noactor" and c.identity == CASCADE
    )
    assert (cell.cell, cell.reason) == ("unknown", "no_actor_transcript")
    scout = next(
        c for c in matrix.cells if c.item.subject_id == "pos-noactor" and c.identity == SCOUT
    )
    assert scout.cell == "unknown"  # not run, but no transcript: still unknown, not clear


def test_kind_compatibility(scenario: tuple[list[IssueItem], Path, Path, Path]) -> None:
    items, first, second, tasks = scenario
    matrix = build_matrix(items, load_run_findings([first, second]), load_task_findings([tasks]))
    cells = _cells(matrix)
    # scout's loop alarm on a cheat positive is a cheat miss, a loop catch on the loop item
    assert cells[("pos-caught", SCOUT)] == "miss"
    assert cells[("loop-pos", SCOUT)] == "catch"
    # a cheat alarm on a loop negative is not a loop false alarm
    assert cells[("loop-neg", SCOUT)] == "correct_clear"
    # run_health.loop is never scored on cheat items; cascade never on loop items
    assert ("pos-caught", LOOP) not in cells
    assert ("loop-pos", CASCADE) not in cells
    assert cells[("loop-pos", LOOP)] == "catch"
    assert cells[("loop-neg", LOOP)] == "correct_clear"
    # task findings join task items by normalized task name, kind-aware
    assert cells[("task-a", GATE)] == "catch"
    assert cells[("task-b", GATE)] == "false_alarm"
    assert cells[("task-c", GATE)] == "miss"
    assert not any(c.identity == GATE for c in matrix.cells if c.item.subject_type == "run")


def test_cheatbench_join_uses_qualified_item_id(
    scenario: tuple[list[IssueItem], Path, Path, Path],
) -> None:
    items, first, second, _ = scenario
    cells = _cells(build_matrix(items, load_run_findings([first, second])))
    assert cells[("cheatbench-actions-v1:abc123", CASCADE)] == "catch"


def test_task_findings_item_ids_restrict(tmp_path: Path) -> None:
    one = _item(
        "t1",
        subject_type="task",
        granularity="task",
        kind="task_defect",
        task_id="t",
        harness_selector=None,
        actor_transcript=None,
    )
    two = _item(
        "t2",
        subject_type="task",
        granularity="task",
        kind="task_defect",
        task_id="t",
        harness_selector=None,
        actor_transcript=None,
    )
    tasks = _write(
        tmp_path / "t.jsonl",
        [
            {
                "task_id": "t",
                "item_ids": [one.item_id],
                "detector": "taskqa_gate",
                "detector_version": "g1",
                "reader_model": None,
                "arm": "a",
                "verdict": "alarm",
                "kind": "task_defect",
                "score": None,
                "evidence": "e",
                "source_ref": "r",
            }
        ],
    )
    matrix = build_matrix([one, two], {}, load_task_findings([tasks]))
    assert _cells(matrix) == {
        ("t1", "taskqa_gate@g1#a"): "catch",
        ("t2", "taskqa_gate@g1#a"): "not_run",
    }


def test_circular_cells_leave_the_summary(
    scenario: tuple[list[IssueItem], Path, Path, Path],
) -> None:
    items, first, second, _ = scenario
    matrix = build_matrix(items, load_run_findings([first, second]))
    weak = next(c for c in matrix.cells if c.item.subject_id == "loop-weak" and c.identity == LOOP)
    assert weak.circular and weak.cell == "catch"
    row = next(
        g for g in summarize(matrix.cells) if g["detector"] == LOOP and g["source"] == "run-health"
    )
    assert (row["n_pos"], row["catch"], row["circular"]) == (1, 1, 1)


def test_union_and_outputs(
    scenario: tuple[list[IssueItem], Path, Path, Path], tmp_path: Path
) -> None:
    items, first, second, tasks = scenario
    matrix = build_matrix(
        items,
        load_run_findings([first, second]),
        load_task_findings([tasks]),
        unions=[["cascade", "scout"]],
    )
    union = "union(cascade+scout)"
    cells = _cells(matrix)
    assert cells[("pos-caught", union)] == "catch"
    assert cells[("loop-pos", union)] == "catch"  # only scout covers loop
    assert cells[("neg-alarmed", union)] == "false_alarm"
    out = tmp_path / "out"
    summary = write_outputs(
        matrix, out, items=items, findings_paths=[first, second], task_findings_paths=[tasks]
    )
    group = next(g for g in summary["groups"] if g["detector"] == CASCADE and g["source"] == "fake")
    assert (group["n_pos"], group["catch"], group["miss"], group["abstain"]) == (6, 1, 1, 1)
    assert (group["unknown"], group["not_run"], group["n_neg"], group["false_alarm"]) == (
        2,
        1,
        2,
        1,
    )
    assert group["recall"] == 0.5 and group["recall_ci"] == wilson(1, 2)
    rows = (out / "cells.jsonl").read_text().splitlines()
    assert len(rows) == len(matrix.cells)
    misses = [json.loads(line) for line in (out / "misses.jsonl").read_text().splitlines()]
    assert {m["subject_id"] for m in misses} >= {"pos-missed", "task-c"}
    assert all(m["detector"] != union for m in misses)
    assert (out / "false_alarms.jsonl").read_text()
    assert "| " + CASCADE in markdown(summary["groups"])


def test_unknown_union_member_raises(scenario: tuple[list[IssueItem], Path, Path, Path]) -> None:
    items, first, second, _ = scenario
    with pytest.raises(ValueError, match="matches no detector identity"):
        build_matrix(items, load_run_findings([first, second]), unions=[["nope"]])


def test_wilson_bounds() -> None:
    assert wilson(0, 0) is None
    low, high = wilson(5, 10) or [0, 0]
    assert 0.23 < low < 0.24 and 0.76 < high < 0.77
    assert Identity("d", "v", "p/m", "x", "task").label == "d@v[p/m]#x"


@pytest.mark.parametrize("verdict", ["alarm", "clear", "abstain", "unknown"])
@pytest.mark.parametrize("polarity", ["positive", "negative"])
def test_missing_actor_is_unknown_even_with_cached_alarm(
    tmp_path: Path, verdict: str, polarity: str
) -> None:
    item = _item("without-actor", actor_transcript=False, polarity=polarity)
    findings = _write(
        tmp_path / "findings.jsonl",
        [_finding(item.subject_id, "cascade", "v8", verdict, reader="kimi-code/k3")],
    )
    matrix = build_matrix([item], load_run_findings([findings]))
    assert _cells(matrix) == {(item.subject_id, CASCADE): "unknown"}
    assert matrix.cells[0].reason == "no_actor_transcript"


def test_criterion_negative_requires_item_specific_task_finding(tmp_path: Path) -> None:
    item = _item(
        "criterion-only",
        source="clean-controls",
        kind="grader_unfair_fail",
        polarity="negative",
        subject_type="task",
        granularity="task",
        task_id="task-a",
        locator="task-a/underspecified",
        actor_transcript=None,
    )
    row = {
        "task_id": "task-a",
        "detector": "taskqa_gate",
        "detector_version": "g1",
        "arm": "arm-a",
        "verdict": "alarm",
        "kind": "grader_unfair_fail",
    }
    broad = _write(tmp_path / "broad.jsonl", [row])
    matrix = build_matrix([item], {}, load_task_findings([broad]))
    assert _cells(matrix) == {(item.subject_id, GATE): "not_run"}
    exact = _write(tmp_path / "exact.jsonl", [{**row, "item_ids": [item.item_id]}])
    matrix = build_matrix([item], {}, load_task_findings([exact]))
    assert _cells(matrix) == {(item.subject_id, GATE): "false_alarm"}


def test_unobserved_detector_does_not_silently_disappear(tmp_path: Path) -> None:
    trace = _item("not-read")
    pair = _item(
        "repair-pair",
        kind="grader_repair",
        subject_type="repair",
        granularity="repair_pair",
        actor_transcript=None,
    )
    matrix = build_matrix([trace, pair], {}, expected=[Identity("laminar", "not_observed")])
    assert _cells(matrix) == {(trace.subject_id, "laminar@not_observed"): "not_run"}
    summary = write_outputs(matrix, tmp_path, items=[trace, pair])
    assert summary["items_with_cells"] == 1
    assert summary["items_without_cells"] == 1
    omitted = [
        json.loads(line) for line in (tmp_path / "unscored_items.jsonl").read_text().splitlines()
    ]
    assert omitted == [
        {
            "item_id": pair.item_id,
            "source": pair.source,
            "kind": "grader_repair",
            "subject_type": "repair",
            "subject_id": pair.subject_id,
            "reason": "repair_pair_requires_bound_before_after_runs",
        }
    ]


def test_command_rechecks_declared_dev_items_against_current_cross_key_seals(
    tmp_path: Path,
) -> None:
    from evallab.failure_library.commands import bank_matrix

    ledger = tmp_path / "research/answer-keys/splits/assignments.csv"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(
        "key_id,item_id,task,split\n"
        "tb4-audit-v1,allowed,permitted-task,dev\n"
        "tb4-audit-v1,held,protected-task,test\n",
        encoding="utf-8",
    )
    for relative in (
        "tb4-audit-v1/items.jsonl",
        "tb4-audit-v1/tasks.jsonl",
        "cheatbench-actions-v1/items.jsonl",
        "hacktrace-act-v1/items.jsonl",
    ):
        path = tmp_path / "research/answer-keys" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    inventory = tmp_path / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        json.dumps(
            {
                "dataset_id": "mimo-defect-map-v1",
                "heldout_task_ids_excluded_from_training_all_versions": [],
                "tasks": [],
            }
        ),
        encoding="utf-8",
    )
    items = _write(
        tmp_path / "items.jsonl",
        [
            _item("allowed", task_id="permitted-task").model_dump(mode="json"),
            _item("held", task_id="protected-task").model_dump(mode="json"),
        ],
    )
    out = tmp_path / "matrix"
    args = argparse.Namespace(
        items=items,
        build_sources=None,
        source=None,
        state_root=None,
        findings=[],
        task_findings=[],
        union=[],
        out=out,
        all_rows=False,
    )
    assert bank_matrix._run(args, tmp_path) == 0
    admission = json.loads((out / "admission.json").read_text())
    assert admission["admitted_dev_items"] == 1
    assert admission["excluded_by_effective_split"] == {"TEST": 1}
    allowed = _item("allowed", task_id="permitted-task").item_id
    assert {
        json.loads(line)["item_id"] for line in (out / "cells.jsonl").read_text().splitlines()
    } == {allowed}
    assert {
        json.loads(line)["cell"] for line in (out / "cells.jsonl").read_text().splitlines()
    } == {"not_run"}
