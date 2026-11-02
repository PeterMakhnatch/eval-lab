"""Temporal workflow definition for the durable merge train."""
from __future__ import annotations

from dataclasses import asdict
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from mergetrain.activities import (
        check_ci_activity,
        merge_pr_activity,
        rebase_pr_activity,
        view_pr_activity,
    )
    from mergetrain.models import (
        CheckCiParams,
        MergePrParams,
        MergeTrainInput,
        PrStatus,
        RebasePrParams,
        ViewPrParams,
    )


@workflow.defn
class MergeTrainWorkflow:
    """Durable merge train orchestrator managing rebase, CI wait, approval, and merge."""

    def __init__(self) -> None:
        self.prs: list[int] = []
        self.states: dict[int, PrStatus] = {}
        self.approved_prs: set[int] = set()
        self.skipped_prs: set[int] = set()
        self.aborted: bool = False
        self.current_pr: int | None = None

    @workflow.signal
    def approve(self, pr_number: int) -> None:
        """Approves a PR to proceed through the approval gate."""
        self.approved_prs.add(pr_number)

    @workflow.signal
    def skip(self, pr_number: int) -> None:
        """Skips a PR, bypassing merge and moving to the next PR."""
        self.skipped_prs.add(pr_number)

    @workflow.signal
    def abort(self) -> None:
        """Aborts the merge train."""
        self.aborted = True

    @workflow.query
    def board(self) -> str:
        """Renders a formatted markdown board of the merge train status."""
        lines = [
            "# Merge Train Board",
            "",
            "| PR | Branch | State | Reason / Details |",
            "|---|---|---|---|",
        ]
        for pr_num in self.prs:
            st = self.states.get(pr_num)
            if not st:
                lines.append(f"| #{pr_num} | - | PENDING | - |")
            else:
                branch = st.branch or "-"
                reason = (st.reason or "-").replace("\n", " ")
                lines.append(f"| #{pr_num} | `{branch}` | **{st.state}** | {reason} |")
        return "\n".join(lines)

    @workflow.query
    def status(self) -> dict:
        """Returns structured JSON dictionary of train status."""
        return {
            "current_pr": self.current_pr,
            "aborted": self.aborted,
            "approved_prs": sorted(self.approved_prs),
            "skipped_prs": sorted(self.skipped_prs),
            "states": {str(k): asdict(v) for k, v in self.states.items()},
        }

    @workflow.run
    async def run(self, input_data: MergeTrainInput) -> dict:
        """Executes the merge train sequentially across PRs."""
        self.prs = list(input_data.prs)
        config = input_data.config

        # Initialize status for all PRs
        for pr_num in self.prs:
            self.states[pr_num] = PrStatus(pr_number=pr_num, state="PENDING")

        activity_retry = RetryPolicy(
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2.0,
            maximum_attempts=3,
        )

        for pr_num in self.prs:
            if self.aborted:
                self.states[pr_num].state = "ABORTED"
                self.states[pr_num].reason = "Train aborted by signal"
                continue

            if pr_num in self.skipped_prs:
                self.states[pr_num].state = "SKIPPED"
                self.states[pr_num].reason = "Skipped by signal"
                continue

            self.current_pr = pr_num
            status = self.states[pr_num]

            # 1. View PR state
            view_params = ViewPrParams(pr_number=pr_num, gh_repo=config.gh_repo, env=config.env)
            pr_view = await workflow.execute_activity(
                view_pr_activity,
                view_params,
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=activity_retry,
            )

            status.branch = pr_view.head_ref_name
            status.head_oid = pr_view.head_ref_oid
            status.title = pr_view.title

            if pr_view.state == "MERGED":
                status.state = "ALREADY_MERGED"
                status.reason = "PR is already merged"
                continue

            if pr_view.state != "OPEN":
                status.state = "CLOSED"
                status.reason = f"PR is in state {pr_view.state}"
                continue

            # 2. Check if rebase needed
            needs_rebase = (
                pr_view.mergeable == "CONFLICTING"
                or pr_view.merge_state_status in ("BEHIND", "DIRTY")
            )

            if needs_rebase:
                status.state = "REBASING"
                rebase_params = RebasePrParams(
                    pr_number=pr_num,
                    repo_path=config.repo_path,
                    worktrees_dir=config.worktrees_dir,
                    head_ref_name=pr_view.head_ref_name,
                    head_ref_oid=pr_view.head_ref_oid,
                    generated_files=config.generated_files,
                    generator_cmd=config.generator_cmd,
                    check_cmds=config.check_cmds,
                    rebase_max_steps=config.rebase_max_steps,
                    env=config.env,
                )
                rebase_res = await workflow.execute_activity(
                    rebase_pr_activity,
                    rebase_params,
                    start_to_close_timeout=timedelta(minutes=15),
                    retry_policy=RetryPolicy(maximum_attempts=1),
                )

                if not rebase_res.success:
                    status.state = "NEEDS_HUMAN"
                    status.reason = rebase_res.reason
                    continue

                # Refresh view after rebase push
                pr_view = await workflow.execute_activity(
                    view_pr_activity,
                    view_params,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=activity_retry,
                )
                status.head_oid = pr_view.head_ref_oid

            # 3. Wait for CI checks
            status.state = "WAITING_CI"
            ci_params = CheckCiParams(
                pr_number=pr_num,
                gh_repo=config.gh_repo,
                required_checks=config.required_checks,
                env=config.env,
            )

            poll_interval = timedelta(seconds=config.ci_poll_interval_seconds)
            timeout_delta = timedelta(seconds=config.ci_timeout_seconds)
            ci_deadline = workflow.now() + timeout_delta

            ci_ok = False
            while workflow.now() < ci_deadline:
                if self.aborted:
                    status.state = "ABORTED"
                    status.reason = "Aborted during CI wait"
                    break
                if pr_num in self.skipped_prs:
                    status.state = "SKIPPED"
                    status.reason = "Skipped during CI wait"
                    break

                ci_res = await workflow.execute_activity(
                    check_ci_activity,
                    ci_params,
                    start_to_close_timeout=timedelta(minutes=2),
                    retry_policy=activity_retry,
                )

                if ci_res.is_green:
                    ci_ok = True
                    break

                if ci_res.is_failing:
                    status.state = "CI_FAILED"
                    status.reason = f"CI failing checks: {ci_res.failing_checks}"
                    break

                await workflow.sleep(poll_interval)

            if self.aborted:
                break
            if pr_num in self.skipped_prs or status.state == "SKIPPED":
                continue
            if not ci_ok:
                if status.state == "WAITING_CI":
                    status.state = "NEEDS_HUMAN"
                    status.reason = "Timeout waiting for required checks"
                continue

            # 4. Human approval gate
            if config.require_approval and pr_num not in self.approved_prs:
                status.state = "WAITING_APPROVAL"
                status.reason = "Waiting for human approve signal"

                await workflow.wait_condition(
                    lambda p=pr_num: p in self.approved_prs or p in self.skipped_prs or self.aborted
                )

                if self.aborted:
                    status.state = "ABORTED"
                    status.reason = "Aborted during approval gate"
                    break

                if pr_num in self.skipped_prs:
                    status.state = "SKIPPED"
                    status.reason = "Skipped by signal during approval gate"
                    continue

            # 5. Squash merge
            if config.dry_run:
                status.state = "DRY_RUN"
                status.reason = "Dry run completed successfully"
                continue

            status.state = "MERGING"
            merge_params = MergePrParams(
                pr_number=pr_num,
                gh_repo=config.gh_repo,
                branch_name=status.branch,
                gates_dir=config.gates_dir,
                env=config.env,
            )
            merge_res = await workflow.execute_activity(
                merge_pr_activity,
                merge_params,
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=activity_retry,
            )

            if merge_res.success:
                status.state = "ALREADY_MERGED" if merge_res.already_merged else "MERGED"
                status.reason = merge_res.message
                status.merge_commit = merge_res.merge_commit
            else:
                status.state = "MERGE_FAILED"
                status.reason = merge_res.error

        if self.aborted:
            for p in self.prs:
                if self.states[p].state == "PENDING":
                    self.states[p].state = "ABORTED"
                    self.states[p].reason = "Train aborted by signal"

        self.current_pr = None
        return self.status()
