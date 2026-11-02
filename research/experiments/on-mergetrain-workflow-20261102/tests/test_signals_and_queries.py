"""Tests for signals (skip, abort) and queries (board, status) in merge train."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from mergetrain.activities import (
    check_ci_activity,
    merge_pr_activity,
    rebase_pr_activity,
    view_pr_activity,
)
from mergetrain.models import MergeTrainConfig, MergeTrainInput
from mergetrain.workflows import MergeTrainWorkflow
from tests.test_harness import FakeRepoHarness


@pytest.mark.asyncio
async def test_skip_signal_skips_pr_and_continues(tmp_path: Path) -> None:
    """Sending skip signal causes the specified PR to be skipped and train continues."""
    harness = FakeRepoHarness(tmp_path)
    local_repo = harness.create_local_clone()
    env = harness.get_env()

    harness.create_branch("feat/pr-801", {"src/f801.py": "x = 801\n"})
    harness.register_pr(801, "feat/pr-801", "PR 801")

    harness.create_branch("feat/pr-802", {"src/f802.py": "x = 802\n"})
    harness.register_pr(802, "feat/pr-802", "PR 802")

    config = MergeTrainConfig(
        repo_path=str(local_repo),
        worktrees_dir=str(harness.worktree_base),
        gates_dir=str(harness.gates_dir),
        check_cmds=[],
        ci_poll_interval_seconds=0.1,
        require_approval=True,
        env=env,
    )
    train_input = MergeTrainInput(prs=[801, 802], config=config)
    task_queue = "tq-signals-skip"

    async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
        client = wf_env.client
        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[MergeTrainWorkflow],
            activities=[view_pr_activity, rebase_pr_activity, check_ci_activity, merge_pr_activity],
        )

        async with worker:
            handle = await client.start_workflow(
                MergeTrainWorkflow.run,
                train_input,
                id="merge-train-skip-test",
                task_queue=task_queue,
            )

            # Wait until PR 801 is waiting approval
            for _ in range(50):
                st = await handle.query(MergeTrainWorkflow.status)
                if st["states"].get("801", {}).get("state") == "WAITING_APPROVAL":
                    break
                await asyncio.sleep(0.05)

            # Signal skip on PR 801
            await handle.signal(MergeTrainWorkflow.skip, 801)

            # Wait until PR 802 is waiting approval
            for _ in range(50):
                st = await handle.query(MergeTrainWorkflow.status)
                if st["states"].get("802", {}).get("state") == "WAITING_APPROVAL":
                    break
                await asyncio.sleep(0.05)

            # Approve PR 802
            await handle.signal(MergeTrainWorkflow.approve, 802)

            result = await handle.result()

    states = result["states"]
    assert states["801"]["state"] == "SKIPPED"
    assert states["802"]["state"] == "MERGED"

    ledger = harness.get_merge_ledger()
    assert len(ledger) == 1
    assert ledger[0]["pr"] == 802


@pytest.mark.asyncio
async def test_abort_signal_halts_train(tmp_path: Path) -> None:
    """Sending abort signal stops remaining PRs immediately."""
    harness = FakeRepoHarness(tmp_path)
    local_repo = harness.create_local_clone()
    env = harness.get_env()

    harness.create_branch("feat/pr-901", {"src/f901.py": "x = 901\n"})
    harness.register_pr(901, "feat/pr-901", "PR 901")

    harness.create_branch("feat/pr-902", {"src/f902.py": "x = 902\n"})
    harness.register_pr(902, "feat/pr-902", "PR 902")

    config = MergeTrainConfig(
        repo_path=str(local_repo),
        worktrees_dir=str(harness.worktree_base),
        gates_dir=str(harness.gates_dir),
        check_cmds=[],
        ci_poll_interval_seconds=0.1,
        require_approval=True,
        env=env,
    )
    train_input = MergeTrainInput(prs=[901, 902], config=config)
    task_queue = "tq-signals-abort"

    async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
        client = wf_env.client
        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[MergeTrainWorkflow],
            activities=[view_pr_activity, rebase_pr_activity, check_ci_activity, merge_pr_activity],
        )

        async with worker:
            handle = await client.start_workflow(
                MergeTrainWorkflow.run,
                train_input,
                id="merge-train-abort-test",
                task_queue=task_queue,
            )

            # Wait until PR 901 is waiting approval
            for _ in range(50):
                st = await handle.query(MergeTrainWorkflow.status)
                if st["states"].get("901", {}).get("state") == "WAITING_APPROVAL":
                    break
                await asyncio.sleep(0.05)

            # Signal abort
            await handle.signal(MergeTrainWorkflow.abort)

            result = await handle.result()

    states = result["states"]
    assert states["901"]["state"] == "ABORTED"
    assert states["902"]["state"] == "ABORTED"

    ledger = harness.get_merge_ledger()
    assert len(ledger) == 0


@pytest.mark.asyncio
async def test_dry_run_mode(tmp_path: Path) -> None:
    """Dry-run mode checks mergeability and CI without merging."""
    harness = FakeRepoHarness(tmp_path)
    local_repo = harness.create_local_clone()
    env = harness.get_env()

    harness.create_branch("feat/pr-1001", {"src/f1001.py": "x = 1001\n"})
    harness.register_pr(1001, "feat/pr-1001", "Dry run PR")

    config = MergeTrainConfig(
        repo_path=str(local_repo),
        worktrees_dir=str(harness.worktree_base),
        gates_dir=str(harness.gates_dir),
        check_cmds=[],
        ci_poll_interval_seconds=0.1,
        dry_run=True,
        env=env,
    )
    train_input = MergeTrainInput(prs=[1001], config=config)
    task_queue = "tq-dry-run"

    async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
        client = wf_env.client
        worker = Worker(
            client,
            task_queue=task_queue,
            workflows=[MergeTrainWorkflow],
            activities=[view_pr_activity, rebase_pr_activity, check_ci_activity, merge_pr_activity],
        )

        async with worker:
            handle = await client.start_workflow(
                MergeTrainWorkflow.run,
                train_input,
                id="merge-train-dry-run-test",
                task_queue=task_queue,
            )
            result = await handle.result()

    states = result["states"]
    assert states["1001"]["state"] == "DRY_RUN"
    assert harness.get_merge_ledger() == []
