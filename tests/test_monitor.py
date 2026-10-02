from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab import monitor
from evallab.interpretation.monitor_contracts import (
    EvidenceRecord,
    InvestigationReport,
    MonitorAlert,
    MonitorCorpus,
    MonitorFinding,
    TrialSnapshot,
    content_digest,
)


def _trial(key: str, *, task: str = "task-a", steps: int = 2,
           flagged: bool = True, finished: bool = False) -> TrialSnapshot:
    alerts = (MonitorAlert(rule="grader_tamper", severity="high", job="job", trial=key,
                           task=task, step_ref="head#2", quote="write tests/test_a.py"),) if flagged else ()
    records = tuple(EvidenceRecord(
        record_id=f"{key}:head#{step}", trial_key=key, document="agent/trajectory.json",
        source_sha256=content_digest({"key": key, "step": step}), step_ref=f"head#{step}",
        ordinal=step, role="agent", text=f"step {step}: write tests/test_a.py",
    ) for step in range(1, steps + 1))
    return TrialSnapshot(trial_key=key, job="job", trial=key, task=task,
                         source_path=f"/source/job/{key}", state="finished" if finished else "running",
                         reward=1.0 if finished else None, complete=finished,
                         records=records, alerts=alerts)


def _prepare(tmp_path: Path, monkeypatch, corpus: MonitorCorpus) -> tuple[Path, dict]:
    from evallab.interpretation import monitor_evidence

    source = tmp_path / "runs"
    source.mkdir(exist_ok=True)
    status = tmp_path / "watch.json"
    status.write_text(json.dumps({"schema": "evallab.live_watch/v1", "trials": []}))
    monkeypatch.setattr(monitor_evidence, "snapshot_watch", lambda *_args: corpus)
    out = tmp_path / "analysis"
    return out, monitor.prepare_watch(status, [source], out, unflagged=0)


def test_selection_retains_same_task_unflagged_counterexample() -> None:
    primary = _trial("flagged")
    control = _trial("same-task-control", flagged=False)
    unrelated = _trial("different-task", task="task-b", flagged=False)
    corpus = MonitorCorpus(trials=(unrelated, primary, control))
    selected = monitor.select_cases(corpus, unflagged=1, related=1)
    case, subset = next((case, subset) for case, subset in selected if case.primary_trial == "flagged")
    assert case.related_trials == ("same-task-control",)
    assert {trial.trial_key for trial in subset.trials} == {"flagged", "same-task-control"}
    assert case.snapshot_id == subset.digest
    assert [case.selection for case, _ in selected].count("unflagged_control") == 1
    reordered = monitor.select_cases(corpus.model_copy(update={"trials": tuple(reversed(corpus.trials))}),
                                     unflagged=1, related=1)
    assert {(case.case_id, case.primary_trial) for case, _ in reordered} == {
        (case.case_id, case.primary_trial) for case, _ in selected
    }


def test_live_growth_is_coalesced_but_terminal_result_revises_case(tmp_path: Path, monkeypatch) -> None:
    initial = MonitorCorpus(trials=(_trial("trial", steps=2),))
    out, first = _prepare(tmp_path, monkeypatch, initial)
    original = monitor.latest_cases(out)[0][0]
    assert first["new_revisions"] == 1
    _, same = _prepare(tmp_path, monkeypatch, initial)
    assert same["new_revisions"] == 0
    _, prefix = _prepare(tmp_path, monkeypatch, MonitorCorpus(trials=(_trial("trial", steps=5),)))
    assert prefix["coalesced_live_updates"] == 1
    assert monitor.latest_cases(out)[0][0].snapshot_id == original.snapshot_id
    _, completed = _prepare(tmp_path, monkeypatch, MonitorCorpus(trials=(
        _trial("trial", steps=5, finished=True),
    )))
    final = monitor.latest_cases(out)[0][0]
    assert completed["new_revisions"] == 1
    assert final.case_id == original.case_id
    assert final.snapshot_id != original.snapshot_id
    assert (out / "snapshots" / f"{original.snapshot_id}.json").is_file()
    assert monitor.latest_cases(out)[0][1].trials[0].reward == 1.0


def test_new_alert_bypasses_live_throttle_and_related_changes_do_not_requeue(tmp_path: Path, monkeypatch) -> None:
    primary = _trial("primary")
    related = _trial("related", flagged=False)
    out, _ = _prepare(tmp_path, monkeypatch, MonitorCorpus(trials=(primary, related)))
    original = monitor.latest_cases(out)[0][0]
    _, related_update = _prepare(tmp_path, monkeypatch, MonitorCorpus(trials=(
        primary, _trial("related", steps=30, flagged=False),
    )))
    assert related_update["new_revisions"] == 0
    assert monitor.latest_cases(out)[0][0].snapshot_id == original.snapshot_id
    new_alert = MonitorAlert(rule="hidden_info_read", severity="medium", job="job", trial="primary",
                             task="task-a", step_ref="head#3", quote="cat /solution/fix.py")
    changed = _trial("primary", steps=3).model_copy(update={"alerts": (*primary.alerts, new_alert)})
    _, refreshed = _prepare(tmp_path, monkeypatch, MonitorCorpus(trials=(changed, related)))
    assert refreshed["new_revisions"] == 1
    assert len(monitor.latest_cases(out)[0][0].alerts) == 2


def test_prepare_cannot_write_inside_source_or_follow_output_link(tmp_path: Path, monkeypatch) -> None:
    from evallab.interpretation import monitor_evidence

    source = tmp_path / "runs"
    source.mkdir()
    status = tmp_path / "watch.json"
    status.write_text("{}")
    monkeypatch.setattr(monitor_evidence, "snapshot_watch", lambda *_args: MonitorCorpus(trials=()))
    with pytest.raises(ValueError, match="overlap"):
        monitor.prepare_watch(status, [source], source / "analysis")
    assert not (source / "analysis").exists()
    out = tmp_path / "analysis"
    out.mkdir()
    (out / "status.json").symlink_to(source / "untouched")
    with pytest.raises(ValueError, match="symlink"):
        monitor.prepare_watch(status, [source], out)
    assert not (source / "untouched").exists()


def test_tampered_snapshot_refuses_analysis_before_provider(tmp_path: Path, monkeypatch) -> None:
    out, _ = _prepare(tmp_path, monkeypatch, MonitorCorpus(trials=(_trial("trial"),)))
    case = monitor.latest_cases(out)[0][0]
    path = out / "snapshots" / f"{case.snapshot_id}.json"
    payload = json.loads(path.read_text())
    payload["trials"][0]["records"][0]["text"] = "fabricated benign output"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="digest mismatch"):
        monitor.latest_cases(out)


def test_ambiguous_previous_revision_cannot_be_bypassed_with_profile_change(tmp_path: Path, monkeypatch) -> None:
    out, _ = _prepare(tmp_path, monkeypatch, MonitorCorpus(trials=(_trial("trial"),)))
    case, _, revision = monitor.latest_cases(out)[0]
    old_request = revision / "analyses" / ("a" * 64)
    old_request.mkdir(parents=True)
    (old_request / "journal.jsonl").write_text('{"event":"request_started"}\n')
    with pytest.raises(ValueError, match="ambiguous prior"):
        monitor.run_prepared(out, transport=None, budget=None, profile={"model": "other"})
    assert not list((revision / "analyses").glob("*/report.json"))
    assert monitor._ambiguous_prior_request(out, case.case_id, revision / "analyses" / ("b" * 64))


def test_report_keeps_reward_separate_from_hypothesis_and_escapes_source_markup(tmp_path: Path, monkeypatch) -> None:
    out, _ = _prepare(tmp_path, monkeypatch, MonitorCorpus(trials=(_trial("trial", finished=True),)))
    case, _, revision = monitor.latest_cases(out)[0]
    finding = MonitorFinding(disposition="inconclusive", category="reward_hacking",
                             summary="<script>alert('approved')</script>",
                             missing_evidence=("independent reward-impact check",))
    report = InvestigationReport(case_id=case.case_id, snapshot_id=case.snapshot_id,
                                 status="completed", model="test/model", finding=finding)
    destination = revision / "analyses" / ("d" * 64) / "report.json"
    destination.parent.mkdir(parents=True)
    destination.write_text(report.model_dump_json())
    rendered = monitor.render_monitor_report(out)
    assert "Recorded reward: **1.0**" in rendered
    assert "inconclusive / reward_hacking" in rendered
    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered
    assert "usage-priced estimate: unavailable" in rendered
