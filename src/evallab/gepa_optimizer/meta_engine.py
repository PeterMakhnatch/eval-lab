"""MetaHarnessEngine qualification and configuration for Eval Lab.

This module qualifies and configures GEPA's included MetaHarnessEngine
against Eval Lab execution without implementing a redundant second optimizer.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from gepa.oa.budget import BudgetTracker  # ty: ignore[unresolved-import]
from gepa.oa.config import OptimizeAnythingConfig  # ty: ignore[unresolved-import]
from gepa.oa.engines.meta_harness import (  # ty: ignore[unresolved-import]
    MetaHarnessEngine,
    _copy_session_transcript,
    _is_under,
    _load_candidate,
    _materialize_sandbox,
    _score_candidate,
)
from gepa.oa.eval_server import EvalServer  # ty: ignore[unresolved-import]
from gepa.oa.sandbox import _FILE_TOOLS, _IS_MACOS, DENY_WEB_TOOLS  # ty: ignore[unresolved-import]
from gepa.oa.task import Task  # ty: ignore[unresolved-import]

from .release import verify_release

PINNED_GEPA_COMMIT = "0632cdb5dcc052e690eab439e1b4a7e3e9cfe407"
PINNED_UPSTREAM_META_HARNESS_URL = (
    f"https://github.com/gepa-ai/gepa/blob/{PINNED_GEPA_COMMIT}/src/gepa/oa/engines/meta_harness.py"
)


def compute_candidate_digest(candidate: str) -> str:
    """Return canonical sha256 hex digest for candidate instruction text."""
    return f"sha256:{hashlib.sha256(candidate.encode('utf-8')).hexdigest()}"


def check_prerequisite_facts(*, sandbox: bool = True) -> dict[str, Any]:
    """Inspect environment prerequisite facts without asserting runtime isolation as proven."""
    claude_path = shutil.which("claude")
    bwrap_path = shutil.which("bwrap")
    return {
        "platform": sys.platform,
        "is_macos": _IS_MACOS,
        "sandbox_configured": sandbox,
        "sandbox_backend": "seatbelt" if _IS_MACOS else "bwrap",
        "claude_cli_found": claude_path is not None,
        "claude_cli_path": claude_path,
        "bwrap_found": bwrap_path is not None,
        "bwrap_path": bwrap_path,
        "runtime_isolation_tested": False,
        "isolation_status": "untested",
        "fail_closed_os_launch_supported": not _IS_MACOS,
        "live_launch_blocker": (
            "Pinned GEPA macOS settings set sandbox.failIfUnavailable=False; "
            "live launch is refused until a fail-closed OS route is qualified"
            if _IS_MACOS
            else None
        ),
        "deny_web_tools": DENY_WEB_TOOLS,
        "file_tools_whitelisted": list(_FILE_TOOLS),
        "permission_mode": "default (seatbelt whitelist)"
        if _IS_MACOS
        else "bypassPermissions (inside bwrap)",
    }


def build_meta_harness_task(
    *,
    name: str = "meta_harness_task",
    seed_candidate: str,
    examples: Sequence[dict[str, Any]],
    validation_task_ids: Sequence[str] | set[str] | None = None,
    objective: str = "Improve task performance using supplementary instruction text",
    background: str = "Eval Lab execution boundary with hash-addressed supplementary instructions.",
) -> Task:
    """Build a GEPA Task strictly enforcing split='development', sealing test_set.

    Args:
        name: Task identifier.
        seed_candidate: Starting candidate text.
        examples: Permitted development examples (must all have split == 'development').
        validation_task_ids: Optional explicit task_ids to route to val_set. Remainder go to train_set.
            When None or empty, all examples go to train_set (train-only).
        objective: Natural language objective.
        background: Natural language background.

    Returns:
        A Task instance with train_set, val_set (or None if no validation tasks), and test_set=None.

    Raises:
        ValueError: If any example does not have split == 'development', or if candidate is invalid.
    """
    if not isinstance(seed_candidate, str):
        raise TypeError(f"Seed candidate must be a string, got {type(seed_candidate).__name__}")

    val_id_set = set(validation_task_ids or ())

    train_set: list[dict[str, Any]] = []
    val_set: list[dict[str, Any]] = []

    for i, ex in enumerate(examples):
        task_id = str(ex.get("task_id", ex.get("id", f"ex_{i}")))
        split = ex.get("split")
        if split != "development":
            raise ValueError(
                f"Invalid example split {split!r} for task {task_id!r}: only split='development' "
                f"examples are permitted in optimizer search space; sealed/test splits are strictly prohibited"
            )

        item = dict(ex)
        # Upstream uses example IDs as filenames; registry task IDs may contain '/'.
        item["id"] = hashlib.sha256(task_id.encode("utf-8")).hexdigest()

        if task_id in val_id_set:
            val_set.append(item)
        else:
            train_set.append(item)

    # STRICT INVARIANT: test_set is None, never passed to GEPA
    return Task(
        name=name,
        seed_candidate=seed_candidate,
        objective=objective,
        background=background,
        train_set=train_set if train_set else None,
        val_set=val_set if val_set else None,
        test_set=None,  # Sealed final tasks must NEVER be passed to GEPA
    )


class SafeMetaHarnessEngine(MetaHarnessEngine):
    """Hardened MetaHarnessEngine wrapper enforcing symlink preservation and tempdir isolation.

    Upstream MetaHarnessEngine.process_result (lines 951-958) invokes shutil.copytree(work_dir, dest / 'work')
    with default symlinks=False, which follows proposer-created symlinks and copies outside host files into
    the evaluation artifact directory.

    SafeMetaHarnessEngine overrides artifact retention:
    1. Strictly verifies that only the engine's actually owned _pending_tempdir is retained,
       rejecting any unexpected or mismatched work_dir path.
    2. Copies with symlinks=True so external files are never dereferenced or leaked.
    3. Exposes an explicit retain(dest_dir, *, result=None, cleanup_tempdir=True) method to safely persist
       workspace artifacts even when an evaluation is interrupted or halted.
    4. Repeated retain after cleanup is a harmless no-op.
    """

    def run(self, task: Task, server: EvalServer):
        # The pinned macOS route may continue without its OS sandbox. Tool
        # allowlists are not a substitute for the required OS confinement.
        if _IS_MACOS:
            raise RuntimeError(
                "Pinned GEPA macOS sandbox is not fail-closed; live MetaHarnessEngine launch is refused"
            )
        if not shutil.which("claude") or not shutil.which("bwrap"):
            raise RuntimeError(
                "Live MetaHarnessEngine requires both Claude Code and a working bwrap route"
            )
        return super().run(task, server)

    def retain(
        self,
        dest_dir: Path | str,
        *,
        result: Any | None = None,
        cleanup_tempdir: bool = True,
    ) -> Path:
        """Explicitly retain owned workspace artifacts without dereferencing symlinks.

        Args:
            dest_dir: Target directory where artifacts should be persisted.
            result: Optional Result object from an engine run.
            cleanup_tempdir: Whether to clean up the owned tempdir after copying (default True).

        Returns:
            The Path to the retained destination work directory (dest / "work").

        Raises:
            ValueError: If result metadata reports an unexpected work_dir that does not match
                the engine's owned workspace, or if an unknown work_dir is reported without
                an active owned workspace.
        """
        dest = Path(dest_dir).resolve()
        dest.mkdir(parents=True, exist_ok=True)
        target_work = dest / "work"

        # If no active owned tempdir exists:
        if self._pending_tempdir is None:
            if result is not None and getattr(result, "metadata", None):
                reported = result.metadata.get("work_dir")
                if reported:
                    raise ValueError(
                        f"Cannot retain reported work_dir {reported!r}: engine has no active owned temporary directory"
                    )
            # Repeated retain after cleanup is a harmless no-op
            return target_work

        owned_dir = Path(self._pending_tempdir.name).resolve()

        # If result is provided, strictly verify reported work_dir matches owned tempdir
        if result is not None and getattr(result, "metadata", None):
            reported = result.metadata.get("work_dir")
            if reported:
                reported_dir = Path(reported).resolve()
                if reported_dir != owned_dir:
                    raise ValueError(
                        f"Unexpected work_dir {reported_dir} does not match owned workspace {owned_dir}. "
                        "Rejecting artifact retention."
                    )

        if not owned_dir.exists():
            if cleanup_tempdir:
                self._pending_tempdir.cleanup()
                self._pending_tempdir = None
            return target_work

        # Copy session transcripts if available in result metadata
        if result is not None and getattr(result, "metadata", None):
            session_ids = result.metadata.get("session_ids", []) or []
            transcripts_dir = dest / "sessions"
            transcripts_dir.mkdir(parents=True, exist_ok=True)
            for sid in session_ids:
                _copy_session_transcript(owned_dir, sid, transcripts_dir)

        # Copy owned directory tree with symlinks=True (never follow links)
        if not _is_under(owned_dir, dest):
            shutil.copytree(owned_dir, target_work, symlinks=True, dirs_exist_ok=True)

        if cleanup_tempdir:
            self._pending_tempdir.cleanup()
            self._pending_tempdir = None

        return target_work

    def process_result(self, result: Any, output_dir: Path | None) -> None:
        """Override upstream process_result to route through secure retain()."""
        dest = (
            output_dir if output_dir is not None else (Path(self.run_dir) if self.run_dir else None)
        )
        if dest is None:
            if self._pending_tempdir is not None:
                self._pending_tempdir.cleanup()
                self._pending_tempdir = None
            return
        self.retain(dest, result=result, cleanup_tempdir=True)


def make_meta_harness_engine(config: OptimizeAnythingConfig) -> SafeMetaHarnessEngine:
    """Reuse the pinned included engine with isolated artifact retention."""
    verify_release()
    if config.sandbox is not True:
        raise ValueError("MetaHarnessEngine requires sandbox=True")
    return SafeMetaHarnessEngine(config)


def qualify_meta_harness(
    *,
    evaluator: Callable[..., tuple[float, dict[str, Any]]],
    examples: Sequence[dict[str, Any]],
    seed_candidate: str,
    output_dir: Path,
    validation_task_ids: Sequence[str] | set[str] | None = None,
    task_name: str = "meta_harness_qualification",
    objective: str = "Qualify MetaHarnessEngine with Eval Lab execution boundary",
    background: str = "Evaluation of candidate instruction text on declared development tasks.",
    max_evals: int | None = 100,
    max_token_cost: float | None = None,
) -> dict[str, Any]:
    """Qualify MetaHarnessEngine against Lab evaluator interface without model calls.

    Returns populated facts with dynamically computed qualification status.
    """
    output_path = Path(output_dir)
    if output_path.is_symlink():
        raise ValueError(f"Output directory cannot be a symlink: {output_dir}")
    resolved_output = output_path.resolve()
    resolved_output.mkdir(parents=True, exist_ok=True)

    # 1. Verified Release Pin
    try:
        release_info = verify_release()
        pin_verified = release_info.get("commit") == PINNED_GEPA_COMMIT and bool(
            release_info.get("python_source_tree_sha256")
        )
    except Exception as e:
        release_info = {"error": str(e)}
        pin_verified = False

    prereq_facts = check_prerequisite_facts(sandbox=True)

    # 2. Fresh workspace verification (exist_ok=False required)
    work_dir = resolved_output / "workspace"
    if work_dir.exists() or work_dir.is_symlink():
        raise ValueError(
            f"Workspace directory already exists or is a symlink: {work_dir}. Fresh workspace required."
        )
    work_dir.mkdir(parents=False, exist_ok=False)
    output_containment_valid = work_dir.resolve().is_relative_to(resolved_output)

    # 3. Build Task & Validate Split Invariants (NO fallback task on invalid split!)
    try:
        task = build_meta_harness_task(
            name=task_name,
            seed_candidate=seed_candidate,
            examples=examples,
            validation_task_ids=validation_task_ids,
            objective=objective,
            background=background,
        )
    except Exception as e:
        # Invalid split immediately halts qualification as incomplete
        fail_result: dict[str, Any] = {
            "engine_name": "meta_harness",
            "qualified": False,
            "status": "incomplete",
            "release": release_info,
            "prerequisites": prereq_facts,
            "error": f"Task construction failed: {e}",
            "dataset_splits": {
                "splits_valid": False,
                "split_error": str(e),
                "test_set_passed": False,
            },
        }
        qual_file = resolved_output / "meta_harness_qualification.json"
        qual_file.write_text(json.dumps(fail_result, indent=2, default=str))
        return fail_result

    train_set = task.train_set or []
    val_set = task.val_set or []
    has_val = len(val_set) > 0
    splits_valid = (task.test_set is None) and (len(train_set) > 0)

    if not splits_valid:
        fail_result = {
            "engine_name": "meta_harness",
            "qualified": False,
            "status": "incomplete",
            "release": release_info,
            "prerequisites": prereq_facts,
            "error": "Task must contain at least 1 development training example.",
            "dataset_splits": {
                "splits_valid": False,
                "train_count": len(train_set),
                "val_count": len(val_set),
                "test_set_passed": False,
            },
        }
        qual_file = resolved_output / "meta_harness_qualification.json"
        qual_file.write_text(json.dumps(fail_result, indent=2, default=str))
        return fail_result

    # 4. EvalServer and BudgetTracker
    budget_cap = (
        max_evals if max_evals is not None else max(10, (len(train_set) + len(val_set)) * 2)
    )
    budget = BudgetTracker(max_evals=budget_cap)

    server_out = resolved_output / "eval_server"
    evaluation_errors: list[str] = []

    def observed_evaluator(candidate, example):
        try:
            return evaluator(candidate, example)
        except Exception as exc:
            # evaluate_examples converts exceptions into zero scores. Retain the
            # actual failure and refuse qualification instead of crediting it.
            evaluation_errors.append(f"{type(exc).__name__}: {exc}")
            raise

    server_out.mkdir(parents=True, exist_ok=True)
    server = EvalServer(
        task=task,
        evaluate=observed_evaluator,
        budget=budget,
        output_dir=server_out,
    )

    # 5. Materialize Sandbox Workspace
    _materialize_sandbox(work_dir, task, server, budget)

    task_md_path = work_dir / "task.md"
    skill_path = (
        work_dir / ".claude" / "skills" / "gepa-optimize-anything-meta-harness" / "SKILL.md"
    )
    baseline_path = work_dir / "agents" / "baseline.txt"
    frontier_path = work_dir / "state" / "frontier.json"
    summary_path = work_dir / "state" / "evolution_summary.jsonl"
    train_dir = work_dir / "train"
    test_dir = work_dir / "test"

    train_files = list(train_dir.glob("*.json")) if train_dir.exists() else []

    train_items_materialized = (
        all((work_dir / "train" / f"{item['id']}.json").exists() for item in train_set)
        if train_set
        else False
    )

    val_items_materialized = (
        all((work_dir / "train" / f"{item['id']}.json").exists() for item in val_set)
        if has_val
        else True
    )

    expected_files_count = len(train_set) + (len(val_set) if has_val else 0)
    all_materialized = (
        train_items_materialized
        and val_items_materialized
        and len(train_files) == expected_files_count
    )

    workspace_valid = (
        task_md_path.exists()
        and skill_path.exists()
        and baseline_path.exists()
        and frontier_path.exists()
        and summary_path.exists()
        and not test_dir.exists()
    )

    # 6. Candidate Containment Checks
    loaded_baseline = _load_candidate(work_dir, "agents/baseline.txt")
    traversal_escape = _load_candidate(work_dir, "../../../etc/passwd")
    abs_escape = _load_candidate(work_dir, "/etc/passwd")

    containment_valid = (
        (loaded_baseline == seed_candidate) and (traversal_escape is None) and (abs_escape is None)
    )

    # 7. Score Candidate via Evaluator
    baseline_score: float | None = None
    eval_info: dict[str, Any] = {}
    eval_error: str | None = None
    baseline_scored = False

    try:
        score_val, info_val = _score_candidate(server, task, seed_candidate)
        if score_val is not None and math.isfinite(score_val) and not evaluation_errors:
            baseline_score = float(score_val)
            eval_info = info_val if isinstance(info_val, dict) else {}
            baseline_scored = True
        else:
            eval_error = "; ".join(evaluation_errors) or "No finite complete evaluation score"
    except Exception as e:
        eval_error = f"{type(e).__name__}: {e}"

    # 9. Computed Qualified Status
    if not baseline_scored:
        qualified = False
        status = "eval_failed"
    elif not (
        pin_verified
        and splits_valid
        and output_containment_valid
        and workspace_valid
        and all_materialized
        and containment_valid
    ):
        qualified = False
        status = "incomplete"
    else:
        qualified = True
        status = "qualified"

    result: dict[str, Any] = {
        "engine_name": "meta_harness",
        "qualified": qualified,
        "status": status,
        "qualification_scope": "source_pin_materialization_containment_and_native_Lab_scoring",
        "live_engine_ready": False,
        "release": release_info,
        "prerequisites": prereq_facts,
        "candidate": {
            "digest": compute_candidate_digest(seed_candidate),
            "type": "utf-8 supplementary instruction text",
            "host_execution": False,
        },
        "output_containment": {
            "output_dir": str(resolved_output),
            "work_dir": str(work_dir),
            "containment_valid": output_containment_valid,
        },
        "dataset_splits": {
            "train_count": len(train_set),
            "val_count": len(val_set),
            "splits_valid": splits_valid,
            "has_validation_set": has_val,
            "test_set_passed": False,
            "train_val_search_visible": all_materialized if has_val else "train_only",
            "sealed_tasks_never_materialized": not test_dir.exists(),
        },
        "materialization": {
            "workspace_valid": workspace_valid,
            "train_files_count": len(train_files),
            "train_materialized": train_items_materialized,
            "val_materialized": val_items_materialized,
            "test_dir_exists": test_dir.exists(),
        },
        "candidate_containment": {
            "baseline_readable": loaded_baseline == seed_candidate,
            "path_traversal_blocked": traversal_escape is None,
            "absolute_escape_blocked": abs_escape is None,
            "containment_valid": containment_valid,
        },
        "baseline_evaluation": {
            "score": baseline_score,
            "scored": baseline_scored,
            "error": eval_error,
            "info": eval_info,
            "eval_log_records": len(server.eval_log),
        },
        "live_proposer_gate": {
            "authorized": False,
            "gated": True,
            "status": "held_unexecuted",
            "reason": "This qualification never invokes the model-backed proposer",
        },
        "source": PINNED_UPSTREAM_META_HARNESS_URL,
        "engine_api": {
            "class": "SafeMetaHarnessEngine",
            "factory": "make_meta_harness_engine(config)",
            "methods": [
                "run(task, server)",
                "retain(dest_dir, *, result=None, cleanup_tempdir=True)",
                "process_result(result, output_dir)",
            ],
            "symlinks_preserved": True,
            "work_dir_identity_verified": True,
            "preserves_on_interruption": True,
        },
    }

    qual_file = resolved_output / "meta_harness_qualification.json"
    qual_file.write_text(json.dumps(result, indent=2, default=str))

    return result
