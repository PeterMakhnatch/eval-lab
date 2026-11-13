"""DEV label joins over the production SQLite column contract."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from evallab.production import run_atlas_score as atlas
from evallab.production.run_atlas import CHEATBENCH_DATASET, GOODFIRE_DATASET

# DDL copied from cloud_trace_db.CloudTraceDatabase._initialize, including its
# migrated trust/passage columns. No production DB, traces, or model calls.
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


def _gf(campaign: str, name: str) -> str:
    return f"{GOODFIRE_DATASET}/{campaign}/{name}"


A, B, C, D = (_gf("camp-a", "a"), _gf("camp-a", "b"), _gf("camp-b", "c"), _gf("camp-b", "d"))
E, F, G = _gf("camp-a", "e"), _gf("camp-b", "f"), _gf("camp-b", "g")
H, OUTSIDE, X = _gf("camp-a", "unjudged"), _gf("camp-c", "outside-sample"), _gf("camp-x", "heldout")
P, Q, R, S, T = (f"{CHEATBENCH_DATASET}:{name}" for name in ("p", "q", "r", "s", "heldout"))


@dataclass
class Fixture:
    root: Path
    database: Path

    def seal(self, *, item_id: str, task: str = "") -> None:
        with (self.root / "research/answer-keys/splits/assignments.csv").open("a") as stream:
            stream.write(f"other-key,{item_id},{task},test\n")


@pytest.fixture
def fixture(tmp_path: Path) -> Fixture:
    root = tmp_path / "repo"
    keys = root / "research/answer-keys"
    (keys / "splits").mkdir(parents=True)
    (keys / CHEATBENCH_DATASET).mkdir()
    for relative in ("tb4-audit-v1/items.jsonl", "tb4-audit-v1/tasks.jsonl", "hacktrace-act-v1/items.jsonl"):
        path = keys / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    inventory = root / "research/experiments/mimo-defect-map-v1/inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({
        "dataset_id": "mimo-defect-map-v1", "tasks": [],
        "heldout_task_ids_excluded_from_training_all_versions": [],
    }))
    cb_items = [{
        "item_id": run_id, "key_id": CHEATBENCH_DATASET, "split": "test" if run_id == T else "dev",
        "expected_verdict": "fail" if run_id in (P, Q, T) else "pass",
        "subject": {"task_id": f"cheatbench/task-{run_id.split(':')[1]}"},
    } for run_id in (P, Q, R, S, T)]
    (keys / CHEATBENCH_DATASET / "items.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in cb_items), encoding="utf-8",
    )
    assignments = "key_id,item_id,task,split\n" + "".join(
        f"{CHEATBENCH_DATASET},{row['item_id']},{row['subject']['task_id']},{row['split']}\n"
        for row in cb_items
    )
    (keys / "splits/assignments.csv").write_text(assignments)
    database = tmp_path / "cloud.sqlite"
    strata = {A: "enacted_yes", B: "enacted_yes", C: "enacted_yes", D: "enacted_yes",
              E: "clean_no", F: "clean_no", G: "considered_only", H: "unjudged", OUTSIDE: "enacted_yes", X: "enacted_yes"}
    with sqlite3.connect(database) as connection:
        connection.executescript(_DDL)
        for run_id, stratum in strata.items():
            name = run_id.rsplit("/", 1)[1]
            task_id = f"goodfire/task-{name}"
            receipt = {
                "admission": {"effective_split": "TEST" if run_id == X else "DEV"},
                "source_label": {"judged": stratum != "unjudged", "verdict": "no" if stratum == "clean_no" else "yes",
                                 "verdict_enacted": "yes" if stratum == "enacted_yes" else "no"},
                "passage_step_labels": {"positive_step_indices": [20] if stratum == "enacted_yes" else []},
            }
            connection.execute(
                "INSERT INTO corpus_tasks VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?)",
                (task_id, GOODFIRE_DATASET, "TEST" if run_id == X else "DEV", "fixture instruction",
                 json.dumps({"item_id": f"native-{name}"})),
            )
            connection.execute(
                "INSERT INTO agent_traces (trace_id, run_id, task_id, model_id, outcome, reward,"
                " is_exploit, is_task_defect_fail, step_count, verified_receipt_json)"
                " VALUES (?, ?, ?, 'fixture-agent', 'unknown', NULL, 0, 0, 41, ?)",
                (run_id, f"fixture/{run_id.split('/')[1]}", task_id,
                 "deliberately invalid TEST receipt" if run_id == X else json.dumps(receipt)),
            )
            connection.executemany(
                "INSERT INTO trace_steps (trace_id, step_index, role, content_text, flags_json)"
                " VALUES (?, ?, ?, 'fixture', '{}')",
                [(run_id, step, "user" if step == 0 else "assistant" if step % 2 == 0 else "tool")
                 for step in range(41)],
            )
    return Fixture(root, database)


def test_label_loader_preserves_dev_guards(fixture: Fixture) -> None:
    labels = atlas.load_labels(database=fixture.database, repo_root=fixture.root,
                               run_ids=(A, E, G, H, P, R))
    assert labels == {A: True, E: False, G: None, P: True, R: False}
    assert atlas.load_labels(database=fixture.database, repo_root=fixture.root, run_ids=[]) == {}
    with pytest.raises(ValueError, match="TEST"):
        atlas.load_labels(database=fixture.database, repo_root=fixture.root, run_ids=[T])


@pytest.mark.parametrize("identity", ["task", "native-item", "trace-id", "missing-facet-task"])
def test_refuses_cross_key_test_aliases(fixture: Fixture, identity: str) -> None:
    if identity == "task":
        fixture.seal(item_id="alias-a", task="different-namespace/task-a@other-version")
    elif identity == "native-item":
        fixture.seal(item_id="native-a")
    elif identity == "trace-id":
        fixture.seal(item_id=A)
    else:
        fixture.seal(item_id="alias-d", task="goodfire/task-d")
    with pytest.raises(ValueError, match="TEST"):
        atlas.load_labels(database=fixture.database, repo_root=fixture.root, run_ids=[A, D])


def test_refuses_cloud_test_before_reading_receipt(fixture: Fixture) -> None:
    # X's receipt is not JSON: reaching it would prove the guard happened too late.
    with pytest.raises(ValueError, match="TEST/non-DEV run"):
        atlas.load_labels(database=fixture.database, repo_root=fixture.root, run_ids=[X])


def test_refuses_receipt_test(fixture: Fixture) -> None:
    with sqlite3.connect(fixture.database) as connection:
        receipt = json.loads(connection.execute(
            "SELECT verified_receipt_json FROM agent_traces WHERE trace_id = ?", (A,),
        ).fetchone()[0])
        receipt["admission"]["effective_split"] = "TEST"
        connection.execute("UPDATE agent_traces SET verified_receipt_json = ? WHERE trace_id = ?",
                           (json.dumps(receipt), A))
    with pytest.raises(ValueError, match="TEST/non-DEV"):
        atlas.load_labels(database=fixture.database, repo_root=fixture.root, run_ids=[A])
