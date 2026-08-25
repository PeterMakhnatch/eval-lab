from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from evallab.task_workbench import (
    NETWORK_OVERLAY_CONTENT,
    NETWORK_OVERLAY_RELATIVE,
    ControlObservation,
    _harbor_task_digest,
    _tree_digest,
    _verifier_output_digest,
)


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def _digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


class FreeFixtureBackend:
    """Offline fixture backend: materialize retained M049-shaped control evidence."""

    def run(
        self,
        *,
        repo_root: Path,
        task_dir: Path,
        candidate: dict[str, Any],
        plan: Any,
        run_root: Path,
    ) -> ControlObservation:
        stage = run_root / "staging" / plan.control_id
        shutil.copytree(task_dir, stage)
        if plan.mutation_path is not None:
            shutil.copyfile(stage / plan.mutation_path, stage / "solution/solve.sh")
        overlay = stage / NETWORK_OVERLAY_RELATIVE
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_bytes(NETWORK_OVERLAY_CONTENT)

        job_name = plan.command[plan.command.index("--job-name") + 1]
        job = run_root / "jobs" / job_name
        trial = job / f"{plan.control_id}__fixture"
        trial.mkdir(parents=True)
        (job / "result.json").write_bytes(
            _canonical(
                {
                    "id": f"job-{plan.control_id}",
                    "n_total_trials": 1,
                    "stats": {},
                    "finished_at": "2026-08-15T00:00:00Z",
                }
            )
        )
        stage_path = str(stage.resolve())
        overlay_path = str(overlay.resolve())
        reward = plan.expected_reward
        (trial / "result.json").write_bytes(
            _canonical(
                {
                    "id": f"trial-{plan.control_id}",
                    "task_name": candidate["task_name"],
                    "trial_name": f"{plan.control_id}__fixture",
                    "task_id": {"path": stage_path},
                    "task_checksum": "c" * 64,
                    "config": {
                        "task": {"path": stage_path},
                        "environment": {
                            "type": "docker",
                            "extra_docker_compose": [overlay_path],
                        },
                    },
                    "agent_info": {"name": plan.agent},
                    "verifier_result": {"rewards": {"reward": reward}},
                    "verifier_environment_mode": "separate",
                    "exception_info": None,
                }
            )
        )
        (trial / "lock.json").write_bytes(
            _canonical(
                {
                    "task": {
                        "name": plan.control_id,
                        "version": candidate["task_version"],
                        "type": "local",
                        "digest": _harbor_task_digest(stage),
                        "path": stage_path,
                    },
                    "agent": {"name": plan.agent},
                    "environment": {
                        "type": "docker",
                        "extra_docker_compose": [overlay_path],
                    },
                    "extra_docker_compose": [
                        {"path": overlay_path, "digest": _digest(NETWORK_OVERLAY_CONTENT)}
                    ],
                    "verifier": {"disable": False, "environment_mode": "separate"},
                }
            )
        )
        verifier = trial / "verifier"
        verifier.mkdir()
        (verifier / "reward.txt").write_text(f"{reward}\n")
        (verifier / "test-stdout.txt").write_text("")
        digests = candidate["digests"]
        return ControlObservation(
            control_id=plan.control_id,
            status="completed",
            reward=reward,
            reward_vector={"reward": reward},
            verifier_output_digest=_verifier_output_digest(trial),
            evidence_digest=_tree_digest(job),
            image_digest=digests["image_definition"],
            verifier_digest=digests["verifier"],
            source_package_digest=digests["package"],
            staged_package_digest=_tree_digest(stage),
            command=plan.command,
            command_digest=plan.command_digest,
            job_path=job.relative_to(repo_root).as_posix(),
            exception_type=None,
            diagnostic=None,
        )
