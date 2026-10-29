from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evallab.failure_library.bank import BankContext, IssueItem
from evallab.failure_library.bank.sources import seeker as source
from evallab.failure_library.records import Split
from evallab.failure_library.sources import normalize_split

SCHEMA = """
CREATE TABLE corpus_tasks (task_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL,
    split TEXT NOT NULL, instruction_text TEXT NOT NULL DEFAULT '',
    original_grader_digest TEXT, repaired_grader_digest TEXT, cas_uri TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE agent_traces (trace_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL);
"""


@dataclass
class StubContext(BankContext):
    """A ``BankContext`` whose split authority is a set of sealed task identities."""

    sealed: frozenset[str] = frozenset()

    def strict_split(
        self,
        declared: str,
        *,
        key_id: str | None = None,
        item_id: str | None = None,
        task_id: str | None = None,
        task_digest: str | None = None,
    ) -> Split:
        split = normalize_split(declared)
        if split != "DEV":
            return split
        if {item_id, task_id, task_digest} & self.sealed:
            return "TEST"
        return "DEV"


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _cloud(tmp_path: Path, traces: tuple[str, ...] = ()) -> Path:
    path = tmp_path / "cloud.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT INTO corpus_tasks (task_id, dataset_id, split) VALUES"
        " ('terminal-bench/cad-model', 'tb4-audit-v1', 'DEV')"
    )
    for trace in traces:
        connection.execute(
            "INSERT INTO agent_traces VALUES (?, ?, 'terminal-bench/cad-model')", (trace, trace)
        )
    connection.commit()
    connection.close()
    return path


def _label(run_id: str, verdict: str = "yes", *, receipts: bool = True, **extra: Any) -> dict:
    row: dict[str, Any] = {
        "schema": source.LABEL_SCHEMA,
        "run_id": run_id,
        "source": "seeker",
        "dataset_id": "tb4-audit-v1",
        "task_id": "terminal-bench/cad-model",
        "admission": {"effective_split": "dev"},
        "source_label": {"judged": True, "verdict": verdict, "verdict_enacted": verdict},
        "passage_step_labels": {"positive_step_indices": [3]},
        "evidence": {"original": {"reward": 1}, "repaired": {"reward": 0}} if receipts else {},
        "candidate_digest": "sha256:abc",
    }
    row.update(extra)
    return row


def _build(tmp_path: Path, sealed: frozenset[str] = frozenset(), cloud: Path | None = None) -> dict:
    ctx = StubContext(
        repo_root=tmp_path,
        prod_live=tmp_path / "live",
        cloud_db=cloud or tmp_path / "absent.sqlite",
        sealed=sealed,
    )
    items = [item for item in source.build(ctx) if isinstance(item, IssueItem)]
    return {(item.subject_id, item.kind, item.locator): item for item in items}


@pytest.fixture(autouse=True)
def results_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "results"
    monkeypatch.setenv(source.RESULTS_ROOT_ENV, str(root))
    return root


def test_known_hack_polarity_label_type_and_actor(tmp_path: Path) -> None:
    live = tmp_path / "live" / "seeker-known-hacks"
    _write_jsonl(
        live / "labels-v1.jsonl",
        [
            _label("run-yes"),
            _label("run-ledger", receipts=False),
            _label("run-no", verdict="no"),
            _label("run-unjudged", source_label={"judged": False, "verdict": "yes"}),
            _label("run-pending", verdict="maybe"),
        ],
    )
    _write_jsonl(
        live / "runs-v1.jsonl",
        [
            {
                "run_id": "run-yes",
                "doc": {
                    "steps": [
                        {"source": "user", "message": "task"},
                        {
                            "source": "agent",
                            "message": "",
                            "tool_calls": [{"function_name": "bash"}],
                        },
                    ]
                },
            }
        ],
    )
    items = _build(tmp_path, cloud=_cloud(tmp_path, traces=("run-ledger",)))

    assert {key[0] for key in items} == {"run-yes", "run-ledger", "run-no"}
    yes, ledger, no = (items[(run, "cheat", "")] for run in ("run-yes", "run-ledger", "run-no"))
    assert (yes.polarity, yes.label_type, yes.granularity, yes.positive_units) == (
        "positive",
        "executed",
        "run",
        1,
    )
    assert yes.actor_transcript is True and yes.harness_selector is None
    # A verdict without both native measured receipts is a recorded verdict, not an execution.
    assert (ledger.polarity, ledger.label_type) == ("positive", "judge")
    assert ledger.harness_selector == "run-ledger" and ledger.actor_transcript is True
    # Negative only because the receipt labels it not-hack.
    assert (no.polarity, no.positive_units, no.actor_transcript) == ("negative", None, False)
    assert yes.provenance.digest and yes.provenance.ref == "labels-v1.jsonl#1"


def test_known_hack_split_is_tightened_never_upgraded(tmp_path: Path) -> None:
    live = tmp_path / "live" / "seeker-known-hacks"
    sealed = _label("run-sealed", task_id="terminal-bench/sealed-task")
    declared_test = _label("run-test", admission={"effective_split": "TEST"})
    _write_jsonl(live / "labels-v1.jsonl", [_label("run-ok"), sealed, declared_test])
    items = _build(tmp_path, sealed=frozenset({"terminal-bench/sealed-task"}))

    assert items[("run-ok", "cheat", "")].split == "DEV"
    assert items[("run-sealed", "cheat", "")].split == "TEST"
    assert items[("run-sealed", "cheat", "")].declared_split == "dev"
    assert items[("run-test", "cheat", "")].split == "TEST"


def _write_cells(results_root: Path, rows: list[dict[str, Any]], pilot: str = "sb1-pilot") -> Path:
    root = results_root / "2026-10-28" / pilot
    _write_jsonl(root / "cells.jsonl", rows)
    return root


def _cell(
    task: str,
    model: str,
    turn_1: str,
    turn_2: str = "not_evaluated_no_runner_error",
    differential: bool = False,
) -> dict[str, Any]:
    return {
        "dataset_id": "tb4-audit-v1",
        "task": task,
        "model": model,
        "turn_1": {"outcome": turn_1, "verified_differential": differential},
        "turn_2": {"outcome": turn_2, "verified_differential": False},
    }


def test_sb1_infra_outcomes_become_executed_infra_positives(
    tmp_path: Path, results_root: Path
) -> None:
    model = "anthropic/claude-sonnet-5-5"
    root = _write_cells(
        results_root,
        [
            _cell(
                "cad-model", model, "original_grader_unavailable", "generation_failed_or_truncated"
            ),
            _cell("cad-model", "meta/muse-spark-1.3-contributor", "served_route_mismatch"),
            _cell("fin-saccr-rwa", model, "upstream_failed", "upstream_failed"),
        ],
    )
    response = root / "tb4-audit-v1/cad-model/anthropic_claude-sonnet-5-5/turn-1/response.jsonl"
    response.parent.mkdir(parents=True)
    response.write_text('{"text": "hi"}\n', encoding="utf-8")

    items = _build(tmp_path, cloud=_cloud(tmp_path))

    assert len(items) == 3  # upstream_failed is a measured grader rejection: not an item
    first = items[(f"sb1-pilot:tb4-audit-v1/cad-model/{model}", "infra", "turn_1")]
    second = items[(f"sb1-pilot:tb4-audit-v1/cad-model/{model}", "infra", "turn_2")]
    mismatch = items[
        ("sb1-pilot:tb4-audit-v1/cad-model/meta/muse-spark-1.3-contributor", "infra", "turn_1")
    ]
    assert (first.polarity, first.label_type, first.granularity) == ("positive", "executed", "run")
    assert first.label == "original_grader_unavailable"
    assert first.task_id == "terminal-bench/cad-model"  # resolved through the cloud task table
    assert (first.actor_transcript, second.actor_transcript, mismatch.actor_transcript) == (
        True,
        False,
        False,
    )
    assert first.harness_selector is None
    assert first.provenance.ref == "2026-10-28/sb1-pilot/cells.jsonl#1:turn_1"
    assert first.split == "DEV"


def test_sb1_verified_differential_is_a_wrong_pass_positive(
    tmp_path: Path, results_root: Path
) -> None:
    _write_cells(
        results_root, [_cell("cad-model", "m/x", "differential_confirmed", differential=True)]
    )
    items = _build(tmp_path)

    (item,) = items.values()
    assert (item.kind, item.polarity, item.label_type) == (
        "grader_wrong_pass",
        "positive",
        "executed",
    )
    assert item.task_id == "cad-model"  # no cloud DB: the cell's own name


def test_sb1_rerun_cell_is_a_distinct_run_with_unknown_actor_binding(
    tmp_path: Path, results_root: Path
) -> None:
    root = _write_cells(
        results_root,
        [
            _cell(
                "cad-model", "m/x", "original_grader_unavailable", "generation_failed_or_truncated"
            ),
            _cell("cad-model", "m/x", "served_route_mismatch", "generation_failed_or_truncated"),
        ],
    )
    response = root / "tb4-audit-v1/cad-model/m_x/turn-1/response.jsonl"
    response.parent.mkdir(parents=True)
    response.write_text('{"text": "latest attempt only"}\n', encoding="utf-8")

    items = _build(tmp_path)

    subject = "sb1-pilot:tb4-audit-v1/cad-model/m/x"
    assert {key: item.label for key, item in items.items() if key[2] == "turn_1"} == {
        (subject, "infra", "turn_1"): "original_grader_unavailable",
        (subject + "#attempt-2", "infra", "turn_1"): "served_route_mismatch",
    }
    assert {item.actor_transcript for item in items.values()} == {None}


def test_sb1_sealed_task_is_never_dev(tmp_path: Path, results_root: Path) -> None:
    _write_cells(results_root, [_cell("cad-model", "m/x", "served_route_mismatch")])
    _write_cells(
        results_root, [_cell("cad-model", "m/y", "served_route_mismatch")], pilot="sb1-pilot-b"
    )

    items = _build(tmp_path, sealed=frozenset({"cad-model"}))

    assert len(items) == 2
    assert {item.split for item in items.values()} == {"TEST"}
    assert {item.declared_split for item in items.values()} == {"DEV"}
