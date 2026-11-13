from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from evallab.digest import DigestRenderer
from evallab.queue import DirectoryQueue, Executor
from evallab.schemas import (
    AutoRunRule,
    CanaryDriftObservation,
    StandingApprovalsPolicy,
)


def policy() -> StandingApprovalsPolicy:
    return StandingApprovalsPolicy(
        daily_cost_ceiling_usd=20,
        per_job_cost_ceiling_usd=3,
        quiet_failure_rule=3,
        auto_run=[
            AutoRunRule(
                name="canary",
                tasks=["canary/*"],
                agents=["codex", "claude-code"],
                max_attempts=3,
            )
        ],
    )


def make_executor(root: Path, requests: list, ingested: list[Path]) -> Executor:
    def runner(request):
        requests.append(request)
        destination = request.jobs_dir / request.name
        destination.mkdir(parents=True)
        return destination

    return Executor(
        repo_root=root,
        queue=DirectoryQueue(root / "queue"),
        policy=policy(),
        runner=runner,
        ingester=ingested.append,
        spent_today=lambda: 0,
        consecutive_harness_failures=lambda: 0,
        credential_probe=lambda: frozenset({"codex_auth"}),
    )


def test_digest_labels_version_perturbation_as_harness_drift(tmp_path: Path) -> None:
    observation = CanaryDriftObservation(
        task_name="canary/transaction-reconciliation",
        task_version="1.1.0",
        agent_name="codex",
        reward=1.0,
        attempt_count=3,
        exception_count=0,
        baseline_n=6,
        baseline_mean=1.0,
        baseline_stddev=0.0,
        previous_task_version="1.0.0",
        task_version_changed=True,
        is_harness_drift_suspect=True,
        drift_reason="task_version_changed",
    )
    renderer = DigestRenderer(
        repo_root=tmp_path,
        queue=DirectoryQueue(tmp_path / "queue"),
        policy=policy(),
        trial_loader=lambda day: [],
        drift_loader=lambda day: [observation] if day == date(2026, 8, 13) else [],
    )

    content = renderer.write(report_date=date(2026, 8, 13)).read_text()

    assert "1.000 ± 0.000" in content
    assert "harness-drift suspect (task_version_changed)" in content
    assert "harness_failure=1" in content
    assert "not capability news" in content


def test_executor_rejects_mutable_dataset_download_before_harbor(
    tmp_path: Path,
) -> None:
    service = make_executor(tmp_path, [], [])

    with pytest.raises(ValueError, match="mutable"):
        service.download_dataset(
            "terminal-bench/terminal-bench@latest",
            tmp_path / "download",
        )
