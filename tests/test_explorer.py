"""Explorer contract tests (M005). Fixture-driven, read-only, no host state."""

from __future__ import annotations

import hashlib
import json
import shlex
import shutil
from pathlib import Path

import pytest

from evallab.atif import project_trial
from evallab.explorer import (
    TrajectoryView,
    _resolve_citation,
    _status_root_for_jobs_root,
    build_index,
    citation_state,
    content_summary,
    jail,
    next_actions_for_queue,
    next_actions_for_task,
    next_actions_for_trial,
    redact_mapping,
)
from evallab.facts import write_analysis_review
from evallab.results import load_job
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


def test_the_fixture_records_exit_codes_where_the_validated_shape_puts_them():
    """The exit codes are in the document, and `atif` reads all four of them.

    `atif._command_exit_code` (`src/evallab/atif.py:446-454`) takes an exit code
    from `observation.results[].extra.{exit_code,returncode,return_code}` and
    projects it as the derived column `command_exit_code`. Asserting it here
    fixes the fact that the data *is* in the fixture, so the explorer's
    blindness to it in the next test is a defect in the explorer's reader
    rather than a fixture that forgot to record an exit code.
    """
    # .resolve(): project_trial derives source paths relative to trial.path and
    # raises ValueError for a JobRecord loaded through a relative path.
    job = load_job((JOBS / "job-fail").resolve())
    projection = project_trial(job, job.trials[0])
    assert [t.validation_status for t in projection.trajectories] == ["valid"]
    assert sorted(
        (observation.source_call_id, observation.command_exit_code)
        for observation in projection.observations
    ) == [("L0", 2), ("L1", 2), ("L2", 2), ("L3", 2)]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "OPEN SOURCE DEFECT, the same class PR #66 fixed and did not finish: "
        "explorer.py:428 reads `obs.get('command_exit_code')` from the raw observation. "
        "`command_exit_code` is a derived projection column (atif.py:132, atif.py:744); "
        "no ATIF document carries it. A raw observation carries extra.exit_code, which is "
        "what atif.py:446-454 reads and what tests/test_truth.py writes. This assertion "
        "only ever passed because tests/fixtures/explorer invented the key, so the "
        "explorer has never shown an exit code for a real trajectory. The fix is a src "
        "change outside this mission's lease; strict xfail so the suite fails the moment "
        "explorer.py starts reading the field a real document has."
    ),
)
def test_explorer_shows_the_exit_code_of_a_failing_command():
    trajectory = index().trials["job-fail/t1"].trajectory
    assert isinstance(trajectory, TrajectoryView)
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


def test_secret_shaped_config_values_are_redacted(tmp_path: Path):
    """Redaction proven against a constructed config, no longer against fiction.

    The committed fixture used to carry `{"env": {"FAKE_API_KEY": ...}}`. Harbor
    writes no `env` mapping into a trial `config.json` — the real file holds
    `agent`, `task`, `trial_name`, `trials_dir`, `job_id` and nothing else — so
    the suite was proving the redactor against a document that cannot exist.
    The fixture now matches the real shape and the adversarial input is built
    here, which is where a hypothetical belongs.
    """
    assert redact_mapping({"MY_TOKEN": "x"})["MY_TOKEN"] == "[redacted]"
    nested = redact_mapping({"providers": [{"PASSWORD": "nested-secret"}]})
    assert "nested-secret" not in str(nested)

    trial = tmp_path / "jobs" / "job-secret" / "t1"
    write_trial(trial, steps=[{"step_id": 1, "source": "agent", "message": "m"}])
    (trial / "config.json").write_text(
        json.dumps({"agent": {"name": "codex"}, "env": {"FAKE_API_KEY": "sk-should-never-render"}})
    )
    rendered = str(build_index([tmp_path / "jobs"]).trials["job-secret/t1"].config.value)
    assert "sk-should-never-render" not in rendered
    assert "[redacted]" in rendered


def test_committed_trial_config_is_the_shape_harbor_writes():
    """A fixture config must be a document Harbor could have produced."""
    config = index().trials["job-pass/t1"].config.value
    assert config["agent"] == {"name": "codex", "model_name": "gpt-5.6-terra"}
    assert "env" not in config


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


def test_tool_call_citation_must_belong_to_the_cited_step():
    trial = index().trials["job-fail/t1"]
    citation = _resolve_citation(
        {
            "path": "agent/trajectory.json",
            "step_id": 1,
            "tool_call_id": "L2",  # exists, but only in step 2
            "supports": "wrong step",
        },
        trial,
    )
    assert citation.resolution.provenance == "unavailable"
    assert "not found in step 1" in (citation.resolution.reason or "")


def test_malformed_sidecar_becomes_a_note():
    idx = index()
    assert any("broken/analysis.json" in note for note in idx.notes)


def test_review_beside_a_sidecar_is_not_parsed_as_one(tmp_path: Path):
    """M009 F-03: `analyze review` output must never corrupt the explorer.

    Discovery selects sidecars positively by filename, so the review written
    next to a sidecar is left alone instead of failing sidecar validation and
    pinning `unreadable (ValidationError)` to every tab, permanently.
    """
    analyses = tmp_path / "analyses"
    shutil.copytree(ANALYSES / "valid", analyses / "11111111-1111-4111-8111-111111111111")
    sidecar_path = analyses / "11111111-1111-4111-8111-111111111111" / "analysis.json"
    review_path, review = write_analysis_review(
        sidecar_path,
        disposition="accepted",
        rationale="reviewed during the M009 flight",
        reviewer="operator-fixes",
    )
    assert review_path.is_file()  # the review really is next to the sidecar

    idx = build_index([JOBS], analyses)

    (analysis,) = idx.analyses
    assert analysis.analysis_id == "11111111-1111-4111-8111-111111111111"
    assert analysis.trial_key == "job-fail/t1"
    assert analysis.status.value == "valid"
    assert analysis.category.value == "tool_use"
    (citation,) = analysis.citations
    assert citation.resolution.value == "resolved"
    assert not [note for note in idx.notes if "unreadable" in note]
    assert not [note for note in idx.notes if str(review.review_id) in note]


# ---- withheld vs missing vs readable evidence --------------------------------

PROMOTED_RUNS = Path(__file__).parents[1] / "research" / "evidence" / "runs"


def marker(text: str) -> str:
    """The exact marker `scripts/promote_codex_bundle.py::_marker` writes."""
    raw = text.encode("utf-8")
    return f"<<evallab-redacted: {len(raw)} bytes, sha256:{hashlib.sha256(raw).hexdigest()}>>"


def write_trial(trial_dir: Path, *, steps: list[dict], trial_id: str = "t-1") -> None:
    """A trial built at run time, with the envelope Harbor really writes.

    Only the envelope is guaranteed real. Callers pass `steps` deliberately,
    including malformed ones, because these documents exist to drive the
    explorer's degradation paths — see `tests/test_fixture_conformance.py` for
    why run-time documents are out of the conformance guard's scope.
    """
    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "result.json").write_text(
        json.dumps(
            {
                "id": trial_id,
                "task_name": "lab/demo",
                "trial_name": trial_dir.name,
                "agent_info": {"name": "codex", "version": "0.147.0"},
                "verifier_result": {"rewards": {"reward": 1.0}},
            }
        )
    )
    (trial_dir / "agent").mkdir(exist_ok=True)
    (trial_dir / "agent" / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "session_id": "01a00420-0d94-7d50-8e01-0000000000ff",
                "agent": {"name": "codex", "version": "0.147.0"},
                "steps": steps,
            }
        )
    )


def redacted_and_verbatim_trial(jobs: Path) -> Path:
    """One trial holding a withheld step, a readable step, and an absent one."""
    prompt = "SYSTEM PROMPT " * 40
    trial = jobs / "job-mixed" / "t1"
    write_trial(
        trial,
        steps=[
            {"step_id": 1, "source": "system", "message": marker(prompt)},
            {
                "step_id": 2,
                "source": "agent",
                "message": "I will sanitize the HTML in place.",
                "tool_calls": [
                    {"tool_call_id": "c1", "function_name": "exec", "arguments": {"cmd": "ls"}}
                ],
                "observation": {"results": [{"source_call_id": "c1", "content": "ok"}]},
            },
            {"step_id": 3, "source": "agent"},  # no message field at all
        ],
    )
    return trial


def test_withheld_step_never_renders_like_a_verbatim_one(tmp_path: Path):
    """The defect: a promoted prompt step and a real agent step looked identical.

    Only the step envelope (`step_id`, `source`, counts) was ever rendered, so
    `{'step_id': 1, 'source': 'system', 'n_tool_calls': 0}` said the same thing
    whether the text was present or removed by `promote_codex_bundle.py`.
    """
    redacted_and_verbatim_trial(tmp_path / "jobs")
    trajectory = build_index([tmp_path / "jobs"]).trials["job-mixed/t1"].trajectory
    assert isinstance(trajectory, TrajectoryView)
    hidden, readable, absent = trajectory.steps

    # three distinct states, distinguishable without opening a file
    assert (hidden.message.provenance, readable.message.provenance, absent.message.provenance) == (
        "withheld",
        "observed",
        "unavailable",
    )
    assert hidden.message != readable.message
    # the withheld state keeps the audit trail that is already in the marker
    prompt = "SYSTEM PROMPT " * 40
    (audit,) = hidden.message.value["markers"]
    assert audit["bytes"] == len(prompt.encode("utf-8"))
    assert audit["digest"] == f"sha256:{hashlib.sha256(prompt.encode()).hexdigest()}"
    assert hidden.message.value["readable_chars"] == 0
    assert readable.message.value["readable_chars"] == len("I will sanitize the HTML in place.")
    # and the trial states it up front rather than only per step
    assert trajectory.redaction.provenance == "withheld"
    assert trajectory.redaction.value["steps_withheld"] == 1
    assert trajectory.redaction.value["withheld_bytes"] == len(prompt.encode("utf-8"))


def test_citation_into_a_withheld_step_is_marked_withheld(tmp_path: Path):
    """A citation may resolve perfectly and still point at nothing readable."""
    trial_dir = redacted_and_verbatim_trial(tmp_path / "jobs")
    trial = build_index([tmp_path / "jobs"]).trials["job-mixed/t1"]

    into_prompt = _resolve_citation(
        {"path": "agent/trajectory.json", "step_id": 1, "supports": "the instructions"}, trial
    )
    into_agent = _resolve_citation(
        {"path": "agent/trajectory.json", "step_id": 2, "supports": "what the agent did"}, trial
    )
    into_call = _resolve_citation(
        {
            "path": "agent/trajectory.json",
            "step_id": 2,
            "tool_call_id": "c1",
            "supports": "the command it ran",
        },
        trial,
    )

    # both resolve — resolution alone can never separate them
    assert into_prompt.resolution.value == into_agent.resolution.value == "resolved"
    # content does
    assert into_prompt.content.provenance == "withheld"
    assert into_agent.content.provenance == "observed"
    assert into_call.content.provenance == "observed"
    assert into_prompt.content.value["withheld_bytes"] == 560
    assert into_prompt.content.value["markers"][0]["digest"].startswith("sha256:")
    assert trial_dir.exists()


def test_unresolvable_citation_content_is_absent_not_withheld(tmp_path: Path):
    """`missing` and `withheld` are different claims and must not collapse."""
    redacted_and_verbatim_trial(tmp_path / "jobs")
    trial = build_index([tmp_path / "jobs"]).trials["job-mixed/t1"]
    gone = _resolve_citation(
        {"path": "agent/nothing-here.json", "supports": "a file that never existed"}, trial
    )
    assert gone.resolution.provenance == "unavailable"
    assert gone.content.provenance == "unavailable"
    assert "withheld" not in (gone.content.reason or "")


def test_promoted_codex_evidence_reports_its_withheld_bytes_and_digest():
    """Measured against the committed bundle, not a fixture (PR #58, rule R1)."""
    trial_dir = (
        PROMOTED_RUNS
        / "canary-terminal-bench-html-js-filter-codex-20260815"
        / "terminal-bench-html-js-filter__5rgjEEt"
    )
    assert trial_dir.is_dir(), "promoted evidence is immutable; it must still be here"
    trajectory = build_index([PROMOTED_RUNS]).trials[
        f"{trial_dir.parent.name}/{trial_dir.name}"
    ].trajectory
    assert isinstance(trajectory, TrajectoryView)
    step_one, step_six = trajectory.steps[0], trajectory.steps[5]

    assert step_one.message.provenance == "withheld"
    assert step_one.message.value["withheld_bytes"] == 4876
    assert step_one.message.value["markers"][0]["digest"] == (
        "sha256:6866d85ebcbbc2331083e0522d413dc0d18ffde525370a1feada271f40f7d858"
    )
    assert step_one.message.value["readable_chars"] == 0
    assert step_six.message.provenance == "observed"
    assert step_six.message.value["readable_chars"] == 283
    # the observations promotion kept verbatim are visible, not counted as zero
    assert step_six.n_observations == 1
    assert trajectory.redaction.value["sources"] == ("system", "user")


def promoted_trial():
    job = "canary-terminal-bench-html-js-filter-codex-20260815"
    return build_index([PROMOTED_RUNS]).trials[f"{job}/terminal-bench-html-js-filter__5rgjEEt"]


def test_redacted_artifact_states_how_little_of_it_survived():
    """`verifier/test-stdout.redacted.txt` is 107 bytes of a 77,080-byte original.

    The artifacts table used to show only the promoted size, which reads as a
    complete 107-byte verifier log rather than as almost all of it removed.
    Reported from `PROMOTION.json`, which records both sizes and the parent
    digest, so nothing is inferred from file names.
    """
    artifacts = {a.relative_path: a for a in promoted_trial().artifacts}
    stdout = artifacts["verifier/test-stdout.redacted.txt"]

    assert stdout.size_bytes == 107
    assert stdout.content.provenance == "withheld"
    assert stdout.content.value["withheld_bytes"] == 77080 - 107
    assert stdout.content.value["markers"][0]["bytes"] == 77080
    assert stdout.content.value["rule"] == "R3"
    # a file promotion kept whole is not labelled withheld
    assert artifacts["verifier/reward.txt"].content.provenance == "observed"


def test_files_promotion_removed_entirely_are_still_reported():
    """Rule R2 drops the raw rollout, so no artifact list can ever show it."""
    omitted = promoted_trial().omitted_files

    assert omitted.provenance == "withheld"
    assert omitted.value["withheld_bytes"] == 194005
    (record,) = omitted.value["markers"]
    assert record["path"].startswith("agent/sessions/")
    assert record["rule"] == "R2"
    assert record["digest"].startswith("sha256:")


def test_live_run_artifacts_are_not_labelled_redacted(tmp_path: Path):
    """Only promotion redacts. A run directory has no manifest and no claim."""
    trial = tmp_path / "jobs" / "job-live" / "t1"
    write_trial(trial, steps=[{"step_id": 1, "source": "agent", "message": "hi"}])
    (trial / "artifacts").mkdir()
    (trial / "artifacts" / "out.txt").write_text("everything is here")

    (artifact,) = [
        a
        for a in build_index([tmp_path / "jobs"]).trials["job-live/t1"].artifacts
        if a.name == "out.txt"
    ]
    assert artifact.content.provenance == "derived"
    assert "nothing here was redacted" in (artifact.content.reason or "")
    assert build_index([tmp_path / "jobs"]).trials["job-live/t1"].omitted_files.value == ()


def test_rendered_summaries_of_the_three_states_are_all_different(tmp_path: Path):
    """What a surface actually shows must differ, not only the internal label.

    `dashboard/explorer.py` renders `content_summary` verbatim. Streamlit is
    deliberately not a project dependency, so the sentence is asserted here —
    the page is a thin map from these strings to a glyph.
    """
    redacted_and_verbatim_trial(tmp_path / "jobs")
    trajectory = build_index([tmp_path / "jobs"]).trials["job-mixed/t1"].trajectory
    assert isinstance(trajectory, TrajectoryView)
    hidden, readable, absent = (content_summary(s.message) for s in trajectory.steps)

    assert len({hidden, readable, absent}) == 3
    assert hidden.startswith("withheld 560 bytes (sha256:")
    assert "chars readable" not in hidden  # nothing readable at all here
    assert readable == "readable · 34 chars"
    assert absent.startswith("unavailable:")


def test_citation_states_separate_withheld_from_readable_and_missing(tmp_path: Path):
    redacted_and_verbatim_trial(tmp_path / "jobs")
    trial = build_index([tmp_path / "jobs"]).trials["job-mixed/t1"]

    def state(**citation):
        resolved = _resolve_citation({"path": "agent/trajectory.json", **citation}, trial)
        return citation_state(resolved)

    assert state(step_id=1) == "withheld"
    assert state(step_id=2) == "readable"
    assert state(step_id=2, tool_call_id="c1") == "readable"
    assert state(step_id=99) == "unresolved"
    assert citation_state(_resolve_citation({"path": "agent/gone.json"}, trial)) == "unresolved"


def test_partly_withheld_file_citation_reports_what_is_left(tmp_path: Path):
    """Promotion also redacts oversize verifier strings in place (rule R3a)."""
    trial_dir = redacted_and_verbatim_trial(tmp_path / "jobs")
    (trial_dir / "verifier").mkdir()
    body = "PASS 3 of 3\n"
    (trial_dir / "verifier" / "ctrf.redacted.json").write_text(body + marker("x" * 9000))
    trial = build_index([tmp_path / "jobs"]).trials["job-mixed/t1"]

    cited = _resolve_citation(
        {"path": "verifier/ctrf.redacted.json", "supports": "the verifier output"}, trial
    )

    assert cited.resolution.value == "resolved"
    assert citation_state(cited) == "withheld"
    assert cited.content.value["withheld_bytes"] == 9000
    assert cited.content.value["readable_chars"] == len(body)
    assert "chars readable" in content_summary(cited.content)


# ---- F-04: a nested jobs_dir is named, never silently dropped ----------------


def test_nested_jobs_dir_run_is_named_with_its_location_not_dropped(tmp_path: Path):
    """F-04, proven by A/B: the same job, flat and nested.

    `ExperimentSpec.jobs_dir` is free-form (`schemas.py:27`) while every reader
    of these directories — the executor at `runner.py:601` and Harbor's own
    viewer at `harbor/viewer/scanner.py:50,86` — addresses a job as
    `<jobs-root>/<job>/<trial>`. The explorer used to render the intermediate
    directory as a job with no trials and say nothing about the real run.
    """
    flat, nested = tmp_path / "flat", tmp_path / "nested"
    shutil.copytree(JOBS / "job-pass", flat / "job-pass")
    shutil.copytree(JOBS / "job-pass", nested / "nightly" / "2026-08-16" / "job-pass")

    control = build_index([flat])
    subject = build_index([nested])

    assert list(control.trials) == ["job-pass/t1"]  # the A side still works
    # no phantom job stands in for the run that is out of reach
    assert [job.job_name for job in subject.jobs] == []
    # and the reader is told exactly what exists, where, and what to do
    (located,) = [n for n in subject.notes if "nightly/" in n]
    assert "nightly/2026-08-16/job-pass (1 trial)" in located
    assert "nightly/2026-08-16" in located.split("add a jobs root at")[1]
    # the remedy the note names is the one that works
    assert list(build_index([nested / "nightly" / "2026-08-16"]).trials) == ["job-pass/t1"]


def test_job_roll_up_result_is_never_counted_as_a_trial(tmp_path: Path):
    """The phantom job came from reading a job's own `result.json` as a trial."""
    jobs = tmp_path / "jobs"
    shutil.copytree(JOBS / "job-pass", jobs / "nightly" / "job-pass")

    idx = build_index([jobs])

    assert "nightly/job-pass" not in idx.trials
    assert [job.job_name for job in idx.jobs] == []


def test_directory_with_no_trials_is_reported_rather_than_rendered(tmp_path: Path):
    jobs = tmp_path / "jobs"
    (jobs / "empty-job").mkdir(parents=True)
    idx = build_index([jobs])
    assert idx.jobs == ()
    assert any("empty-job/ holds no trial result" in note for note in idx.notes)


def test_analysis_whose_source_trial_is_not_indexed_says_why(tmp_path: Path):
    """The F-04 symptom the human actually saw: `unlinked` with no reason."""
    analyses = tmp_path / "analyses"
    shutil.copytree(ANALYSES / "valid", analyses / "valid")

    idx = build_index([tmp_path / "no-jobs-here"], analyses)

    (analysis,) = idx.analyses
    assert analysis.trial_key is None
    assert analysis.link.provenance == "unavailable"
    assert "was not found among the 0 trials discovered" in (analysis.link.reason or "")
    assert any("is not under any configured jobs root" in note for note in idx.notes)
    # every citation says there is nothing to read, rather than resolving
    assert all(c.content.provenance == "unavailable" for c in analysis.citations)


# ---- duplicates, cold start, degradation ------------------------------------


def test_duplicate_trial_keys_are_skipped_and_noted(tmp_path: Path):
    for root in ("a", "b"):
        src = JOBS / "job-pass"
        shutil.copytree(src, tmp_path / root / "job-pass")
    idx = build_index([tmp_path / "a", tmp_path / "b"])
    assert len([k for k in idx.trials if k == "job-pass/t1"]) == 1
    dup_notes = [n for job in idx.jobs for n in job.notes]
    assert any("duplicate trial key" in n for n in dup_notes)


def test_executor_bookkeeping_is_not_listed_as_a_job(tmp_path: Path):
    """M009 F-08: `.executor` is executor state, not evaluation output."""
    jobs = tmp_path / "jobs"
    shutil.copytree(JOBS / "job-pass", jobs / "real-job")
    (jobs / ".executor" / "leases").mkdir(parents=True)
    (jobs / ".executor" / "state.json").write_text("{}")
    (jobs / ".tombstones").mkdir()

    idx = build_index([jobs])

    assert [job.job_name for job in idx.jobs] == ["real-job"]
    assert all(not name.startswith(".") for name in idx.trials)


def test_duplicate_trial_ids_leave_analysis_unlinked(tmp_path: Path):
    jobs = tmp_path / "jobs"
    shutil.copytree(JOBS / "job-pass", jobs / "job-one")
    shutil.copytree(JOBS / "job-pass", jobs / "job-two")
    analyses = tmp_path / "analyses"
    shutil.copytree(ANALYSES / "badstep", analyses / "badstep")
    idx = build_index([jobs], analyses)
    assert idx.analyses[0].trial_key is None
    assert any("source trial id" in note and "duplicated" in note for note in idx.notes)


def test_cold_start_stays_navigable(tmp_path: Path):
    idx = build_index([tmp_path / "nowhere"], tmp_path / "no-analyses")
    assert idx.trials == {} and idx.jobs == () and idx.tasks == ()
    assert any("cold start" in n for n in idx.notes)
    assert any("unavailable" in n for n in idx.notes)


def test_malformed_result_shapes_degrade_without_raising(tmp_path: Path):
    trial = tmp_path / "jobs" / "bad-job" / "t1"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(
        '{"task_name":"bad","agent_info":[],"verifier_result":'
        '{"rewards":{"reward":"not-a-number"}}}'
    )
    (trial / "config.json").write_text("{}")
    view = build_index([tmp_path / "jobs"]).trials["bad-job/t1"]
    assert view.reward.provenance == "unavailable"
    assert view.outcome_class.provenance == "unavailable"
    assert view.config.provenance == "observed"


def test_registry_absence_is_observed_when_registry_is_loaded(tmp_path: Path):
    registry = tmp_path / "registry"
    registry.mkdir()
    idx = build_index([JOBS], registry_dir=registry)
    assert idx.tasks[0].registration.value == "not registered"
    assert idx.tasks[0].registration.provenance == "observed"


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
    viewer = next(c for c in trial_cmds if c.startswith("harbor view "))
    assert shlex.split(viewer) == ["harbor", "view", trial.jobs_root, "--jobs"]
    assert any("evallab analyze plan" in c for c in trial_cmds)
    infra_commands = [a.command for a in next_actions_for_trial(index().trials["job-exc/t1"])]
    status = next(c for c in infra_commands if "evallab status" in c)
    assert shlex.split(status)[-1] == str(FIXTURES.resolve())
    queue_cmds = [a.command for a in next_actions_for_queue()]
    assert any("evallab submit" in c for c in queue_cmds)
    assert any("evallab approve" in c for c in queue_cmds)
    assert all("<" not in c and "$" not in c for c in task_cmds + trial_cmds + queue_cmds)


def test_next_action_sanitizes_untrusted_task_names_and_quotes_paths():
    commands = next_actions_for_task("lab/$(touch bad)", "path with spaces/task")
    for action in commands:
        assert "$(" not in action.command
        tokens = shlex.split(action.command)
        assert tokens[tokens.index("--task") + 1] == "path with spaces/task"


def test_status_root_handles_repo_and_promoted_evidence_layouts(tmp_path: Path):
    assert _status_root_for_jobs_root(tmp_path / "runs") == tmp_path.resolve()
    promoted = tmp_path / "research" / "evidence" / "runs"
    assert _status_root_for_jobs_root(promoted) == tmp_path.resolve()


def test_index_build_performs_zero_writes():
    before = tree_state(FIXTURES)
    index()
    index()
    assert tree_state(FIXTURES) == before
