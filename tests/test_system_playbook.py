"""The system playbook is built only from recorded results and stays inside its cohorts."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from test_prod_cli import _ts4_cells, _ts4_manifest, _write_ts4_cell

from evallab.production.benchmark_reports import validate_benchmark_document
from evallab.production.qc_reports import system_playbook

STAMP = "2026-10-27T10:00:00Z"
JUDGE = "provider/new-judge"


def _write(path: Path, document: Any) -> None:
    path.write_text(json.dumps(document), encoding="utf-8")


def _oq5_directory(directory: Path) -> Path:
    def arm(*, keys: int, matched: int, planned_keys: int = 16, tasks: int = 8) -> dict[str, Any]:
        return {
            "planned_tasks": 8,
            "complete_tasks": tasks,
            "planned_keys": planned_keys,
            "evaluable_keys": keys,
            "matched_keys": matched,
            "accepted_findings": 1,
        }

    def calls(attempts: int, tokens_in: int, tokens_out: int) -> dict[str, Any]:
        return {
            "attempts": attempts,
            "missing_receipts": 0,
            "missing_tokens_in": 0,
            "missing_tokens_out": 0,
            "observed_tokens_in": tokens_in,
            "observed_tokens_out": tokens_out,
            "observed_cost_usd": None,
            "cost_usd_applicability": "not_applicable_subscription",
            "served_identities": [{"provider": "provider", "model": "solver"}],
        }

    directory.mkdir()
    freeze = {
        "selected_arm": "A",
        "protocol": {"model": "provider/solver", "confidence_threshold": 0.8},
    }
    _write(directory / "freeze.json", freeze)
    _write(
        directory / "dev-summary.json",
        {
            "selected_arm": "A",
            "arms": {"A": arm(keys=16, matched=0), "B": arm(keys=9, matched=1, tasks=7)},
            "by_dataset": {"dataset-a": {}, "dataset-b": {}},
            "arm_calls": {"A": calls(9, 125_000, 18_000), "B": calls(9, 172_000, 17_000)},
            "calls": calls(18, 297_000, 35_000),
            "paired_metrics": {"A": arm(keys=9, matched=0), "B": arm(keys=9, matched=1)},
            "complete_paired_task_ids": ["t1", "t2", "t3", "t4", "t5", "t6", "t7"],
        },
    )
    _write(
        directory / "cross-summary.json",
        {
            "freeze_sha256": hashlib.sha256((directory / "freeze.json").read_bytes()).hexdigest(),
            "TEST_exposure": 0,
            "new_grader_executions": 0,
            "metrics": arm(keys=24, matched=2, planned_keys=28, tasks=12) | {"planned_tasks": 15},
            "calls": calls(12, 123_000, 16_000),
            "rows": [{"dataset_id": "dataset-c"}],
        },
    )
    return directory


def _scored(
    corpus: str, arm: str, *, tp: int, fp: int, stage1_tp: int, stage1_fp: int, cm5: bool
) -> dict[str, Any]:
    stage1 = "stage1_any" if cm5 else "stage1"
    rows = [
        {"positive": True, "stratum": "enacted_yes", stage1: i < stage1_tp, "alarmed": i < tp}
        for i in range(10)
    ]
    rows += [
        {"positive": False, "stratum": "clean_no", stage1: i < stage1_fp, "alarmed": i < fp}
        for i in range(20)
    ]
    rows += [
        {"positive": False, "stratum": "considered_only", stage1: False, "alarmed": False}
        for _ in range(5)
    ]
    summary = {
        "corpus": corpus,
        "positives": 10,
        "negatives": 20,
        "traces": 35,
        "excluded_unavailable": 0,
        "stage1_any_card" if cm5 else "stage1": {
            "recall": round(100 * stage1_tp / 10, 1),
            "far": round(100 * stage1_fp / 20, 1),
        },
        "cascade": {"tp": tp, "fp": fp},
        "judged_cards" if cm5 else "judge_calls": 9 + tp,
        "input_tokens": 1000 + tp,
        "output_tokens": 400,
    }
    if not cm5:
        summary |= {"arm": arm, "notional_usd": 0.01 * (tp + 1)}
    return {"summary": summary, "traces": rows}


def _cm_directories(root: Path) -> tuple[Path, Path]:
    cm4, cm5 = root / "cm4", root / "cm5"
    cm4.mkdir()
    cm5.mkdir()
    _write(
        cm4 / "crosskey.json",
        {
            key: {"identities": 35, "assigned_test_anywhere": 0, "sealed_identity": 0}
            for key in ("goodfire_sample", "cheatbench-actions-v1")
        },
    )
    for corpus, plain, examples in (("goodfire", (3, 0), (2, 0)), ("cheatbench", (1, 0), (1, 1))):
        for arm, (tp, fp) in (("plain", plain), ("with_examples", examples)):
            _write(
                cm4 / f"score2-{corpus}-{arm}.json",
                _scored(corpus, arm, tp=tp, fp=fp, stage1_tp=6, stage1_fp=8, cm5=False),
            )
    (cm4 / "judge-receipts.jsonl").write_text(
        json.dumps({"arm": "plain", "served": "provider/old-judge", "fallback": False}) + "\n",
        encoding="utf-8",
    )
    _write(
        cm5 / "freeze.json",
        {
            "cascade_version": "cascade/v3",
            "frozen_arm": "pf_all",
            "judge": JUDGE,
            "note": "frozen on the tune cohort before any cross-test result",
        },
    )
    for corpus, frozen, base in (("goodfire", (4, 1), (2, 1)), ("cheatbench", (3, 0), (1, 0))):
        for arm, (tp, fp) in (("pf_all", frozen), ("base", base)):
            _write(
                cm5 / f"e2escore-{corpus}-{arm}.json",
                _scored(corpus, arm, tp=tp, fp=fp, stage1_tp=7, stage1_fp=5, cm5=True),
            )
    (cm5 / "judge-receipts.jsonl").write_text(
        "".join(json.dumps({"served": JUDGE, "fallback": False}) + "\n" for _ in range(3)),
        encoding="utf-8",
    )
    return cm4, cm5


def _seeker_cells(cells: Path) -> Path:
    _ts4_cells(cells)
    # The runner-accurate turn finds b1, whose own turn-1 cell was served by another model.
    _write_ts4_cell(cells, "ds-b", "b1", "turn-2-runner-accurate")
    result = cells / "ds-a" / "a1" / "provider_model-one" / "turn-1" / "result.json"
    cell = json.loads(result.read_text())
    cell |= {"input_tokens": 700, "output_tokens": 70, "cost_usd": 0.5}
    _write(result, cell)
    return cells


def _build(root: Path) -> dict[str, Any]:
    cm4, cm5 = _cm_directories(root)
    return system_playbook(
        oq5_directory=_oq5_directory(root / "oq5"),
        cm4_directory=cm4,
        cm5_directory=cm5,
        ts4_manifest=_ts4_manifest(),
        ts4_cells=_seeker_cells(root / "cells"),
        generated_at=STAMP,
    )


def _result(row: dict[str, Any]) -> tuple[int, int]:
    return row["hits_n"], row["of_n"]


def test_playbook_reports_each_system_from_its_recorded_counts(tmp_path: Path) -> None:
    document = _build(tmp_path)
    assert validate_benchmark_document(json.dumps(document).encode()) == document
    finder, seeker, monitor = document["systems"]

    tune, cross = finder["runs"]
    assert (tune["role"], _result(tune["result"]), tune["result"]["planned_of_n"]) == (
        "tune",
        (0, 16),
        16,
    )
    assert (cross["role"], _result(cross["result"]), cross["result"]["planned_of_n"]) == (
        "cross_test",
        (2, 24),
        28,
    )
    assert cross["result"]["cost"]["input_tokens_n"] == 123_000
    assert cross["result"]["cost"]["usd"] is None
    assert "subscription" in cross["result"]["cost"]["usd_basis"]
    history = finder["flags"][0]["variants"]
    assert [_result(v) for v in history] == [(0, 9), (1, 9)]
    assert [v["cost"]["input_tokens_n"] for v in history] == [125_000, 172_000]
    assert "1 finding" in " ".join(finder["caveats"]) and "4 of 28 keys" in " ".join(
        finder["caveats"]
    )

    tune, cross = monitor["runs"]
    assert (tune["role"], _result(tune["result"]), tune["result"]["false_alarm_n"]) == (
        "tune",
        (4, 10),
        1,
    )
    assert (cross["role"], _result(cross["result"]), cross["result"]["false_alarm_n"]) == (
        "cross_test",
        (3, 10),
        0,
    )
    # Stage 1 and the cascade are reported separately, and unscored traces are in neither rate.
    assert (tune["result"]["stage_one_hits_n"], tune["result"]["stage_one_false_alarm_n"]) == (7, 5)
    assert tune["result"]["false_alarm_of_n"] == 20
    assert tune["result"]["cost"]["calls_n"] == 13
    flags = {
        (f["flag"][:7], f["role"]): [_result(v) for v in f["variants"]] for f in monitor["flags"]
    }
    assert flags[("Command", "tune")] == [(2, 10), (4, 10)]
    assert flags[("Referen", "cross_test")] == [(1, 10), (1, 10)]
    examples = [
        f
        for f in monitor["flags"]
        if f["flag"].startswith("Reference") and f["role"] == "cross_test"
    ]
    assert [v["false_alarm_n"] for v in examples[0]["variants"]] == [0, 1]
    assert monitor["served_models"] == ["provider/new-judge", "provider/old-judge"]

    # One task counts once across turns; a differential on an unattested cell never scores.
    (run,) = seeker["runs"]
    assert run["role"] == "dev_only" and run["frozen"] is False
    (flag,) = seeker["flags"]
    assert [_result(v) for v in flag["variants"]] == [(1, 4), (1, 4), (2, 4)]
    assert run["result"]["label"].startswith("provider/model-one · on: + turn 2, runner-accurate")
    first = flag["variants"][0]["cost"]
    assert (first["input_tokens_n"], first["usd"], first["complete"]) == (700, None, False)
    assert seeker["served_models"] == ["provider/model-one"]
    assert not any(r["role"] == "cross_test" for r in seeker["runs"])


def test_playbook_refuses_unbound_or_inconsistent_inputs(tmp_path: Path) -> None:
    def rebuild(name: str, mutate: Any) -> None:
        root = tmp_path / name
        root.mkdir()
        cm4, cm5 = _cm_directories(root)
        oq5 = _oq5_directory(root / "oq5")
        mutate(oq5, cm4, cm5)
        system_playbook(
            oq5_directory=oq5,
            cm4_directory=cm4,
            cm5_directory=cm5,
            ts4_manifest=_ts4_manifest(),
            ts4_cells=_seeker_cells(root / "cells"),
            generated_at=STAMP,
        )

    def edit(path: Path, change: Any) -> None:
        document = json.loads(path.read_text())
        change(document)
        _write(path, document)

    def rebind(oq5: Path, *_: Path) -> None:
        edit(oq5 / "freeze.json", lambda d: d["protocol"].update(confidence_threshold=0.5))

    def exposed(oq5: Path, *_: Path) -> None:
        edit(oq5 / "cross-summary.json", lambda d: d.update(TEST_exposure=1))

    def fallback(_: Path, __: Path, cm5: Path) -> None:
        (cm5 / "judge-receipts.jsonl").write_text(
            json.dumps({"served": JUDGE, "fallback": True}) + "\n", encoding="utf-8"
        )

    def other_judge(_: Path, __: Path, cm5: Path) -> None:
        edit(cm5 / "freeze.json", lambda d: d.update(judge="provider/someone-else"))

    def miscounted(_: Path, __: Path, cm5: Path) -> None:
        edit(cm5 / "e2escore-goodfire-pf_all.json", lambda d: d["summary"]["cascade"].update(tp=9))

    def non_dev(_: Path, cm4: Path, __: Path) -> None:
        edit(cm4 / "crosskey.json", lambda d: d["goodfire_sample"].update(assigned_test_anywhere=1))

    for name, mutate, message in (
        ("rebind", rebind, "not bound to this freeze receipt"),
        ("exposed", exposed, "protected-split exposure"),
        ("fallback", fallback, "fallback"),
        ("judge", other_judge, "frozen judge"),
        ("miscount", miscounted, "disagrees with the summary"),
        ("nondev", non_dev, "non-DEV"),
    ):
        with pytest.raises(ValueError, match=message):
            rebuild(name, mutate)
