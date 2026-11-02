"""Comprehensive test suite for the 7 required scenarios of the durable merge train."""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
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

STUBS_PATH = Path(__file__).parent / "stubs.py"
MOCK_CHECKS = [("mock-check", [sys.executable, str(STUBS_PATH), "pass"])]
MOCK_GENERATOR = [sys.executable, str(STUBS_PATH), "generate"]


@pytest.mark.asyncio
async def test_scenario_1_clean_merge_three_prs_in_order(tmp_path: Path) -> None:
    """Scenario 1: clean merge of three PRs in order."""
    harness = FakeRepoHarness(tmp_path)
    local_repo = harness.create_local_clone()
    env = harness.get_env()

    # PR 101, 102, 103 with non-conflicting files
    harness.create_branch("feat/pr-101", {"src/feat1.py": "def feat1(): return 1\n"})
    harness.register_pr(101, "feat/pr-101", "Add feature 1")

    harness.create_branch("feat/pr-102", {"src/feat2.py": "def feat2(): return 2\n"})
    harness.register_pr(102, "feat/pr-102", "Add feature 2")

    harness.create_branch("feat/pr-103", {"src/feat3.py": "def feat3(): return 3\n"})
    harness.register_pr(103, "feat/pr-103", "Add feature 3")

    config = MergeTrainConfig(
        repo_path=str(local_repo),
        worktrees_dir=str(harness.worktree_base),
        gates_dir=str(harness.gates_dir),
        generator_cmd=MOCK_GENERATOR,
        check_cmds=MOCK_CHECKS,
        ci_poll_interval_seconds=0.1,
        env=env,
    )
    train_input = MergeTrainInput(prs=[101, 102, 103], config=config)
    task_queue = "tq-scenario-1"

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
                id=f"merge-train-sc1-{int(time.time())}",
                task_queue=task_queue,
            )
            result = await handle.result()

    states = result["states"]
    assert states["101"]["state"] == "MERGED"
    assert states["102"]["state"] == "MERGED"
    assert states["103"]["state"] == "MERGED"

    # Verify merge ledger in fake gh
    ledger = harness.get_merge_ledger()
    assert len(ledger) == 3
    assert [r["pr"] for r in ledger] == [101, 102, 103]

    # Verify git log on bare origin main
    main_log = harness.get_main_log()
    assert any("Merge pull request #101" in line for line in main_log)
    assert any("Merge pull request #102" in line for line in main_log)
    assert any("Merge pull request #103" in line for line in main_log)

    # Verify gate files touched
    assert (harness.gates_dir / "PR-merged").exists()


@pytest.mark.asyncio
async def test_scenario_2_conflict_in_repomap_autoresolved(tmp_path: Path) -> None:
    """Scenario 2: conflict only in docs/repo-map.md, auto-resolved with generator stub."""
    harness = FakeRepoHarness(tmp_path)
    local_repo = harness.create_local_clone()
    env = harness.get_env()

    # PR 201 modifies docs/repo-map.md and adds a tool
    harness.create_branch(
        "feat/pr-201",
        {
            "docs/repo-map.md": "# Repo Map\n- initial_symbol\n- branch_symbol\n",
            "src/tool.py": "TOOL = 1\n",
        },
    )
    harness.register_pr(201, "feat/pr-201", "Add tool and edit repo map")

    # Main advances in origin with conflicting changes in docs/repo-map.md
    harness.advance_main(
        {"docs/repo-map.md": "# Repo Map\n- initial_symbol\n- conflicting_main_symbol\n"},
        msg="Update repo map on main",
    )

    config = MergeTrainConfig(
        repo_path=str(local_repo),
        worktrees_dir=str(harness.worktree_base),
        gates_dir=str(harness.gates_dir),
        generator_cmd=[sys.executable, str(STUBS_PATH), "generate"],
        check_cmds=MOCK_CHECKS,
        ci_poll_interval_seconds=0.1,
        env=env,
    )
    train_input = MergeTrainInput(prs=[201], config=config)
    task_queue = "tq-scenario-2"

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
                id=f"merge-train-sc2-{int(time.time())}",
                task_queue=task_queue,
            )
            result = await handle.result()

    states = result["states"]
    assert states["201"]["state"] == "MERGED"

    # Verify origin main has merged commit
    main_log = harness.get_main_log()
    assert any("Merge pull request #201" in line for line in main_log)

    # Verify docs/repo-map.md was regenerated
    r = subprocess.run(
        ["git", "-C", str(harness.bare_repo), "show", "refs/heads/main:docs/repo-map.md"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "regenerated_symbol_after_rebase" in r.stdout


@pytest.mark.asyncio
async def test_scenario_3_conflict_in_source_file_needs_human(tmp_path: Path) -> None:
    """Scenario 3: conflict in a source file marks PR needs-human, train continues."""
    harness = FakeRepoHarness(tmp_path)
    local_repo = harness.create_local_clone()
    env = harness.get_env()

    # Seed src/calc.py on main
    harness.advance_main({"src/calc.py": "def calc(): return 10\n"}, msg="Add calc")

    # PR 301 modifies calc.py
    harness.create_branch("feat/pr-301", {"src/calc.py": "def calc(): return 200\n"})
    harness.register_pr(301, "feat/pr-301", "Conflicting calc change")

    # PR 302 modifies unrelated src/other.py
    harness.create_branch("feat/pr-302", {"src/other.py": "def other(): return 999\n"})
    harness.register_pr(302, "feat/pr-302", "Clean other change")

    # Now main advances with a conflicting change to calc.py
    harness.advance_main({"src/calc.py": "def calc(): return 300\n"}, msg="Change calc on main")

    config = MergeTrainConfig(
        repo_path=str(local_repo),
        worktrees_dir=str(harness.worktree_base),
        gates_dir=str(harness.gates_dir),
        generator_cmd=MOCK_GENERATOR,
        check_cmds=MOCK_CHECKS,
        ci_poll_interval_seconds=0.1,
        env=env,
    )
    train_input = MergeTrainInput(prs=[301, 302], config=config)
    task_queue = "tq-scenario-3"

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
                id=f"merge-train-sc3-{int(time.time())}",
                task_queue=task_queue,
            )
            result = await handle.result()

    states = result["states"]
    # PR 301 marked NEEDS_HUMAN due to source conflict
    assert states["301"]["state"] == "NEEDS_HUMAN"
    assert "rebase conflicts outside generated docs" in states["301"]["reason"]
    assert "src/calc.py" in states["301"]["reason"]

    # Train continued and merged PR 302!
    assert states["302"]["state"] == "MERGED"

    ledger = harness.get_merge_ledger()
    assert len(ledger) == 1
    assert ledger[0]["pr"] == 302


@pytest.mark.asyncio
async def test_scenario_4_ci_red_skipped_and_reported(tmp_path: Path) -> None:
    """Scenario 4: CI red is skipped and reported."""
    harness = FakeRepoHarness(tmp_path)
    local_repo = harness.create_local_clone()
    env = harness.get_env()

    # PR 401 has failing checks
    harness.create_branch("feat/pr-401", {"src/failing.py": "x = 1\n"})
    harness.register_pr(
        401,
        "feat/pr-401",
        "PR with failing CI",
        checks=[
            {"name": "quality-required", "conclusion": "FAILURE"},
            {"name": "typecheck-required", "conclusion": "SUCCESS"},
        ],
    )

    # PR 402 is clean
    harness.create_branch("feat/pr-402", {"src/ok.py": "x = 2\n"})
    harness.register_pr(402, "feat/pr-402", "Clean PR")

    config = MergeTrainConfig(
        repo_path=str(local_repo),
        worktrees_dir=str(harness.worktree_base),
        gates_dir=str(harness.gates_dir),
        check_cmds=MOCK_CHECKS,
        ci_poll_interval_seconds=0.1,
        env=env,
    )
    train_input = MergeTrainInput(prs=[401, 402], config=config)
    task_queue = "tq-scenario-4"

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
                id=f"merge-train-sc4-{int(time.time())}",
                task_queue=task_queue,
            )
            result = await handle.result()

    states = result["states"]
    assert states["401"]["state"] == "CI_FAILED"
    assert "quality-required" in states["401"]["reason"]

    # Train continued and merged PR 402
    assert states["402"]["state"] == "MERGED"

    ledger = harness.get_merge_ledger()
    assert len(ledger) == 1
    assert ledger[0]["pr"] == 402


@pytest.mark.asyncio
async def test_scenario_5_ci_pending_then_green_time_skipping(tmp_path: Path) -> None:
    """Scenario 5: CI pending then green in time-skipping environment."""
    harness = FakeRepoHarness(tmp_path)
    local_repo = harness.create_local_clone()
    env = harness.get_env()

    # PR 501 initially has PENDING checks
    harness.create_branch("feat/pr-501", {"src/pending.py": "x = 501\n"})
    harness.register_pr(
        501,
        "feat/pr-501",
        "Pending CI PR",
        checks=[
            {"name": "quality-required", "conclusion": "PENDING"},
            {"name": "typecheck-required", "conclusion": "PENDING"},
        ],
    )

    config = MergeTrainConfig(
        repo_path=str(local_repo),
        worktrees_dir=str(harness.worktree_base),
        gates_dir=str(harness.gates_dir),
        check_cmds=MOCK_CHECKS,
        ci_timeout_seconds=600.0,
        ci_poll_interval_seconds=10.0,  # Temporal skips this time instantly!
        env=env,
    )
    train_input = MergeTrainInput(prs=[501], config=config)
    task_queue = "tq-scenario-5"

    async with await WorkflowEnvironment.start_time_skipping() as wf_env:
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
                id=f"merge-train-sc5-{int(time.time())}",
                task_queue=task_queue,
            )

            # Wait until workflow enters WAITING_CI state
            for _ in range(50):
                st = await handle.query(MergeTrainWorkflow.status)
                pr_st = st["states"].get("501", {})
                if pr_st.get("state") == "WAITING_CI":
                    break
                await asyncio.sleep(0.05)

            # Update checks to green in fake gh
            harness.update_pr_checks(
                501,
                [
                    {"name": "quality-required", "conclusion": "SUCCESS"},
                    {"name": "typecheck-required", "conclusion": "SUCCESS"},
                ],
            )

            # Workflow should advance and merge cleanly
            result = await handle.result()

    states = result["states"]
    assert states["501"]["state"] == "MERGED"
    ledger = harness.get_merge_ledger()
    assert len(ledger) == 1
    assert ledger[0]["pr"] == 501


@pytest.mark.asyncio
async def test_scenario_6_worker_kill_midway_pr2_no_double_merge(tmp_path: Path) -> None:
    """Scenario 6: worker kill midway through PR 2: on restart PR 1 is NOT merged twice."""
    harness = FakeRepoHarness(tmp_path)
    local_repo = harness.create_local_clone()
    env = harness.get_env()

    harness.create_branch("feat/pr-601", {"src/f601.py": "x = 601\n"})
    harness.register_pr(601, "feat/pr-601", "PR 601")

    harness.create_branch("feat/pr-602", {"src/f602.py": "x = 602\n"})
    harness.register_pr(602, "feat/pr-602", "PR 602")

    config = MergeTrainConfig(
        repo_path=str(local_repo),
        worktrees_dir=str(harness.worktree_base),
        gates_dir=str(harness.gates_dir),
        generator_cmd=MOCK_GENERATOR,
        check_cmds=MOCK_CHECKS,
        ci_poll_interval_seconds=0.2,
        env=env,
    )
    train_input = MergeTrainInput(prs=[601, 602], config=config)
    task_queue = "tq-scenario-6"

    worker_script = Path(__file__).parent / "worker_runner.py"

    async with await WorkflowEnvironment.start_local(ui=False) as wf_env:
        client = wf_env.client
        target = client.service_client.config.target_host

        worker_env = dict(env)
        pythonpath = str(Path(__file__).parents[1] / "src")
        worker_env["PYTHONPATH"] = pythonpath

        # Start worker 1 process
        worker1 = subprocess.Popen(
            [
                sys.executable,
                str(worker_script),
                "--target",
                target,
                "--task-queue",
                task_queue,
                "--repo-dir",
                str(harness.bare_repo),
                "--state-file",
                str(harness.state_file),
                "--merge-ledger",
                str(harness.ledger_file),
            ],
            env=worker_env,
        )

        try:
            handle = await client.start_workflow(
                MergeTrainWorkflow.run,
                train_input,
                id=f"merge-train-sc6-{int(time.time())}",
                task_queue=task_queue,
            )

            # Wait until PR 601 has completed and merged
            t0 = time.time()
            pr_601_merged = False
            while time.time() - t0 < 15.0:
                ledger = harness.get_merge_ledger()
                if any(r["pr"] == 601 for r in ledger):
                    pr_601_merged = True
                    break
                await asyncio.sleep(0.1)

            assert pr_601_merged, "PR 601 was not merged within timeout"

            # Wait until PR 602 starts execution (query workflow status)
            while time.time() - t0 < 15.0:
                st = await handle.query(MergeTrainWorkflow.status)
                if st.get("current_pr") == 602:
                    break
                await asyncio.sleep(0.05)

            # KILL -9 worker 1 mid-way through PR 2!
            os.kill(worker1.pid, signal.SIGKILL)
            worker1.wait()

            # Wait briefly while worker is dead
            await asyncio.sleep(1.0)

            # Start worker 2 process
            worker2 = subprocess.Popen(
                [
                    sys.executable,
                    str(worker_script),
                    "--target",
                    target,
                    "--task-queue",
                    task_queue,
                    "--repo-dir",
                    str(harness.bare_repo),
                    "--state-file",
                    str(harness.state_file),
                    "--merge-ledger",
                    str(harness.ledger_file),
                ],
                env=worker_env,
            )

            try:
                # Await workflow completion via worker 2
                result = await handle.result()
                states = result["states"]
                assert states["601"]["state"] == "MERGED"
                assert states["602"]["state"] == "MERGED"

                # CRITICAL ASSERTIONS:
                # 1. Fake gh merge ledger has EXACTLY 1 merge for PR 601
                ledger = harness.get_merge_ledger()
                merges_601 = [r for r in ledger if r["pr"] == 601]
                merges_602 = [r for r in ledger if r["pr"] == 602]
                assert len(merges_601) == 1, f"Expected 1 merge for 601, got {len(merges_601)}"
                assert len(merges_602) == 1, f"Expected 1 merge for 602, got {len(merges_602)}"

                # 2. Git log on bare origin main has EXACTLY ONE squash commit for PR 601
                main_log = harness.get_main_log()
                commits_601 = [ln for ln in main_log if "Merge pull request #601" in ln]
                commits_602 = [ln for ln in main_log if "Merge pull request #602" in ln]
                assert len(commits_601) == 1, f"Expected 1 commit for 601 in git log, got {commits_601}"
                assert len(commits_602) == 1, f"Expected 1 commit for 602 in git log, got {commits_602}"

            finally:
                worker2.terminate()
                try:
                    worker2.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    worker2.kill()

        finally:
            if worker1.poll() is None:
                worker1.kill()


@pytest.mark.asyncio
async def test_scenario_7_human_approve_signal_gates_merge(tmp_path: Path) -> None:
    """Scenario 7: the human approve signal gates the merge."""
    harness = FakeRepoHarness(tmp_path)
    local_repo = harness.create_local_clone()
    env = harness.get_env()

    harness.create_branch("feat/pr-701", {"src/gated.py": "x = 701\n"})
    harness.register_pr(701, "feat/pr-701", "Gated PR")

    config = MergeTrainConfig(
        repo_path=str(local_repo),
        worktrees_dir=str(harness.worktree_base),
        gates_dir=str(harness.gates_dir),
        check_cmds=MOCK_CHECKS,
        ci_poll_interval_seconds=0.1,
        require_approval=True,  # Gated by approve signal!
        env=env,
    )
    train_input = MergeTrainInput(prs=[701], config=config)
    task_queue = "tq-scenario-7"

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
                id=f"merge-train-sc7-{int(time.time())}",
                task_queue=task_queue,
            )

            # Wait until workflow enters WAITING_APPROVAL
            for _ in range(50):
                st = await handle.query(MergeTrainWorkflow.status)
                if st["states"].get("701", {}).get("state") == "WAITING_APPROVAL":
                    break
                await asyncio.sleep(0.1)

            # Query board() to verify markdown presentation
            board_text = await handle.query(MergeTrainWorkflow.board)
            assert "#701" in board_text
            assert "WAITING_APPROVAL" in board_text

            # Assert PR is NOT yet merged
            ledger = harness.get_merge_ledger()
            assert len(ledger) == 0

            # Send approve signal
            await handle.signal(MergeTrainWorkflow.approve, 701)

            # Workflow should now complete merge
            result = await handle.result()

    states = result["states"]
    assert states["701"]["state"] == "MERGED"

    ledger = harness.get_merge_ledger()
    assert len(ledger) == 1
    assert ledger[0]["pr"] == 701
