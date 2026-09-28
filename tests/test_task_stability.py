"""Verifier-stability behavior: verdicts, stability.json parsing/parquet, repeat wrapper."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab import task_stability
from evallab.harbor_repeat_verifier import (
    DEFAULT_REPEAT_N,
    VERIFIER_IMPORT_PATH,
    RepeatVerifier,
    _coerce_repeat_n,
)
from evallab.task_stability import (
    collect_jobs,
    collect_trial,
    iter_trial_dirs,
    load_manifest,
    parse_stability_file,
    read_task_stability_parquet,
    read_trial_reward,
    run_stability_jobs,
    stability_run_command,
    verdict_for,
    write_job_manifest,
    write_task_stability_parquet,
)


def test_verdict_requires_all_rewards_present_and_equal() -> None:
    assert verdict_for([0.0, 0.0, 0.0]) == "stable"
    assert verdict_for([1.0]) == "stable"
    assert verdict_for([0.0, 1.0, 0.0]) == "flipped"
    assert verdict_for([0.0, None, 0.0]) == "errored"
    assert verdict_for([None, None]) == "errored"
    assert verdict_for([]) == "errored"


def test_repeat_n_coerces_harbor_string_kwargs() -> None:
    assert _coerce_repeat_n("3") == 3
    assert _coerce_repeat_n(3) == 3
    with pytest.raises(ValueError):
        _coerce_repeat_n("0")
    with pytest.raises(ValueError):
        _coerce_repeat_n("many")


def test_parse_stability_file_missing_is_none(tmp_path: Path) -> None:
    assert parse_stability_file(tmp_path / "stability.json") is None


def test_parse_stability_file_reads_per_run_rewards(tmp_path: Path) -> None:
    path = tmp_path / "stability.json"
    path.write_text(json.dumps({"runs": [
        {"reward": 0.0}, {"reward": 1.0}, {"reward": None},
    ]}))
    parsed = parse_stability_file(path)
    assert parsed is not None
    assert parsed.method == "repeat_verifier"
    assert parsed.n_runs == 3
    assert parsed.rewards == [0.0, 1.0, None]


def test_parse_stability_file_rejects_empty_runs(tmp_path: Path) -> None:
    path = tmp_path / "stability.json"
    path.write_text(json.dumps({"runs": []}))
    with pytest.raises(ValueError):
        parse_stability_file(path)


def _trial_with_reward(tmp_path: Path, name: str, reward: str) -> Path:
    trial = tmp_path / name
    (trial / "verifier").mkdir(parents=True)
    (trial / "verifier" / "reward.txt").write_text(reward)
    (trial / "trial.log").write_text("log")
    return trial


def test_collect_plain_trial_is_single_run_nop_repeat(tmp_path: Path) -> None:
    trial = _trial_with_reward(tmp_path, "job__abc", "0\n")
    row = collect_trial("job", trial, backend="docker", manifest={},
                        produced_at="2026-09-28T00:00:00+00:00")
    assert row["method"] == "nop_repeat"
    assert row["n_runs"] == 1
    assert row["rewards"] == [0.0]
    assert row["verdict"] == "stable"
    assert row["task_version_digest"] is None


def test_collect_missing_reward_is_errored(tmp_path: Path) -> None:
    trial = tmp_path / "job__abc"
    (trial / "verifier").mkdir(parents=True)
    (trial / "trial.log").write_text("log")
    row = collect_trial("job", trial, backend="docker", manifest={},
                        produced_at="2026-09-28T00:00:00+00:00")
    assert row["rewards"] == [None]
    assert row["verdict"] == "errored"


def test_collect_prefers_stability_json_and_manifest_digests(tmp_path: Path) -> None:
    trial = _trial_with_reward(tmp_path, "job__abc", "0\n")
    (trial / "verifier" / "stability.json").write_text(json.dumps({"runs": [
        {"reward": 1.0}, {"reward": 0.0},
    ]}))
    manifest = {"job__abc": {"task_version_digest": "sha256:x", "harbor_digest": "sha256:y"}}
    row = collect_trial("job", trial, backend="docker", manifest=manifest,
                        produced_at="2026-09-28T00:00:00+00:00")
    assert row["method"] == "repeat_verifier"
    assert row["n_runs"] == 2
    assert row["verdict"] == "flipped"
    assert row["task_version_digest"] == "sha256:x"


def test_collect_jobs_sorts_and_skips_job_files(tmp_path: Path) -> None:
    job = tmp_path / "job"
    _trial_with_reward(job, "job__b", "1\n")
    _trial_with_reward(job, "job__a", "0\n")
    (job / "config.json").write_text("{}")
    rows = collect_jobs([job], backend="docker", produced_at="2026-09-28T00:00:00+00:00")
    assert [row["trial_name"] for row in rows] == ["job__a", "job__b"]
    assert {row["verdict"] for row in rows} == {"stable"}


def test_parquet_round_trip_preserves_nulls(tmp_path: Path) -> None:
    rows = [
        {"task_version_digest": "sha256:x", "harbor_digest": None, "job_name": "j",
         "trial_name": "j__a", "method": "repeat_verifier", "backend": "docker",
         "state_preservation": "kept", "n_runs": 3, "rewards": [0.0, 0.0, 0.0],
         "verdict": "stable", "evidence_path": "/e/s.json",
         "produced_at": "2026-09-28T00:00:00+00:00"},
        {"task_version_digest": None, "harbor_digest": None, "job_name": "j",
         "trial_name": "j__b", "method": "nop_repeat", "backend": "docker",
         "state_preservation": "kept", "n_runs": 1, "rewards": [None],
         "verdict": "errored", "evidence_path": "/e",
         "produced_at": "2026-09-28T00:00:00+00:00"},
    ]
    path = tmp_path / "task_stability.parquet"
    write_task_stability_parquet(rows, path)
    assert read_task_stability_parquet(path) == rows


def test_stability_run_command_is_free_control_with_verifier_hook(tmp_path: Path) -> None:
    argv, env = stability_run_command(
        task_path=tmp_path / "task", agent="nop", job_name="job-task",
        jobs_dir=tmp_path, repeat_n=3, repo_src=tmp_path / "src",
    )
    assert "--verifier" in argv
    assert argv[argv.index("--verifier") + 1] == VERIFIER_IMPORT_PATH
    assert "repeat_n=3" in argv
    assert "--model" not in argv
    assert env["PYTHONPATH"] == str(tmp_path / "src")
    with pytest.raises(ValueError):
        stability_run_command(
            task_path=tmp_path / "task", agent="codex", job_name="j",
            jobs_dir=tmp_path,
        )


def test_run_stability_jobs_stages_and_manifests_with_fake_runner(tmp_path: Path) -> None:
    source = tmp_path / "candidate-0036"
    (source / "tests").mkdir(parents=True)
    (source / "task.toml").write_text("[task]\nname = \"x\"\n")
    jobs_dir = tmp_path / "runs"
    calls: list[list[str]] = []

    class Completed:
        returncode = 0

    def runner(argv: list[str], env: dict[str, str]) -> Completed:
        calls.append(argv)
        # Harbor would create the trial dir; the fake mimics that layout.
        trial = jobs_dir / "stab-candidate-0036" / "stab-candidate-0036__abc"
        (trial / "verifier").mkdir(parents=True)
        (trial / "verifier" / "reward.txt").write_text("0\n")
        (trial / "trial.log").write_text("log")
        return Completed()

    outcomes = run_stability_jobs(
        tasks=[source], job_prefix="stab", jobs_dir=jobs_dir,
        repo_src=tmp_path, runner=runner,
    )
    assert outcomes[0]["returncode"] == 0
    assert len(calls) == 1
    staged = jobs_dir / ".stage" / "stab-candidate-0036" / "candidate-0036"
    assert (staged / "task.toml").is_file()
    # The source tree is untouched: only read.
    assert (source / "task.toml").is_file()
    manifest = load_manifest(jobs_dir / "stab-candidate-0036")
    assert manifest["stab-candidate-0036__abc"]["task_version_digest"] is not None


def test_run_stability_jobs_dry_run_calls_nothing(tmp_path: Path) -> None:
    source = tmp_path / "task-a"
    source.mkdir()

    def _forbidden(argv: list[str], env: dict[str, str]) -> None:
        raise AssertionError("must not run")

    outcomes = run_stability_jobs(
        tasks=[source], job_prefix="stab", jobs_dir=tmp_path / "runs",
        repo_src=tmp_path, dry_run=True, runner=_forbidden,
    )
    assert outcomes[0]["returncode"] is None


class _FakeExecResult:
    def __init__(self, return_code: int) -> None:
        self.return_code = return_code


class _FakeEnvironment:
    """Replays one return code per test-script exec, in call order.

    Setup/cleanup execs (chmod, rm) return 0 without consuming codes.
    """

    def __init__(self, return_codes: list[int] | None = None) -> None:
        self._return_codes = list(return_codes or [])
        self.exec_commands: list[str] = []

    async def exec(self, command: str, *args: object, **kwargs: object) -> _FakeExecResult:
        self.exec_commands.append(command)
        if "test-stdout" in command and self._return_codes:
            return _FakeExecResult(self._return_codes.pop(0))
        return _FakeExecResult(0)


class _FakeTrialPaths:
    def __init__(self, root: Path) -> None:
        self.verifier_dir = root / "verifier"
        self.test_stdout_path = root / "verifier" / "test-stdout.txt"
        self.reward_text_path = root / "verifier" / "reward.txt"
        self.reward_json_path = root / "verifier" / "reward.json"


def _scripted_inner(rewards: list[float | None], exit_codes: list[int] | None = None):
    """Inner-verifier factory mimicking Harbor's Verifier across k runs."""
    state = {"calls": 0}

    def factory(*, task: object, trial_paths: _FakeTrialPaths,
                environment: _FakeEnvironment, **kwargs: object) -> object:
        class FakeInner:
            async def verify(self) -> SimpleNamespace:
                index = state["calls"]
                state["calls"] += 1
                await environment.exec("chmod +x /tests/test.sh", user="root")
                await environment.exec("(/tests/test.sh) > /logs/verifier/test-stdout.txt 2>&1")
                reward = rewards[index]
                trial_paths.test_stdout_path.parent.mkdir(parents=True, exist_ok=True)
                trial_paths.test_stdout_path.write_bytes(f"run {index} stdout\n".encode())
                if reward is None:
                    raise FileNotFoundError("No reward file found")
                return SimpleNamespace(rewards={"reward": reward})

        return FakeInner()

    return factory


def test_repeat_verifier_reports_first_reward_and_records_all_runs(tmp_path: Path) -> None:
    paths = _FakeTrialPaths(tmp_path)
    env = _FakeEnvironment()
    verifier = RepeatVerifier(
        task=object(), trial_paths=paths, environment=env, repeat_n=3,
        _inner_factory=_scripted_inner([0.0, 0.0, 0.0]),
    )
    result = asyncio.run(verifier.verify())
    assert result.rewards == {"reward": 0.0}
    payload = json.loads((paths.verifier_dir / "stability.json").read_text())
    assert payload["n_runs"] == 3
    assert payload["first_reward"] == 0.0
    assert [run["reward"] for run in payload["runs"]] == [0.0, 0.0, 0.0]
    assert [run["exit_code"] for run in payload["runs"]] == [0, 0, 0]
    assert all(run["duration_sec"] >= 0 for run in payload["runs"])
    assert len({run["stdout_tail_sha256"] for run in payload["runs"]}) == 3
    assert all(run["error"] is None for run in payload["runs"])


def test_repeat_verifier_detects_flips_without_changing_trial_reward(tmp_path: Path) -> None:
    paths = _FakeTrialPaths(tmp_path)
    env = _FakeEnvironment(return_codes=[0, 1, 0])
    verifier = RepeatVerifier(
        task=object(), trial_paths=paths, environment=env, repeat_n="3",
        _inner_factory=_scripted_inner([1.0, 0.0, 1.0]),
    )
    result = asyncio.run(verifier.verify())
    assert result.rewards == {"reward": 1.0}
    payload = json.loads((paths.verifier_dir / "stability.json").read_text())
    assert [run["reward"] for run in payload["runs"]] == [1.0, 0.0, 1.0]
    assert [run["exit_code"] for run in payload["runs"]] == [0, 1, 0]
    assert verdict_for([run["reward"] for run in payload["runs"]]) == "flipped"


def test_repeat_verifier_reraises_first_error_after_recording(tmp_path: Path) -> None:
    paths = _FakeTrialPaths(tmp_path)
    env = _FakeEnvironment()
    verifier = RepeatVerifier(
        task=object(), trial_paths=paths, environment=env, repeat_n=2,
        _inner_factory=_scripted_inner([None, 0.0]),
    )
    with pytest.raises(FileNotFoundError):
        asyncio.run(verifier.verify())
    payload = json.loads((paths.verifier_dir / "stability.json").read_text())
    assert payload["runs"][0]["reward"] is None
    assert "FileNotFoundError" in payload["runs"][0]["error"]
    assert payload["runs"][1]["reward"] == 0.0


def test_repeat_verifier_default_repeats_three_times() -> None:
    verifier = RepeatVerifier(task=object(), trial_paths=object(), environment=object())
    assert verifier._repeat_n == DEFAULT_REPEAT_N


def test_manifest_round_trip(tmp_path: Path) -> None:
    job = tmp_path / "job"
    write_job_manifest(job, {"t__a": {"task_version_digest": "sha256:x"}})
    assert load_manifest(job) == {"t__a": {"task_version_digest": "sha256:x"}}
    assert load_manifest(tmp_path / "missing") == {}


def test_read_trial_reward_prefers_text(tmp_path: Path) -> None:
    trial = tmp_path / "t"
    (trial / "verifier").mkdir(parents=True)
    assert read_trial_reward(trial) is None
    (trial / "verifier" / "reward.txt").write_text("1\n")
    assert read_trial_reward(trial) == 1.0


def test_make_inner_builds_default_verifier_without_inner_factory(tmp_path: Path) -> None:
    """The Harbor-runtime branch imports and constructs the default Verifier."""
    import sys
    import types

    created: dict[str, object] = {}

    class FakeDefault:
        def __init__(self, *, task: object, trial_paths: object, environment: object,
                     override_env: object = None, logger: object = None,
                     verifier_env: object = None, step_name: object = None,
                     include_logs: object = None, exclude_logs: object = None) -> None:
            created.update(task=task, trial_paths=trial_paths, environment=environment,
                           step_name=step_name)

    leaf = types.ModuleType("harbor.verifier.verifier")
    leaf.Verifier = FakeDefault  # type: ignore[attr-defined]
    middle = types.ModuleType("harbor.verifier")
    middle.verifier = leaf  # type: ignore[attr-defined]
    top = types.ModuleType("harbor")
    top.verifier = middle  # type: ignore[attr-defined]
    modules = {
        "harbor": top, "harbor.verifier": middle, "harbor.verifier.verifier": leaf,
    }
    for name, module in modules.items():
        sys.modules[name] = module
    try:
        paths = _FakeTrialPaths(tmp_path)
        env = _FakeEnvironment()
        task = object()
        verifier = RepeatVerifier(task=task, trial_paths=paths, environment=env)
        inner = verifier._make_inner(env)
    finally:
        for name in modules:
            del sys.modules[name]
    assert isinstance(inner, FakeDefault)
    assert created == {"task": task, "trial_paths": paths, "environment": env,
                        "step_name": None}


def test_task_digests_carry_single_prefix(tmp_path: Path) -> None:
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.toml").write_text("[task]\nname = \"x\"\n")
    digests = task_stability.task_digests(task)
    assert digests["task_version_digest"] is not None
    assert digests["task_version_digest"].startswith("sha256:")
    assert not digests["task_version_digest"].startswith("sha256:sha256:")
    assert digests["harbor_digest"] is not None
    assert not digests["harbor_digest"].startswith("sha256:sha256:")

def test_iter_trial_dirs_ignores_job_files(tmp_path: Path) -> None:
    job = tmp_path / "other-job"
    job.mkdir()
    (job / "config.json").write_text("{}")
    (job / "t__a").mkdir()
    assert iter_trial_dirs(job) == []
    assert iter_trial_dirs(tmp_path / "missing") == []


def test_summarize_job_trials_marks_missing_stability(tmp_path: Path) -> None:
    job = tmp_path / "job"
    good = _trial_with_reward(job, "job__good", "1\n")
    (good / "verifier" / "stability.json").write_text(json.dumps({"runs": [
        {"reward": 1.0}, {"reward": 1.0}, {"reward": 1.0},
    ]}))
    _trial_with_reward(job, "job__bad", "0\n")
    (job / "job__bad" / "verifier" / "stability.json").write_text(json.dumps({"runs": []}))
    summaries = task_stability.summarize_job_trials(job)
    by_trial = {summary["trial"]: summary for summary in summaries}
    assert by_trial["job__good"]["has_stability"] is True
    assert by_trial["job__good"]["verdict"] == "stable"
    assert by_trial["job__bad"]["has_stability"] is False
    assert by_trial["job__bad"]["rewards"] == [0.0]


def _file_backed_inner(writes: list[str | None]):
    """Inner verifier that grades like Harbor: reads the host reward file.

    Each call writes ``reward.txt`` per the script (or nothing when the run
    dies first), rewrites ``test-stdout.txt``, then parses the reward file —
    raising ``FileNotFoundError`` when no file exists, exactly like Harbor's
    ``Verifier`` on a testbed failure.
    """
    state = {"calls": 0}

    def factory(*, task: object, trial_paths: _FakeTrialPaths,
                environment: _FakeEnvironment, **kwargs: object) -> object:
        class FileBackedInner:
            async def verify(self) -> SimpleNamespace:
                index = state["calls"]
                state["calls"] += 1
                await environment.exec("chmod +x /tests/test.sh", user="root")
                await environment.exec("(/tests/test.sh) > /logs/verifier/test-stdout.txt 2>&1")
                trial_paths.test_stdout_path.parent.mkdir(parents=True, exist_ok=True)
                trial_paths.test_stdout_path.write_bytes(f"run {index} stdout\n".encode())
                written = writes[index]
                if written is not None:
                    trial_paths.reward_text_path.write_text(written)
                try:
                    reward = float(trial_paths.reward_text_path.read_text().strip())
                except (OSError, ValueError) as exc:
                    raise FileNotFoundError("No reward file found") from exc
                return SimpleNamespace(rewards={"reward": reward})

        return FileBackedInner()

    return factory


def test_rerun_without_reward_reads_null_not_stale(tmp_path: Path) -> None:
    """Run 2 writes nothing: clearing must prevent run 1's stale reward."""
    paths = _FakeTrialPaths(tmp_path)
    env = _FakeEnvironment()
    verifier = RepeatVerifier(
        task=object(), trial_paths=paths, environment=env, repeat_n=3,
        _inner_factory=_file_backed_inner(["1", None, "0"]),
    )
    result = asyncio.run(verifier.verify())
    assert result.rewards == {"reward": 1.0}
    payload = json.loads((paths.verifier_dir / "stability.json").read_text())
    assert [run["reward"] for run in payload["runs"]] == [1.0, None, 0.0]
    assert "FileNotFoundError" in payload["runs"][1]["error"]
    assert verdict_for([run["reward"] for run in payload["runs"]]) == "errored"
    # The container-side clear was issued before reruns.
    rm_calls = [cmd for cmd in env.exec_commands if cmd.startswith("rm -f")]
    assert len(rm_calls) == 2


def test_top_level_matches_run_zero_with_per_run_copies(tmp_path: Path) -> None:
    """Trial dir looks like a single-verify trial of run 0 plus repeat/."""
    paths = _FakeTrialPaths(tmp_path)
    env = _FakeEnvironment()
    verifier = RepeatVerifier(
        task=object(), trial_paths=paths, environment=env, repeat_n=3,
        _inner_factory=_file_backed_inner(["1", None, "0"]),
    )
    asyncio.run(verifier.verify())
    top = paths.verifier_dir
    assert (top / "reward.txt").read_text() == "1"
    assert (top / "test-stdout.txt").read_bytes() == b"run 0 stdout\n"
    assert (top / "stability.json").is_file()
    for index, expected_reward, expected_stdout in (
        (0, "1", b"run 0 stdout\n"),
        (1, None, b"run 1 stdout\n"),
        (2, "0", b"run 2 stdout\n"),
    ):
        rundir = top / "repeat" / str(index)
        assert (rundir / "test-stdout.txt").read_bytes() == expected_stdout
        reward_file = rundir / "reward.txt"
        if expected_reward is None:
            assert not reward_file.exists()
        else:
            assert reward_file.read_text() == expected_reward
