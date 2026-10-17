from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path
from typing import Any

import pytest

from evallab.mimo_clean import build_solution_sh
from evallab.registry import harbor_task_digest, task_directory_digest

DRIVER = Path(__file__).resolve().parents[1] / "research/experiments/mimo-v6-full-fleet/census.py"
TASK = "format-code-task-000081"


@pytest.fixture
def census() -> dict[str, Any]:
    return runpy.run_path(str(DRIVER))


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[dict[str, str], dict[str, str]]:
    patch = tmp_path / "reference.patch"
    patch.write_bytes(b"diff --git a/project.py b/project.py\n")
    reference = {
        "task_id": TASK, "label": "oracle:pass+nop:fail", "fix_commit": "a" * 40,
        "patch_path": str(patch), "patch_sha256": hashlib.sha256(patch.read_bytes()).hexdigest(),
    }
    package = tmp_path / "package"
    (package / "solution").mkdir(parents=True)
    (package / "task.toml").write_text('[environment]\ndocker_image="fixture"\n')
    (package / "instruction.md").write_text("Repair the implementation; preserve its tests.\n")
    (package / "solution/solve.sh").write_bytes(build_solution_sh(
        task_id=TASK, label=reference["label"], fix_commit=reference["fix_commit"],
        patch_bytes=patch.read_bytes(),
    ))
    manifest = {
        "task_id": TASK, "status": "built", "language": "python", "package_path": str(package),
        "final_digest": task_directory_digest(package), "run_digest": "sha256:root",
    }
    return reference, manifest


def control(
    root: Path, manifest: dict[str, str], agent: str, *, reward: float | None,
    changes: dict[str, Any] | None = None, cases: int = 1, suffix: str = "",
) -> Path:
    job = root / f"{agent}{suffix}"
    trial = job / "trial"
    (trial / "verifier").mkdir(parents=True)
    harbor_digest = harbor_task_digest(Path(manifest["package_path"]))
    staging = {
        "declared_task_name": TASK, "source_package_digest": manifest["final_digest"],
        "source_harbor_digest": harbor_digest, "staged_harbor_digest": harbor_digest,
    } | (changes or {})
    metadata = {"exit_code": 0, "timed_out": False, "task_staging": staging}
    metadata_path = job / "lab-metadata.json"
    metadata_path.write_text(json.dumps(metadata))
    native = {
        "id": f"00000000-0000-0000-0000-{'000000000001' if agent == 'oracle' else '000000000002'}{suffix}",
        "config": {"agent": {"name": agent}}, "exception_info": None,
        "verifier_result": {"rewards": {"reward": reward}},
    }
    (trial / "result.json").write_text(json.dumps(native))
    (trial / "verifier/junit-grade.log").write_text(
        f"rc={0 if reward else 1} cases={cases} bad={0 if reward else 1} named={cases} missing=[]\n"
    )
    return metadata_path


def classify(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]],
    root: Path, paths: set[Path], *, issues: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    reference, manifest = inputs
    return census["census_row"](
        reference, manifest, repo=root, package_root=root,
        observations=census["native_observations"](paths), reported_issues=issues or [],
    )


@pytest.mark.parametrize(
    ("change", "cases", "reward", "expected"),
    [
        ({}, 1, 1.0, "v6_verified"),
        ({"source_package_digest": "sha256:older"}, 1, 1.0, "v6_unverified"),
        ({"source_harbor_digest": "sha256:other"}, 1, 1.0, "v6_unverified"),
        ({"staged_harbor_digest": "sha256:mutated"}, 1, 1.0, "v6_unverified"),
        ({}, 0, 1.0, "v6_unverified"),
        ({}, 1, 0.0, "broken_ref_or_env"),
        ({}, 1, None, "broken_ref_or_env"),
    ],
)
def test_only_exact_executed_controls_qualify(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
    change: dict[str, Any], cases: int, reward: float | None, expected: str,
) -> None:
    _, manifest = inputs
    oracle = control(tmp_path, manifest, "oracle", reward=reward, changes=change, cases=cases)
    nop = control(tmp_path, manifest, "nop", reward=0.0)
    assert classify(census, inputs, tmp_path, {oracle, nop})["bucket"] == expected


def test_unscored_attempt_is_preserved_but_does_not_erase_later_pair(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
) -> None:
    _, manifest = inputs
    first = control(tmp_path, manifest, "oracle", reward=None, suffix="-failed")
    assert classify(census, inputs, tmp_path, {first})["bucket"] == "broken_ref_or_env"
    oracle = control(tmp_path, manifest, "oracle", reward=1.0)
    nop = control(tmp_path, manifest, "nop", reward=0.0)
    row = classify(census, inputs, tmp_path, {first, oracle, nop})
    assert row["bucket"] == "v6_verified"
    assert json.loads(row["oracle_rewards"]) == [[1.0], [None]]


def test_reported_broken_reference_requires_a_real_resolving_pair(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
) -> None:
    reference, manifest = inputs
    issue = {"task_id": TASK, "reference_patch_sha256": reference["patch_sha256"], "reason": "accepted report"}
    row = classify(census, inputs, tmp_path, set(), issues=[issue])
    assert row["bucket"] == "broken_ref_or_env"
    assert json.loads(row["reported_issues"]) == [issue]
    oracle = control(tmp_path, manifest, "oracle", reward=1.0)
    nop = control(tmp_path, manifest, "nop", reward=0.0)
    assert classify(census, inputs, tmp_path, {oracle, nop}, issues=[issue])["bucket"] == "v6_verified"


def test_changed_package_does_not_inherit_its_old_control_pair(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
) -> None:
    reference, manifest = inputs
    oracle = control(tmp_path, manifest, "oracle", reward=1.0)
    nop = control(tmp_path, manifest, "nop", reward=0.0)
    package = Path(manifest["package_path"])
    (package / "instruction.md").write_text("A changed, independently qualified input.\n")
    rebuilt = manifest | {"final_digest": task_directory_digest(package)}
    assert classify(census, (reference, rebuilt), tmp_path, {oracle, nop})["bucket"] == "v6_unverified"


def test_duplicate_native_uuid_with_conflicting_reward_is_refused(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
) -> None:
    _, manifest = inputs
    original = control(tmp_path / "one", manifest, "oracle", reward=1.0)
    conflicting = control(tmp_path / "two", manifest, "oracle", reward=0.0)
    with pytest.raises(ValueError, match="conflicting copies of native execution"):
        census["native_observations"]({original, conflicting})
