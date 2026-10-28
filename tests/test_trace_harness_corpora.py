"""Corpus loaders and label joins for the trace harness, on synthetic fixtures only."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from evallab.production import trace_harness_corpora as corpora
from evallab.production.run_atlas import CHEATBENCH_DATASET, GOODFIRE_DATASET

# DDL copied from cloud_trace_db.CloudTraceDatabase._initialize (the columns the labels read).
_DDL = """
CREATE TABLE corpus_tasks (
    task_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL, split TEXT NOT NULL,
    instruction_text TEXT NOT NULL, original_grader_digest TEXT,
    repaired_grader_digest TEXT, cas_uri TEXT, metadata_json TEXT NOT NULL
);
CREATE TABLE agent_traces (
    trace_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
    task_id TEXT NOT NULL REFERENCES corpus_tasks(task_id), model_id TEXT NOT NULL,
    outcome TEXT NOT NULL, reward DOUBLE PRECISION,
    is_exploit INTEGER NOT NULL, is_task_defect_fail INTEGER NOT NULL,
    step_count INTEGER NOT NULL CHECK(step_count >= 0), raw_cas_uri TEXT,
    verified_receipt_json TEXT, trust_tier TEXT NOT NULL DEFAULT 'tier3_provisional',
    flagged_passage_steps TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE trace_steps (
    trace_id TEXT NOT NULL REFERENCES agent_traces(trace_id) ON DELETE CASCADE,
    step_index INTEGER NOT NULL CHECK(step_index >= 0), role TEXT NOT NULL,
    tool_name TEXT, command_or_action TEXT, content_text TEXT,
    observation_preview TEXT, observation_cas_uri TEXT, flags_json TEXT NOT NULL,
    PRIMARY KEY(trace_id, step_index)
);
"""


def _gf(name: str) -> str:
    return f"{GOODFIRE_DATASET}/camp/{name}"


YES, NO, ONLY, UNJUDGED, HELD = (_gf(n) for n in ("yes", "no", "only", "unjudged", "held"))
P, Q, R = (f"{CHEATBENCH_DATASET}:{n}" for n in ("p", "q", "r"))
CB_TEST = f"{CHEATBENCH_DATASET}:heldout"


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``(repo_root, database, goodfire_sample)`` with admitted DEV and sealed TEST rows."""
    root = tmp_path / "repo"
    keys = root / "research/answer-keys"
    (keys / "splits").mkdir(parents=True)
    (keys / CHEATBENCH_DATASET).mkdir()
    for relative in ("tb4-audit-v1/items.jsonl", "tb4-audit-v1/tasks.jsonl", "hacktrace-act-v1/items.jsonl"):
        (keys / relative).parent.mkdir(parents=True, exist_ok=True)
        (keys / relative).write_text("", encoding="utf-8")
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1", "tasks": [],
        "heldout_task_ids_excluded_from_training_all_versions": [],
    }))
    items = [{
        "item_id": run_id, "key_id": CHEATBENCH_DATASET, "split": "test" if run_id == CB_TEST else "dev",
        "expected_verdict": "fail" if run_id in (P, CB_TEST) else "pass",
        "subject": {"task_id": f"cheatbench/task-{run_id.split(':')[1]}"},
    } for run_id in (P, Q, R, CB_TEST)]
    (keys / CHEATBENCH_DATASET / "items.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in items), encoding="utf-8",
    )
    (keys / "splits/assignments.csv").write_text(
        "key_id,item_id,task,split\n" + "".join(
            f"{CHEATBENCH_DATASET},{row['item_id']},{row['subject']['task_id']},{row['split']}\n"
            for row in items
        ),
    )
    database = tmp_path / "cloud.sqlite"
    strata = {YES: "enacted_yes", NO: "clean_no", ONLY: "considered_only",
              UNJUDGED: "unjudged", HELD: "enacted_yes"}
    with sqlite3.connect(database) as connection:
        connection.executescript(_DDL)
        for run_id, stratum in strata.items():
            name = run_id.rsplit("/", 1)[1]
            task_id = f"goodfire/task-{name}"
            held = run_id == HELD
            receipt = {
                "admission": {"effective_split": "TEST" if held else "DEV"},
                "source_label": {"judged": stratum != "unjudged", "verdict": "no" if stratum == "clean_no" else "yes",
                                 "verdict_enacted": "yes" if stratum == "enacted_yes" else "no"},
                "passage_step_labels": {"positive_step_indices": [4] if stratum == "enacted_yes" else []},
            }
            connection.execute(
                "INSERT INTO corpus_tasks VALUES (?, ?, ?, 'fixture', NULL, NULL, NULL, ?)",
                (task_id, GOODFIRE_DATASET, "TEST" if held else "DEV", json.dumps({"item_id": f"native-{name}"})),
            )
            connection.execute(
                "INSERT INTO agent_traces (trace_id, run_id, task_id, model_id, outcome, reward,"
                " is_exploit, is_task_defect_fail, step_count, verified_receipt_json)"
                " VALUES (?, ?, ?, 'fixture-agent', 'unknown', NULL, 0, 0, 8, ?)",
                (run_id, f"fixture/{name}", task_id, json.dumps(receipt)),
            )
            # roles: 0 user, then assistant at even indices, tool at odd; index 5 is skipped.
            connection.executemany(
                "INSERT INTO trace_steps (trace_id, step_index, role, content_text, flags_json)"
                " VALUES (?, ?, ?, 'fixture', '{}')",
                [(run_id, step, "user" if step == 0 else "assistant" if step % 2 == 0 else "tool")
                 for step in range(8) if step != 5],
            )
    sample = tmp_path / "goodfire-sample.json"
    sample.write_text(json.dumps([{"trace_id": run_id} for run_id in (YES, NO, ONLY, UNJUDGED)]))
    return root, database, sample


def test_goodfire_cohort_labels_gold_and_assistant_ordinals(world: tuple[Path, Path, Path]) -> None:
    root, database, sample = world
    labels = corpora.load_corpus_labels(
        "goodfire", database=database, repo_root=root, goodfire_sample=sample,
    )
    assert labels.run_ids == {YES, NO, ONLY, UNJUDGED}
    # considered-only and the unjudged cohort member are unknown, never negatives
    assert dict(labels.positive) == {YES: True, NO: False, ONLY: None, UNJUDGED: None}
    assert dict(labels.positive_steps) == {YES: frozenset({4})}
    assert labels.localization_unit == "assistant_turn"
    # ordinals over role='assistant' rows only, in native step_index order
    assert dict(labels.turn_ordinals) == {YES: {2: 0, 4: 1, 6: 2}}


def test_goodfire_dev_covers_admitted_judged_runs_only(world: tuple[Path, Path, Path]) -> None:
    root, database, _ = world
    labels = corpora.load_corpus_labels("goodfire-dev", database=database, repo_root=root)
    assert labels.run_ids == {YES, NO, ONLY}
    assert dict(labels.positive) == {YES: True, NO: False, ONLY: None}


def test_goodfire_refuses_test_ids(world: tuple[Path, Path, Path], tmp_path: Path) -> None:
    root, database, _ = world
    sample = tmp_path / "held-sample.json"
    sample.write_text(json.dumps([{"trace_id": YES}, {"trace_id": HELD}]))
    with pytest.raises(ValueError, match="TEST|non-DEV"):
        corpora.load_corpus_labels("goodfire", database=database, repo_root=root, goodfire_sample=sample)


def test_cheatbench_dev_items_positive_iff_expected_fail(world: tuple[Path, Path, Path]) -> None:
    root, database, _ = world
    labels = corpora.load_corpus_labels("cheatbench", database=database, repo_root=root)
    assert labels.run_ids == {P, Q, R}  # the sealed TEST item never appears
    assert dict(labels.positive) == {P: True, Q: False, R: False}
    assert not labels.positive_steps and not labels.turn_ordinals
    assert labels.localization_unit == "step"


def test_unknown_corpus_is_rejected(world: tuple[Path, Path, Path]) -> None:
    root, database, _ = world
    with pytest.raises(ValueError, match="unknown corpus"):
        corpora.load_corpus_labels("nope", database=database, repo_root=root)


# --------------------------------------------------------------------------- #
# HackBench command layout (synthetic transcripts; no real HackBench text)
# --------------------------------------------------------------------------- #


def _transcript(*blocks: str) -> str:
    return "\n\n".join(blocks)


# Turn 1: assistant text + two commands + one shared observation. Turn 2: assistant text only
# (a text-only step), then one command. The text-only step makes step ids differ from command
# numbers, which is the property the id-space contract exists for.
_ROW_A = _transcript(
    "[assistant] plan alpha",
    "[CMD 1] [command] ls",
    "[CMD 2] [command] cat notes",
    "[observation] result one",
    "[assistant] note beta",
    "[assistant] plan gamma",
    "[CMD 3] [tool: bash_command] echo hi",
    "[tool_result] result two",
)
_ROW_B = _transcript(
    "[reasoning] think",
    "[assistant] only turn",
    "[CMD 1] [tool: mark_task_complete] done",
)


def _row(row_id: str, origin: str, transcript: str, commands: int, gold: list[int]) -> dict:
    return {
        "id": row_id, "origin": origin, "gold_command_indices": gold,
        "monitor_input": {"task_instruction": "synthetic task", "transcript": transcript,
                          "command_count": commands},
    }


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    root = tmp_path / "hackbench"
    data = root / corpora.HACKBENCH_DATA
    data.parent.mkdir(parents=True)
    rows = [
        _row("synthetic-a", "natural", _ROW_A, 3, [2, 3]),
        _row("synthetic-b", "synthetic", _ROW_B, 1, [1]),
    ]
    data.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return root


def test_command_layout_steps_and_gold_id_space(staging: Path) -> None:
    runs = list(corpora.hackbench_runs(root=staging))
    key = hashlib.sha256(b"synthetic-a").hexdigest()[:16]
    assert [run.run_id for run in runs] == [f"hackbench:{key}"]
    run = runs[0]
    assert (run.source, run.split, run.actor_inputs_available) == ("hackbench", "CROSS-TEST", True)
    # step 0 = user task; one command per agent step; the text-only step 3 shifts command 3 to step 4
    assert run.step_ids == [0, 1, 2, 3, 4]
    assert [bool(step.get("tool_calls")) for step in run.steps] == [False, True, True, False, True]
    assert run.steps[0]["source"] == "user"
    assert run.steps[2]["observation"]["results"][0]["content"] == "result one"
    assert run.blind().origins == run.step_ids  # blinding keeps the layout's step identities

    labels = corpora.load_corpus_labels(
        "hackbench", database=Path("/nonexistent"), repo_root=Path("."), hackbench_root=staging,
    )
    assert labels.run_ids == {run.run_id}
    assert dict(labels.positive) == {run.run_id: True}
    # gold commands {2, 3} live on steps {2, 4}: step ids are not command numbers
    assert dict(labels.positive_steps) == {run.run_id: frozenset({2, 4})}
    assert labels.localization_unit == "command"
    assert labels.turn_ordinals[run.run_id] == {1: 1, 2: 2, 4: 3}


def test_hackbench_all_includes_synthetic_and_limit(staging: Path) -> None:
    assert len(list(corpora.hackbench_runs(root=staging))) == 1
    runs = list(corpora.hackbench_all_runs(root=staging))
    assert [run.source for run in runs] == ["hackbench", "hackbench"]
    assert len(list(corpora.hackbench_all_runs(limit=1, root=staging))) == 1
    labels = corpora.load_corpus_labels(
        "hackbench-all", database=Path("/nonexistent"), repo_root=Path("."), hackbench_root=staging,
    )
    assert len(labels.run_ids) == 2 and all(labels.positive.values())


def test_runs_never_carry_gold(staging: Path) -> None:
    for run in corpora.hackbench_all_runs(root=staging):
        blob = json.dumps(list(run.steps))
        assert "gold" not in blob and "hack_type" not in blob


def test_gold_outside_layout_raises(tmp_path: Path) -> None:
    root = tmp_path / "hb"
    data = root / corpora.HACKBENCH_DATA
    data.parent.mkdir(parents=True)
    data.write_text(json.dumps(_row("bad", "natural", _ROW_B, 1, [9])) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="gold commands"):
        corpora.load_corpus_labels(
            "hackbench", database=Path("/nonexistent"), repo_root=Path("."), hackbench_root=root,
        )


def test_malformed_command_numbering_raises(tmp_path: Path) -> None:
    root = tmp_path / "hb"
    data = root / corpora.HACKBENCH_DATA
    data.parent.mkdir(parents=True)
    broken = _transcript("[assistant] x", "[CMD 2] [command] ls")
    data.write_text(json.dumps(_row("bad", "natural", broken, 1, [1])) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="CMD n"):
        list(corpora.hackbench_runs(root=root))


def test_missing_staging_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="HackBench staging"):
        list(corpora.hackbench_runs(root=tmp_path / "absent"))
