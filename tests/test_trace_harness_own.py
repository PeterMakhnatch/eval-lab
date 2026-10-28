from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evallab.production import trace_harness_own as own
from evallab.production.trace_harness import (
    FindingStore,
    HarnessOptions,
    HarnessRun,
    run_detectors,
    validate_finding,
)

KIMI = own.JUDGE_MODEL
GROK = own.GROK_MODEL


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _step(step_id: int, source: str = "agent", **extra: Any) -> dict[str, Any]:
    return {"step_id": step_id, "source": source, "message": f"m{step_id}", **extra}


def _trial(root: Path, name: str, documents: dict[str, dict[str, Any]]) -> Path:
    agent = root / name / "agent"
    agent.mkdir(parents=True)
    for file, document in documents.items():
        (agent / file).write_text(json.dumps(document), encoding="utf-8")
    return root / name


def _card(rule: str, step: int, status: str = "provisional", triage: str = "not_escalated") -> dict[str, Any]:
    return {"rule": rule, "flagged_step": step, "status": status, "triage": triage}


def _row(run_id: str, model: str | None, cards: list[dict[str, Any]], steps: int, unavailable: int = 0) -> dict[str, Any]:
    # the stored rows also carry a precomputed ``alarm``; the detector must not read it
    return {
        "run_id": run_id, "model": model, "cards": cards, "n_steps": steps,
        "triage_unavailable": unavailable, "alarm": None,
    }


@pytest.fixture
def cohort(tmp_path: Path) -> Path:
    plain = _trial(tmp_path, "plain", {"trajectory.json": {"steps": [_step(1, "user"), _step(2), _step(3)]}})
    chained = _trial(tmp_path, "chained", {
        "trajectory.json": {
            "steps": [_step(1, "user"), _step(2, "assistant", reasoning_content="r", model_name="x", extra={"a": 1}),
                      _step(3)],
            "continued_trajectory_ref": "cont.json",
        },
        "cont.json": {"steps": [_step(1, "user", is_copied_context=True), _step(5), _step(6, "agent", tool_calls=[])]},
    })
    path = tmp_path / "cohort.jsonl"
    _write_jsonl(path, [
        {"run_id": "plain-run", "trial_dir": str(plain), "cohort": "trace-paired-v1:dev", "task_name": "t"},
        {"run_id": "chained-run", "trial_dir": str(chained), "cohort": "contamination-audit:unsplit"},
    ])
    return path


def test_loader_stitches_continuations_and_keeps_only_actor_fields(cohort: Path) -> None:
    plain, chained = list(own.own_runs(cohort=cohort))
    assert [step["step_id"] for step in plain.steps] == [1, 2, 3]
    assert [step["step_id"] for step in chained.steps] == [1, 2, 3, 4, 5]  # copied-context step omitted
    assert chained.steps[1]["source"] == "agent" and chained.steps[1]["reasoning_content"] == "r"
    assert all(set(step) <= {"step_id", *own.ACTOR_FIELDS} for step in chained.steps)
    assert (plain.split, chained.split) == ("DEV", "UNSPLIT")
    assert plain.source == own.OWN_SOURCE and plain.outcome == "unknown" and plain.actor_inputs_available


def test_dev_loader_and_limit_select_in_cohort_order(cohort: Path) -> None:
    assert [run.run_id for run in own.own_dev_runs(cohort=cohort)] == ["plain-run"]
    assert [run.run_id for run in own.own_runs(1, cohort=cohort)] == ["plain-run"]


def test_continuation_chain_must_stay_in_agent_dir_and_not_loop(tmp_path: Path) -> None:
    loop = _trial(tmp_path, "loop", {"trajectory.json": {"steps": [], "continued_trajectory_ref": "trajectory.json"}})
    with pytest.raises(ValueError, match="continuation"):
        own.stitched_steps(loop)
    escape = _trial(tmp_path, "escape", {"trajectory.json": {"steps": [], "continued_trajectory_ref": "../x.json"}})
    with pytest.raises(ValueError, match="continuation"):
        own.stitched_steps(escape)


def test_repeated_cohort_run_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "cohort.jsonl"
    _write_jsonl(path, [{"run_id": "a", "trial_dir": "/x"}, {"run_id": "a", "trial_dir": "/y"}])
    with pytest.raises(ValueError, match="repeated"):
        own.cohort_trials(path)


def _run(run_id: str, steps: int) -> HarnessRun:
    return HarnessRun(run_id, own.OWN_SOURCE, "DEV", "unknown", tuple(_step(i) for i in range(1, steps + 1)), True)


@pytest.fixture
def rows(tmp_path: Path) -> Path:
    confirmed = _card("hidden_info_read", 4, "confirmed", "completed")
    path = tmp_path / "cascade-own.jsonl"
    _write_jsonl(path, [
        _row("kimi-alarm", KIMI, [confirmed, _card("fetch_attempt", 2)], 6),
        _row("kimi-clear", KIMI, [_card("fetch_attempt", 2)], 6),
        _row("kimi-abstain", KIMI, [_card("fetch_attempt", 2)], 6, unavailable=1),
        _row("grok-alarm", GROK, [confirmed], 6),
        _row("no-judge-clear", None, [], 6),
        _row("no-judge-passthrough", None, [_card("submission_tamper", 5, "provisional", "gate_pass_through")], 6),
        _row("no-judge-unavailable", None, [_card("fetch_attempt", 2)], 6, unavailable=1),
    ])
    return path


def _detect(rows: Path, reader: str | None, *run_ids: str) -> dict[str, dict[str, Any]]:
    detector = own.CascadeOwnDetector(HarnessOptions(reader=reader), rows=rows)
    records = list(detector.detect([_run(run_id, 6) for run_id in run_ids]))
    assert all(not validate_finding(record) for record in records)
    assert all(record["reader_model"] == detector.reader_model for record in records)
    return {record["run_id"]: record for record in records}


ALL = (
    "kimi-alarm", "kimi-clear", "kimi-abstain", "grok-alarm", "no-judge-clear", "no-judge-passthrough",
    "no-judge-unavailable", "never-replayed",
)


def test_default_reader_covers_kimi_and_judge_free_rows(rows: Path) -> None:
    found = _detect(rows, None, *ALL)
    assert {run_id: (record["verdict"], record["score"], record["evidence_steps"]) for run_id, record in found.items()} == {
        "kimi-alarm": ("alarm", 1.0, [4]),
        "kimi-clear": ("clear", 0.0, []),
        "kimi-abstain": ("abstain", None, []),
        "no-judge-clear": ("clear", 0.0, []),
        "no-judge-passthrough": ("alarm", 1.0, [5]),  # v7 arm: submission_tamper passes through
        "no-judge-unavailable": ("abstain", None, []),
    }


def test_grok_reader_covers_only_grok_judged_rows(rows: Path) -> None:
    found = _detect(rows, GROK, *ALL)
    assert {run_id: record["verdict"] for run_id, record in found.items()} == {"grok-alarm": "alarm"}


def test_unknown_reader_is_refused(rows: Path) -> None:
    with pytest.raises(ValueError, match="reader"):
        own.CascadeOwnDetector(HarnessOptions(reader="anthropic/claude-sonnet-5-5"), rows=rows)


def test_step_count_mismatch_is_refused(rows: Path) -> None:
    detector = own.CascadeOwnDetector(rows=rows)
    with pytest.raises(ValueError, match="stored replay saw 6 steps"):
        list(detector.detect([_run("kimi-clear", 5)]))


def test_missing_rows_file_yields_nothing(tmp_path: Path) -> None:
    detector = own.CascadeOwnDetector(rows=tmp_path / "absent.jsonl")
    assert list(detector.detect([_run("kimi-clear", 6)])) == []


def test_harness_run_caches_findings_under_the_reader_identity(rows: Path, tmp_path: Path) -> None:
    store = FindingStore(tmp_path / "findings.jsonl")
    runs = [_run(run_id, 6) for run_id in ("kimi-alarm", "grok-alarm")]
    summary = run_detectors(runs, [own.CascadeOwnDetector(rows=rows)], store)
    (label,) = summary["detectors"]
    assert summary["detectors"][label]["written"] == 1
    again = run_detectors(runs, [own.CascadeOwnDetector(HarnessOptions(reader=GROK), rows=rows)], store)
    (grok_label,) = again["detectors"]
    assert again["detectors"][grok_label]["written"] == 1
    assert {key[3] for key in store.records()} == {KIMI, GROK}


def _laminar_row(
    run_id: str, signal: str, flag: bool | None, payload: dict[str, Any] | None = None, tokens: tuple[int, int] = (100, 10)
) -> dict[str, Any]:
    observation = {
        "flag": flag, "provenance": "cloud-sql-readback",
        "signal_run": {"input_tokens": tokens[0], "output_tokens": tokens[1]},
        "event_payload": json.dumps(payload) if payload else None,
    }
    return {
        "schema": "evallab.trace_tool_result/v1", "run_id": run_id, "source": own.OWN_SOURCE, "tool": "laminar",
        "detector": signal, "detector_version": own.LaminarDetector.version, "value": {"observations": [observation]},
        "flag": flag, "evidence_steps": [], "scan": "s", "at": "2026-10-27T22:06:30+00:00",
    }


def test_laminar_copy_reads_only_the_copy_signal(tmp_path: Path) -> None:
    path = tmp_path / "results.jsonl"
    _write_jsonl(path, [
        _laminar_row("hit", "copied_upstream_fix", True, {"evidence_step": "2", "source": "git"}, (50, 5)),
        _laminar_row("hit", "stuck_loop", False),
        _laminar_row("quiet", "copied_upstream_fix", False),
        _laminar_row("quiet", "stuck_loop", True, {"start_step": "1"}),
        _laminar_row("loop-only", "stuck_loop", True, {"start_step": "1"}),
        _laminar_row("unread", "copied_upstream_fix", None),
        _laminar_row("unread", "stuck_loop", False),
    ])
    detector = own.LaminarCopyDetector(HarnessOptions(), results=path)
    runs = [_run(name, 3) for name in ("hit", "quiet", "loop-only", "unread", "absent")]
    found = {record["run_id"]: record for record in detector.detect(runs)}
    assert all(not validate_finding(record) for record in found.values())
    assert {run_id: record["verdict"] for run_id, record in found.items()} == {
        "hit": "alarm", "quiet": "clear", "unread": "abstain",
    }
    assert found["hit"]["evidence_steps"] == [2]
    assert (found["hit"]["input_tokens"], found["hit"]["output_tokens"]) == (50, 5)
    assert found["quiet"]["evidence_steps"] == []
