"""Focused behavioral boundary tests for MetaHarnessEngine in Eval Lab."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

gepa_config = pytest.importorskip(
    "gepa.oa.config", reason="Install the isolated harness-gepa requirements"
)
meta_engine = import_module("evallab.gepa_optimizer.meta_engine")


def test_example_split_boundary_rejection() -> None:
    """Verify strictly only split='development' is permitted; all others are rejected."""
    with pytest.raises(ValueError, match="only split='development'"):
        meta_engine.build_meta_harness_task(
            seed_candidate="Test instructions",
            examples=[{"task_id": "t1", "split": "test"}],
        )

    with pytest.raises(ValueError, match="only split='development'"):
        meta_engine.build_meta_harness_task(
            seed_candidate="Test instructions",
            examples=[{"task_id": "t1", "split": "evaluation"}],
        )

    with pytest.raises(ValueError, match="only split='development'"):
        meta_engine.build_meta_harness_task(
            seed_candidate="Test instructions",
            examples=[{"task_id": "t1"}],
        )


def test_task_partitioning_and_sealed_test_set() -> None:
    """Verify single-task train-only and explicit validation partitioning, with sealed test_set."""
    # Single permitted task (no validation task ids): train-only
    single_task = meta_engine.build_meta_harness_task(
        seed_candidate="Baseline preamble",
        examples=[{"task_id": "t1", "split": "development", "path": "p1"}],
        validation_task_ids=None,
    )
    assert single_task.test_set is None
    assert single_task.train_set is not None
    assert len(single_task.train_set) == 1
    assert single_task.val_set is None

    # Multiple tasks with explicit validation_task_ids
    multi_task = meta_engine.build_meta_harness_task(
        seed_candidate="Baseline preamble",
        examples=[
            {"task_id": "t1", "split": "development", "path": "p1"},
            {"task_id": "t2", "split": "development", "path": "p2"},
            {"task_id": "t3", "split": "development", "path": "p3"},
        ],
        validation_task_ids=["t2"],
    )
    assert multi_task.test_set is None
    assert multi_task.train_set is not None
    assert multi_task.val_set is not None


def test_included_engine_rejects_disabled_sandbox() -> None:
    with pytest.raises(ValueError):
        meta_engine.make_meta_harness_engine(gepa_config.OptimizeAnythingConfig(sandbox=False))


def test_candidate_containment_boundary(tmp_path: Path) -> None:
    """Verify _load_candidate rejects directory traversal and absolute path escape."""
    from gepa.oa.engines.meta_harness import _load_candidate

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    (work_dir / "valid.txt").write_text("Valid instruction")

    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("Outside secret")

    # Inside read works
    assert _load_candidate(work_dir, "valid.txt") == "Valid instruction"

    # Traversal escapes return None
    assert _load_candidate(work_dir, "../outside.txt") is None
    assert _load_candidate(work_dir, "../../outside.txt") is None
    assert _load_candidate(work_dir, "/etc/passwd") is None
    assert _load_candidate(work_dir, "missing.txt") is None


def test_materialization_boundary_single_and_both(tmp_path: Path) -> None:
    """Verify train-only and train+val materialization into train/ and absence of test/."""
    from gepa.oa.budget import BudgetTracker
    from gepa.oa.engines.meta_harness import _materialize_sandbox
    from gepa.oa.eval_server import EvalServer
    from gepa.oa.task import Task

    # Single-task train-only
    task_single = Task(
        name="mat_single",
        seed_candidate="Seed text",
        train_set=[{"id": "tr_1", "desc": "train 1"}],
        val_set=None,
        test_set=None,
    )
    budget = BudgetTracker(max_evals=10)
    server_single = EvalServer(task=task_single, evaluate=lambda c, ex: (1.0, {}), budget=budget)
    work_single = tmp_path / "work_single"
    _materialize_sandbox(work_single, task_single, server_single, budget)

    assert (work_single / "train" / "tr_1.json").exists()
    assert not (work_single / "test").exists()

    # Train + Val both materialize into train/
    task_both = Task(
        name="mat_both",
        seed_candidate="Seed text",
        train_set=[{"id": "tr_1", "desc": "train 1"}],
        val_set=[{"id": "val_1", "desc": "val 1"}],
        test_set=None,
    )
    server_both = EvalServer(task=task_both, evaluate=lambda c, ex: (1.0, {}), budget=budget)
    work_both = tmp_path / "work_both"
    _materialize_sandbox(work_both, task_both, server_both, budget)

    assert (work_both / "train" / "tr_1.json").exists()
    assert (work_both / "train" / "val_1.json").exists()
    assert not (work_both / "test").exists()


def test_safe_meta_harness_engine_symlinks_preserved_no_outside_bytes(tmp_path: Path) -> None:
    """Verify SafeMetaHarnessEngine.retain preserves symlinks and copies zero outside bytes."""
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_secret = outside_dir / "secret.txt"
    outside_secret.write_text("CONFIDENTIAL_OUTSIDE_BYTES")

    owned_temp = tmp_path / "owned_workspace"
    owned_temp.mkdir()
    (owned_temp / "regular.txt").write_text("Inside normal instruction")

    # Create symlink pointing outside owned workspace
    symlink_target = owned_temp / "link_to_outside"
    symlink_target.symlink_to(outside_secret)

    cfg = gepa_config.OptimizeAnythingConfig(engine="meta_harness", sandbox=True)
    engine = meta_engine.make_meta_harness_engine(cfg)

    class MockTempDir:
        name = str(owned_temp)

        def cleanup(self) -> None:
            pass

    engine._pending_tempdir = MockTempDir()  # type: ignore[assignment]

    dest_dir = tmp_path / "retained_output"
    target_work = engine.retain(dest_dir, cleanup_tempdir=False)

    # Regular file copied
    assert (target_work / "regular.txt").read_text() == "Inside normal instruction"

    # Symlink MUST be preserved as a symlink, NOT dereferenced into a regular file
    retained_link = target_work / "link_to_outside"
    assert retained_link.is_symlink()

    # Prove zero outside bytes are inlined: when outside file is removed,
    # the link is broken rather than containing stale copied host bytes
    outside_secret.unlink()
    assert not retained_link.exists()  # Broken symlink
    with pytest.raises(FileNotFoundError):
        retained_link.read_text()


def test_safe_meta_harness_engine_unexpected_work_dir_rejected(tmp_path: Path) -> None:
    """Verify SafeMetaHarnessEngine.retain rejects result metadata with mismatched work_dir."""
    owned_temp = tmp_path / "owned_workspace"
    owned_temp.mkdir()

    cfg = gepa_config.OptimizeAnythingConfig(engine="meta_harness", sandbox=True)
    engine = meta_engine.make_meta_harness_engine(cfg)

    class MockTempDir:
        name = str(owned_temp)

        def cleanup(self) -> None:
            pass

    engine._pending_tempdir = MockTempDir()  # type: ignore[assignment]

    class FakeResult:
        metadata = {"work_dir": str(tmp_path / "tampered_arbitrary_dir")}

    dest_dir = tmp_path / "retained_output"
    with pytest.raises(ValueError, match="Unexpected work_dir"):
        engine.retain(dest_dir, result=FakeResult())


def test_safe_meta_harness_engine_repeated_retain_harmless_no_op(tmp_path: Path) -> None:
    """Verify repeated retain after tempdir cleanup is a harmless no-op, but unknown reported dir rejects."""
    cfg = gepa_config.OptimizeAnythingConfig(engine="meta_harness", sandbox=True)
    engine = meta_engine.make_meta_harness_engine(cfg)
    assert engine._pending_tempdir is None

    dest_dir = tmp_path / "retained_output"

    # Harmless no-op when no reported work_dir
    target = engine.retain(dest_dir)
    assert target == dest_dir / "work"

    # Rejection if a result reports an unknown work_dir while no owned tempdir exists
    class FakeResult:
        metadata = {"work_dir": str(tmp_path / "arbitrary_dir")}

    with pytest.raises(ValueError, match="Cannot retain reported work_dir"):
        engine.retain(dest_dir, result=FakeResult())


def test_qualification_symlink_output_dir_rejected(tmp_path: Path) -> None:
    """Verify qualify_meta_harness rejects symlinked output_dir before resolve."""
    real_out = tmp_path / "real_output"
    real_out.mkdir()
    sym_out = tmp_path / "symlink_output"
    sym_out.symlink_to(real_out)

    with pytest.raises(ValueError, match="Output directory cannot be a symlink"):
        meta_engine.qualify_meta_harness(
            evaluator=lambda c, ex: (1.0, {}),
            examples=[{"task_id": "t1", "split": "development"}],
            seed_candidate="Seed",
            output_dir=sym_out,
        )


def test_qualification_existing_workspace_rejected(tmp_path: Path) -> None:
    """Verify qualify_meta_harness rejects already existing workspace dir (exist_ok=False)."""
    out_dir = tmp_path / "test_out"
    out_dir.mkdir()
    existing_work = out_dir / "workspace"
    existing_work.mkdir()

    with pytest.raises(ValueError, match="Workspace directory already exists"):
        meta_engine.qualify_meta_harness(
            evaluator=lambda c, ex: (1.0, {}),
            examples=[{"task_id": "t1", "split": "development"}],
            seed_candidate="Seed",
            output_dir=out_dir,
        )


def test_qualification_failure_status_incomplete_vs_eval_failed(tmp_path: Path) -> None:
    """Verify failure before evaluation returns status='incomplete' while eval failure returns 'eval_failed'."""
    # Failure during task setup (invalid split) -> status="incomplete"
    out_incomplete = tmp_path / "incomplete_out"
    res_incomplete = meta_engine.qualify_meta_harness(
        evaluator=lambda c, ex: (1.0, {}),
        examples=[{"task_id": "t1", "split": "test"}],  # Invalid split
        seed_candidate="Seed",
        output_dir=out_incomplete,
    )
    assert res_incomplete["qualified"] is False
    assert res_incomplete["status"] == "incomplete"
    assert "Task construction failed" in str(res_incomplete.get("error"))

    # Failure during evaluation -> status="eval_failed"
    def failing_eval(candidate: str, example: dict[str, Any]) -> tuple[float, dict[str, Any]]:
        raise RuntimeError("Evaluator infra error")

    out_eval_failed = tmp_path / "eval_failed_out"
    res_eval_failed = meta_engine.qualify_meta_harness(
        evaluator=failing_eval,
        examples=[{"task_id": "t1", "split": "development"}],
        seed_candidate="Seed",
        output_dir=out_eval_failed,
    )
    assert res_eval_failed["qualified"] is False
    assert res_eval_failed["status"] == "eval_failed"
    assert "Evaluator infra error" in str(res_eval_failed["baseline_evaluation"]["error"])


def test_macos_fail_open_source_cannot_launch_proposer(monkeypatch):
    monkeypatch.setattr(meta_engine, "_IS_MACOS", True)

    def unexpected_launch(*args, **kwargs):
        pytest.fail("An unqualified OS route reached the upstream proposer")

    monkeypatch.setattr(meta_engine.MetaHarnessEngine, "run", unexpected_launch)
    engine = meta_engine.make_meta_harness_engine(gepa_config.OptimizeAnythingConfig(sandbox=True))
    with pytest.raises(RuntimeError):
        engine.run(None, None)
