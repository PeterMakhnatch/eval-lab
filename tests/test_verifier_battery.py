"""Hollow-program battery: fakes of the changed functions and known-good rewrites."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evallab.verifier_battery import (
    equivalent_variants,
    hollow_variants,
    renamed_locals,
    run_battery_audit,
)
from evallab.verifier_mutation import CAPTURE_ROOT

_BEFORE = '''\
def untouched(x):
    return x + 1


def merge(own: set, other: set) -> set:
    """Union of aliases."""
    return own | other
'''
# The reference fix rewrites ``merge`` only.
_FIXED = '''\
def untouched(x):
    return x + 1


def merge(own: set, other: set) -> set:
    """Union of aliases without our own id."""
    # Our own id never belongs in the alias set.
    merged = own | other
    result = {alias for alias in merged if alias != 0}
    return result
'''


def _load(source: bytes) -> dict:
    namespace: dict = {}
    exec(compile(source, "<variant>", "exec"), namespace)
    return namespace


def test_fakes_hollow_only_the_functions_the_fix_changed() -> None:
    fakes = dict(hollow_variants(_FIXED.encode(), _BEFORE.encode()))
    assert set(fakes) == {
        "empty-implementation",
        "constant-implementation",
        "raise-not-implemented",
        "success-exit",
        "equality-spoofing",
    }
    for name, content in fakes.items():
        module = _load(content)
        assert module["untouched"](1) == 2, name
        assert module["merge"].__doc__ == "Union of aliases without our own id.", name
    assert _load(fakes["empty-implementation"])["merge"]({1}, {2}) is None
    assert _load(fakes["constant-implementation"])["merge"]({1}, {2}) == set()
    assert _load(fakes["equality-spoofing"])["merge"]({1}, {2}) == "anything"
    with pytest.raises(NotImplementedError):
        _load(fakes["raise-not-implemented"])["merge"]({1}, {2})
    with pytest.raises(SystemExit):
        _load(fakes["success-exit"])["merge"]({1}, {2})
    # Nothing the reference solution changed is a function: nothing to hollow.
    assert hollow_variants(_FIXED.encode(), _FIXED.encode()) == []


def test_known_good_controls_keep_behaviour() -> None:
    controls = dict(equivalent_variants(_FIXED.encode(), _BEFORE.encode()))
    assert set(controls) == {"comment-free", "reformatted", "renamed-locals"}
    assert b"# Our own id" not in controls["comment-free"]
    assert b"merged_rn" in controls["renamed-locals"]
    reference = _load(_FIXED.encode())["merge"]
    for name, content in controls.items():
        merge = _load(content)["merge"]
        # Parameters keep their names: keyword calls still work.
        assert merge(own={0, 1}, other={2, 3}) == reference({0, 1}, {2, 3}) == {1, 2, 3}, name


def test_renaming_refuses_functions_with_scope_escapes() -> None:
    nested = _FIXED.replace("    return result\n", "    return (lambda: result)()\n")
    assert renamed_locals(nested.encode(), _BEFORE.encode()) is None
    dynamic = _FIXED.replace("    return result\n", "    return eval('result')\n")
    assert renamed_locals(dynamic.encode(), _BEFORE.encode()) is None


def _package(root: Path) -> Path:
    package = root / "pkg"
    (package / "solution").mkdir(parents=True)
    (package / "tests").mkdir()
    (package / "task.toml").write_text(
        'schema_version = "1.0"\nartifacts = ["/app/merge.py"]\n[task]\nname = "bench/merge"\n'
    )
    (package / "solution" / "solve.sh").write_text("#!/bin/bash\necho solved\n")
    (package / "tests" / "test.sh").write_text("#!/bin/bash\necho 1 > /logs/verifier/reward.txt\n")
    return package


class _Recorded:
    """Writes the capture layout and scores each run by its role."""

    def __init__(self, rewards: dict[str, float]) -> None:
        self.rewards = rewards

    def __call__(self, matrix, repo_root: Path) -> list[dict]:
        results = []
        for run in matrix.runs:
            title = (repo_root / run.solution).read_text().splitlines()[1]
            role = title.removeprefix("# envcheck: ").split()[0]
            probe = title.split()[3] if role == "battery" else role
            trial = repo_root / "runs" / run.name / "trial__1"
            capture = trial / "artifacts" / CAPTURE_ROOT.lstrip("/") / "app"
            if role in {"capture-oracle", "capture-base"}:
                capture.mkdir(parents=True)
                (capture / "merge.py").write_text(_FIXED if role == "capture-oracle" else _BEFORE)
            trial.mkdir(parents=True, exist_ok=True)
            reward = self.rewards.get(probe, self.rewards.get(role, 0.0))
            results.append({"name": run.name, "status": "ok", "rewards": [reward], "error": None})
        return results


def test_audit_rejects_a_grader_that_rewards_a_fake(tmp_path: Path) -> None:
    package = _package(tmp_path)
    rewards = {"capture-oracle": 1.0, "capture-base": 0.0, "battery": 0.0}
    accepted = {"comment-free": 1.0, "reformatted": 1.0, "renamed-locals": 1.0}
    clean, _ = run_battery_audit(
        package, repo_root=tmp_path, execute=True, runner=_Recorded(rewards | accepted)
    )
    assert clean.verdict == "clean" and len(clean.probes) == 8
    leaky, run_dir = run_battery_audit(
        package,
        repo_root=tmp_path,
        execute=True,
        runner=_Recorded(rewards | {"equality-spoofing": 1.0, "reformatted": 1.0}),
    )
    # The rewarded fake outranks the rejected controls.
    assert leaky.verdict == "fake_rewarded"
    (spoof,) = [p for p in leaky.probes if p.kind == "fake" and p.outcome == "rewarded"]
    assert spoof.probe == "equality-spoofing"
    saved = json.loads((run_dir / "battery-report.json").read_text())
    assert saved["verdict"] == "fake_rewarded"
    assert (run_dir / "inputs" / spoof.id / f"{spoof.id}.patch").is_file()
