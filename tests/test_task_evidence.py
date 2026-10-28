"""One task's QA evidence is assembled from retained records and never invents any."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from evallab.production.task_evidence import (
    JOURNAL_EVENT_TYPES,
    MAX_EXPORT_IDS,
    MAX_RUNS,
    JournalTaskIndex,
    ToolFindingIndex,
    build_task_evidence,
    export_command,
    fetch_commands,
)


def _event(seq: int, event_type: str, payload: dict[str, Any], *, system: str = "reward_seeker") -> dict:
    return {
        "seq": seq,
        "event_type": event_type,
        "system": system,
        "run_id": f"campaign-{seq}",
        "ts": f"2026-10-24T20:{seq % 60:02d}:00+00:00",
        "status": "confirmed",
        "payload": payload,
    }


class _Journal:
    """A fake journal that honours ``after_seq`` and ``limit`` like the real reader."""

    def __init__(self, events: list[dict]) -> None:
        self.events = events
        self.calls: list[int] = []

    def __call__(self, types, after: int, limit: int) -> list[dict]:
        self.calls.append(after)
        assert set(types) == set(JOURNAL_EVENT_TYPES)
        return [e for e in self.events if e["seq"] > after and e["event_type"] in types][:limit]


def test_seeker_and_task_qa_events_are_indexed_by_task_or_item_and_stay_bounded() -> None:
    files = {"solve.sh": "SECRET-EXPLOIT-BODY", "notes.json": "x"}
    journal = _Journal(
        [
            _event(1, "seeker.grader_feedback", {
                "task_id": "t1", "item_id": "item-1", "attempt_index": 1,
                "differential_confirmed": False, "original_reward": None, "repaired_reward": None,
                "failure_kind": "upstream_failed", "taxonomy_class": "weak_check",
                "hackbook_entry_id": "HB-1", "target_check": "x" * 900,
                "failure_memory": [{"big": "y" * 5000}],
            }),
            _event(2, "seeker.exploit_confirmed", {
                "task_id": "t1", "item_id": "item-1", "attempt_index": 2, "original_reward": 1.0,
                "repaired_reward": 0.0, "mechanism": "copy the golden file " * 40,
                "model_route": "vendor/model", "exploit_files": files,
            }),
            _event(3, "qa.finding/v1", {
                "item_id": "item-1", "finding_id": "tq-1", "phase": "static_reader",
                "severity": "high", "status": "provisional", "summary": "Possible wrong pass",
                "static": {"huge": "z" * 9000},
            }, system="task_quality"),
            _event(4, "task_quality.repair_qualified", {
                "task_id": "t1", "item_id": "item-1", "qualified": True, "status": "confirmed",
                "source": "mimo-weak-tests", "repaired_task_digest": "sha256:" + "a" * 64,
                "exploit": 0, "exploit_trials": 3, "nop": 0, "nop_trials": 3, "ref_passes": 3,
                "reference_trials": 3, "control_rewards": {"exploit": 0.0, "reference": 1.0},
                "control_evidence": {"logs": "w" * 9000},
            }, system="task_quality"),
            _event(5, "worker.completed", {"task_id": "other", "attempts_run": 1}),
            # A task-quality worker's completion is not a seeker differential.
            _event(6, "worker.completed", {"task_id": "t1", "attempts_run": 9}, system="task_quality"),
        ]
    )
    found = JournalTaskIndex(journal).for_keys({"t1", "item-1"})

    assert [(r["kind"], r["attempt_index"]) for r in found["seeker"]] == [("attempt", 1), ("confirmed", 2)]
    assert found["seeker"][1]["original_reward"] == 1.0 and found["seeker"][1]["repaired_reward"] == 0.0
    assert [r["finding_id"] for r in found["qa_findings"]] == ["tq-1"]
    assert found["repairs"][0]["qualified"] is True
    assert found["repairs"][0]["control_rewards"] == {"exploit": 0.0, "reference": 1.0}
    blob = json.dumps(found)
    assert "SECRET-EXPLOIT-BODY" not in blob and "y" * 100 not in blob and "z" * 100 not in blob
    assert len(found["seeker"][0]["target_check"]) == 200
    assert len(found["seeker"][1]["mechanism"]) == 240
    # Naming both keys must not return one event twice.
    assert len(found["seeker"]) == 2


def test_the_journal_is_read_incrementally_and_unrelated_tasks_see_nothing() -> None:
    journal = _Journal([_event(1, "qa.finding/v1", {"item_id": "t1", "finding_id": "a"}, system="task_quality")])
    index = JournalTaskIndex(journal)
    assert [r["finding_id"] for r in index.for_keys({"t1"})["qa_findings"]] == ["a"]
    journal.events.append(_event(2, "qa.finding/v1", {"item_id": "t1", "finding_id": "b"}, system="task_quality"))
    assert [r["finding_id"] for r in index.for_keys({"t1"})["qa_findings"]] == ["a", "b"]
    assert journal.calls[-1] == 1  # only events after the last one seen were requested
    assert index.for_keys({"nobody"}) == {"seeker": [], "qa_findings": [], "repairs": []}


def _results(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _row(run: str, tool: str, detector: str, flag: bool | None, at: str, steps: list[int] | None = None) -> dict:
    return {"run_id": run, "tool": tool, "detector": detector, "flag": flag, "at": at,
            "evidence_steps": steps or []}


def test_tool_findings_keep_the_latest_result_and_separate_abstentions_from_clean(tmp_path: Path) -> None:
    path = tmp_path / "results-v1.jsonl"
    _results(path, [
        _row("r1", "scout", "repeat", False, "2026-10-27T20:00:00+00:00"),
        _row("r1", "scout", "repeat", True, "2026-10-27T21:00:00+00:00", [4, 9]),
        _row("r1", "scout", "parse", False, "2026-10-27T20:00:00+00:00"),
        _row("r1", "docent", "rh", None, "2026-10-27T20:00:00+00:00"),
        _row("r2", "scout", "repeat", False, "2026-10-27T20:00:00+00:00"),
    ])
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    index = ToolFindingIndex(path)
    found = index.for_runs(["r1", "r2", "unread"])
    assert found["r1"]["scout"] == {
        "flagged": [{"detector": "repeat", "evidence_steps": [4, 9]}],
        "clean_n": 1,
        "abstained_n": 0,
    }
    assert found["r1"]["docent"] == {"flagged": [], "clean_n": 0, "abstained_n": 1}
    assert "unread" not in found  # no result is not a clean result
    assert index.error is None


def test_tool_findings_reload_when_the_file_changes_and_report_a_missing_file(tmp_path: Path) -> None:
    path = tmp_path / "results-v1.jsonl"
    missing = ToolFindingIndex(path)
    assert missing.for_runs(["r1"]) == {} and missing.error == "not retained"
    _results(path, [_row("r1", "scout", "a", True, "2026-10-27T20:00:00+00:00")])
    assert missing.for_runs(["r1"])["r1"]["scout"]["flagged"] and missing.error is None
    _results(path, [_row("r1", "scout", "a", False, "2026-10-27T20:00:00+00:00"),
                    _row("r1", "scout", "b", False, "2026-10-27T20:00:00+00:00")])
    assert missing.for_runs(["r1"])["r1"]["scout"]["clean_n"] == 2


def test_a_symlinked_results_file_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "real.jsonl"
    _results(real, [_row("r1", "scout", "a", True, "2026-10-27T20:00:00+00:00")])
    link = tmp_path / "results-v1.jsonl"
    os.symlink(real, link)
    index = ToolFindingIndex(link)
    assert index.for_runs(["r1"]) == {} and index.error


def test_fetch_commands_quote_hostile_ids_and_cap_the_export() -> None:
    hostile = "task; rm -rf ~ $(x)"
    runs = [f"run-{n}'; echo hi" for n in range(25)]
    commands = fetch_commands(hostile, runs)
    assert [c["label"].split(" of ")[0] for c in commands][:2] == ["This page as JSON", "Local task dossier"]
    dossier = next(c for c in commands if c["label"] == "Local task dossier")["command"]
    assert dossier == "uv run evallab task 'task; rm -rf ~ $(x)' --json"
    curl = commands[0]["command"]
    assert "task_id=task%3B%20rm%20-rf%20~%20%24%28x%29" in curl and "$EVALLAB_CLOUD_TOKEN" in curl
    export = export_command(hostile, runs)
    assert export.count("--trace-id") == MAX_EXPORT_IDS
    assert "'run-0'\"'\"'; echo hi'" in export
    directory = export.split("--output-directory /private/tmp/evallab-task-evidence/")[1].split()[0]
    assert directory and all(ch.isalnum() or ch in "._-" for ch in directory)
    assert fetch_commands("t", [])[-1]["label"] == "Local task dossier"  # nothing to export, no export command


def _trace(run_id: str, **fields: Any) -> dict[str, Any]:
    return {"trace_id": run_id, "task_id": "t1", "model_id": "vendor/model", "step_count": 10,
            "trust_tier": "tier3_provisional", "outcome": "unknown", **fields}


def _facet(suspicion: int) -> dict[str, Any]:
    return {"goal": "g", "approach": "a", "shortcut": "s", "suspicion": suspicion, "tests": "read",
            "eval_awareness": "none", "grader_or_harness_touched": False,
            "answer_material_accessed": False, "success_claim_without_evidence": False,
            "evidence_steps": [0], "facet_model": "p/m", "split": "DEV", "run_id": "never-copied"}


def _build(traces, facets, tools=None, issues=(), journal=None) -> dict[str, Any]:
    return build_task_evidence(
        task={"task_id": "t1", "dataset_id": "d", "split": "DEV", "metadata_json": {"item_id": "i1"},
              "original_grader_digest": "sha256:o", "repaired_grader_digest": None,
              "instruction_text": "NEVER SHOWN"},
        issues=list(issues),
        traces={t["trace_id"]: t for t in traces},
        facets=facets,
        cluster_paths=lambda run: [[{"cluster_id": "c", "name": f"cluster of {run}"}]],
        tools=tools or {},
        tools_error=None,
        journal=journal or {"seeker": [], "qa_findings": [], "repairs": []},
    )


def test_runs_are_ordered_by_suspicion_and_only_faceted_runs_carry_clusters() -> None:
    document = _build(
        [_trace("a"), _trace("b"), _trace("c")],
        {"a": _facet(1), "b": _facet(3)},
        tools={"c": {"scout": {"flagged": [{"detector": "x", "evidence_steps": [1]}], "clean_n": 0, "abstained_n": 0}}},
    )
    assert [run["run_id"] for run in document["runs"]] == ["b", "a", "c"]
    by_id = {run["run_id"]: run for run in document["runs"]}
    assert by_id["b"]["clusters"] == [["cluster of b"]] and by_id["c"]["facet"] is None
    assert by_id["c"]["clusters"] == [] and "run_id" not in by_id["b"]["facet"]
    summary = document["summary"]
    assert (summary["runs_n"], summary["faceted_n"], summary["suspicion_at_least_2_n"]) == (3, 2, 1)
    assert summary["tool_flagged_runs_n"] == 1
    assert "NEVER SHOWN" not in json.dumps(document)
    assert document["task"]["item_id"] == "i1" and document["task"]["repaired_grader_digest"] is None


def test_a_large_task_is_capped_but_still_counted() -> None:
    traces = [_trace(f"run-{n:03}") for n in range(MAX_RUNS + 20)]
    document = _build(traces, {})
    assert document["summary"]["runs_n"] == MAX_RUNS + 20
    assert len(document["runs"]) == MAX_RUNS == document["summary"]["runs_shown_n"]
    export = next(c for c in document["commands"] if c["label"].startswith("Blinded ATIF"))
    assert export["command"].count("--trace-id") == MAX_EXPORT_IDS


def test_issue_rows_drop_reproducers_and_summaries_count_what_was_recorded() -> None:
    issue = {"issue_id": "i", "defect_class": "weak", "direction": "wrong_pass", "status": "confirmed",
             "trust_tier": "tier1_container_verified", "original_reward": 1.0, "repaired_reward": 0.0,
             "citation_file": "tests/a.py", "citation_line": 4, "witness_digest": "sha256:w",
             "reproducer_script": "printf 'LEAK'", "cas_uri": "cas://x"}
    journal = {
        "seeker": [{"kind": "attempt", "seq": 1}, {"kind": "confirmed", "seq": 2}, {"kind": "worker", "seq": 3}],
        "qa_findings": [{"kind": "finding", "seq": 4}, {"kind": "blocked", "seq": 5}],
        "repairs": [{"qualified": True, "seq": 6}, {"qualified": False, "seq": 7}],
    }
    document = _build([_trace("a")], {}, issues=[issue], journal=journal)
    assert "LEAK" not in json.dumps(document) and "cas://x" not in json.dumps(document)
    summary = document["summary"]
    assert (summary["issues_n"], summary["issues_confirmed_n"]) == (1, 1)
    assert (summary["seeker_attempts_n"], summary["seeker_confirmed_n"]) == (1, 1)
    assert (summary["qa_findings_n"], summary["repairs_qualified_n"]) == (1, 1)
