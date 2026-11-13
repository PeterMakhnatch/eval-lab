"""Native repeat-verifier reward, evidence, error, and restore contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab import harbor_repeat_verifier
from evallab.harbor_repeat_verifier import (
    STDOUT_TAIL_BYTES,
    RepeatVerifier,
    _coerce_repeat_n,
    _scalar_reward,
    _stdout_tail_digest,
    _test_exit_code,
)


def test_repeat_n_coerces_harbor_string_kwargs() -> None:
    assert _coerce_repeat_n("3") == 3
    assert _coerce_repeat_n(3) == 3
    with pytest.raises(ValueError):
        _coerce_repeat_n("0")
    with pytest.raises(ValueError):
        _coerce_repeat_n("many")


@pytest.mark.parametrize(
    ("rewards", "expected"),
    [
        (None, None),
        ({}, None),
        ([], None),
        ({"other": 2}, 2.0),
        ({"other": 3, "reward": 1}, 1.0),
        ({"reward": "not-a-number"}, None),
        ({"reward": None}, None),
    ],
)
def test_scalar_reward_handles_missing_odd_and_invalid_rewards(
    rewards: object, expected: float | None,
) -> None:
    assert _scalar_reward(rewards) == expected


def test_stdout_tail_digest_handles_absent_empty_and_long_output(tmp_path: Path) -> None:
    path = tmp_path / "test-stdout.txt"
    assert _stdout_tail_digest(path) is None
    path.write_bytes(b"")
    assert _stdout_tail_digest(path) is None

    output = b"prefix" + bytes(range(256)) * 40 + b"suffix"
    path.write_bytes(output)
    assert _stdout_tail_digest(path) == hashlib.sha256(
        output[-STDOUT_TAIL_BYTES:],
    ).hexdigest()




@pytest.mark.parametrize(
    ("calls", "expected"),
    [
        ([], None),
        (
            [
                {"command": " chmod +x /tests/test.sh", "return_code": 0},
                {"command": "rm -f /logs/verifier/reward.txt", "return_code": 0},
                {"command": " /tests/test.sh", "return_code": 7},
            ],
            7,
        ),
        ([{"command": "/tests/test.sh", "return_code": None}], None),
        (
            [
                {"command": "chmod +x /tests/test.sh", "return_code": 3},
                {"command": "rm -f /logs/verifier/reward.txt", "return_code": 4},
            ],
            4,
        ),
    ],
)
def test_test_exit_code_skips_preparation_and_handles_missing_codes(
    calls: list[dict[str, object]], expected: int | None,
) -> None:
    assert _test_exit_code(calls) == expected


class _FakeExecResult:
    def __init__(self, return_code: int) -> None:
        self.return_code = return_code


class _FakeEnvironment:
    """Replays one return code per test-script exec, in call order.

    Setup/cleanup execs (chmod, rm) return 0 without consuming codes.
    """

    os = "linux"

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




def test_clear_reward_files_warns_for_exec_missing_path_and_unlink_errors(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingEnvironment:
        os = "linux"

        async def exec(self, command: str, **kwargs: object) -> None:
            raise RuntimeError("container unavailable")

    paths = SimpleNamespace(reward_text_path=tmp_path / "reward.txt")
    paths.reward_text_path.mkdir()
    verifier = RepeatVerifier(
        task=object(), trial_paths=paths, environment=FailingEnvironment(), repeat_n=2,
    )

    with caplog.at_level(logging.WARNING, logger=harbor_repeat_verifier.__name__):
        asyncio.run(verifier._clear_reward_files())

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 3
    assert messages[0] == "repeat verifier could not clear container rewards: container unavailable"
    assert messages[1].startswith("repeat verifier could not clear host reward_text_path:")
    assert messages[2] == (
        "repeat verifier could not clear host reward_json_path: "
        "'types.SimpleNamespace' object has no attribute 'reward_json_path'"
    )


def test_snapshot_and_restore_replace_stale_files_directories_and_symlinks(
    tmp_path: Path,
) -> None:
    paths = _FakeTrialPaths(tmp_path)
    top = paths.verifier_dir
    top.mkdir(parents=True)
    paths.reward_text_path.write_text("1")
    (top / "nested").mkdir()
    (top / "nested" / "result.txt").write_text("original")
    (top / "stdout-link").symlink_to("reward.txt")
    snapshot = top / "repeat" / "0"
    snapshot.mkdir(parents=True)
    (snapshot / "stale.txt").write_text("stale")
    verifier = RepeatVerifier(task=object(), trial_paths=paths, environment=object())
    snapshots_ok: set[int] = set()

    verifier._snapshot_run(0, snapshots_ok)

    assert snapshots_ok == {0}
    assert not (snapshot / "stale.txt").exists()
    assert (snapshot / "nested" / "result.txt").read_text() == "original"
    assert (snapshot / "stdout-link").is_symlink()
    assert (snapshot / "stdout-link").read_text() == "1"

    (top / "nested" / "result.txt").write_text("changed")
    (top / "obsolete").mkdir()
    (top / "obsolete" / "old.txt").write_text("old")
    verifier._restore_run(0, snapshots_ok)

    assert not (top / "obsolete").exists()
    assert (top / "nested" / "result.txt").read_text() == "original"
    assert (top / "stdout-link").is_symlink()
    assert (top / "stdout-link").read_text() == "1"


def test_snapshot_oserror_warns_once_without_changing_reward_or_recorded_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    paths = _FakeTrialPaths(tmp_path)
    verifier = RepeatVerifier(
        task=object(), trial_paths=paths, environment=_FakeEnvironment(), repeat_n=2,
        _inner_factory=_file_backed_inner(["1", "0"]),
    )
    real_copy2 = harbor_repeat_verifier.shutil.copy2

    def fail_second_snapshot(src: object, dst: object, **kwargs: object) -> object:
        if Path(str(dst)).parent.name == "1":
            raise OSError("snapshot storage full")
        return real_copy2(src, dst, **kwargs)

    monkeypatch.setattr(harbor_repeat_verifier.shutil, "copy2", fail_second_snapshot)
    with caplog.at_level(logging.WARNING, logger=harbor_repeat_verifier.__name__):
        result = asyncio.run(verifier.verify())

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "repeat verifier could not snapshot run 1: snapshot storage full",
    ]
    assert result.rewards == {"reward": 1.0}
    payload = json.loads((paths.verifier_dir / "stability.json").read_text())
    assert payload["n_runs"] == 2
    assert payload["first_reward"] == 1.0
    assert [run["reward"] for run in payload["runs"]] == [1.0, 0.0]


def test_restore_without_snapshot_warns_once_and_leaves_recorded_files_unchanged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    paths = _FakeTrialPaths(tmp_path)
    paths.verifier_dir.mkdir(parents=True)
    paths.reward_text_path.write_text("1")
    stability = paths.verifier_dir / "stability.json"
    stability.write_text(json.dumps({"runs": [{"reward": 1.0}]}))
    verifier = RepeatVerifier(task=object(), trial_paths=paths, environment=object())

    with caplog.at_level(logging.WARNING, logger=harbor_repeat_verifier.__name__):
        verifier._restore_run(0, set())

    messages = [record.getMessage() for record in caplog.records]
    assert messages == ["repeat verifier skips restore of run 0 (no snapshot)"]
    assert paths.reward_text_path.read_text() == "1"
    assert json.loads(stability.read_text())["runs"] == [{"reward": 1.0}]


def test_restore_oserror_warns_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    paths = _FakeTrialPaths(tmp_path)
    verifier = RepeatVerifier(task=object(), trial_paths=paths, environment=object())

    def fail_iterdir(self: Path) -> object:
        raise OSError("restore storage unavailable")

    monkeypatch.setattr(Path, "iterdir", fail_iterdir)
    with caplog.at_level(logging.WARNING, logger=harbor_repeat_verifier.__name__):
        verifier._restore_run(0, {0})

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "repeat verifier could not restore run 0: restore storage unavailable",
    ]


def test_stability_write_oserror_warns_once_without_mutating_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    paths = _FakeTrialPaths(tmp_path)
    verifier = RepeatVerifier(task=object(), trial_paths=paths, environment=object())
    runs = [{"index": 0, "reward": 1.0}]

    def fail_mkdir(self: Path, **kwargs: object) -> None:
        raise OSError("stability output unavailable")

    monkeypatch.setattr(Path, "mkdir", fail_mkdir)
    with caplog.at_level(logging.WARNING, logger=harbor_repeat_verifier.__name__):
        verifier._write_stability(runs)

    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "repeat verifier could not write stability.json: stability output unavailable",
    ]
    assert runs == [{"index": 0, "reward": 1.0}]


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


