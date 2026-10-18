from __future__ import annotations

import hashlib
import json
import runpy
import shutil
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
    (package / "task.toml").write_text(
        f'[task]\nname="mimo-v2.6-rl/{TASK}"\n[environment]\ndocker_image="fixture"\n'
    )
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
    changes: dict[str, Any] | None = None, cases: int = 1, suffix: str = "", legacy: bool = False,
) -> Path:
    job = root / f"{agent}{suffix}"
    trial = job / "trial"
    (trial / "verifier").mkdir(parents=True)
    harbor_digest = harbor_task_digest(Path(manifest["package_path"]))
    staged_name = Path(manifest["package_path"]).name if legacy else f"staged-{TASK}"
    staged_path = str(root / ".stage" / staged_name)
    staging = {
        "declared_task_name": f"mimo-v2.6-rl/{TASK}",
        "source_package_digest": manifest["final_digest"],
        "source_harbor_digest": harbor_digest, "staged_harbor_digest": harbor_digest,
        "staged_task_name": staged_name,
    } | (changes or {})
    if legacy:
        metadata = {"schema": "evallab.stability_manifest/v1", "trials": {"trial": {
            "task_version_digest": staging["source_package_digest"],
            "harbor_digest": staging["source_harbor_digest"],
            "task_name": staging["staged_task_name"],
            "source": str(root / "sources" / f"mimo-v2.6-rl__{TASK}" / staged_name),
        }}}
        metadata_path = job / "stability-manifest.json"
        (job / "result.json").write_text(json.dumps({
            "finished_at": "2026-10-17T00:00:00Z", "stats": {"n_completed_trials": 1},
        }))
    else:
        metadata = {"exit_code": 0, "timed_out": False, "task_staging": staging}
        metadata_path = job / "lab-metadata.json"
    metadata_path.write_text(json.dumps(metadata))
    native = {
        "id": f"00000000-0000-0000-0000-{'000000000001' if agent == 'oracle' else '000000000002'}{suffix}",
        "task_name": f"mimo-v2.6-rl/{TASK}", "task_id": {"path": staged_path},
        "task_checksum": "f" * 64,
        "config": {"agent": {"name": agent}, "task": {"path": staged_path}},
        "exception_info": None, "verifier_result": {"rewards": {"reward": reward}},
    }
    (trial / "config.json").write_text(json.dumps(native["config"]))
    (trial / "lock.json").write_text(json.dumps({
        "task": {"name": staged_name, "path": staged_path, "digest": harbor_digest, "type": "local"},
        "agent": {"name": agent},
    }))
    (trial / "result.json").write_text(json.dumps(native))
    (trial / "verifier/junit-grade.log").write_text(
        f"rc={0 if reward else 1} cases={cases} bad={0 if reward else 1} named={cases} missing=[]\n"
    )
    return metadata_path


def repeat_runs(
    metadata: Path, rewards: list[float | None], errors: list[str | None], executed: list[bool],
) -> None:
    verifier = metadata.parent / "trial/verifier"
    (verifier / "stability.json").write_text(json.dumps({
        "runs": [{"reward": reward, "error": error} for reward, error in zip(rewards, errors, strict=True)],
    }))
    for index, ran in enumerate(executed):
        repeat = verifier / "repeat" / str(index)
        repeat.mkdir(parents=True)
        if ran:
            (repeat / "junit-grade.log").write_text("rc=0 cases=1 bad=0 named=1 missing=[]\n")


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


@pytest.mark.parametrize("legacy", [False, True], ids=["lab-metadata", "stability-manifest"])
def test_relabelled_old_native_pair_cannot_qualify_new_package(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
    legacy: bool,
) -> None:
    reference, manifest = inputs
    paths = {
        control(tmp_path, manifest, "oracle", reward=1.0, legacy=legacy),
        control(tmp_path, manifest, "nop", reward=0.0, legacy=legacy),
    }
    assert classify(census, inputs, tmp_path, paths)["bucket"] == "v6_verified"
    package = Path(manifest["package_path"])
    (package / "instruction.md").write_text("Different executed input bytes.\n")
    rebuilt = manifest | {"final_digest": task_directory_digest(package)}
    new_harbor = harbor_task_digest(package)
    for path in paths:
        metadata = json.loads(path.read_text())
        if legacy:
            metadata["trials"]["trial"].update(
                task_version_digest=rebuilt["final_digest"], harbor_digest=new_harbor,
            )
        else:
            metadata["task_staging"].update(
                source_package_digest=rebuilt["final_digest"],
                source_harbor_digest=new_harbor, staged_harbor_digest=new_harbor,
            )
        path.write_text(json.dumps(metadata))
    row = classify(census, (reference, rebuilt), tmp_path, paths)
    assert row["bucket"] == "v6_unverified"
    assert all(not item["native_identity_verified"] for item in json.loads(row["native_evidence"]))


@pytest.mark.parametrize("legacy", [False, True], ids=["lab-metadata", "stability-manifest"])
@pytest.mark.parametrize("change", [
    "missing-lock", "missing-config", "lock-path", "lock-digest", "lock-name",
    "result-config-path", "result-task-path", "declared-name", "staged-name",
])
def test_native_lock_config_and_staging_must_agree(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
    legacy: bool, change: str,
) -> None:
    _, manifest = inputs
    oracle = control(tmp_path, manifest, "oracle", reward=1.0, legacy=legacy)
    nop = control(tmp_path, manifest, "nop", reward=0.0, legacy=legacy)
    trial = oracle.parent / "trial"
    if change in ("missing-lock", "missing-config"):
        (trial / f"{change.removeprefix('missing-')}.json").unlink()
    elif change.startswith("lock-"):
        path = trial / "lock.json"
        lock = json.loads(path.read_text())
        if change == "lock-path":
            lock["task"]["path"] = "/another-root/" + lock["task"]["name"]
        elif change == "lock-digest":
            lock["task"]["digest"] = "sha256:" + "e" * 64
        else:
            lock["task"]["name"] = "another-task"
        path.write_text(json.dumps(lock))
    elif change == "staged-name":
        metadata = json.loads(oracle.read_text())
        if legacy:
            metadata["trials"]["trial"]["task_name"] = "another-stage"
        else:
            metadata["task_staging"]["staged_task_name"] = "another-stage"
        oracle.write_text(json.dumps(metadata))
    else:
        path = trial / "result.json"
        native = json.loads(path.read_text())
        if change == "result-config-path":
            native["config"]["task"]["path"] = "/another-root/" + Path(native["task_id"]["path"]).name
        elif change == "result-task-path":
            native["task_id"]["path"] = "/another-root/" + Path(native["task_id"]["path"]).name
        else:
            native["task_name"] = "mimo-v2.6-rl/format-code-task-999999"
        path.write_text(json.dumps(native))
    assert classify(census, inputs, tmp_path, {oracle, nop})["bucket"] == "v6_unverified"


def test_matching_native_job_lock_can_bind_a_missing_trial_lock(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
) -> None:
    _, manifest = inputs
    oracle = control(tmp_path, manifest, "oracle", reward=1.0)
    nop = control(tmp_path, manifest, "nop", reward=0.0)
    lock_path = oracle.parent / "trial/lock.json"
    lock = json.loads(lock_path.read_text())
    other = lock | {"task": lock["task"] | {
        "path": "/another-root/" + lock["task"]["name"], "digest": "sha256:" + "e" * 64,
    }}
    (oracle.parent / "lock.json").write_text(json.dumps({"trials": [lock, other]}))
    lock_path.unlink()
    assert classify(census, inputs, tmp_path, {oracle, nop})["bucket"] == "v6_verified"


def test_native_checksum_is_diagnostic_not_a_package_or_harbor_digest(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
) -> None:
    _, manifest = inputs
    oracle = control(tmp_path, manifest, "oracle", reward=1.0)
    nop = control(tmp_path, manifest, "nop", reward=0.0)
    row = classify(census, inputs, tmp_path, {oracle, nop})
    assert row["bucket"] == "v6_verified"
    for item in json.loads(row["native_evidence"]):
        assert item["native_task_checksum"] == "f" * 64
        assert item["native_task_checksum"] != item["source_package_digest"].removeprefix("sha256:")
        assert item["native_task_checksum"] != item["executed_harbor_digest"].removeprefix("sha256:")


@pytest.mark.parametrize("agent,wrong", [("oracle", 0.0), ("oracle", 0.5), ("nop", 1.0), ("nop", 0.5)])
@pytest.mark.parametrize("wrong_first", [True, False], ids=["wrong-then-setup", "setup-then-wrong"])
def test_scored_wrong_repeat_blocks_later_exact_pair(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
    agent: str, wrong: float, wrong_first: bool,
) -> None:
    _, manifest = inputs
    partial = control(tmp_path, manifest, agent, reward=wrong if wrong_first else None, suffix="-partial")
    rewards = [wrong, None] if wrong_first else [None, wrong]
    errors = [None, "setup failure"] if wrong_first else ["setup failure", None]
    repeat_runs(partial, rewards, errors, [wrong_first, not wrong_first])
    if not wrong_first:
        result_path = partial.parent / "trial/result.json"
        native = json.loads(result_path.read_text())
        native["exception_info"] = {"exception_type": "SetupError", "exception_message": "setup failure"}
        result_path.write_text(json.dumps(native))
        metadata = json.loads(partial.read_text()) | {"exit_code": 1}
        partial.write_text(json.dumps(metadata))
    oracle = control(tmp_path, manifest, "oracle", reward=1.0)
    nop = control(tmp_path, manifest, "nop", reward=0.0)
    row = classify(census, inputs, tmp_path, {partial, oracle, nop})
    assert row["bucket"] == "broken_ref_or_env"
    assert row["reason"] == "exact_input_control_disagrees_with_reference_1_nop_0"
    evidence = next(item for item in json.loads(row["native_evidence"]) if item["execution_id"].endswith("-partial"))
    assert evidence["repeat_scored"] == [wrong_first, not wrong_first]
    assert not evidence["scored"]


@pytest.mark.parametrize("first_reward", [None, 1.0], ids=["only-setup-failures", "pass-then-setup"])
def test_partial_repeat_setup_failure_is_supersedable_by_exact_pair(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
    first_reward: float | None,
) -> None:
    _, manifest = inputs
    partial = control(tmp_path, manifest, "oracle", reward=first_reward, suffix="-partial")
    repeat_runs(
        partial, [first_reward, None],
        [None if first_reward is not None else "setup failure", "setup failure"],
        [first_reward is not None, False],
    )
    nop = control(tmp_path, manifest, "nop", reward=0.0)
    assert classify(census, inputs, tmp_path, {partial, nop})["bucket"] == "broken_ref_or_env"
    oracle = control(tmp_path, manifest, "oracle", reward=1.0)
    assert classify(census, inputs, tmp_path, {partial, oracle, nop})["bucket"] == "v6_verified"


@pytest.mark.parametrize("runner,output,reward,expected", [
    ("mocha", "  0 passing (5ms)\n", 1.0, "v6_unverified"),
    ("mocha", "  0 passing (5ms)\n", 0.0, "v6_unverified"),
    ("mocha", "  2 passing (5ms)\n", 1.0, "v6_verified"),
    ("mocha", "  0 passing (5ms)\n  1 failing\n", 0.0, "broken_ref_or_env"),
    ("jest", "No tests found\n", 1.0, "v6_unverified"),
    ("jest", "Tests: 2 passed, 2 total\n", 1.0, "v6_verified"),
    ("karma", "Executed 0 of 0 FAILED (0 secs)\n", 1.0, "v6_unverified"),
    ("karma", "Executed 1 of 1 SUCCESS (0 secs)\n", 1.0, "v6_verified"),
    ("phpunit", "OK (0 tests, 0 assertions)\n", 1.0, "v6_unverified"),
    ("phpunit", "OK (2 tests, 2 assertions)\n", 1.0, "v6_verified"),
    ("vitest", "Test Files 1 passed (1)\nTests 0 passed (0)\n", 1.0, "v6_unverified"),
    ("vitest", "Test Files 1 passed (1)\nTests 2 passed (2)\n", 1.0, "v6_verified"),
    ("custom", "script finished successfully\n", 0.0, "v6_unverified"),
    ("mocha", "", 1.0, "v6_unverified"),
])
def test_marker_verdict_requires_positive_cases_and_unknown_runners_stay_unverified(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
    runner: str, output: str, reward: float, expected: str,
) -> None:
    _, manifest = inputs
    oracle = control(tmp_path, manifest, "oracle", reward=reward)
    verifier = oracle.parent / "trial/verifier"
    (verifier / "junit-grade.log").write_text(f"runner={runner} rc=0\nrc=0 {runner} markers=True\n")
    (verifier / "runner.txt").write_text(f"RUNNER={runner}\n")
    (verifier / "test_output.log").write_text(output)
    nop = control(tmp_path, manifest, "nop", reward=0.0)
    assert classify(census, inputs, tmp_path, {oracle, nop})["bucket"] == expected


def test_positive_surefire_case_counts_still_qualify(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
) -> None:
    _, manifest = inputs
    oracle = control(tmp_path, manifest, "oracle", reward=1.0)
    (oracle.parent / "trial/verifier/junit-grade.log").write_text(
        "runner=mvn rc=0\nrc=0 mvn surefire=1 cases=2 bad=0\n"
    )
    nop = control(tmp_path, manifest, "nop", reward=0.0)
    assert classify(census, inputs, tmp_path, {oracle, nop})["bucket"] == "v6_verified"


@pytest.mark.parametrize("legacy", [False, True], ids=["lab-metadata", "stability-manifest"])
def test_duplicate_native_uuid_with_conflicting_bound_harbor_identity_is_refused(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
    legacy: bool,
) -> None:
    _, manifest = inputs
    original = control(tmp_path / "one", manifest, "oracle", reward=1.0, legacy=legacy)
    other_package = tmp_path / "other-package"
    shutil.copytree(Path(manifest["package_path"]), other_package)
    (other_package / "instruction.md").write_text("A conflicting recorded Harbor input.\n")
    # Keep the claimed source-package identity equal: the conflicting Harbor
    # identities must be rejected even when both copies are individually scored.
    conflicting = control(
        tmp_path / "two", manifest | {"package_path": str(other_package)},
        "oracle", reward=1.0, legacy=legacy,
    )
    with pytest.raises(ValueError, match="conflicting copies of native execution"):
        census["native_observations"]({original, conflicting})


@pytest.mark.parametrize("change", [
    "staged-harbor", "execution", "repeat-error", "exception", "process", "timeout", "lock-digest",
])
def test_duplicate_native_uuid_conflicting_unscored_facts_are_refused(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
    change: str,
) -> None:
    _, manifest = inputs
    original = control(tmp_path / "one", manifest, "oracle", reward=None, cases=0)
    conflicting = control(tmp_path / "two", manifest, "oracle", reward=None, cases=0)
    if change == "execution":
        (conflicting.parent / "trial/verifier/junit-grade.log").write_text(
            "rc=0 cases=1 bad=0 named=1 missing=[]\n"
        )
    elif change == "repeat-error":
        repeat_runs(original, [None], [None], [False])
        repeat_runs(conflicting, [None], ["setup failure"], [False])
    elif change == "exception":
        path = conflicting.parent / "trial/result.json"
        native = json.loads(path.read_text())
        native["exception_info"] = {"exception_type": "SetupError"}
        path.write_text(json.dumps(native))
    elif change == "lock-digest":
        path = conflicting.parent / "trial/lock.json"
        lock = json.loads(path.read_text())
        lock["task"]["digest"] = "sha256:" + "e" * 64
        path.write_text(json.dumps(lock))
    else:
        metadata = json.loads(conflicting.read_text())
        if change == "staged-harbor":
            metadata["task_staging"]["staged_harbor_digest"] = "sha256:" + "e" * 64
        elif change == "process":
            metadata["exit_code"] = 1
        else:
            metadata["timed_out"] = True
        conflicting.write_text(json.dumps(metadata))
    with pytest.raises(ValueError, match="conflicting copies of native execution"):
        census["native_observations"]({original, conflicting})


@pytest.mark.parametrize("legacy", [False, True], ids=["lab-metadata", "stability-manifest"])
@pytest.mark.parametrize("setup_error", [False, True], ids=["scored-copy", "unscored-copy"])
def test_recorded_native_relocations_deduplicate_without_losing_qualification(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
    legacy: bool, setup_error: bool,
) -> None:
    _, manifest = inputs
    original = control(tmp_path / "original", manifest, "oracle", reward=1.0, legacy=legacy)
    if setup_error:
        repeat_runs(original, [None], [f"setup failure at {original.parent}"], [False])
        path = original.parent / "trial/result.json"
        native = json.loads(path.read_text())
        native["exception_info"] = {
            "exception_type": "SetupError", "exception_message": f"setup failure at {original.parent}",
        }
        path.write_text(json.dumps(native))
    relocated_job = tmp_path / "published" / "oracle"
    shutil.copytree(original.parent, relocated_job)
    relocated = relocated_job / original.name
    trial = relocated_job / "trial"
    for name in ("config.json", "lock.json", "result.json"):
        path = trial / name
        # All recorded task references relocate together; the recorded stage
        # need not still exist, and byte-level receipt hashes legitimately differ.
        path.write_text(path.read_text().replace(str(tmp_path / "original"), str(tmp_path / "published")))
    if legacy:
        relocated.write_text(
            relocated.read_text().replace(str(tmp_path / "original"), str(tmp_path / "published"))
        )
    if setup_error:
        path = trial / "verifier/stability.json"
        path.write_text(path.read_text().replace(str(tmp_path / "original"), str(tmp_path / "published")))
    paths = {original, relocated, control(tmp_path, manifest, "nop", reward=0.0, legacy=legacy)}
    if setup_error:
        paths.add(control(tmp_path, manifest, "oracle", reward=1.0, suffix="-passed", legacy=legacy))
    row = classify(census, inputs, tmp_path, paths)
    assert row["bucket"] == "v6_verified"
    ids = json.loads(row["oracle_execution_ids"])
    assert ids.count("00000000-0000-0000-0000-000000000001") == 1


@pytest.mark.parametrize(("command", "output", "expected"), [
    (
        "/root/.pyenv/versions/2.7.18/bin/python -m nose -v test_search.py\n",
        "test_search (test_search.QueryTest) ... ok\n\n"
        "----------------------------------------------------------------------\nRan 4 tests in 0.364s\n\nOK\n",
        True,
    ),
    (
        "python3 -m unittest -v test_search\n",
        "test_search (test_search.QueryTest.test_search) ... FAIL\n\n"
        "Ran 1 test in 0.010s\n\nFAILED (failures=1)\n",
        True,
    ),
    (
        "python -m nose -v test_search.py\n",
        "test_search (test_search.QueryTest) ... ERROR\n\n"
        "Ran 1 test in 0.010s\n\nFAILED (errors=1)\n",
        True,
    ),
    (
        "python -m nose -v test_search.py\n",
        "test_search (test_search.QueryTest) ... [ERROR] HTTP request failed\n"
        "Traceback from application logging\nFAIL\n\n"
        "FAIL: test_search (test_search.QueryTest)\nAssertionError: 500 != 200\n\n"
        "Ran 1 test in 0.010s\n\nFAILED (failures=1)\n",
        True,
    ),
    ("python -m nose test_search.py\n", "Ran 0 tests in 0.001s\n\nOK\n", False),
    (
        "python3 -m unittest test_missing\n",
        "test_missing (unittest.loader._FailedTest.test_missing) ... ERROR\n\n"
        "Ran 1 test in 0.001s\n\nFAILED (errors=1)\n",
        False,
    ),
    (
        "python3 -m unittest test_missing\n",
        "ERROR: test_missing (unittest.loader._FailedTest.test_missing)\n"
        "Ran 1 test in 0.001s\n\nFAILED (errors=1)\n",
        False,
    ),
    (
        "python -m nose test_missing.py\n",
        "Failure: ImportError (test_missing) ... ERROR\n\n"
        "Ran 1 test in 0.001s\n\nFAILED (errors=1)\n",
        False,
    ),
    (
        "python -m nose test_search.py\n",
        "test_search (test_search.QueryTest) ... ok\n\nRan 1 test in 0.001s\n",
        False,
    ),
    (
        "echo 'python -m nose test_search.py'\n",
        "test_search (test_search.QueryTest) ... ok\n\nRan 1 test in 0.001s\n\nOK\n",
        False,
    ),
    (
        "",
        "test_search (test_search.QueryTest) ... ok\n\nRan 1 test in 0.001s\n\nOK\n",
        False,
    ),
    (
        "python -m nose test_search.py\n",
        "test_search (test_search.QueryTest) ... skipped 'unavailable'\n\n"
        "Ran 1 test in 0.001s\n\nOK (skipped=1)\n",
        False,
    ),
    ("python -m nose test_search.py\n", "OK\nREWARD=1 rc=0\n", False),
    ("python -m nose test_search.py\n", "Ran 1 test in 0.001s\n\nOK\n", False),
])
def test_custom_python_execution_requires_command_cases_and_completed_suite(
    census: dict[str, Any], inputs: tuple[dict[str, str], dict[str, str]], tmp_path: Path,
    command: str, output: str, expected: bool,
) -> None:
    _, manifest = inputs
    metadata = control(tmp_path, manifest, "oracle", reward=0.0)
    verifier = metadata.parent / "trial/verifier"
    (verifier / "runner.txt").write_text("RUNNER=custom\n")
    (verifier / "junit-grade.log").write_text("runner=custom rc=1\nrc=1 custom markers=True\n")
    (verifier / "resolved_command.txt").write_text(command)
    (verifier / "test_output.log").write_text(output)
    observed = census["native_observations"]({metadata})
    assert observed[0]["scored"] is expected


DJANGO_CASE = "test_order (example.tests.OrderTests.test_order) ... ok\n"
DJANGO_SUMMARY = "\nRan 1 test in 0.010s\n\nOK\n"
TWISTED_CASE = "example.tests.test_order\n  OrderTests\n    test_order ... [OK]\n"
TWISTED_SUMMARY = "\nRan 1 test in 0.010s\n\nPASSED (successes=1)\n"


def framework_logs(
    root: Path, command: str, output: str, *,
    grade: str = "runner=custom rc=0\nrc=0 custom: exit-code grading\nrc=0 custom markers=True\n",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "junit-grade.log").write_text(grade)
    (root / "resolved_command.txt").write_text(command)
    (root / "test_output.log").write_text(output)
    return root


@pytest.mark.parametrize(("command", "output"), [
    (
        "pipenv run python manage.py test example.tests.OrderTests.test_order -v 2\n",
        "test_order (example.tests.OrderTests.test_order) ... application warning\n"
        "Traceback (most recent call last):\n  application_callback()\n"
        "ValueError: optional integration unavailable\nok\n"
        "----------------------------------------------------------------------\n"
        + DJANGO_SUMMARY,
    ),
    ("python3 manage.py test example.tests -v 2\n", DJANGO_CASE + DJANGO_SUMMARY),
    (
        "exec /opt/env/bin/python3.12 ./manage.py test example.tests -v 2\n",
        "test_order (example.tests.OrderTests.test_order) ... FAIL\n"
        "\nRan 1 test in 0.010s\n\nFAILED (failures=1)\n",
    ),
    ("trial example.tests.test_order\n", TWISTED_CASE + TWISTED_SUMMARY),
    (
        "/opt/env/bin/trial example.tests.test_order\n",
        "example.tests.test_order\n  OrderTests\n    test_order ... [ERROR]\n"
        "\nRan 1 test in 0.010s\n\nFAILED (errors=1)\n",
    ),
    (
        "python2.7 -m twisted.trial example.tests.test_order\n",
        "example.tests.test_order\n  OrderTests\n"
        "    test_optional ... [SKIPPED]\n    test_order ... [OK]\n"
        "\nRan 2 tests in 0.010s\n\nPASSED (skips=1, successes=1)\n",
    ),
])
def test_custom_framework_counted_named_execution(
    census: dict[str, Any], tmp_path: Path, command: str, output: str,
) -> None:
    assert census["positive_case_execution"](framework_logs(tmp_path, command, output))


@pytest.mark.parametrize("output", [
    DJANGO_CASE + DJANGO_SUMMARY, TWISTED_CASE + TWISTED_SUMMARY,
], ids=["django-output", "twisted-output"])
@pytest.mark.parametrize("command", [
    "",
    "bash /testbed/test_command.sh\n",
    "echo 'python manage.py test example.tests'\n",
    "# python manage.py test example.tests\n",
    "echo 'trial example.tests'\n",
    "# trial example.tests\n",
    "python manage.py collectstatic --noinput\n",
    "python manage.py testbed example.tests\n",
    "python manage.py test-extra example.tests\n",
    "pipenv run python helper.py test example.tests\n",
    "python -c 'import django; import twisted.trial'\n",
    "nottrial example.tests\n",
])
def test_custom_framework_commands_are_not_inferred_from_output(
    census: dict[str, Any], tmp_path: Path, command: str, output: str,
) -> None:
    assert not census["positive_case_execution"](framework_logs(tmp_path, command, output))


@pytest.mark.parametrize("command", [
    "pipenv run python manage.py test example.tests -v 2\n",
    "trial example.tests\n",
])
@pytest.mark.parametrize("output", [
    "OK\n", "PASSED\n", "PASSED (successes=20)\n", "20\n",
    "Test Files 1 passed (1)\nTests 0 passed (0)\n",
    "REWARD=1 rc=0\n", DJANGO_SUMMARY, TWISTED_SUMMARY,
    "ImportError: cannot import name 'missing'\n",
])
def test_custom_framework_status_counts_and_markers_alone_are_unmeasured(
    census: dict[str, Any], tmp_path: Path, command: str, output: str,
) -> None:
    assert not census["positive_case_execution"](framework_logs(tmp_path, command, output))


@pytest.mark.parametrize(("command", "output"), [
    ("trial example.tests\n", DJANGO_CASE + DJANGO_SUMMARY),
    ("python manage.py test example.tests\n", TWISTED_CASE + TWISTED_SUMMARY),
    (
        "python manage.py test example.tests\n",
        DJANGO_CASE + "\nRan 0 tests in 0.010s\n\nOK\n",
    ),
    (
        "python manage.py test example.tests\n",
        "test_order (example.tests.OrderTests.test_order) ... starting\n" + DJANGO_SUMMARY,
    ),
    ("python manage.py test example.tests\n", DJANGO_CASE + "\nRan 1 test in 0.010s\n"),
    (
        "python manage.py test example.tests\n",
        "test_order (example.tests.OrderTests.test_order) ... skipped 'unavailable'\n"
        "\nRan 1 test in 0.010s\n\nOK (skipped=1)\n",
    ),
    (
        "python manage.py test example.tests\n",
        "test_missing (unittest.loader._FailedTest.test_missing) ... ERROR\n"
        "\nRan 1 test in 0.010s\n\nFAILED (errors=1)\n",
    ),
    (
        "python manage.py test example.tests\n",
        "setUpClass (unittest.suite._ErrorHolder) ... ERROR\n"
        "\nRan 1 test in 0.010s\n\nFAILED (errors=1)\n",
    ),
    (
        "python manage.py test example.tests\n",
        "Failure: ImportError (test_missing) ... ERROR\n"
        "\nRan 1 test in 0.010s\n\nFAILED (errors=1)\n",
    ),
    ("trial example.tests\n", TWISTED_CASE + "\nRan 0 tests in 0.010s\n\nPASSED\n"),
    (
        "trial example.tests\n",
        "example.tests.test_order\n  OrderTests\n    test_order ... starting\n" + TWISTED_SUMMARY,
    ),
    ("trial example.tests\n", TWISTED_CASE + "\nRan 1 test in 0.010s\n"),
    ("trial example.tests\n", TWISTED_CASE + "\nRan 1 test in 0.010s\n\nPASSED\n"),
    (
        "trial example.tests\n",
        TWISTED_CASE + "\nRan 1 test in 0.010s\n\nPASSED (files=1)\n",
    ),
    (
        "trial example.tests\n",
        "example.tests.test_order\n  OrderTests\n    test_order ... [SKIPPED]\n"
        "\nRan 1 test in 0.010s\n\nPASSED (skips=1)\n",
    ),
    (
        "trial example.tests\n",
        "example.tests.test_missing\n  test_missing ... [ERROR]\n"
        "ImportError: no module named test_missing\n"
        "\nRan 1 test in 0.010s\n\nFAILED (errors=1)\n",
    ),
    (
        "trial example.tests\n",
        "example.tests.test_missing\n  ErrorHolder\n    test_missing ... [ERROR]\n"
        "\nRan 1 test in 0.010s\n\nFAILED (errors=1)\n",
    ),
    (
        "trial example.tests\n",
        "example.tests.test_missing\n    test_missing ... [ERROR]\n"
        "\nRan 1 test in 0.010s\n\nFAILED (errors=1)\n",
    ),
])
def test_custom_framework_unfinished_zero_skipped_and_loader_suites_are_unmeasured(
    census: dict[str, Any], tmp_path: Path, command: str, output: str,
) -> None:
    assert not census["positive_case_execution"](framework_logs(tmp_path, command, output))


def test_custom_framework_evidence_stays_with_its_invocation(
    census: dict[str, Any], tmp_path: Path,
) -> None:
    framework_logs(
        tmp_path / "repeat/0", "python manage.py test example.tests -v 2\n", "starting\n",
    )
    framework_logs(
        tmp_path / "repeat/1", "bash unrelated_command.sh\n", DJANGO_CASE + DJANGO_SUMMARY,
    )
    assert not census["positive_case_execution"](tmp_path)


def test_twisted_completed_twenty_case_suite_survives_site_warning(
    census: dict[str, Any], tmp_path: Path,
) -> None:
    warning = (
        "Error processing line 1 of /usr/lib/python3/dist-packages/distutils-precedence.pth:\n\n"
        "  Traceback (most recent call last):\n"
        '    File "<frozen site>", line 219, in addpackage\n'
        "  ModuleNotFoundError: No module named '_distutils_hack'\n\nRemainder of file ignored\n"
    )
    output = (
        "CryptographyDeprecationWarning: legacy Python\nexample.tests.test_transport\n"
        "  TransportTests\n"
        + "".join(f"    test_case_{index} ... [OK]\n" for index in range(20))
        + "\nRan 20 tests in 0.564s\n\nPASSED (successes=20)\n"
    )
    trial = framework_logs(
        tmp_path, "trial example.tests.test_transport\n", output,
        grade=warning + "runner=custom rc=0\nrc=0 custom: exit-code grading\n",
    )
    assert census["positive_case_execution"](trial)


def test_pytest_zero_case_import_collection_remains_unmeasured(
    census: dict[str, Any], tmp_path: Path,
) -> None:
    trial = framework_logs(
        tmp_path, "exec python -m pytest tests/test_solution_activities.py -v\n",
        "collecting ... collected 0 items / 1 error\n"
        "______ ERROR collecting tests/test_solution_activities.py _______\n"
        "ImportError while importing test module 'tests/test_solution_activities.py'.\n"
        "E   ImportError: cannot import name '_ideal_activities_fct'"
        " from 'burnman.classes.solutionmodel'\n"
        "ERROR tests/test_solution_activities.py\n"
        "Interrupted: 1 error during collection\n1 warning, 1 error in 3.73s\n",
        grade="runner=pytest rc=2\nrc=2 cases=0 bad=0 skipped=0 named=0 missing=[]\n",
    )
    assert not census["positive_case_execution"](trial)


@pytest.mark.parametrize("terminal", [
    "OK (files=1)", "OK (custom)", "FAILED", "FAILED (files=1)", "FAILED (custom)",
])
def test_django_completion_requires_first_party_suite_status(
    census: dict[str, Any], tmp_path: Path, terminal: str,
) -> None:
    trial = framework_logs(
        tmp_path, "python manage.py test example.tests -v 2\n",
        DJANGO_CASE + f"\nRan 1 test in 0.010s\n\n{terminal}\n",
    )
    assert not census["positive_case_execution"](trial)
