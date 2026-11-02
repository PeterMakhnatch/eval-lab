"""Fake GitHub CLI shim for testing merge train workflows locally."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _get_env_path(var_name: str) -> Path | None:
    val = os.environ.get(var_name)
    return Path(val) if val else None


def load_state() -> dict:
    state_file = _get_env_path("FAKE_GH_STATE_FILE")
    if state_file and state_file.exists():
        with open(state_file) as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    state_file = _get_env_path("FAKE_GH_STATE_FILE")
    if state_file:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, "w") as f:
            json.dump(state, f, indent=2)


def append_ledger(record: dict) -> None:
    ledger_file = _get_env_path("FAKE_GH_MERGE_LEDGER")
    if ledger_file:
        ledger_file.parent.mkdir(parents=True, exist_ok=True)
        with open(ledger_file, "a") as f:
            f.write(json.dumps(record) + "\n")


def squash_merge_git(bare_repo: Path, pr_num: int, branch_name: str, title: str) -> str:
    """Squash merge branch into main in bare repo and return new commit SHA."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        wt = Path(td) / "merge_wt"
        # Add temporary worktree on main
        subprocess.run(
            ["git", "-C", str(bare_repo), "worktree", "add", "-q", str(wt), "main"],
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            # Squash merge the branch
            subprocess.run(
                ["git", "-C", str(wt), "merge", "--squash", branch_name],
                check=True,
                capture_output=True,
                text=True,
            )
            # Commit
            commit_msg = f"Merge pull request #{pr_num} from {branch_name}\n\n{title}"
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(wt),
                    "-c",
                    "user.name=FakeGH",
                    "-c",
                    "user.email=fakegh@eval-lab.local",
                    "commit",
                    "-q",
                    "-m",
                    commit_msg,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            # Get new commit SHA
            r = subprocess.run(
                ["git", "-C", str(bare_repo), "rev-parse", "refs/heads/main"],
                check=True,
                capture_output=True,
                text=True,
            )
            return r.stdout.strip()
        finally:
            subprocess.run(
                ["git", "-C", str(bare_repo), "worktree", "remove", "--force", str(wt)],
                capture_output=True,
            )


def handle_pr_view(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="gh pr view")
    parser.add_argument("number", type=int)
    parser.add_argument("--repo", default="")
    parser.add_argument("--json", default="")
    parsed = parser.parse_args(args)

    state = load_state()
    prs = state.get("prs", {})
    pr_key = str(parsed.number)

    if pr_key not in prs:
        sys.stderr.write(f"Could not resolve to a PullRequest with the number {parsed.number}.\n")
        return 1

    pr_data = dict(prs[pr_key])
    pr_data.setdefault("number", parsed.number)
    pr_data.setdefault("title", f"PR #{parsed.number}")
    pr_data.setdefault("state", "OPEN")
    pr_data.setdefault("headRefName", f"branch-{parsed.number}")
    pr_data.setdefault("statusCheckRollup", [])
    pr_data.setdefault("isDraft", False)

    repo_dir = _get_env_path("FAKE_GH_REPO_DIR")
    branch = pr_data.get("headRefName", f"branch-{parsed.number}")

    if repo_dir and repo_dir.exists():
        # Read actual headRefOid from git if not pinned
        if "headRefOid" not in prs[pr_key]:
            try:
                r = subprocess.run(
                    ["git", "-C", str(repo_dir), "rev-parse", f"refs/heads/{branch}"],
                    capture_output=True,
                    text=True,
                )
                if r.returncode == 0:
                    pr_data["headRefOid"] = r.stdout.strip()
            except Exception:
                pass

        # Dynamically check mergeability against main if not pinned
        if pr_data.get("state") == "OPEN":
            try:
                r_anc = subprocess.run(
                    ["git", "-C", str(repo_dir), "merge-base", "--is-ancestor", "refs/heads/main", f"refs/heads/{branch}"],
                    capture_output=True,
                )
                is_behind = (r_anc.returncode != 0)

                r_mt = subprocess.run(
                    ["git", "-C", str(repo_dir), "merge-tree", "--write-tree", "refs/heads/main", f"refs/heads/{branch}"],
                    capture_output=True,
                    text=True,
                )
                is_conflict = (r_mt.returncode != 0)

                if "mergeable" not in prs[pr_key]:
                    pr_data["mergeable"] = "CONFLICTING" if is_conflict else "MERGEABLE"
                if "mergeStateStatus" not in prs[pr_key]:
                    pr_data["mergeStateStatus"] = "DIRTY" if is_conflict else ("BEHIND" if is_behind else "CLEAN")
            except Exception:
                pass

    pr_data.setdefault("mergeable", "MERGEABLE")
    pr_data.setdefault("mergeStateStatus", "CLEAN")
    pr_data.setdefault("headRefOid", "0000000000000000000000000000000000000000")

    # If json fields requested, filter keys
    if parsed.json:
        fields = [f.strip() for f in parsed.json.split(",") if f.strip()]
        out = {k: pr_data.get(k) for k in fields}
    else:
        out = pr_data

    sys.stdout.write(json.dumps(out))
    return 0


def handle_pr_merge(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="gh pr merge")
    parser.add_argument("number", type=int)
    parser.add_argument("--repo", default="")
    parser.add_argument("--squash", action="store_true")
    parser.add_argument("--auto", action="store_true")
    parsed = parser.parse_args(args)

    state = load_state()
    prs = state.get("prs", {})
    pr_key = str(parsed.number)

    if pr_key not in prs:
        sys.stderr.write(f"Pull request #{parsed.number} not found\n")
        return 1

    pr = prs[pr_key]
    if pr.get("state") == "MERGED":
        sys.stderr.write(f"Pull request #{parsed.number} is already merged\n")
        return 1

    repo_dir = _get_env_path("FAKE_GH_REPO_DIR")
    merged_sha = ""
    branch_name = pr.get("headRefName", f"branch-{parsed.number}")
    title = pr.get("title", f"PR #{parsed.number}")

    if repo_dir and repo_dir.exists():
        try:
            merged_sha = squash_merge_git(repo_dir, parsed.number, branch_name, title)
        except Exception as exc:
            sys.stderr.write(f"Git squash merge failed: {exc}\n")
            return 1

    pr["state"] = "MERGED"
    pr["mergedAt"] = time.time()
    pr["mergeCommit"] = merged_sha
    prs[pr_key] = pr
    state["prs"] = prs
    save_state(state)

    append_ledger({
        "event": "merge",
        "pr": parsed.number,
        "branch": branch_name,
        "commit": merged_sha,
        "timestamp": time.time(),
    })

    sys.stdout.write(f"Merged pull request #{parsed.number}\n")
    return 0


def handle_pr_checks(args: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="gh pr checks")
    parser.add_argument("number", type=int)
    parser.add_argument("--repo", default="")
    parsed = parser.parse_args(args)

    state = load_state()
    prs = state.get("prs", {})
    pr = prs.get(str(parsed.number), {})
    checks = pr.get("statusCheckRollup", [])

    all_ok = True
    for c in checks:
        name = c.get("name") or c.get("context", "check")
        status = c.get("conclusion") or c.get("state", "PENDING")
        sys.stdout.write(f"{name}\t{status}\n")
        if status not in ("SUCCESS", "NEUTRAL"):
            all_ok = False

    return 0 if all_ok else 1


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if not argv:
        sys.stderr.write("Fake gh CLI\n")
        return 1

    if argv[0] == "pr":
        if len(argv) > 1 and argv[1] == "view":
            return handle_pr_view(argv[2:])
        if len(argv) > 1 and argv[1] == "merge":
            return handle_pr_merge(argv[2:])
        if len(argv) > 1 and argv[1] == "checks":
            return handle_pr_checks(argv[2:])

    sys.stderr.write(f"Unsupported fake gh command: {' '.join(argv)}\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
