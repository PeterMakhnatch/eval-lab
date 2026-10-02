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
    monkeypatch.setattr(monitor_evidence, "snapshot_watch", lambda *_args, **_kwargs: corpus)
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
    monkeypatch.setattr(monitor_evidence, "snapshot_watch", lambda *_args, **_kwargs: MonitorCorpus(trials=()))
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
    assert monitor.latest_cases(out)[0][1].trials[0].reward == 1.0
    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered


def test_missing_current_source_retains_case_without_dispatch(tmp_path: Path, monkeypatch) -> None:
    out, _ = _prepare(tmp_path, monkeypatch, MonitorCorpus(trials=(_trial("trial"),)))
    original = monitor.latest_cases(out)[0][0]
    _, summary = _prepare(tmp_path, monkeypatch, MonitorCorpus(trials=()))
    assert summary["inactive_case_ids"] == [original.case_id]
    assert monitor.latest_cases(out)[0][0] == original
    assert monitor.latest_cases(out, active_only=True) == []


def test_fleet_signal_routes_only_real_members_and_preserves_unroutable_signal(tmp_path: Path, monkeypatch) -> None:
    trial_a = _trial("a", finished=True)
    trial_b = _trial("b", finished=True)
    fleet = MonitorAlert(rule="same_task_copy", severity="high", scope="fleet", job="fleet",
                         trial="fleet:copy", task="task-a", trials=("a", "b"), detail="two acquisitions")
    unknown = MonitorAlert(rule="future_rule", severity="medium", scope="fleet", job="fleet",
                           trial="fleet:unknown", task="fleet", detail="no source members declared")
    corpus = MonitorCorpus(trials=(trial_a, trial_b), fleet_alerts=(fleet, unknown))
    out, summary = _prepare(tmp_path, monkeypatch, corpus)
    fleet_cases = [case for case, _ in monitor.select_cases(corpus, unflagged=0)
                   if any(alert.scope == "fleet" for alert in case.alerts)]
    assert len(fleet_cases) == 1
    assert {fleet_cases[0].primary_trial, *fleet_cases[0].related_trials} == {"a", "b"}
    assert summary["unrouted_fleet_alerts"] == [unknown.model_dump(mode="json")]
    assert summary["fleet_alerts"] == [fleet.model_dump(mode="json"), unknown.model_dump(mode="json")]
    assert "future_rule" in monitor.render_monitor_report(out)


def test_unflagged_sample_not_starved_by_flag_backlog() -> None:
    corpus = MonitorCorpus(trials=tuple(_trial(f"flag-{n}") for n in range(5)) + (
        _trial("unflagged", flagged=False),
    ))
    selected = monitor.select_cases(corpus, unflagged=1, max_cases=2)
    assert [case.selection for case, _ in selected] == ["alert", "unflagged_control"]


def test_elapsed_stall_detail_alone_cannot_trigger_another_investigation(tmp_path: Path, monkeypatch) -> None:
    old = _trial("trial")
    old = old.model_copy(update={"alerts": (
        MonitorAlert(rule="stalled", severity="medium", job="job", trial="trial", task="task-a",
                     detail="10 minutes without an update"),
    )})
    out, _ = _prepare(tmp_path, monkeypatch, MonitorCorpus(trials=(old,)))
    original = monitor.latest_cases(out)[0][0]
    new = old.model_copy(update={"alerts": (
        old.alerts[0].model_copy(update={"detail": "20 minutes without an update"}),
    )})
    _, summary = _prepare(tmp_path, monkeypatch, MonitorCorpus(trials=(new,)))
    assert summary["new_revisions"] == 0
    assert monitor.latest_cases(out)[0][0] == original


def test_selection_cap_is_visible_and_does_not_claim_source_vanished(tmp_path: Path, monkeypatch) -> None:
    corpus = MonitorCorpus(trials=(_trial("a"), _trial("b")))
    out, _ = _prepare(tmp_path, monkeypatch, corpus)
    summary = monitor.prepare_watch(tmp_path / "watch.json", [tmp_path / "runs"], out,
                                    unflagged=0, max_cases=1)
    assert summary["candidate_cases"] == 2
    assert len(summary["omitted_candidates"]) == 1
    omitted_id = summary["omitted_candidates"][0]["case_id"]
    assert summary["inactive_reasons"][omitted_id] == "selection_cap"
    assert len(monitor.latest_cases(out)) == 2
    assert len(monitor.latest_cases(out, active_only=True)) == 1


def test_unfinished_analysis_profile_is_visible_in_report(tmp_path: Path, monkeypatch) -> None:
    out, _ = _prepare(tmp_path, monkeypatch, MonitorCorpus(trials=(_trial("trial"),)))
    _, _, revision = monitor.latest_cases(out)[0]
    profile = "a" * 64
    journal = revision / "analyses" / profile / "journal.jsonl"
    journal.parent.mkdir(parents=True)
    journal.write_text('{"event":"request_started"}\\n')
    rendered = monitor.render_monitor_report(out)
    assert profile in rendered
