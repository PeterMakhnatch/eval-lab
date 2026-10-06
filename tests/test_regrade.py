from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.regrade import (
    RegradeJobVerdict,
    RegradeRefusalCode,
    RegradeVerdict,
    plan_regrade_job,
    regrade_job,
    regrade_trial,
    verifier_identity,
)

VERIFIER_TABLE = """
schema_version = "1.4"

[task]
name = "local-lab/probe"

[verifier]
timeout_sec = 60.0
environment_mode = "separate"
"""


def _task(root: Path, *, mode: str = "separate", verify_body: str = "pass\n") -> Path:
    task = root / "task"
    (task / "tests").mkdir(parents=True, exist_ok=True)
    task.joinpath("task.toml").write_text(VERIFIER_TABLE.replace("separate", mode), "utf-8")
    task.joinpath("tests", "verify.py").write_text(verify_body, "utf-8")
    return task


def _trial(root: Path, name: str, rewards: dict[str, float] | None) -> Path:
    trial = root / name
    (trial / "agent").mkdir(parents=True, exist_ok=True)
    (trial / "artifacts").mkdir(parents=True, exist_ok=True)
    trial.joinpath("agent", "trajectory.json").write_text('{"steps": []}', "utf-8")
    trial.joinpath("artifacts", "manifest.json").write_text("[]", "utf-8")
    result: dict[str, object] = {"trial_name": name, "task_name": "local-lab/probe"}
    if rewards is not None:
        result["verifier_result"] = {"rewards": rewards}
        result["verifier_environment_mode"] = "separate"
    trial.joinpath("result.json").write_text(json.dumps(result), "utf-8")
    return trial


def _runner_writing(rewards: dict[str, float], *, exit_code: int = 0):
    """Stand in for Harbor: materialise the regrade trial the command names."""

    def run(command: list[str], **_: object) -> SimpleNamespace:
        trials_dir = Path(command[command.index("--trials-dir") + 1])
        name = command[command.index("--trial-name") + 1]
        if exit_code == 0:
            _trial(trials_dir, name, rewards)
        return SimpleNamespace(returncode=exit_code, stderr="")

    return run


def test_verifier_identity_tracks_build_context_content(tmp_path: Path) -> None:
    """`same_verifier` decides determinism-probe vs hardening, so identity must follow bytes."""
    task = _task(tmp_path, verify_body="assert True\n")
    before = verifier_identity(task).digest

    assert verifier_identity(task).digest == before

    task.joinpath("tests", "verify.py").write_text("assert False\n", "utf-8")
    assert verifier_identity(task).digest != before

    task.joinpath("tests", "__pycache__").mkdir()
    task.joinpath("tests", "__pycache__", "verify.pyc").write_bytes(b"cache")
    assert verifier_identity(task).digest != before
    assert verifier_identity(task).context_file_count == 1


def test_shared_mode_task_refuses_instead_of_regrading(tmp_path: Path) -> None:
    """A non-isolated verifier cannot be regraded; refusing beats an unattributable reward."""
    task = _task(tmp_path, mode="shared")
    trial = _trial(tmp_path, "trial-a", {"reward": 1.0})
    invoked: list[list[str]] = []

    receipt = regrade_trial(
        trial_dir=trial,
        task_dir=task,
        trials_dir=tmp_path / "out",
        runner=lambda command, **_: invoked.append(command) or SimpleNamespace(returncode=0),
    )

    assert receipt.verdict is RegradeVerdict.REFUSED
    assert RegradeRefusalCode.VERIFIER_NOT_ISOLATED in receipt.refusals
    assert invoked == []
    assert receipt.recorded is not None and receipt.recorded.primary == 1.0
    assert receipt.regraded is None


def test_missing_artifact_manifest_refuses(tmp_path: Path) -> None:
    """Harbor regrade consumes collected artifacts; without a manifest there is nothing to score."""
    task = _task(tmp_path)
    trial = _trial(tmp_path, "trial-b", {"reward": 1.0})
    trial.joinpath("artifacts", "manifest.json").unlink()

    receipt = regrade_trial(
        trial_dir=trial,
        task_dir=task,
        trials_dir=tmp_path / "out",
        runner=_runner_writing({"reward": 1.0}),
    )

    assert receipt.verdict is RegradeVerdict.REFUSED
    assert RegradeRefusalCode.SOURCE_ARTIFACT_MANIFEST_MISSING in receipt.refusals


def test_reward_drop_under_a_changed_verifier_is_tightened(tmp_path: Path) -> None:
    """The hardening loop: same trajectory, stricter verifier, reward falls."""
    task = _task(tmp_path)
    trial = _trial(tmp_path, "trial-c", {"reward": 1.0, "correctness": 1.0})

    receipt = regrade_trial(
        trial_dir=trial,
        task_dir=task,
        trials_dir=tmp_path / "out",
        runner=_runner_writing({"reward": 0.0, "correctness": 0.0}),
    )

    assert receipt.verdict is RegradeVerdict.TIGHTENED
    assert receipt.reward_delta == {"correctness": -1.0, "reward": -1.0}
    assert receipt.same_verifier is False


def test_reward_rise_under_a_changed_verifier_is_loosened(tmp_path: Path) -> None:
    """A verifier edit that starts accepting a previously failing trajectory must be visible."""
    task = _task(tmp_path)
    trial = _trial(tmp_path, "trial-d", {"reward": 0.0})

    receipt = regrade_trial(
        trial_dir=trial,
        task_dir=task,
        trials_dir=tmp_path / "out",
        runner=_runner_writing({"reward": 1.0}),
    )

    assert receipt.verdict is RegradeVerdict.LOOSENED
    assert receipt.reward_delta == {"reward": 1.0}


def test_split_direction_and_new_dimensions_are_repartitioned(tmp_path: Path) -> None:
    """Dimensions moving both ways, or a changed dimension set, is not tighter or looser."""
    task = _task(tmp_path)
    trial = _trial(tmp_path, "trial-e", {"reward": 1.0, "hygiene": 0.0})
    mixed = regrade_trial(
        trial_dir=trial,
        task_dir=task,
        trials_dir=tmp_path / "mixed",
        runner=_runner_writing({"reward": 0.0, "hygiene": 1.0}),
    )
    assert mixed.verdict is RegradeVerdict.REPARTITIONED

    trial_f = _trial(tmp_path, "trial-f", {"reward": 1.0})
    added = regrade_trial(
        trial_dir=trial_f,
        task_dir=task,
        trials_dir=tmp_path / "added",
        runner=_runner_writing({"reward": 1.0, "tool_discipline": 1.0}),
    )
    assert added.verdict is RegradeVerdict.REPARTITIONED


def test_identical_verifier_probes_grader_determinism(tmp_path: Path) -> None:
    """Re-scoring with a byte-identical verifier must reproduce the reward, or the grader is broken."""
    task = _task(tmp_path)
    trial = _trial(tmp_path, "trial-g", {"reward": 1.0})
    # A receipt beside the trial records which verifier produced its reward.
    trial.joinpath("regrade-receipt.json").write_text(
        json.dumps({"verifier": {"digest": verifier_identity(task).digest}}), "utf-8"
    )

    stable = regrade_trial(
        trial_dir=trial,
        task_dir=task,
        trials_dir=tmp_path / "stable",
        runner=_runner_writing({"reward": 1.0}),
    )
    assert stable.same_verifier is True
    assert stable.verdict is RegradeVerdict.DETERMINISTIC

    flaky = regrade_trial(
        trial_dir=trial,
        task_dir=task,
        trials_dir=tmp_path / "flaky",
        runner=_runner_writing({"reward": 0.0}),
    )
    assert flaky.verdict is RegradeVerdict.NONDETERMINISTIC
    assert flaky.reward_delta == {"reward": -1.0}


def test_failed_harbor_invocation_never_reports_a_reward(tmp_path: Path) -> None:
    """A non-zero exit must not be read as a score of zero."""
    task = _task(tmp_path)
    trial = _trial(tmp_path, "trial-h", {"reward": 1.0})

    receipt = regrade_trial(
        trial_dir=trial,
        task_dir=task,
        trials_dir=tmp_path / "out",
        runner=_runner_writing({"reward": 0.0}, exit_code=2),
    )

    assert receipt.verdict is RegradeVerdict.REFUSED
    assert RegradeRefusalCode.HARBOR_INVOCATION_FAILED in receipt.refusals
    assert receipt.regraded is None


def test_receipt_binds_source_bytes_and_leaves_the_source_untouched(tmp_path: Path) -> None:
    """A receipt is about specific bytes; the trial it re-scores is read-only evidence."""
    task = _task(tmp_path)
    trial = _trial(tmp_path, "trial-i", {"reward": 1.0})
    before = sorted(p.name for p in trial.iterdir())

    receipt = regrade_trial(
        trial_dir=trial,
        task_dir=task,
        trials_dir=tmp_path / "out",
        runner=_runner_writing({"reward": 1.0}),
    )

    assert receipt.source.trajectory_digest is not None
    assert receipt.source.artifact_manifest_digest is not None
    assert receipt.verifier.digest == verifier_identity(task).digest
    assert sorted(p.name for p in trial.iterdir()) == before
    assert json.loads(trial.joinpath("result.json").read_text())["verifier_result"]["rewards"] == {
        "reward": 1.0
    }
    written = json.loads(
        Path(receipt.regrade_trial_dir or "").joinpath("regrade-receipt.json").read_text()
    )
    assert written["source"]["trajectory_digest"] == receipt.source.trajectory_digest


def _job_task(
    root: Path,
    dirname: str,
    task_name: str,
    *,
    mode: str | None = "separate",
    verify_body: str = "pass\n",
    extra_verifier: str = "",
) -> Path:
    """A task directory with a chosen name, for multi-task job fixtures."""
    task = root / dirname
    (task / "tests").mkdir(parents=True, exist_ok=True)
    mode_line = f'environment_mode = "{mode}"\n' if mode is not None else ""
    task.joinpath("task.toml").write_text(
        "schema_version = \"1.4\"\n"
        "\n"
        "[task]\n"
        f'name = "{task_name}"\n'
        "\n"
        "[verifier]\n"
        "timeout_sec = 60.0\n"
        f"{mode_line}{extra_verifier}",
        "utf-8",
    )
    task.joinpath("tests", "verify.py").write_text(verify_body, "utf-8")
    return task


def _job(root: Path, name: str, *, harbor_version: str | None = "0.24.0") -> Path:
    """A Harbor-shaped source job directory with lock provenance."""
    job = root / name
    job.mkdir(parents=True, exist_ok=True)
    job.joinpath("config.json").write_text(json.dumps({"job_name": name}), "utf-8")
    if harbor_version is not None:
        job.joinpath("lock.json").write_text(
            json.dumps({"harbor": {"version": harbor_version}}), "utf-8"
        )
    job.joinpath("result.json").write_text(json.dumps({"job_name": name}), "utf-8")
    return job


def _job_trial(
    job: Path,
    name: str,
    task_name: str,
    rewards: dict[str, float],
    *,
    task_path: Path | None = None,
    manifest: bool = True,
    trial_id: str = "11111111-1111-4111-8111-111111111111",
) -> Path:
    """A recorded trial with Harbor-shaped result/config, for job fixtures."""
    trial = job / name
    (trial / "agent").mkdir(parents=True, exist_ok=True)
    (trial / "artifacts").mkdir(parents=True, exist_ok=True)
    trial.joinpath("agent", "trajectory.json").write_text('{"steps": []}', "utf-8")
    if manifest:
        trial.joinpath("artifacts", "manifest.json").write_text("[]", "utf-8")
    trial.joinpath("result.json").write_text(
        json.dumps(
            {
                "id": trial_id,
                "trial_name": name,
                "task_name": task_name,
                "agent_info": {"name": "oracle"},
                "verifier_result": {"rewards": rewards},
                "verifier_environment_mode": "separate",
                "exception_info": None,
            }
        ),
        "utf-8",
    )
    task_entry: dict[str, object] = (
        {"path": str(task_path)} if task_path is not None else {}
    )
    trial.joinpath("config.json").write_text(
        json.dumps(
            {"task": task_entry, "agent": {"name": "oracle"}, "trial_name": name}
        ),
        "utf-8",
    )
    return trial


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    """File bytes under a directory, to prove the source job stayed read-only."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _job_runner(
    regraded_by_trial: dict[str, dict[str, float]], *, exit_code: int = 0
):
    """Stand in for `harbor job regrade`: materialise the job it names.

    Writes a standard jobs-root/job/trial shape — one output trial per
    source trial, each config carrying `source_trial.path` back to its
    source — so the collector is tested against real on-disk structure.
    """
    calls: list[list[str]] = []

    def run(command: list[str], **_: object) -> SimpleNamespace:
        calls.append(list(command))
        source = Path(command[command.index("regrade") + 1])
        jobs_dir = Path(command[command.index("--jobs-dir") + 1])
        job_name = command[command.index("--job-name") + 1]
        if exit_code == 0:
            new_job = jobs_dir / job_name
            new_job.mkdir(parents=True, exist_ok=True)
            new_job.joinpath("config.json").write_text(
                json.dumps({"job_name": job_name}), "utf-8"
            )
            new_job.joinpath("lock.json").write_text(
                json.dumps({"harbor": {"version": "0.24.0"}}), "utf-8"
            )
            for trial_dir in sorted(source.iterdir(), key=lambda p: p.name):
                if not trial_dir.is_dir():
                    continue
                result_path = trial_dir / "result.json"
                if not result_path.is_file():
                    continue
                result = json.loads(result_path.read_text("utf-8"))
                out = new_job / f"{trial_dir.name}__regrade"
                out.mkdir(parents=True, exist_ok=True)
                rewards = regraded_by_trial.get(trial_dir.name, {})
                out.joinpath("result.json").write_text(
                    json.dumps(
                        {
                            "trial_name": out.name,
                            "task_name": result.get("task_name"),
                            "verifier_result": {"rewards": rewards},
                            "verifier_environment_mode": "separate",
                        }
                    ),
                    "utf-8",
                )
                out.joinpath("config.json").write_text(
                    json.dumps(
                        {
                            "trial_name": out.name,
                            "source_trial": {
                                "action": "regrade",
                                "type": "local",
                                "trial_id": result.get("id"),
                                "path": str(trial_dir.resolve()),
                            },
                        }
                    ),
                    "utf-8",
                )
        return SimpleNamespace(returncode=exit_code, stderr="")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_job_regrade_compares_all_dims_and_keeps_them_separate(tmp_path: Path) -> None:
    """The job receipt carries old and new reward dims per trial, never merged."""
    task = _job_task(tmp_path, "probe", "local-lab/probe")
    job = _job(tmp_path, "source-job")
    _job_trial(
        job,
        "trial-a",
        "local-lab/probe",
        {"reward": 1.0, "integrity": 1.0},
        task_path=task,
        trial_id="11111111-1111-4111-8111-111111111111",
    )
    _job_trial(
        job,
        "trial-b",
        "local-lab/probe",
        {"reward": 0.0, "integrity": 1.0},
        task_path=task,
        trial_id="22222222-2222-4222-8222-222222222222",
    )
    before = _snapshot_tree(job)
    runner = _job_runner(
        {
            "trial-a": {"reward": 0.0, "integrity": 1.0},
            "trial-b": {"reward": 0.0, "integrity": 0.0},
        }
    )

    receipt = regrade_job(
        job_dir=job,
        task_dir=task,
        jobs_dir=tmp_path / "jobs",
        name="regraded",
        runner=runner,
    )

    assert receipt.verdict is RegradeJobVerdict.COMPLETE
    assert receipt.n_compared == 2 and receipt.n_refused == 0
    assert receipt.source_harbor_version == "0.24.0"
    assert receipt.task_dirs == [str(task)]
    assert receipt.job_dir == str(tmp_path / "jobs" / "regraded")
    by_name = {entry.source.trial_name: entry for entry in receipt.trials}
    assert by_name["trial-a"].recorded is not None
    assert by_name["trial-a"].recorded.rewards == {"reward": 1.0, "integrity": 1.0}
    assert by_name["trial-a"].regraded is not None
    assert by_name["trial-a"].regraded.rewards == {"reward": 0.0, "integrity": 1.0}
    assert by_name["trial-a"].reward_delta == {"reward": -1.0}
    assert by_name["trial-b"].reward_delta == {"integrity": -1.0}
    # Recorded and regraded are distinct observations, not aliases.
    assert by_name["trial-a"].recorded is not by_name["trial-a"].regraded
    # Standard jobs-root/job/trial shape with receipts beside Harbor files.
    new_job = tmp_path / "jobs" / "regraded"
    assert new_job.joinpath("regrade-job-receipt.json").is_file()
    for trial_name in ("trial-a__regrade", "trial-b__regrade"):
        assert new_job.joinpath(trial_name, "result.json").is_file()
        assert new_job.joinpath(trial_name, "regrade-receipt.json").is_file()
    written = json.loads(new_job.joinpath("regrade-job-receipt.json").read_text())
    assert written["verdict"] == "complete" and len(written["trials"]) == 2
    # Original evidence is immutable: lock, manifest, rewards untouched.
    assert _snapshot_tree(job) == before


def test_job_regrade_task_dir_defaults_to_saved_config(tmp_path: Path) -> None:
    """Omitting task_dir reuses each trial's recorded task, when it still resolves."""
    task = _job_task(tmp_path, "probe", "local-lab/probe")
    job = _job(tmp_path, "source-job")
    _job_trial(job, "trial-a", "local-lab/probe", {"reward": 1.0}, task_path=task)

    receipt = regrade_job(
        job_dir=job,
        jobs_dir=tmp_path / "jobs",
        name="regraded",
        runner=_job_runner({"trial-a": {"reward": 1.0}}),
    )

    assert receipt.verdict is RegradeJobVerdict.COMPLETE
    assert receipt.task_dirs == [str(task)]
    assert receipt.trials[0].verdict is RegradeVerdict.UNCHANGED


def test_job_regrade_refuses_when_saved_task_missing(tmp_path: Path) -> None:
    """A vanished saved task cannot be resolved; the job refuses without running."""
    task = _job_task(tmp_path, "probe", "local-lab/probe")
    job = _job(tmp_path, "source-job")
    _job_trial(
        job, "trial-a", "local-lab/probe", {"reward": 1.0}, task_path=task / "gone"
    )
    runner = _job_runner({"trial-a": {"reward": 1.0}})

    receipt = regrade_job(
        job_dir=job, jobs_dir=tmp_path / "jobs", name="regraded", runner=runner
    )

    assert receipt.verdict is RegradeJobVerdict.REFUSED
    assert RegradeRefusalCode.TASK_UNRESOLVED in receipt.refusals
    assert receipt.trials == [] and receipt.invocation is None
    assert runner.calls == []
    assert not (tmp_path / "jobs").exists()


def test_job_regrade_refuses_task_name_mismatch(tmp_path: Path) -> None:
    """A given task that names nothing in the job never silently scores it."""
    _job_task(tmp_path, "probe", "local-lab/probe")
    other = _job_task(tmp_path, "other", "local-lab/other")
    job = _job(tmp_path, "source-job")
    _job_trial(job, "trial-a", "local-lab/probe", {"reward": 1.0})
    runner = _job_runner({"trial-a": {"reward": 1.0}})

    receipt = regrade_job(
        job_dir=job,
        task_dir=other,
        jobs_dir=tmp_path / "jobs",
        name="regraded",
        runner=runner,
    )

    assert receipt.verdict is RegradeJobVerdict.REFUSED
    assert RegradeRefusalCode.TASK_NAME_MISMATCH in receipt.refusals
    assert runner.calls == []


def test_job_regrade_refuses_task_coverage_gap(tmp_path: Path) -> None:
    """A given task covering only part of a multi-task job refuses, not partial-runs."""
    probe = _job_task(tmp_path, "probe", "local-lab/probe")
    _job_task(tmp_path, "other", "local-lab/other")
    job = _job(tmp_path, "source-job")
    _job_trial(job, "trial-a", "local-lab/probe", {"reward": 1.0})
    _job_trial(job, "trial-b", "local-lab/other", {"reward": 1.0})
    runner = _job_runner({"trial-a": {"reward": 1.0}, "trial-b": {"reward": 1.0}})

    receipt = regrade_job(
        job_dir=job,
        task_dir=probe,
        jobs_dir=tmp_path / "jobs",
        name="regraded",
        runner=runner,
    )

    assert receipt.verdict is RegradeJobVerdict.REFUSED
    assert RegradeRefusalCode.TASK_COVERAGE_GAP in receipt.refusals
    assert runner.calls == []


def test_job_regrade_refuses_non_docker_environments(tmp_path: Path) -> None:
    """Cloud and custom verifier environments raise; there is no bypass flag."""
    task = _job_task(tmp_path, "probe", "local-lab/probe")
    job = _job(tmp_path, "source-job")
    _job_trial(job, "trial-a", "local-lab/probe", {"reward": 1.0}, task_path=task)
    runner = _job_runner({"trial-a": {"reward": 1.0}})

    for environment in ("daytona", "evallab.harbor_daytona:BoundedDaytonaEnvironment"):
        with pytest.raises(ValueError, match="local Docker only"):
            regrade_job(
                job_dir=job,
                task_dir=task,
                jobs_dir=tmp_path / "jobs",
                name="regraded",
                environment=environment,
                runner=runner,
            )
    assert runner.calls == []
    assert not (tmp_path / "jobs").exists()


def test_job_regrade_partial_when_a_trial_is_incomplete(tmp_path: Path) -> None:
    """One trial without a manifest is refused per-trial; the rest still compare."""
    task = _job_task(tmp_path, "probe", "local-lab/probe")
    job = _job(tmp_path, "source-job")
    _job_trial(job, "trial-a", "local-lab/probe", {"reward": 1.0}, task_path=task)
    _job_trial(
        job,
        "trial-b",
        "local-lab/probe",
        {"reward": 1.0},
        task_path=task,
        manifest=False,
    )

    receipt = regrade_job(
        job_dir=job,
        task_dir=task,
        jobs_dir=tmp_path / "jobs",
        name="regraded",
        runner=_job_runner(
            {"trial-a": {"reward": 0.0}, "trial-b": {"reward": 0.0}}
        ),
    )

    assert receipt.verdict is RegradeJobVerdict.PARTIAL
    assert receipt.n_compared == 1 and receipt.n_refused == 1
    by_name = {entry.source.trial_name: entry for entry in receipt.trials}
    assert by_name["trial-a"].verdict is RegradeVerdict.TIGHTENED
    assert by_name["trial-b"].verdict is RegradeVerdict.REFUSED
    assert (
        RegradeRefusalCode.SOURCE_ARTIFACT_MANIFEST_MISSING in by_name["trial-b"].refusals
    )
    assert by_name["trial-b"].regraded is None
    assert by_name["trial-b"].recorded is not None


def test_implicit_separate_verifier_is_accepted(tmp_path: Path) -> None:
    """A `[verifier.environment]` table implies separate mode, per Harbor."""
    task = _job_task(
        tmp_path,
        "probe",
        "local-lab/probe",
        mode=None,
        extra_verifier='[verifier.environment]\ndocker_image = "python:3.13-slim"\n',
    )
    assert verifier_identity(task).environment_mode == "separate"
    trial = _trial(tmp_path, "trial-implicit", {"reward": 1.0})
    invoked: list[list[str]] = []

    receipt = regrade_trial(
        trial_dir=trial,
        task_dir=task,
        trials_dir=tmp_path / "out",
        runner=lambda command, **_: invoked.append(command)
        or _runner_writing({"reward": 1.0})(command),
    )

    assert invoked != []
    assert receipt.verdict is RegradeVerdict.UNCHANGED


def test_job_regrade_same_verifier_is_deterministic(tmp_path: Path) -> None:
    """An unchanged verifier must reproduce the reward; the receipt says so."""
    task = _job_task(tmp_path, "probe", "local-lab/probe")
    job = _job(tmp_path, "source-job")
    trial = _job_trial(
        job, "trial-a", "local-lab/probe", {"reward": 1.0}, task_path=task
    )
    trial.joinpath("regrade-receipt.json").write_text(
        json.dumps({"verifier": {"digest": verifier_identity(task).digest}}), "utf-8"
    )

    receipt = regrade_job(
        job_dir=job,
        task_dir=task,
        jobs_dir=tmp_path / "jobs",
        name="regraded",
        runner=_job_runner({"trial-a": {"reward": 1.0}}),
    )

    assert receipt.verdict is RegradeJobVerdict.COMPLETE
    assert receipt.trials[0].same_verifier is True
    assert receipt.trials[0].verdict is RegradeVerdict.DETERMINISTIC
    assert receipt.trials[0].reward_delta == {}


def test_plan_previews_without_running_or_writing(tmp_path: Path) -> None:
    """The preview resolves tasks and eligibility with zero side effects."""
    task = _job_task(tmp_path, "probe", "local-lab/probe")
    job = _job(tmp_path, "source-job")
    _job_trial(job, "trial-a", "local-lab/probe", {"reward": 1.0}, task_path=task)

    plan = plan_regrade_job(
        job_dir=job, task_dir=task, jobs_dir=tmp_path / "jobs", name="previewed"
    )

    assert plan.runnable is True
    assert len(plan.trials) == 1 and plan.trials[0].eligible is True
    assert not (tmp_path / "jobs").exists()

    missing = plan_regrade_job(
        job_dir=tmp_path / "nope",
        task_dir=task,
        jobs_dir=tmp_path / "jobs",
        name="previewed",
    )
    assert missing.runnable is False
    assert RegradeRefusalCode.SOURCE_JOB_MISSING in missing.refusals
    assert missing.command == []


def test_job_regrade_failed_invocation_refuses_without_grades(tmp_path: Path) -> None:
    """A non-zero Harbor exit never becomes scores; the failure is receipted."""
    task = _job_task(tmp_path, "probe", "local-lab/probe")
    job = _job(tmp_path, "source-job")
    _job_trial(job, "trial-a", "local-lab/probe", {"reward": 1.0}, task_path=task)

    receipt = regrade_job(
        job_dir=job,
        task_dir=task,
        jobs_dir=tmp_path / "jobs",
        name="regraded",
        runner=_job_runner({"trial-a": {"reward": 0.0}}, exit_code=2),
    )

    assert receipt.verdict is RegradeJobVerdict.REFUSED
    assert RegradeRefusalCode.HARBOR_INVOCATION_FAILED in receipt.refusals
    assert receipt.trials == []
    assert receipt.execution is not None and receipt.execution.exit_code == 2
    assert receipt.invocation is not None


def test_job_regrade_without_lock_records_no_version_but_still_runs(
    tmp_path: Path,
) -> None:
    """lock.json is provenance, not a gate: absent version is recorded, not refused."""
    task = _job_task(tmp_path, "probe", "local-lab/probe")
    job = _job(tmp_path, "source-job", harbor_version=None)
    _job_trial(job, "trial-a", "local-lab/probe", {"reward": 1.0}, task_path=task)

    receipt = regrade_job(
        job_dir=job,
        task_dir=task,
        jobs_dir=tmp_path / "jobs",
        name="regraded",
        runner=_job_runner({"trial-a": {"reward": 1.0}}),
    )

    assert receipt.verdict is RegradeJobVerdict.COMPLETE
    assert receipt.source_harbor_version is None


@pytest.mark.parametrize("destination", ["source", "nested", "existing", "escape"])
def test_regrade_never_resumes_or_writes_into_source(
    tmp_path: Path, destination: str
) -> None:
    task = _job_task(tmp_path, "probe", "local-lab/probe")
    job = _job(tmp_path, "source-job")
    _job_trial(job, "trial-a", "local-lab/probe", {"reward": 1.0}, task_path=task)
    before = _snapshot_tree(job)
    output, name = {
        "source": (job.parent, job.name),
        "nested": (job, "replay"),
        "existing": (tmp_path, "occupied"),
        "escape": (tmp_path / "out", "../source-job"),
    }[destination]
    if destination == "existing":
        (output / name).mkdir()
    runner = _job_runner({"trial-a": {"reward": 0.0}})
    with pytest.raises(ValueError):
        regrade_job(
            job_dir=job, task_dir=task, jobs_dir=output, name=name, runner=runner
        )
    assert runner.calls == []
    assert _snapshot_tree(job) == before
