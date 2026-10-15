"""QA Trace T3 counterfactual regrade: staging, refusals, grader validity, rulings."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from evallab.qa import counterfactual as cf
from evallab.qa import trace

ORIGINAL_MANIFEST = [
    {
        "source": "/logs/artifacts",
        "destination": "artifacts/logs/artifacts",
        "type": "directory",
        "status": "empty",
        "service": None,
        "exclude": [],
    }
]


def _trial(tmp: Path, *, snapshot: bool = False, diff: str | None = None) -> Path:
    trial = tmp / "src" / "trial-1"
    for sub in ("agent", "verifier", "artifacts"):
        (trial / sub).mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"reward": 1.0}}})
    )
    (trial / "artifacts" / "manifest.json").write_text(json.dumps(ORIGINAL_MANIFEST))
    (trial / "agent" / "trajectory.json").write_text('{"steps": [{"step_id": 1}]}')
    if snapshot:
        snap = trial / "artifacts" / "var" / "tmp" / "mimo-separate"
        snap.mkdir(parents=True)
        (snap / "workspace.tgz").write_bytes(b"recorded")
    if diff is not None:
        (trial / "verifier" / "agent.diff").write_text(diff)
    return trial


def _grader(tmp: Path) -> cf.Grader:
    package = tmp / "pkg"
    (package / "environment" / "setup").mkdir(parents=True)
    (package / "environment" / "setup" / "setup.sh").write_text("true\n")
    (package / "task.toml").write_text(
        '[environment]\ndocker_image = "img@sha256:abc"\nworkdir = "/testbed"\n'
    )
    return cf.Grader(
        task_name="mimo-v2.6-rl/format-code-task-000001",
        transform=cf.V10,
        variant_digest="sha256:" + "a" * 64,
        package=package,
        record="library/task-variants/x/aaaaaaaaaaaa.json",
    )


class FakeDocker:
    """``docker run`` writes the rebuilt archive to the ``/out`` mount; ``rc`` is its exit."""

    def __init__(self, rc: int = 0):
        self.rc = rc
        self.runs: list[list[str]] = []

    def __call__(self, command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["docker", "run"]:
            self.runs.append(command)
            if self.rc == 0:
                out = next(m.removesuffix(":/out") for m in command if m.endswith(":/out"))
                Path(out, cf.SNAPSHOT_ARCHIVE).write_bytes(b"rebuilt")
            return subprocess.CompletedProcess(command, self.rc, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")  # image inspect: cached


def _declared(staged: Path) -> dict[str, str]:
    manifest = json.loads((staged / "artifacts" / "manifest.json").read_text())
    return {m["source"]: m["status"] for m in manifest}


def test_staged_replay_declares_snapshot_and_trajectory_and_leaves_source_alone(
    tmp_path: Path,
) -> None:
    # Harbor refuses a regrade whose source never "collected" a declared artifact.
    trial = _trial(tmp_path, snapshot=True)
    docker = FakeDocker()
    staged = cf.stage_replay(trial, _grader(tmp_path), tmp_path / "work", runner=docker)
    assert (staged["snapshot"], staged["trajectory"]) == ("recorded", "recorded")
    assert docker.runs == []
    declared = _declared(staged["trial"])
    assert declared["/var/tmp/mimo-separate"] == declared["/logs/agent/trajectory.json"] == "ok"
    assert json.loads((trial / "artifacts" / "manifest.json").read_text()) == ORIGINAL_MANIFEST


def test_snapshot_is_rebuilt_from_the_recorded_diff_when_none_was_captured(
    tmp_path: Path,
) -> None:
    trial = _trial(tmp_path, diff="diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n")
    staged = cf.stage_replay(trial, _grader(tmp_path), tmp_path / "work", runner=FakeDocker())
    assert staged["snapshot"] == "rebuilt_from_agent_diff"
    assert (
        staged["trial"] / "artifacts/var/tmp/mimo-separate/workspace.tgz"
    ).read_bytes() == b"rebuilt"
    assert _declared(staged["trial"])["/var/tmp/mimo-separate"] == "ok"


def test_controls_replace_the_change_and_never_carry_the_runs_trajectory(tmp_path: Path) -> None:
    trial = _trial(tmp_path, snapshot=True, diff="diff --git a/x.py b/x.py\n")
    staged = cf.stage_replay(
        trial, _grader(tmp_path), tmp_path / "work", diff_text="", runner=FakeDocker()
    )
    assert staged["snapshot"] == "rebuilt_from_agent_diff"  # recorded snapshot not reused
    assert staged["trajectory"] == "placeholder"
    assert (staged["trial"] / "verifier" / "agent.diff").read_text() == ""


def test_bytecode_in_the_recorded_diff_is_dropped_like_at10_drops_it(tmp_path: Path) -> None:
    # Agents' diffs often carry __pycache__ stanzas git cannot apply; @10 drops them too.
    source = (
        "diff --git a/pkg/x.py b/pkg/x.py\n--- a/pkg/x.py\n+++ b/pkg/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    pyc = "diff --git a/pkg/__pycache__/x.cpython-312.pyc b/pkg/__pycache__/x.cpython-312.pyc\nGIT binary patch\nliteral 1\n"
    docker = FakeDocker()
    staged = cf.stage_replay(
        _trial(tmp_path, diff=source + pyc), _grader(tmp_path), tmp_path / "work", runner=docker
    )
    assert staged["dropped_binary_artifacts"] == ["pkg/__pycache__/x.cpython-312.pyc"]
    replayed = next(m.split(":")[0] for m in docker.runs[0] if m.endswith(":/in/agent.diff:ro"))
    assert Path(replayed).read_text() == source


@pytest.mark.parametrize(
    ("snapshot", "diff", "rc", "code"),
    [
        (False, None, 0, "source_patch_unavailable"),
        (False, "diff --git a/x b/x\n", 3, "patch_does_not_apply"),
        (False, "diff --git a/x b/x\nBinary files a/x and b/x differ\n", 0, "diff_unreplayable"),
    ],
)
def test_unreplayable_passes_are_refused_with_a_reason(
    tmp_path: Path, snapshot: bool, diff: str | None, rc: int, code: str
) -> None:
    trial = _trial(tmp_path, snapshot=snapshot, diff=diff)
    staged = cf.stage_replay(trial, _grader(tmp_path), tmp_path / "work", runner=FakeDocker(rc))
    assert staged["refused"].startswith(code)


@pytest.mark.parametrize(
    ("nop", "reference", "valid"),
    [(0.0, 1.0, True), (1.0, 1.0, False), (0.0, 0.0, False), (0.0, None, True), (None, 1.0, False)],
)
def test_grader_is_valid_only_when_nop_fails_and_the_reference_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nop: float | None,
    reference: float | None,
    valid: bool,
) -> None:
    def fake_replay(_trial: Path, _grader: cf.Grader, _work: Path, *, diff_text: str, **_: Any):
        return {"regraded": nop if diff_text == "" else reference}

    monkeypatch.setattr(cf, "replay", fake_replay)
    monkeypatch.setattr(cf, "reference_patch", lambda *_: None if reference is None else "patch")
    controls = cf.grader_controls(
        _grader(tmp_path), tmp_path, tmp_path / "controls", repo_root=tmp_path
    )
    assert controls["valid"] is valid
    assert controls["reference_available"] is (reference is not None)


def test_only_a_valid_graders_flip_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    graders = [_grader(tmp_path)]
    monkeypatch.setattr(cf, "newer_graders", lambda *_: graders)
    monkeypatch.setattr(
        cf, "replay", lambda *_, **__: {"outcome": "flips", "recorded": 1.0, "regraded": 0.0}
    )
    monkeypatch.setattr(trace, "counterfactual_root", lambda: tmp_path / "cf")
    for nop, ran in ((1.0, False), (0.0, True)):
        monkeypatch.setattr(
            cf,
            "grader_controls",
            lambda *_, nop=nop, **__: {"valid": nop == 0.0, "nop": nop, "reference": 1.0},
        )
        executed = trace.regrade_counterfactual(tmp_path, "mimo-v2.6-rl/format-code-task-000001")
        assert executed["ran"] is ran
        assert "src" not in json.dumps(executed)  # reaches the refuter: no paths or run names
    assert executed["lowest"] == 0.0
