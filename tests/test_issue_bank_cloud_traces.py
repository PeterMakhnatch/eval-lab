from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from evallab.failure_library.bank import BankContext, IssueItem
from evallab.failure_library.bank.sources import cloud_traces
from evallab.failure_library.records import Split

GOODFIRE = "goodfire-reward-hack-data"
SCHEMA = """
CREATE TABLE corpus_tasks (task_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL,
    split TEXT NOT NULL, instruction_text TEXT NOT NULL DEFAULT '',
    original_grader_digest TEXT, repaired_grader_digest TEXT, cas_uri TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE agent_traces (trace_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
    model_id TEXT NOT NULL DEFAULT 'm', outcome TEXT NOT NULL DEFAULT 'x', reward REAL,
    is_exploit INTEGER NOT NULL DEFAULT 0, is_task_defect_fail INTEGER NOT NULL DEFAULT 0,
    step_count INTEGER NOT NULL DEFAULT 0, raw_cas_uri TEXT, verified_receipt_json TEXT,
    trust_tier TEXT NOT NULL DEFAULT 'tier3_provisional',
    flagged_passage_steps TEXT NOT NULL DEFAULT '[]');
CREATE TABLE trace_steps (trace_id TEXT NOT NULL, step_index INTEGER NOT NULL, role TEXT NOT NULL,
    tool_name TEXT, command_or_action TEXT, content_text TEXT, observation_preview TEXT,
    observation_cas_uri TEXT, flags_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY(trace_id, step_index));
"""


@dataclass
class StubContext(BankContext):
    """Dict-backed split authority: task_id -> effective split for DEV declarations."""

    sealed: dict[str, Split] = field(default_factory=dict)

    def strict_split(self, declared: str, **keys: Any) -> Split:
        if declared.lower() != "dev":
            return {"heldout": "heldout", "test": "TEST"}.get(declared.lower(), "unassigned")  # type: ignore[return-value]
        return self.sealed.get(keys["task_id"], "DEV")


def goodfire_receipt(verdict: str, enacted: str, *, judged: bool = True, split: str = "DEV") -> dict[str, Any]:
    return {
        "admission": {"effective_split": split},
        "source_label": {
            "judged": judged, "verdict": verdict, "verdict_enacted": enacted,
            "source_protocol": "consensus_v2",
        },
        "label_provenance": "Goodfire/reward-hack-data three judge votes",
    }


class Db:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.executescript(SCHEMA)

    def run(
        self, trace_id: str, *, task: str, dataset: str, split: str = "DEV", tier: str = "tier3_provisional",
        exploit: bool = False, defect: bool = False, flagged: Iterable[int] = (), outcome: str = "x",
        receipt: dict[str, Any] | None = None, actor: bool = True,
    ) -> None:
        self.connection.execute(
            "INSERT OR IGNORE INTO corpus_tasks(task_id, dataset_id, split) VALUES (?, ?, ?)",
            (task, dataset, split),
        )
        self.connection.execute(
            "INSERT INTO agent_traces(trace_id, run_id, task_id, outcome, is_exploit, is_task_defect_fail,"
            " trust_tier, flagged_passage_steps, verified_receipt_json) VALUES (?,?,?,?,?,?,?,?,?)",
            (trace_id, trace_id, task, outcome, int(exploit), int(defect), tier,
             json.dumps(list(flagged)), None if receipt is None else json.dumps(receipt)),
        )
        steps = [("user", "do the task")] + ([("assistant", "I will edit the file")] if actor else [])
        for index, (role, text) in enumerate(steps):
            self.connection.execute(
                "INSERT INTO trace_steps(trace_id, step_index, role, content_text) VALUES (?,?,?,?)",
                (trace_id, index, role, text),
            )

    def commit(self) -> None:
        self.connection.commit()
        self.connection.close()


def make(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, admitted: set[str], *, sealed: dict[str, Split] | None = None) -> tuple[Db, Any]:
    db = Db(tmp_path / "cloud.sqlite")
    monkeypatch.setattr(cloud_traces, "_admitted", lambda _connection, _ctx: frozenset(admitted))

    def context() -> StubContext:
        return StubContext(repo_root=tmp_path, state_root=tmp_path / "state", cloud_db=db.path, sealed=sealed or {})

    return db, context


def items(context: StubContext) -> dict[str, IssueItem]:
    out: dict[str, IssueItem] = {}
    for item in cloud_traces.build(context):
        assert isinstance(item, IssueItem)
        out[f"{item.subject_id}:{item.kind}"] = item
    return out


def test_goodfire_labels_split_positive_from_clean_and_drop_considered_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, context = make(tmp_path, monkeypatch, {"t1", "t2", "t3", "t4", "t5", "t6"})
    db.run("pass", task="t1", dataset=GOODFIRE, tier="tier2_consensus_labeled", exploit=True,
           flagged=[3, 5, 5, 9], receipt=goodfire_receipt("yes", "yes"))
    db.run("prov", task="t2", dataset=GOODFIRE, exploit=True, receipt=goodfire_receipt("yes", "yes"))
    db.run("clean", task="t3", dataset=GOODFIRE, receipt=goodfire_receipt("no", "no"))
    db.run("considered", task="t4", dataset=GOODFIRE, receipt=goodfire_receipt("yes", "no"))
    db.run("ambiguous", task="t5", dataset=GOODFIRE, receipt=goodfire_receipt("split", "split"))
    db.run("unjudged", task="t6", dataset=GOODFIRE, receipt=goodfire_receipt("no", "no", judged=False))
    db.commit()
    found = items(context())

    assert set(found) == {"pass:cheat", "prov:cheat", "clean:cheat"}
    passage = found["pass:cheat"]
    assert (passage.polarity, passage.granularity, passage.positive_units, passage.locator) == (
        "positive", "passage", 3, "",
    )
    assert passage.label_type == "consensus" and passage.split == "DEV"
    assert passage.harness_selector == "pass" and passage.actor_transcript is True
    assert found["prov:cheat"].granularity == "run" and found["prov:cheat"].positive_units is None
    clean = found["clean:cheat"]
    assert (clean.polarity, clean.granularity, clean.label_type) == ("negative", "run", "consensus")
    assert clean.provenance.store == "cloud-trace-db:agent_traces" and clean.provenance.ref == "clean"


def test_held_rows_keep_heldout_and_are_not_reachable_or_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, context = make(tmp_path, monkeypatch, set())
    db.run("h-pos", task="h1", dataset=GOODFIRE, split="held", receipt=goodfire_receipt("yes", "yes", split="held"))
    db.run("h-neg", task="h2", dataset=GOODFIRE, split="held", receipt=goodfire_receipt("no", "no", split="held"))
    db.commit()
    found = items(context())

    assert found["h-pos:cheat"].polarity == "positive" and found["h-neg:cheat"].polarity == "negative"
    for item in found.values():
        assert (item.split, item.declared_split) == ("heldout", "held")
        assert item.harness_selector is None and item.actor_transcript is None


def test_run_without_actor_transcript_is_never_a_negative_but_stays_a_positive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, context = make(tmp_path, monkeypatch, {"t1", "t2"})
    db.run("blank-clean", task="t1", dataset=GOODFIRE, receipt=goodfire_receipt("no", "no"), actor=False)
    db.run("blank-hack", task="t2", dataset="mimo-defect-map-v1", exploit=True, outcome="measured", actor=False,
           receipt={"measured": True})
    db.commit()
    found = items(context())

    assert set(found) == {"blank-hack:cheat"}
    hack = found["blank-hack:cheat"]
    assert hack.actor_transcript is False and hack.label_type == "executed"
    assert hack.split == "DEV" and hack.harness_selector == "blank-hack"


def test_sealed_or_unadmitted_dev_declaration_is_never_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, context = make(tmp_path, monkeypatch, {"admitted", "sealed"}, sealed={"sealed": "TEST"})
    db.run("a", task="admitted", dataset="mimo-defect-map-v1", exploit=True)
    db.run("s", task="sealed", dataset="mimo-defect-map-v1", exploit=True)
    db.run("u", task="not-admitted", dataset="mimo-defect-map-v1", exploit=True)
    db.run("r", task="receipt-held", dataset=GOODFIRE, exploit=True,
           receipt=goodfire_receipt("yes", "yes", split="held"))
    db.commit()
    found = items(context())

    assert found["a:cheat"].split == "DEV"
    assert (found["s:cheat"].split, found["s:cheat"].declared_split) == ("TEST", "DEV")
    assert found["u:cheat"].split == "unassigned"
    assert found["r:cheat"].split == "heldout"
    for key in ("s:cheat", "u:cheat", "r:cheat"):
        assert found[key].harness_selector is None and found[key].actor_transcript is None


def test_source_labelled_neither_runs_are_negatives_with_their_label_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, context = make(tmp_path, monkeypatch, {"t1", "t2", "t3", "t4", "t5"})
    cheatbench = {
        "label_provenance": "steinad/CheatBench@461 human-verified cheating/attempt vs vetted benign",
        "canonical_admission": {"dataset_id": "cheatbench-actions-v1", "item_id": "cheatbench-actions-v1:x"},
    }
    census = {"label_provenance": "PR #1137 census label=neither method=deterministic; not replay-confirmed"}
    db.run("human", task="t1", dataset="qa-trace-sft-v0", outcome="source_label:neither", receipt=cheatbench)
    db.run("rule", task="t2", dataset="tb4-audit-v1", outcome="source_label:neither", receipt=census)
    db.run("judged", task="t3", dataset="qa-trace-sft-v0", outcome="source_label:exploit-pass", exploit=True,
           receipt={"label_provenance": "PR #1137 census label=exploit_candidate method=judge"})
    db.run("control", task="t4", dataset="mimo-defect-map-v1", outcome="measured")  # no clean label
    db.run("tb4-hack", task="t5", dataset="tb4-audit-v1", exploit=True, tier="tier1_container_verified")
    db.commit()
    found = items(context())

    assert set(found) == {"human:cheat", "rule:cheat", "judged:cheat", "tb4-hack:cheat"}
    assert (found["human:cheat"].polarity, found["human:cheat"].label_type, found["human:cheat"].license) == (
        "negative", "human", "MIT",
    )
    assert (found["rule:cheat"].polarity, found["rule:cheat"].label_type) == ("negative", "detector")
    assert (found["judged:cheat"].polarity, found["judged:cheat"].label_type) == ("positive", "judge")
    assert found["tb4-hack:cheat"].label_type == "executed"


def test_cross_key_test_on_canonical_identity_tightens_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    @dataclass
    class CanonicalContext(StubContext):
        def strict_split(self, declared: str, **keys: Any) -> Split:
            if keys.get("key_id") == "cheatbench-actions-v1" and keys.get("item_id") == "cheatbench-actions-v1:test":
                return "TEST"
            return super().strict_split(declared, **keys)

    db = Db(tmp_path / "cloud.sqlite")
    monkeypatch.setattr(cloud_traces, "_admitted", lambda _connection, _ctx: frozenset({"t1"}))
    for trace, item in (("sealed", "cheatbench-actions-v1:test"), ("open", "cheatbench-actions-v1:dev")):
        db.run(trace, task="t1", dataset="qa-trace-sft-v0", outcome="source_label:exploit-pass", exploit=True,
               receipt={"canonical_admission": {"dataset_id": "cheatbench-actions-v1", "item_id": item}})
    db.commit()
    found = items(CanonicalContext(repo_root=tmp_path, state_root=tmp_path / "s", cloud_db=db.path))

    assert found["sealed:cheat"].split == "TEST" and found["sealed:cheat"].harness_selector is None
    assert found["open:cheat"].split == "DEV"


def test_task_defect_positive_is_its_own_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db, context = make(tmp_path, monkeypatch, {"t1"})
    db.run("both", task="t1", dataset="tb4-audit-v1", tier="tier1_container_verified", exploit=True, defect=True)
    db.commit()
    found = items(context())

    assert found["both:cheat"].kind == "cheat" and found["both:task_defect"].kind == "task_defect"
    assert found["both:task_defect"].label_type == "executed"
    assert found["both:cheat"].item_id != found["both:task_defect"].item_id
