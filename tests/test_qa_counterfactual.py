"""Counterfactual regrade: actual patch semantics, fail-closed evidence and T3 rulings."""

from __future__ import annotations

import json
import subprocess
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from evallab.qa import counterfactual as cf
from evallab.qa import trace

TASK = "mimo-v2.6-rl/format-code-task-000001"
PATCH = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"


def _package(path: Path, *, separate: bool = True) -> Path:
    (path / "tests").mkdir(parents=True)
    (path / "tests/test.sh").write_text("#!/bin/bash\ntrue\n")
    (path / "instruction.md").write_text("Replace old with new.\n")
    config = f'[task]\nname = "{TASK}"\n[environment]\nworkdir = "/testbed"\n'
    if separate:
        config += '[verifier]\nenvironment_mode = "separate"\n'
    (path / "task.toml").write_text(config)
    return path


def _grader(tmp: Path) -> cf.Grader:
    return cf.Grader(TASK, cf.V6, "sha256:" + "a" * 64, _package(tmp / "pkg"), "manifest.csv")


def _trial(tmp: Path, *, diff: str | None = PATCH, reward: float = 1.0) -> Path:
    trial = tmp / "old-pass"
    (trial / "verifier").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps({"verifier_result": {"rewards": {"reward": reward}}})
    )
    if diff is not None:
        (trial / "verifier/agent.diff").write_text(diff)
    return trial


@pytest.fixture(autouse=True)
def _isolated_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    import evallab.docker_lock

    monkeypatch.setattr(evallab.docker_lock, "docker_slot", lambda **_: nullcontext())


def test_patch_script_applies_exactly_the_change_and_fails_on_the_wrong_base(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work dir with spaces"
    work.mkdir()
    target = work / "x.py"
    target.write_text("old\n")
    script = cf._patch_script(PATCH, str(work))
    first = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    assert target.read_text() == "new\n"
    second = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert second.returncode != 0
    assert target.read_text() == "new\n"


@pytest.mark.parametrize(
    ("status", "rewards", "recorded", "outcome"),
    [
        ("ok", [0.0], 1.0, "flips"),
        ("ok", [1.0], 1.0, "stays"),
        ("ok", [1.0], 0.0, "loosened"),
        ("ok", [0.0], 0.0, "not_a_pass"),
        ("infra", [0.0], 1.0, "refused"),
        ("ok", [float("nan")], 1.0, "refused"),
        ("ok", [True], 1.0, "refused"),
        ("ok", [], 1.0, "refused"),
    ],
)
def test_patch_replay_never_counts_infra_or_absent_scores_as_rejections(
    tmp_path: Path, status: str, rewards: list[Any], recorded: float, outcome: str
) -> None:
    trial = _trial(tmp_path, reward=recorded)
    before = {str(p.relative_to(trial)): p.read_bytes() for p in trial.rglob("*") if p.is_file()}

    def matrix_runner(matrix, *, root):
        return SimpleNamespace(
            results=[
                {
                    "status": status,
                    "rewards": rewards,
                    "error": "infra" if status == "infra" else None,
                }
            ],
            receipt_path=root / "runs/matrix-receipt.json",
        )

    result = cf.replay(
        trial,
        _grader(tmp_path),
        tmp_path / "work",
        repo_root=tmp_path,
        matrix_runner=matrix_runner,
    )
    assert result["outcome"] == outcome
    assert result["input_kind"] == "native_patch_replay"
    assert before == {
        str(p.relative_to(trial)): p.read_bytes() for p in trial.rglob("*") if p.is_file()
    }
    assert not (trial / "artifacts").exists()  # no fabricated snapshot, manifest, or trajectory
    assert bool(result["refusals"]) is (outcome == "refused")


@pytest.mark.parametrize(
    ("environment", "takes_local_slot"), [("docker", True), ("daytona", False)]
)
def test_replay_runs_on_the_requested_backend_and_only_docker_holds_the_host_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str, takes_local_slot: bool
) -> None:
    import evallab.docker_lock

    slots: list[str] = []

    def docker_slot(*, label: str):
        slots.append(label)
        return nullcontext()

    monkeypatch.setattr(evallab.docker_lock, "docker_slot", docker_slot)
    seen: list[str] = []

    def matrix_runner(matrix, *, root):
        seen.append(matrix.environment)
        return SimpleNamespace(
            results=[{"status": "ok", "rewards": [0.0], "error": None}],
            receipt_path=root / "runs/matrix-receipt.json",
        )

    result = cf.replay(
        _trial(tmp_path),
        _grader(tmp_path),
        tmp_path / "work",
        repo_root=tmp_path,
        matrix_runner=matrix_runner,
        environment=environment,
    )
    assert seen == [environment]
    assert result["environment"] == environment
    assert result["outcome"] == "flips"
    assert bool(slots) is takes_local_slot


def test_replay_rejects_an_unknown_backend_before_running_anything(tmp_path: Path) -> None:
    def matrix_runner(matrix, *, root):
        raise AssertionError("must not run")

    with pytest.raises(ValueError, match="unsupported replay environment"):
        cf.replay(
            _trial(tmp_path),
            _grader(tmp_path),
            tmp_path / "work",
            repo_root=tmp_path,
            matrix_runner=matrix_runner,
            environment="modal",
        )


def test_missing_change_is_unmeasurable_not_a_successful_nop(tmp_path: Path) -> None:
    result = cf.replay(
        _trial(tmp_path, diff=None), _grader(tmp_path), tmp_path / "work", repo_root=tmp_path
    )
    assert result["outcome"] == "refused"
    assert result["refusals"] == ["source_patch_unavailable"]


def test_only_compiled_artifact_stanzas_are_dropped() -> None:
    bytecode = (
        "diff --git a/pkg/__pycache__/x.pyc b/pkg/__pycache__/x.pyc\nGIT binary patch\nliteral 1\n"
    )
    kept, dropped = cf.strip_binary_artifacts(bytecode + PATCH)
    assert kept == PATCH
    assert dropped == ["pkg/__pycache__/x.pyc"]


@pytest.mark.parametrize(
    ("nop", "reference", "valid"),
    [
        (0.0, 1.0, True),
        (1.0, 1.0, False),
        (0.0, 0.0, False),
        (0.0, None, False),
        (None, 1.0, False),
    ],
)
def test_controls_require_both_nop_rejection_and_reference_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nop: float | None,
    reference: float | None,
    valid: bool,
) -> None:
    monkeypatch.setattr(
        cf,
        "replay",
        lambda *_, diff_text, **__: {"regraded": nop if diff_text == "" else reference},
    )
    monkeypatch.setattr(
        cf, "reference_patch", lambda *_: None if reference is None else "reference"
    )
    controls = cf.grader_controls(
        _grader(tmp_path), tmp_path, tmp_path / "controls", repo_root=tmp_path
    )
    assert controls["valid"] is valid
    assert controls["reference_available"] is (reference is not None)


def test_changing_the_reference_invalidates_cached_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch = ["reference-v1"]
    monkeypatch.setattr(cf, "reference_patch", lambda *_: patch[0])
    monkeypatch.setattr(
        cf,
        "replay",
        lambda *_, diff_text, **__: {"regraded": 0.0 if diff_text in ("", "reference-v2") else 1.0},
    )
    grader = _grader(tmp_path)
    first = cf.grader_controls(grader, tmp_path, tmp_path / "controls", repo_root=tmp_path)
    assert first["valid"] is True
    patch[0] = "reference-v2"
    second = cf.grader_controls(grader, tmp_path, tmp_path / "controls", repo_root=tmp_path)
    assert second["valid"] is False
    assert second["reference"] == 0.0


def test_t3_counterfactual_is_path_free_and_ignores_unvalidated_graders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graders = [_grader(tmp_path)]
    monkeypatch.setattr(cf, "newer_graders", lambda *_, **__: graders)
    monkeypatch.setattr(
        cf,
        "replay",
        lambda *_, **__: {
            "grader": graders[0].label,
            "outcome": "flips",
            "recorded": 1.0,
            "regraded": 0.0,
            "refusals": ["diagnostic: /private/runs/cheat-run/source.py"],
        },
    )
    monkeypatch.setattr(trace, "counterfactual_root", lambda: tmp_path / "cf")
    for valid in (False, True):
        monkeypatch.setattr(
            cf,
            "grader_controls",
            lambda *_, valid=valid, **__: {
                "valid": valid,
                "nop": 0.0,
                "reference": 1.0 if valid else None,
            },
        )
        result = trace.regrade_counterfactual(tmp_path, TASK)
        assert result["ran"] is valid
        assert "/private" not in json.dumps(result)
        assert "cheat-run" not in json.dumps(result)
    assert result["lowest"] == 0.0


def test_repaired_graders_use_only_the_recorded_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import evallab.task_variants as task_variants

    store = tmp_path / "variants"
    monkeypatch.setattr(task_variants, "default_variants_root", lambda *_: store)
    digests = {}

    def repair(name: str, *, separate: bool, task_name: str = TASK, tampered: bool = False):
        digest = "sha256:" + name * 64
        package = _package(store / task_name.replace("/", "__") / digest[7:19], separate=separate)
        digests[package] = "sha256:" + "f" * 64 if tampered else digest
        return (
            tmp_path / f"{name}.json",
            SimpleNamespace(
                task_name=task_name, transform="verifier-repair@1", variant_digest=digest
            ),
        )

    def separate_over(parent: Path, task_name: str, repo_root: Path, *, via: str):
        return cf.Grader(task_name, f"{via}>{cf.V10}", "sha256:" + "c" * 64, parent, "c.json")

    monkeypatch.setattr(cf, "_digest", lambda package: digests[package])
    monkeypatch.setattr(cf, "_records", lambda *_: [])
    monkeypatch.setattr(cf, "separate_over", separate_over)
    extra = [
        repair("1", separate=True),
        repair("2", separate=False),
        repair("3", separate=True, tampered=True),
        repair("4", separate=True, task_name="mimo-v2.6-rl/format-code-task-000002"),
    ]
    graders = cf.repaired_graders(TASK, tmp_path, extra)
    assert {g.variant_digest for g in graders} == {"sha256:" + "1" * 64, "sha256:" + "c" * 64}
    assert {g.transform for g in graders} == {
        "verifier-repair@1",
        "verifier-repair@1>separate-verifier@10",
    }


def test_batch_cache_tracks_source_bytes_without_freezing_population_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trial, grader = _trial(tmp_path), _grader(tmp_path)
    monkeypatch.setattr(cf, "newer_graders", lambda *_, **__: [grader])
    monkeypatch.setattr(
        cf, "grader_controls", lambda *_, **__: {"valid": True, "nop": 0.0, "reference": 1.0}
    )
    monkeypatch.setattr(trace, "counterfactual_root", lambda: tmp_path / "cache")

    def observed(*_, **__):
        score = 0.0 if "+wrong" in (trial / "verifier/agent.diff").read_text() else 1.0
        return {
            "grader": grader.label,
            "recorded": 1.0,
            "regraded": score,
            "outcome": "flips" if score == 0 else "stays",
        }

    monkeypatch.setattr(cf, "replay", observed)
    subject = {"item_id": "example", "task": TASK, "trial": trial, "group": "unreviewed"}
    first = trace.regrade_passes([subject], tmp_path / "out", log=lambda _: None)
    assert first[0]["outcome"] == "stays"
    (trial / "verifier/agent.diff").write_text(PATCH.replace("+new", "+wrong"))
    second = trace.regrade_passes([subject], tmp_path / "out", log=lambda _: None)
    assert second[0]["outcome"] == "flips"
    subject["group"] = "faulty"
    third = trace.regrade_passes([subject], tmp_path / "out", log=lambda _: None)
    assert third[0]["outcome"] == "flips"
    assert third[0]["group"] == "faulty"


def test_selected_manifest_refreshes_and_mutated_package_is_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from evallab.mimo_clean import MANIFEST_COLUMNS, write_manifest
    from evallab.storage import paths

    monkeypatch.setattr(paths, "shared_checkout_root", lambda _: tmp_path)
    packages = [_package(tmp_path / name) for name in ("first", "second")]
    (packages[1] / "tests/test.sh").write_text("#!/bin/bash\nfalse\n")
    manifest = tmp_path / "selected.csv"
    for package in packages:
        digest = cf._digest(package)
        row = {
            **dict.fromkeys(MANIFEST_COLUMNS, ""),
            "task_id": TASK.rsplit("/", 1)[-1],
            "package_path": str(package.relative_to(tmp_path)),
            "final_digest": digest,
            "status": "built",
        }
        write_manifest([row], manifest)
        selected = cf.v6_grader(TASK, tmp_path, manifest=manifest)
        assert selected is not None
        assert selected.package == package
        assert selected.variant_digest == digest
    (packages[1] / "tests/test.sh").write_text("#!/bin/bash\necho unexpected mutation\n")
    assert cf.v6_grader(TASK, tmp_path, manifest=manifest) is None
