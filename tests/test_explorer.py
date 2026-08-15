"""Explorer contract tests (M005). Fixture-driven, read-only, no host state."""

from __future__ import annotations

import shutil
from pathlib import Path

from evallab.explorer import (
    TrajectoryView,
    build_index,
    jail,
    next_actions_for_queue,
    next_actions_for_task,
    next_actions_for_trial,
    redact_mapping,
)
from evallab.status import build_status_snapshot

FIXTURES = Path(__file__).parent / "fixtures" / "explorer"
JOBS = FIXTURES / "jobs"
ANALYSES = FIXTURES / "analyses"


def index():
    return build_index([JOBS], ANALYSES)


def tree_state(root: Path) -> dict[str, tuple[int, float]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime)
        for p in sorted(root.rglob("*")) if p.is_file()
    }


# ---- outcomes: pass / reward failure / infra exception ----------------------


def test_pass_fail_and_exception_are_distinct_classes():
    idx = index()
    assert idx.trials["job-pass/t1"].outcome_class.value == "pass"
    assert idx.trials["job-fail/t1"].outcome_class.value == "reward-failure"
    exc = idx.trials["job-exc/t1"]
    assert exc.outcome_class.value == "infra-exception"
    # infra exceptions never masquerade as scores
    assert exc.reward.provenance == "unavailable"
    assert exc.exception.provenance == "observed"


def test_every_field_carries_a_provenance_label():
    trial = index().trials["job-pass/t1"]
    for labeled in (trial.task_name, trial.agent, trial.model, trial.reward,
                    trial.outcome_class, trial.exception, trial.timing,
                    trial.cost, trial.config):
        assert labeled.provenance in {"observed", "derived", "draft", "unavailable"}


# ---- trajectory ------------------------------------------------------------


def test_missing_trajectory_is_unavailable_not_an_error():
    trial = index().trials["job-notraj/t1"]
    assert not isinstance(trial.trajectory, TrajectoryView)
    assert trial.trajectory.provenance == "unavailable"


def test_tool_loop_is_detected_as_repeated_signatures():
    trajectory = index().trials["job-fail/t1"].trajectory
    assert isinstance(trajectory, TrajectoryView)
    repeats = dict(trajectory.repeated_signatures.value)
    assert repeats.get("run_bash") == 4
    # exits observed through linked observations
    assert all(c.exit_code == 2 for c in trajectory.tool_calls)


def test_verify_before_done_true_on_pass_false_on_loop():
    idx = index()
    ok = idx.trials["job-pass/t1"].trajectory
    bad = idx.trials["job-fail/t1"].trajectory
    assert ok.verify_before_done.value is True
    assert bad.verify_before_done.value is False
    assert ok.verify_before_done.provenance == "derived"


# ---- artifacts and path jail -----------------------------------------------


def test_artifact_links_are_trial_relative_and_jailed():
    trial = index().trials["job-pass/t1"]
    names = {a.name for a in trial.artifacts}
    assert "out.txt" in names and "trajectory.json" in names
    for artifact in trial.artifacts:
        assert not artifact.relative_path.startswith("/")
        assert ".." not in artifact.relative_path


def test_jail_refuses_escape_absolute_and_hidden_paths(tmp_path: Path):
    (tmp_path / "ok.txt").write_text("x")
    assert jail(tmp_path, "ok.txt") is not None
    assert jail(tmp_path, "../outside.txt") is None
    assert jail(tmp_path, "/etc/passwd") is None
    assert jail(tmp_path, "tests/golden.csv") is None      # hidden verifier dir
    assert jail(tmp_path, "solution/solve.py") is None     # hidden oracle dir


def test_secret_shaped_config_values_are_redacted():
    trial = index().trials["job-pass/t1"]
    rendered = str(trial.config.value)
    assert "sk-should-never-render" not in rendered
    assert "[redacted]" in rendered
    assert redact_mapping({"MY_TOKEN": "x"})["MY_TOKEN"] == "[redacted]"


# ---- analyses and citations -------------------------------------------------


def test_valid_citation_resolves_to_step_and_call():
    views = {a.analysis_id: a for a in index().analyses}
    valid = views["11111111-1111-4111-8111-111111111111"]
    assert valid.trial_key == "job-fail/t1"
    assert valid.status.provenance == "observed"
    assert valid.validity.provenance == "draft"  # model output stays draft
    (citation,) = valid.citations
    assert citation.resolution.value == "resolved"


def test_invalid_citation_is_flagged_not_hidden():
    views = {a.analysis_id: a for a in index().analyses}
    bad = views["11111111-1111-4111-8111-111111111112"]
    (citation,) = bad.citations
    assert citation.resolution.provenance == "unavailable"
    assert "step 99" in (citation.resolution.reason or "")


def test_malformed_sidecar_becomes_a_note():
    idx = index()
    assert any("broken.json" in note for note in idx.notes)


# ---- duplicates, cold start, degradation ------------------------------------


def test_duplicate_trial_keys_are_skipped_and_noted(tmp_path: Path):
    for root in ("a", "b"):
        src = JOBS / "job-pass"
        shutil.copytree(src, tmp_path / root / "job-pass")
    idx = build_index([tmp_path / "a", tmp_path / "b"])
    assert len([k for k in idx.trials if k == "job-pass/t1"]) == 1
    dup_notes = [n for job in idx.jobs for n in job.notes]
    assert any("duplicate trial key" in n for n in dup_notes)


def test_cold_start_stays_navigable(tmp_path: Path):
    idx = build_index([tmp_path / "nowhere"], tmp_path / "no-analyses")
    assert idx.trials == {} and idx.jobs == () and idx.tasks == ()
    assert any("cold start" in n for n in idx.notes)
    assert any("unavailable" in n for n in idx.notes)


# ---- status/explorer consistency --------------------------------------------


def test_explorer_agrees_with_status_snapshot_on_rewards():
    idx = index()
    # FIXTURES has jobs/ and no src/evallab -> status scratch layout applies
    snapshot = build_status_snapshot(FIXTURES)
    status_text = snapshot.model_dump_json()
    # every observed reward the explorer shows exists in the status projection
    for key in ("job-pass/t1", "job-fail/t1"):
        trial = idx.trials[key]
        assert trial.job_name in status_text
    exc = idx.trials["job-exc/t1"]
    assert exc.outcome_class.value == "infra-exception"
    assert "AgentTimeoutError" in status_text


# ---- next actions: strings only ---------------------------------------------


def test_next_actions_are_copyable_and_real_verbs():
    task_cmds = [a.command for a in next_actions_for_task("lab/demo", "library/tasks/demo")]
    assert any(c.startswith("uv run evallab run --task library/tasks/demo --agent oracle")
               for c in task_cmds)
    trial = index().trials["job-fail/t1"]
    trial_cmds = [a.command for a in next_actions_for_trial(trial)]
    assert any(c.startswith("harbor view ") for c in trial_cmds)
    assert any("evallab analyze plan" in c for c in trial_cmds)
    queue_cmds = [a.command for a in next_actions_for_queue()]
    assert any("evallab submit" in c for c in queue_cmds)
    assert any("evallab approve" in c for c in queue_cmds)


def test_index_build_performs_zero_writes():
    before = tree_state(FIXTURES)
    index()
    index()
    assert tree_state(FIXTURES) == before
