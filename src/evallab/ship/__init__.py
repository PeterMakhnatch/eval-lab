"""Exact-head delivery with durable receipts and conservative retirement.

The earlier F4 experiment supplies the remote-state reconciliation pattern.
This command needs no workflow server, automatic rebases, or force removals.
"""
from __future__ import annotations

import argparse
import fcntl
import fnmatch
import json
import os
import re
import subprocess
import time
import tomllib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.ship.cleanup import inspect_cleanup, inspect_tree, retire


class Blocked(RuntimeError):
    """A delivery boundary needs an author fix."""


class Ship:
    def __init__(self, root: Path, pr: int, state_dir: Path, *, poll: float = 15,
                 timeout: float = 1800, rerun: bool = True, smoke: str | None = None):
        self.root = root.resolve()
        self.pr = pr
        self.directory = state_dir.expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / f"{pr}.json"
        self.data: dict[str, Any] = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.poll, self.timeout, self.rerun, self.smoke = poll, timeout, rerun, smoke
        self.repo = ""

    def save(self, step: str, **fields: Any) -> None:
        self.data.update(fields)
        self.data["step"] = step
        self.data["updated_at"] = datetime.now(UTC).isoformat()
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as stream:
            json.dump(self.data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)
        print(f"ship #{self.pr}: {step}", flush=True)

    def run(self, args: list[str], *, cwd: Path | None = None,
            check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(args, cwd=cwd or self.root, env=env, text=True,
                                capture_output=True, timeout=300)
        log = self.directory / f"{self.pr}-{uuid.uuid4().hex}.log"
        # launchd environments can contain credentials. Retain only the probe verdict.
        log.write_text("launchd output withheld (environment secrets)\n" if args[0] == "launchctl"
                       else result.stdout + result.stderr)
        self.data.setdefault("commands", []).append({"argv": args, "cwd": str(cwd or self.root),
                                                    "exit": result.returncode, "log": str(log)})
        self.save(self.data.get("step", "starting"))
        if check and result.returncode:
            raise Blocked(f"command failed ({result.returncode}): {args}; {log}\n{result.stderr[-2000:]}")
        return result

    def gh(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        # Never let gh switch/delete a local branch in a linked worktree.
        return self.run(["gh", *args], cwd=self.directory, check=check)

    def view(self) -> dict[str, Any]:
        return json.loads(self.gh("pr", "view", str(self.pr), "--repo", self.repo, "--json",
            "number,state,headRefOid,headRefName,baseRefName,baseRefOid,isDraft,mergeable,"
            "mergeStateStatus,changedFiles,labels,mergeCommit,isCrossRepository,statusCheckRollup").stdout)

    def guard(self, pr: dict[str, Any]) -> None:
        for key in ("headRefOid", "baseRefName", "baseRefOid"):
            if pr[key] != self.data["pr"][key]:
                raise Blocked(f"PR {key} moved: {self.data['pr'][key]} -> {pr[key]}; start a new head receipt")
        if pr["state"] != "OPEN" or pr["isDraft"] or pr["mergeable"] != "MERGEABLE":
            raise Blocked(f"PR is not open, non-draft and mergeable: {pr['state']}, {pr['mergeable']}")
        if any("hold" in label["name"].lower() for label in pr.get("labels", [])):
            raise Blocked("PR has an operator hold label")

    def required(self, base: str) -> set[str]:
        result = self.gh("api", f"repos/{self.repo}/branches/{base}/protection/required_status_checks", check=False)
        if result.returncode == 0:
            protection = json.loads(result.stdout)
            names = set(protection.get("contexts", []))
            names.update(row["context"] for row in protection.get("checks", []))
            if names:
                self.save("required-checks", required_source="branch-protection", required=sorted(names))
                return names
        # The checked-in contract is the fallback, not an invented constant.
        text = self.run(["git", "show", f"origin/{base}:agents/CHECKS.md"]).stdout
        names = set(re.findall(r"^\| `([^`]+-required)` \|", text, re.MULTILINE))
        if not names:
            raise Blocked("No required checks in branch protection or agents/CHECKS.md")
        self.save("required-checks", required_source="agents/CHECKS.md", required=sorted(names))
        return names

    @contextmanager
    def checkout(self, commit: str, purpose: str) -> Iterator[Path]:
        tree = self.directory / f"{self.pr}-{purpose}-worktree"
        self.save("checkout-requested", active_checkout={"path": str(tree), "commit": commit, "purpose": purpose})
        if tree.exists():
            inventory = self.run(["git", "worktree", "list", "--porcelain", "-z"]).stdout
            block = next((row for row in inventory.split("\0\0")
                          if row.startswith(f"worktree {tree}\0")), "")
            if (f"\0HEAD {commit}\0" not in block or "\0detached" not in block
                    or "\0locked" in block or "\0prunable" in block):
                raise Blocked(f"Cannot resume an unknown or changed detached checkout: {tree}")
            inspect_tree(self, tree, commit)
        else:
            self.run(["git", "worktree", "add", "--detach", str(tree), commit])
        try:
            yield tree
        finally:
            inspect_tree(self, tree, commit)
            self.run(["git", "worktree", "remove", str(tree)])
            self.save("checkout-removed", active_checkout=None)

    def gates(self, pr: dict[str, Any]) -> None:
        if self.data.get("gates_head") == pr["headRefOid"]:
            return
        # Read gate policy from the base so a PR cannot remove its own gate.
        present = self.run(["git", "ls-tree", "--name-only", f"origin/{pr['baseRefName']}",
                            "--", "agents/ship-gates.toml"]).stdout.strip()
        gates: list[dict[str, Any]] = []
        if present:
            config = self.run(["git", "show", f"origin/{pr['baseRefName']}:agents/ship-gates.toml"])
            gates = tomllib.loads(config.stdout).get("gate", [])
        paths = [row["path"] for row in pr["files"]]
        selected = [gate for gate in gates if any(fnmatch.fnmatchcase(path, pattern)
                    for path in paths for pattern in gate["paths"])]
        if selected:
            with self.checkout(pr["headRefOid"], "gate") as tree:
                for gate in selected:
                    env = {**os.environ, "EVALLAB_SHIP_HEAD": pr["headRefOid"],
                           "EVALLAB_SHIP_BASE": pr["baseRefOid"], "EVALLAB_SHIP_PR": str(self.pr)}
                    result = self.run(gate["command"], cwd=tree, env=env, check=False)
                    if result.returncode:
                        raise Blocked(f"gate {gate['name']}: {'blocked' if result.returncode == 1 else 'error'} "
                                      f"(exit {result.returncode})\n{result.stdout[-2000:]}")
        self.save("gates-passed", gates_head=pr["headRefOid"], gates=[g["name"] for g in selected])

    def checks(self, required: set[str]) -> None:
        deadline = time.monotonic() + self.timeout
        while True:
            pr = self.view()
            self.guard(pr)
            checks = pr.get("statusCheckRollup") or []
            passed, failed, pending = set(), [], []
            for row in checks:
                name = row.get("name") or row.get("context")
                state = row.get("conclusion") or row.get("state") or row.get("status")
                if state == "SUCCESS":
                    passed.add(name)
                elif state in {"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}:
                    pending.append(name)
                else:
                    failed.append(row)
            missing = required - passed
            if failed:
                details, run_ids = [], set()
                for row in failed:
                    name = row.get("name") or row.get("context")
                    url = row.get("detailsUrl") or row.get("targetUrl") or ""
                    match = re.search(r"/actions/runs/(\d+)", url)
                    if match:
                        run_ids.add(match[1])
                    details.append(str(name))
                for run_id in sorted(run_ids):
                    log = self.gh("run", "view", run_id, "--repo", self.repo, "--log-failed", check=False)
                    details.extend(sorted(set(re.findall(r"(?:FAILED|ERROR)\s+(\S+::\S+)", log.stdout))))
                    details.append(log.stdout[-16000:])
                self.save("checks-failed", failures=details)
                print("\n".join(details), flush=True)
                used = self.data.get("rerun_head") == pr["headRefOid"]
                if not self.rerun or used or not run_ids:
                    raise Blocked("CI failed: " + "; ".join(details))
                attempts = {}
                for run_id in sorted(run_ids):
                    before = json.loads(self.gh("run", "view", run_id, "--repo", self.repo,
                                                "--json", "status,attempt").stdout)
                    attempts[run_id] = before["attempt"]
                # Persist intent before side effects. An interruption cannot rerun twice.
                self.save("rerun-requested", rerun_head=pr["headRefOid"], rerun_ids=sorted(run_ids))
                for run_id in sorted(run_ids):
                    self.gh("run", "rerun", run_id, "--repo", self.repo, "--failed")
                # Wait for a new attempt rather than treating the old red rollup as a second failure.
                for run_id in sorted(run_ids):
                    while True:
                        run = json.loads(self.gh("run", "view", run_id, "--repo", self.repo,
                                                 "--json", "status,attempt").stdout)
                        if run["attempt"] > attempts[run_id]:
                            break
                        if time.monotonic() >= deadline:
                            raise Blocked("Rerun did not start before timeout")
                        time.sleep(self.poll)
            elif not missing and not pending:
                runs = {}
                for row in checks:
                    match = re.search(r"/actions/runs/(\d+)", row.get("detailsUrl", ""))
                    name = row.get("name") or row.get("context")
                    if name in required and not match:
                        raise Blocked(f"Required check {name} has no GitHub Actions PR run")
                    if not match or match[1] in runs:
                        continue
                    run = json.loads(self.gh("run", "view", match[1], "--repo", self.repo,
                        "--json", "headSha,event,headBranch,status,conclusion,jobs,url,attempt").stdout)
                    if run["headSha"] != pr["headRefOid"] or run["event"] != "pull_request":
                        raise Blocked("CI run does not prove this pull-request head")
                    if run["status"] != "completed" or run["conclusion"] != "success":
                        raise Blocked("CI workflow is not complete and successful")
                    runs[match[1]] = run
                self.save("ci-runs-verified", ci_runs=runs)
                self.save("checks-passed", checked_head=pr["headRefOid"], checks=checks)
                print("successful checks: " + ", ".join(sorted(passed)), flush=True)
                return
            else:
                self.save("checks-pending", pending=sorted(set(pending) | missing), successful=sorted(passed))
            if time.monotonic() >= deadline:
                raise Blocked("Timed out waiting for exact-head CI")
            time.sleep(self.poll)

    def execute(self) -> None:
        self.repo = json.loads(self.run(["gh", "repo", "view", "--json", "nameWithOwner"]).stdout)["nameWithOwner"]
        if self.data.get("repository", self.repo) != self.repo:
            raise Blocked("Receipt belongs to another repository")
        self.smoke = self.smoke or self.data.get("smoke_command")
        self.save("resolving", repository=self.repo, smoke_command=self.smoke)
        pr = self.view()
        if pr.get("isCrossRepository"):
            raise Blocked("Fork retirement is not supported; no changes made")
        self.run(["git", "fetch", "origin", pr["baseRefName"]])
        if "pr" not in self.data:
            if pr["changedFiles"] > 3000:
                raise Blocked("PR exceeds GitHub's complete changed-path API limit")
            pages = json.loads(self.gh("api", "--paginate", "--slurp",
                f"repos/{self.repo}/pulls/{self.pr}/files?per_page=100").stdout)
            files = [row for page in pages for row in page]
            if len(files) != pr["changedFiles"]:
                raise Blocked("Incomplete changed-path evidence")
            pr["files"] = [{"path": name} for row in files
                           for name in {row["filename"], row.get("previous_filename", row["filename"])}]
            self.save("resolved", pr=pr)
        if pr["headRefOid"] != self.data["pr"]["headRefOid"]:
            raise Blocked("PR head moved; preserve this receipt and use a fresh --state-dir")
        if pr["baseRefName"] != "main":
            raise Blocked("ship only delivers PRs targeting main")
        if pr["state"] != "MERGED":
            self.guard(pr)
            pr["files"] = self.data["pr"]["files"]
            self.run(["git", "fetch", "origin", f"pull/{self.pr}/head"])
            self.run(["git", "merge-base", "--is-ancestor", pr["baseRefOid"], pr["headRefOid"]])
            self.gates(pr)
            self.checks(self.required(pr["baseRefName"]))
            self.guard(self.view())
            # A dirty/in-use tree must not lose even its remote branch.
            plan = inspect_cleanup(self, pr)
            self.save("cleanup-preflight", cleanup_plan=plan)
            self.guard(self.view())
            self.save("merge-requested", merge_attempted=True)
            self.gh("pr", "merge", str(self.pr), "--repo", self.repo, "--squash", "--delete-branch",
                    "--match-head-commit", pr["headRefOid"], check=False)
            pr = self.view()
        if pr["state"] != "MERGED" or not pr.get("mergeCommit", {}).get("oid"):
            raise Blocked("Remote PR is not MERGED with a merge commit")
        commit = pr["mergeCommit"]["oid"]
        self.save("merged", merge_commit=commit)
        self.run(["git", "fetch", "origin", "main"])
        self.run(["git", "merge-base", "--is-ancestor", commit, "origin/main"])
        self.save("main-verified")
        if self.smoke and self.data.get("smoke_passed") != {"commit": commit, "command": self.smoke}:
            with self.checkout(commit, "smoke") as tree:
                self.run(["/bin/sh", "-c", self.smoke], cwd=tree)
            self.save("smoke-passed", smoke_passed={"commit": commit, "command": self.smoke})
        plan = inspect_cleanup(self, pr)
        self.save("cleanup-preflight", cleanup_plan=plan)
        retire(self, pr, plan)
        self.save("complete")


def run_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    ship = Ship(args.repo_root or root, args.pr, args.state_dir, poll=args.poll,
                timeout=args.timeout, rerun=args.rerun_failed_once, smoke=args.smoke)
    with ship.path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Blocked("Another ship process owns this PR") from exc
        # Read state under the lock, not before another writer finishes.
        ship.data = json.loads(ship.path.read_text()) if ship.path.exists() else {}
        try:
            ship.execute()
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            ship.save("blocked", error=str(exc))
            print(f"ship blocked: {exc}", flush=True)
            return 1
    return 0


def register_parser(commands: Any) -> None:
    parser = commands.add_parser("ship", help="Merge exact-head green CI and safely retire its worktree")
    parser.add_argument("pr", type=int)
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/evallab-wf/ship")
    parser.add_argument("--poll", type=float, default=15)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--rerun-failed-once", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--smoke")
    parser.set_defaults(func=run_command)
