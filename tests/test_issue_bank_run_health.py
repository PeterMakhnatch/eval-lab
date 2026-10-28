from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from evallab.failure_library.bank import BankContext, IssueItem
from evallab.failure_library.bank.sources import run_health as source
from evallab.failure_library.records import Split
from evallab.failure_library.sources import normalize_split

SCHEMA = """
CREATE TABLE corpus_tasks (task_id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL,
    split TEXT NOT NULL, instruction_text TEXT NOT NULL DEFAULT '',
    original_grader_digest TEXT, repaired_grader_digest TEXT, cas_uri TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE agent_traces (trace_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, task_id TEXT NOT NULL,
    verified_receipt_json TEXT);
CREATE TABLE trace_steps (trace_id TEXT NOT NULL, step_index INTEGER NOT NULL, role TEXT NOT NULL,
    tool_name TEXT, command_or_action TEXT, content_text TEXT, observation_preview TEXT,
    flags_json TEXT NOT NULL DEFAULT '{}');
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


def _jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _finding(
    run_id: str,
    kind: str,
    verdict: str,
    version: str = "run-health/4",
    steps: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "detector": f"run_health.{kind}",
        "detector_version": version,
        "verdict": verdict,
        "score": 0.9,
        "kind": kind,
        "evidence_steps": steps or [],
        "summary": f"{kind} {verdict}",
    }


def _database(path: Path, runs: dict[str, tuple[str, str, bool]]) -> Path:
    """run id -> (task id, declared split, has an agent step with text)."""
    connection = sqlite3.connect(path)
    connection.executescript(SCHEMA)
    for run_id, (task_id, split, transcript) in runs.items():
        connection.execute(
            "INSERT OR IGNORE INTO corpus_tasks (task_id, dataset_id, split) VALUES (?, 'ds', ?)",
            (task_id, split),
        )
        connection.execute(
            "INSERT INTO agent_traces (trace_id, run_id, task_id) VALUES (?, ?, ?)",
            (run_id, run_id, task_id),
        )
        connection.execute(
            "INSERT INTO trace_steps (trace_id, step_index, role, content_text)"
            " VALUES (?, 0, 'assistant', ?)",
            (run_id, "I will run the tests" if transcript else ""),
        )
    connection.commit()
    connection.close()
    return path


@pytest.fixture(autouse=True)
def admit_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real admission gate loads the corpus registry; tests stub it."""
    monkeypatch.setattr(
        source,
        "_admitted",
        lambda connection, ctx: frozenset(
            row[0] for row in connection.execute("SELECT task_id FROM corpus_tasks")
        ),
    )


def _ctx(tmp_path: Path, db: Path, sealed: frozenset[str] = frozenset()) -> StubContext:
    return StubContext(
        repo_root=tmp_path,
        prod_live=tmp_path / "live",
        cloud_db=db,
        sealed=sealed,
        cross_test_registry=tmp_path / "bridge" / "cross-test-registry.json",
    )


def _build(ctx: StubContext) -> dict[tuple[str, str, str], IssueItem]:
    items = [item for item in source.build(ctx) if isinstance(item, IssueItem)]
    return {(item.subject_id, item.kind, item.locator): item for item in items}


def test_only_alarms_become_positives_from_the_newest_version(tmp_path: Path) -> None:
    db = _database(tmp_path / "c.sqlite", {"r1": ("t1", "DEV", True), "r2": ("t2", "DEV", True)})
    run_health = tmp_path / "live" / "run-health"
    _jsonl(
        run_health / "findings-run-health-3.jsonl",
        [
            _finding("r1", "loop", "alarm", version="run-health/3"),
        ],
    )
    _jsonl(
        run_health / "findings-run-health-4.jsonl",
        [
            _finding("r1", "loop", "clear"),
            _finding("r1", "runaway", "alarm", steps=[4, 9]),
            _finding("r1", "latency", "abstain"),
            _finding("r1", "infra", "unknown"),
            _finding("r2", "latency", "alarm"),
            _finding("r2", "cheat", "alarm"),  # not a run-health kind
        ],
    )

    items = _build(_ctx(tmp_path, db))

    assert set(items) == {("r1", "runaway", ""), ("r2", "latency", "")}
    runaway = items[("r1", "runaway", "")]
    assert (runaway.polarity, runaway.label_type, runaway.granularity) == (
        "positive",
        "detector",
        "run",
    )
    assert runaway.positive_units == 2 and items[("r2", "latency", "")].positive_units is None
    assert (runaway.split, runaway.task_id, runaway.actor_transcript) == ("DEV", "t1", True)
    assert runaway.harness_selector == "r1"
    assert runaway.provenance.store == "run-health:run-health/4" and runaway.provenance.ref == "r1"


def test_detector_alarm_on_sealed_or_unknown_run_is_never_dev(tmp_path: Path) -> None:
    db = _database(tmp_path / "c.sqlite", {"r1": ("sealed-task", "DEV", True)})
    _jsonl(
        tmp_path / "live" / "run-health" / "findings-run-health-4.jsonl",
        [
            _finding("r1", "runaway", "alarm"),
            _finding("ghost", "runaway", "alarm"),
        ],
    )

    items = _build(_ctx(tmp_path, db, sealed=frozenset({"sealed-task"})))

    assert items[("r1", "runaway", "")].split == "TEST"
    assert items[("r1", "runaway", "")].harness_selector is None
    ghost = items[("ghost", "runaway", "")]
    assert (ghost.split, ghost.task_id, ghost.actor_transcript) == ("unassigned", None, None)


def _audit(tmp_path: Path, keys: list[tuple[str, str]], votes: dict[str, tuple[str, str]]) -> None:
    audit = tmp_path / "bridge" / "raw" / "rh-p11"
    _jsonl(
        audit / "key.jsonl",
        [
            {"packet": f"p{index}", "run_id": run_id, "alarm": alarm == "alarm"}
            for index, (run_id, alarm) in enumerate(keys)
        ],
    )
    for rater, position in (("A", 0), ("B", 1)):
        _jsonl(
            audit / f"out-{rater}0.jsonl",
            [
                {"packet": f"p{index}", "verdict": votes[run_id][position]}
                for index, (run_id, _) in enumerate(keys)
                if run_id in votes
            ],
        )


def test_blind_audit_items_follow_rater_consensus(tmp_path: Path) -> None:
    runs = {name: ("t" + name, "DEV", True) for name in ("loop", "clean", "split", "unclear")}
    db = _database(tmp_path / "c.sqlite", runs)
    _audit(
        tmp_path,
        [("loop", "alarm"), ("clean", "alarm"), ("split", "alarm"), ("unclear", "control")],
        {
            "loop": ("loop", "loop"),
            "clean": ("not_loop", "not_loop"),
            "split": ("loop", "not_loop"),
            "unclear": ("loop", "unclear"),
        },
    )
    # The v4 detector alarms on the same run: a different locator keeps the two items apart.
    _jsonl(
        tmp_path / "live" / "run-health" / "findings-run-health-4.jsonl",
        [
            _finding("loop", "loop", "alarm"),
        ],
    )

    items = _build(_ctx(tmp_path, db))

    assert set(items) == {
        ("loop", "loop", ""),
        ("loop", "loop", source.AUDIT_LOCATOR),
        ("clean", "loop", source.AUDIT_LOCATOR),
    }
    positive = items[("loop", "loop", source.AUDIT_LOCATOR)]
    negative = items[("clean", "loop", source.AUDIT_LOCATOR)]
    assert (positive.polarity, positive.label_type, positive.granularity) == (
        "positive",
        "judge",
        "segment",
    )
    assert (negative.polarity, negative.label_type, negative.split) == ("negative", "judge", "DEV")
    assert positive.item_id != items[("loop", "loop", "")].item_id
    assert positive.provenance.store == "run-health:run-health/3"
    assert positive.provenance.ref == f"loop#{source.AUDIT_LOCATOR}"


def test_audit_negative_without_actor_transcript_is_refused(tmp_path: Path) -> None:
    db = _database(
        tmp_path / "c.sqlite", {"mute": ("t1", "DEV", False), "talk": ("t2", "DEV", True)}
    )
    _audit(
        tmp_path,
        [("mute", "control"), ("talk", "control")],
        {"mute": ("not_loop", "not_loop"), "talk": ("not_loop", "not_loop")},
    )

    items = _build(_ctx(tmp_path, db))

    assert set(items) == {("talk", "loop", source.AUDIT_LOCATOR)}


def test_audit_item_on_sealed_task_is_never_dev(tmp_path: Path) -> None:
    db = _database(tmp_path / "c.sqlite", {"r1": ("sealed-task", "DEV", True)})
    _audit(tmp_path, [("r1", "alarm")], {"r1": ("loop", "loop")})

    (item,) = _build(_ctx(tmp_path, db, sealed=frozenset({"sealed-task"}))).values()

    assert item.split == "TEST" and item.declared_split == "DEV"
    assert item.harness_selector is None


def test_no_inputs_yield_nothing(tmp_path: Path) -> None:
    assert _build(_ctx(tmp_path, tmp_path / "absent.sqlite")) == {}
