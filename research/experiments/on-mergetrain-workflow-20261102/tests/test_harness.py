"""Test harness for merge train tests: creates bare repo, PR branches, and fake gh CLI."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path


class FakeRepoHarness:
    """Manages an isolated bare git repository and fake gh environment."""

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.bare_repo = root_dir / "origin.git"
        self.worktree_base = root_dir / "worktrees"
        self.bin_dir = root_dir / "bin"
        self.state_file = root_dir / "gh_state.json"
        self.ledger_file = root_dir / "merge_ledger.jsonl"
        self.gates_dir = root_dir / "gates"

        self.worktree_base.mkdir(parents=True, exist_ok=True)
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.gates_dir.mkdir(parents=True, exist_ok=True)

        self._init_bare_repo()
        self._init_fake_gh()
        self.state: dict = {"prs": {}}
        self._save_state()

    def _init_bare_repo(self) -> None:
        subprocess.run(
            ["git", "init", "--bare", "-b", "main", str(self.bare_repo)],
            check=True,
            capture_output=True,
        )
        # Create initial commit on main using a seed clone
        seed = self.root_dir / "seed"
        subprocess.run(["git", "clone", "-q", str(self.bare_repo), str(seed)], check=True)
        (seed / "README.md").write_text("# Project\nInitial commit\n")
        (seed / "docs").mkdir(parents=True, exist_ok=True)
        (seed / "docs" / "repo-map.md").write_text("# Repo Map\n- initial_symbol\n")
        (seed / "docs" / "INDEX.md").write_text("# Index\n- initial_index\n")
        subprocess.run(["git", "add", "."], cwd=seed, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Author",
                "-c",
                "user.email=author@eval-lab.local",
                "commit",
                "-q",
                "-m",
                "Initial commit",
            ],
            cwd=seed,
            check=True,
        )
        subprocess.run(["git", "push", "-q", "origin", "main"], cwd=seed, check=True)
        shutil.rmtree(seed)
    def create_local_clone(self) -> Path:
        local_repo = self.root_dir / "local_repo"
        if not local_repo.exists():
            subprocess.run(["git", "clone", "-q", str(self.bare_repo), str(local_repo)], check=True)
            subprocess.run(["git", "-C", str(local_repo), "config", "user.name", "MergeTrain"], check=True)
            subprocess.run(["git", "-C", str(local_repo), "config", "user.email", "mergetrain@eval-lab.local"], check=True)
        return local_repo


    def _init_fake_gh(self) -> None:
        gh_script = self.bin_dir / "gh"
        # Script invokes python with fake_gh module
        python_exe = sys.executable
        script_content = f"""#!/bin/sh
exec "{python_exe}" -m mergetrain.fake_gh "$@"
"""
        gh_script.write_text(script_content)
        gh_script.chmod(gh_script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    def get_env(self) -> dict[str, str]:
        env = dict(os.environ)
        path = str(self.bin_dir) + os.pathsep + env.get("PATH", "")
        env["PATH"] = path
        env["FAKE_GH_STATE_FILE"] = str(self.state_file)
        env["FAKE_GH_REPO_DIR"] = str(self.bare_repo)
        env["FAKE_GH_MERGE_LEDGER"] = str(self.ledger_file)
        return env

    def _save_state(self) -> None:
        with open(self.state_file, "w") as f:
            json.dump(self.state, f, indent=2)

    def register_pr(
        self,
        number: int,
        branch: str,
        title: str,
        state: str = "OPEN",
        mergeable: str | None = None,
        merge_state_status: str | None = None,
        checks: list[dict] | None = None,
    ) -> None:
        if checks is None:
            # Default to all required checks green
            checks = [
                {"name": "quality-required", "conclusion": "SUCCESS"},
                {"name": "typecheck-required", "conclusion": "SUCCESS"},
            ]
        pr_entry: dict = {
            "number": number,
            "title": title,
            "state": state,
            "headRefName": branch,
            "statusCheckRollup": checks,
            "isDraft": False,
        }
        if mergeable is not None:
            pr_entry["mergeable"] = mergeable
        if merge_state_status is not None:
            pr_entry["mergeStateStatus"] = merge_state_status
        self.state["prs"][str(number)] = pr_entry
        self._save_state()

    def update_pr_checks(self, number: int, checks: list[dict]) -> None:
        if str(number) in self.state["prs"]:
            self.state["prs"][str(number)]["statusCheckRollup"] = checks
            self._save_state()

    def update_pr_state(self, number: int, state: str) -> None:
        if str(number) in self.state["prs"]:
            self.state["prs"][str(number)]["state"] = state
            self._save_state()

    def create_branch(
        self,
        branch_name: str,
        files: dict[str, str],
        from_ref: str = "main",
        commit_msg: str | None = None,
    ) -> str:
        """Creates a branch in origin with specified files changed."""
        seed = self.root_dir / f"branch_seed_{branch_name.replace('/', '_')}"
        subprocess.run(
            ["git", "clone", "-q", "--branch", from_ref, str(self.bare_repo), str(seed)],
            check=True,
        )
        if branch_name != from_ref:
            subprocess.run(["git", "checkout", "-q", "-b", branch_name], cwd=seed, check=True)
        for rel_path, content in files.items():
            p = seed / rel_path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            subprocess.run(["git", "add", rel_path], cwd=seed, check=True)

        msg = commit_msg or f"feat: changes for {branch_name}"
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Author",
                "-c",
                "user.email=author@eval-lab.local",
                "commit",
                "-q",
                "-m",
                msg,
            ],
            cwd=seed,
            check=True,
        )
        subprocess.run(["git", "push", "-q", "origin", branch_name], cwd=seed, check=True)
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=seed,
            check=True,
            capture_output=True,
            text=True,
        )
        commit_sha = r.stdout.strip()
        shutil.rmtree(seed)
        return commit_sha

    def advance_main(self, files: dict[str, str], msg: str = "Update main") -> str:
        """Advances main directly in origin with specified files."""
        return self.create_branch("main", files, from_ref="main", commit_msg=msg)

    def get_main_log(self) -> list[str]:
        """Returns list of oneline commit messages on main."""
        r = subprocess.run(
            ["git", "-C", str(self.bare_repo), "log", "--oneline", "refs/heads/main"],
            check=True,
            capture_output=True,
            text=True,
        )
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

    def get_merge_ledger(self) -> list[dict]:
        """Reads all entries from merge ledger."""
        if not self.ledger_file.exists():
            return []
        out = []
        for line in self.ledger_file.read_text().splitlines():
            if line.strip():
                out.append(json.loads(line))
        return out
