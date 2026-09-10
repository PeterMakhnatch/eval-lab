from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from evallab.regrade import (
    RegradeRefusalCode,
    RegradeVerdict,
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
