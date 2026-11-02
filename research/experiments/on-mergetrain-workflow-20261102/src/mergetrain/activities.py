"""Temporal activities for merge train: viewing PR, rebasing, checking CI, merging."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from temporalio import activity

from mergetrain.models import (
    CheckCiParams,
    CheckCiResult,
    MergePrParams,
    MergePrResult,
    RebasePrParams,
    RebasePrResult,
    ViewPrParams,
    ViewPrResult,
)


def _run_cmd(
    cmd: list[str],
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 600.0,
) -> subprocess.CompletedProcess[str]:
    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        env=merged_env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@activity.defn
async def view_pr_activity(params: ViewPrParams) -> ViewPrResult:
    """Queries PR metadata and CI rollup via gh CLI."""
    cmd = [
        "gh",
        "pr",
        "view",
        str(params.pr_number),
        "--json",
        "number,state,headRefName,headRefOid,mergeable,mergeStateStatus,statusCheckRollup,title,isDraft",
    ]
    if params.gh_repo:
        cmd.extend(["--repo", params.gh_repo])

    r = _run_cmd(cmd, env=params.env)
    if r.returncode != 0:
        raise RuntimeError(f"gh pr view failed for #{params.pr_number}: {r.stderr or r.stdout}")

    data = json.loads(r.stdout)
    return ViewPrResult(
        pr_number=data.get("number", params.pr_number),
        state=data.get("state", "OPEN"),
        title=data.get("title", ""),
        head_ref_name=data.get("headRefName", ""),
        head_ref_oid=data.get("headRefOid", ""),
        mergeable=data.get("mergeable", "UNKNOWN"),
        merge_state_status=data.get("mergeStateStatus", "UNKNOWN"),
        status_check_rollup=data.get("statusCheckRollup", []),
        is_draft=data.get("isDraft", False),
    )


@activity.defn
async def rebase_pr_activity(params: RebasePrParams) -> RebasePrResult:
    """Rebases a PR branch in an isolated temporary worktree, resolving generated files."""
    repo = Path(params.repo_path)
    wt_base = Path(params.worktrees_dir) if params.worktrees_dir else repo / ".worktrees"
    wt = wt_base / f"on-mt-{params.pr_number}"

    # Fetch origin main and PR branch
    _run_cmd(["git", "-C", str(repo), "fetch", "-q", "origin", "main", params.head_ref_name], env=params.env)

    # Clean existing worktree if present
    if wt.exists():
        _run_cmd(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], env=params.env)

    # Create detached worktree from origin branch
    r = _run_cmd(
        ["git", "-C", str(repo), "worktree", "add", "-q", "--detach", str(wt), f"origin/{params.head_ref_name}"],
        env=params.env,
    )
    if r.returncode != 0:
        return RebasePrResult(
            pr_number=params.pr_number,
            success=False,
            needs_human=True,
            reason=f"Failed to create worktree: {r.stderr or r.stdout}",
        )

    try:
        # Run git rebase origin/main
        r = _run_cmd(["git", "rebase", "origin/main"], cwd=wt, env=params.env)
        gen_set = set(params.generated_files)

        for _ in range(params.rebase_max_steps):
            if r.returncode == 0:
                break
            # Get conflicted files
            diff_r = _run_cmd(["git", "diff", "--name-only", "--diff-filter=U"], cwd=wt, env=params.env)
            conflicts = set(diff_r.stdout.split())

            if not conflicts or not (conflicts <= gen_set):
                _run_cmd(["git", "rebase", "--abort"], cwd=wt, env=params.env)
                return RebasePrResult(
                    pr_number=params.pr_number,
                    success=False,
                    needs_human=True,
                    reason=f"rebase conflicts outside generated docs: {sorted(conflicts)}",
                )

            # Resolve generated files by taking ours/theirs and adding
            for f in conflicts:
                _run_cmd(["git", "checkout", "--ours", "--", f], cwd=wt, env=params.env)
                _run_cmd(["git", "add", f], cwd=wt, env=params.env)

            r = _run_cmd(["git", "rebase", "--continue"], cwd=wt, env=params.env)
        else:
            _run_cmd(["git", "rebase", "--abort"], cwd=wt, env=params.env)
            return RebasePrResult(
                pr_number=params.pr_number,
                success=False,
                needs_human=True,
                reason="rebase did not converge within max steps",
            )

        # Run generator if specified
        if params.generator_cmd:
            gen_r = _run_cmd(params.generator_cmd, cwd=wt, env=params.env)
            if gen_r.returncode != 0:
                return RebasePrResult(
                    pr_number=params.pr_number,
                    success=False,
                    needs_human=True,
                    reason=f"generator failed: {gen_r.stderr or gen_r.stdout}",
                )
            # Check if docs/ was modified and commit if so
            st_r = _run_cmd(["git", "status", "--porcelain", "docs/"], cwd=wt, env=params.env)
            if st_r.stdout.strip():
                _run_cmd(["git", "add", "docs/"], cwd=wt, env=params.env)
                _run_cmd(
                    [
                        "git",
                        "-c",
                        "user.name=MergeTrain",
                        "-c",
                        "user.email=mergetrain@eval-lab.local",
                        "commit",
                        "-q",
                        "-m",
                        "chore: regenerate repo map after rebase",
                    ],
                    cwd=wt,
                    env=params.env,
                )

        # Run required checks (ruff, repomap, governance, etc.)
        for name, cmd in params.check_cmds:
            check_r = _run_cmd(cmd, cwd=wt, env=params.env)
            if check_r.returncode != 0:
                return RebasePrResult(
                    pr_number=params.pr_number,
                    success=False,
                    needs_human=True,
                    reason=f"{name} check failed after rebase: {(check_r.stdout + check_r.stderr)[-300:].strip()}",
                )

        # Push rebased branch with force-with-lease
        push_cmd = [
            "git",
            "push",
            "-q",
            f"--force-with-lease={params.head_ref_name}:{params.head_ref_oid}",
            "origin",
            f"HEAD:{params.head_ref_name}",
        ]
        push_r = _run_cmd(push_cmd, cwd=wt, env=params.env)
        if push_r.returncode != 0:
            return RebasePrResult(
                pr_number=params.pr_number,
                success=False,
                needs_human=True,
                reason=f"push --force-with-lease failed: {push_r.stderr or push_r.stdout}",
            )

        return RebasePrResult(pr_number=params.pr_number, success=True)

    finally:
        _run_cmd(["git", "-C", str(repo), "worktree", "remove", "--force", str(wt)], env=params.env)


@activity.defn
async def check_ci_activity(params: CheckCiParams) -> CheckCiResult:
    """Evaluates CI status rollup for required checks."""
    cmd = [
        "gh",
        "pr",
        "view",
        str(params.pr_number),
        "--json",
        "statusCheckRollup",
    ]
    if params.gh_repo:
        cmd.extend(["--repo", params.gh_repo])

    r = _run_cmd(cmd, env=params.env)
    if r.returncode != 0:
        raise RuntimeError(f"gh pr view failed for #{params.pr_number}: {r.stderr or r.stdout}")

    data = json.loads(r.stdout)
    rollup = data.get("statusCheckRollup") or []

    ok_checks: set[str] = set()
    bad_checks: list[str] = []
    required_set = set(params.required_checks)

    for c in rollup:
        name = c.get("name") or c.get("context", "")
        conclusion = c.get("conclusion") or c.get("state", "PENDING")
        if conclusion == "SUCCESS":
            ok_checks.add(name)
        elif conclusion in ("FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "ERROR"):
            bad_checks.append(name)

    is_green = (required_set <= ok_checks) and not bad_checks
    is_failing = bool(bad_checks)
    is_pending = not is_green and not is_failing

    return CheckCiResult(
        pr_number=params.pr_number,
        is_green=is_green,
        is_failing=is_failing,
        is_pending=is_pending,
        failing_checks=bad_checks,
    )


@activity.defn
async def merge_pr_activity(params: MergePrParams) -> MergePrResult:
    """Squash merges the PR idempotently via gh CLI."""
    # First check if already merged
    view_cmd = ["gh", "pr", "view", str(params.pr_number), "--json", "state"]
    if params.gh_repo:
        view_cmd.extend(["--repo", params.gh_repo])
    r = _run_cmd(view_cmd, env=params.env)
    if r.returncode == 0:
        st = json.loads(r.stdout).get("state")
        if st == "MERGED":
            return MergePrResult(
                pr_number=params.pr_number,
                success=True,
                already_merged=True,
                message="already merged",
            )

    cmd = ["gh", "pr", "merge", str(params.pr_number), "--squash"]
    if params.gh_repo:
        cmd.extend(["--repo", params.gh_repo])

    r = _run_cmd(cmd, env=params.env)
    if r.returncode != 0:
        # Check if error message indicates already merged
        err_msg = (r.stderr or r.stdout).strip()
        if "already merged" in err_msg.lower():
            return MergePrResult(
                pr_number=params.pr_number,
                success=True,
                already_merged=True,
                message="already merged",
            )
        return MergePrResult(
            pr_number=params.pr_number,
            success=False,
            error=err_msg,
        )

    # Touch gate file if configured
    if params.gates_dir and params.branch_name:
        first = params.branch_name.split("/", 1)[-1].split("-", 1)[0].upper()
        gates_path = Path(params.gates_dir)
        gates_path.mkdir(parents=True, exist_ok=True)
        (gates_path / f"{first}-merged").touch()

    return MergePrResult(
        pr_number=params.pr_number,
        success=True,
        message=f"Squash merged PR #{params.pr_number}",
    )
