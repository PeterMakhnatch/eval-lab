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

#: How many times a PR view may report mergeable UNKNOWN before ship gives up on it.
UNKNOWN_MERGEABLE_VIEWS = 12


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
            check: bool = True, env: dict[str, str] | None = None,
            timeout: float = 300) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(args, cwd=cwd or self.root, env=env, text=True,
                                capture_output=True, timeout=timeout)
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

    def view(self, *, wait: bool = True) -> dict[str, Any]:
        # GitHub reports mergeable UNKNOWN until it has computed the merge after a
        # push or a base change; ask again briefly instead of treating it as a conflict.
        for _ in range(UNKNOWN_MERGEABLE_VIEWS):
            pr = json.loads(self.gh("pr", "view", str(self.pr), "--repo", self.repo, "--json",
                "number,state,headRefOid,headRefName,baseRefName,baseRefOid,isDraft,mergeable,"
                "mergeStateStatus,changedFiles,labels,mergeCommit,isCrossRepository,statusCheckRollup").stdout)
            if not wait or pr["state"] != "OPEN" or pr["mergeable"] != "UNKNOWN":
                return pr
            time.sleep(min(self.poll, 5))
        return pr

    def guard(self, pr: dict[str, Any]) -> None:
        for key in ("headRefOid", "baseRefName"):
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
        self.run(["git", "fetch", "origin", pr["baseRefName"]])
        base = self.run(["git", "rev-parse", f"origin/{pr['baseRefName']}"]).stdout.strip()
        inputs = {"head": pr["headRefOid"], "base": base}
        if self.data.get("gates_inputs") == inputs:
            return
        # Pin policy and replay inputs to current main, not the PR's baseRefOid.
        present = self.run(["git", "ls-tree", "--name-only", base,
                            "--", "agents/ship-gates.toml"]).stdout.strip()
        gates: list[dict[str, Any]] = []
        if present:
            config = self.run(["git", "show", f"{base}:agents/ship-gates.toml"])
            gates = tomllib.loads(config.stdout).get("gate", [])
        # New gates apply to their registration PR too. Candidate policy can
        # only add names; the base definition always wins for existing gates.
        head_policy = self.run(["git", "ls-tree", "--name-only", pr["headRefOid"],
                                "--", "agents/ship-gates.toml"]).stdout.strip()
        if head_policy:
            candidate = self.run(["git", "show", f"{pr['headRefOid']}:agents/ship-gates.toml"])
            base_names = {gate["name"] for gate in gates}
            gates.extend(gate for gate in tomllib.loads(candidate.stdout).get("gate", [])
                         if gate["name"] not in base_names)
        paths = [row["path"] for row in pr["files"]]
        selected = [gate for gate in gates if any(fnmatch.fnmatchcase(path, pattern)
                    for path in paths for pattern in gate["paths"])]
        if selected:
            with self.checkout(pr["headRefOid"], "gate") as tree:
                for gate in selected:
                    env = {**os.environ, "EVALLAB_SHIP_HEAD": pr["headRefOid"],
                           "EVALLAB_SHIP_BASE": base, "EVALLAB_SHIP_PR": str(self.pr)}
                    result = self.run(gate["command"], cwd=tree, env=env, check=False,
                                      timeout=self.timeout)
                    if result.returncode:
                        raise Blocked(f"gate {gate['name']}: {'blocked' if result.returncode == 1 else 'error'} "
                                      f"(exit {result.returncode})\n{result.stdout[-2000:]}")
        self.save("gates-passed", gates_inputs=inputs, gates=[g["name"] for g in selected])

    def retarget(self) -> str | None:
        owner, name = self.repo.split("/")
        result = json.loads(self.gh("api", "graphql", "-f", "query="
            "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
            "pullRequest(number:$number){timelineItems(last:1,itemTypes:[BASE_REF_CHANGED_EVENT]){"
            "nodes{... on BaseRefChangedEvent{createdAt}}}}}}",
            "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={self.pr}").stdout)
        events = result["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"]
        return events[0]["createdAt"] if events else None

    def verify_run(self, run_id: str, pr: dict[str, Any], retarget: str | None) -> dict[str, Any]:
        run = json.loads(self.gh("api", f"repos/{self.repo}/actions/runs/{run_id}").stdout)
        if (run["head_sha"] != pr["headRefOid"] or run["event"] != "pull_request"
                or run["head_branch"] != pr["headRefName"]):
            raise Blocked("CI run does not prove this pull-request head")
        if run["status"] != "completed" or run["conclusion"] != "success":
            raise Blocked("CI workflow is not complete and successful")
        # Rerunning an old workflow does not create a new retarget event payload.
        if retarget and run["created_at"] <= retarget:
            raise Blocked("CI run predates the latest PR base retarget")
        associations = [row for row in run["pull_requests"] if row["number"] == self.pr
                        and row["head"]["sha"] == pr["headRefOid"]
                        and row["base"]["ref"] == pr["baseRefName"]
                        and row["base"]["repo"]["url"] == f"https://api.github.com/repos/{self.repo}"
                        and row["head"]["repo"]["url"] == f"https://api.github.com/repos/{self.repo}"]
        if not associations:
            raise Blocked("CI run does not prove this PR and target base branch")
        tested: set[tuple[str, str, str]] = set()
        checkout_attempts = []
        # A failed-jobs rerun can execute only a gate, without another checkout.
        # Earlier attempts share this exact head and PR event, not a new retarget.
        for attempt in range(1, run["run_attempt"] + 1):
            logs = self.gh("run", "view", run_id, "--repo", self.repo,
                           "--attempt", str(attempt), "--log").stdout
            checkouts = re.findall(
                r"HEAD is now at ([0-9a-f]+) Merge ([0-9a-f]{40}) into ([0-9a-f]{40})(?:\r?\n|$)", logs)
            if checkouts:
                checkout_attempts.append(attempt)
                tested.update(checkouts)
        if not tested:
            raise Blocked("CI run has no tested PR merge checkout evidence")
        merges = []
        for short, head, base in sorted(tested):
            if head != pr["headRefOid"]:
                raise Blocked("CI tested merge has a stale PR head")
            commit = json.loads(self.gh("api", f"repos/{self.repo}/commits/{short}").stdout)
            if not commit["sha"].startswith(short) or [p["sha"] for p in commit["parents"]] != [base, head]:
                raise Blocked("CI tested merge does not combine the recorded base and head")
            self.run(["git", "fetch", "origin", base])
            self.run(["git", "merge-base", "--is-ancestor", base, f"origin/{pr['baseRefName']}"])
            merges.append({"commit": commit["sha"], "head": head, "base": base})
        run["checkout_attempts"] = checkout_attempts
        run["tested_merges"] = merges
        return run

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
                retarget = self.retarget()
                runs = {}
                for row in checks:
                    match = re.search(r"/actions/runs/(\d+)", row.get("detailsUrl", ""))
                    name = row.get("name") or row.get("context")
                    if name in required and not match:
                        raise Blocked(f"Required check {name} has no GitHub Actions PR run")
                    if not match or match[1] in runs:
                        continue
                    runs[match[1]] = self.verify_run(match[1], pr, retarget)
                self.save("ci-runs-verified", ci_runs=runs)
                self.save("checks-passed", checked_head=pr["headRefOid"], checked_retarget=retarget, checks=checks)
                print("successful checks: " + ", ".join(sorted(passed)), flush=True)
                return
            else:
                self.save("checks-pending", pending=sorted(set(pending) | missing), successful=sorted(passed))
            if time.monotonic() >= deadline:
                raise Blocked("Timed out waiting for exact-head CI")
            time.sleep(self.poll)

    def execute(self, *, preflight_only: bool = False) -> None:
        self.repo = json.loads(self.run(["gh", "repo", "view", "--json", "nameWithOwner"]).stdout)["nameWithOwner"]
        if self.data.get("repository", self.repo) != self.repo:
            raise Blocked("Receipt belongs to another repository")
        self.smoke = self.smoke or self.data.get("smoke_command")
        self.save("resolving", repository=self.repo, smoke_command=self.smoke)
        pr = self.view(wait=False)
        if pr.get("isCrossRepository"):
            raise Blocked("Fork retirement is not supported; no changes made")
        self.run(["git", "fetch", "origin", pr["baseRefName"]])
        from evallab.ship.preflight import inspect_preflight

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
        pr["files"] = self.data["pr"]["files"]
        if preflight_only:
            if pr["baseRefName"] != "main":
                raise Blocked("ship only delivers PRs targeting main")
            inspect_preflight(self, pr, preflight_only=True)
            return
        if pr["headRefOid"] != self.data["pr"]["headRefOid"]:
            raise Blocked("PR head moved; preserve this receipt and use a fresh --state-dir")
        if pr["baseRefName"] != "main":
            raise Blocked("ship only delivers PRs targeting main")
        inspect_preflight(self, pr)
        if pr["state"] != "MERGED":
            if pr["mergeable"] == "UNKNOWN":
                pr = self.view()
            self.guard(pr)
            pr["files"] = self.data["pr"]["files"]
            self.run(["git", "fetch", "origin", f"pull/{self.pr}/head"])
            self.checks(self.required(pr["baseRefName"]))
            self.gates(pr)
            self.guard(self.view())
            # A dirty/in-use tree must not lose even its remote branch.
            plan = inspect_cleanup(self, pr)
            self.save("cleanup-preflight", cleanup_plan=plan)
            latest = self.view()
            self.guard(latest)
            if (sorted(json.dumps(row, sort_keys=True) for row in latest.get("statusCheckRollup") or [])
                    != sorted(json.dumps(row, sort_keys=True) for row in self.data["checks"])):
                raise Blocked("Reported checks changed after CI verification; repeat ship")
            self.run(["git", "fetch", "origin", pr["baseRefName"]])
            base = self.run(["git", "rev-parse", f"origin/{pr['baseRefName']}"]).stdout.strip()
            if base != self.data["gates_inputs"]["base"]:
                raise Blocked("Gate base moved; repeat ship to refresh current-base gates without rebasing")
            if self.retarget() != self.data["checked_retarget"]:
                raise Blocked("PR base retargeted after CI verification")
            self.save("merge-requested", merge_attempted=True)
            self.gh("pr", "merge", str(self.pr), "--repo", self.repo, "--squash", "--delete-branch",
                    "--match-head-commit", pr["headRefOid"], check=False)
            pr = self.view()
        if pr["state"] != "MERGED" or not pr.get("mergeCommit", {}).get("oid"):
            from evallab.ship.preflight import workflow_paths, workflow_recovery

            message = "Remote PR is not MERGED with a merge commit"
            paths = workflow_paths(self.data["pr"])
            if paths:
                base = self.run(["git", "rev-parse", "origin/main"]).stdout.strip()
                message += (f". If main changed {paths[0]} after this branch point and the gh token has no "
                            "workflow scope, GitHub will refuse the squash merge although checks are green. "
                            + workflow_recovery(base))
            raise Blocked(message)
        commit = pr["mergeCommit"]["oid"]
        self.save("merged", merge_commit=commit)
        self.run(["git", "fetch", "origin", "main"])
        self.run(["git", "merge-base", "--is-ancestor", commit, "origin/main"])
        self.save("main-verified")
        if self.smoke and self.data.get("smoke_passed") != {"commit": commit, "command": self.smoke}:
            with self.checkout(commit, "smoke") as tree:
                self.run(["/bin/sh", "-c", self.smoke], cwd=tree, timeout=self.timeout)
            self.save("smoke-passed", smoke_passed={"commit": commit, "command": self.smoke})
        plan = inspect_cleanup(self, pr)
        self.save("cleanup-preflight", cleanup_plan=plan)
        retire(self, pr, plan)
        self.save("complete")


def run_command(args: argparse.Namespace, root: Path, **_: Any) -> int:
    if isinstance(args.pr, str):
        from evallab.ship.publish import run_publish

        return run_publish(args, root)
    if any(getattr(args, name, None) is not None
           for name in ("target", "title_file", "body_file", "needs_base", "reason")):
        print("ship blocked: publication arguments require open, comment, or check-push.", flush=True)
        return 1
    ship = Ship(args.repo_root or root, args.pr, args.state_dir, poll=args.poll,
                timeout=args.timeout, rerun=args.rerun_failed_once, smoke=args.smoke)
    preflight_only = getattr(args, "preflight_only", False)
    if preflight_only:
        ship.path = ship.directory / f"{ship.pr}-preflight.json"
    with ship.path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Blocked("Another ship process owns this PR") from exc
        # Read state under the lock, not before another writer finishes.
        ship.data = json.loads(ship.path.read_text()) if ship.path.exists() and not preflight_only else {}
        try:
            ship.execute(preflight_only=preflight_only)
        except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
            ship.save("blocked", error=str(exc))
            print(f"ship blocked: {exc}", flush=True)
            return 1
    return 0


def delivery_target(value: str) -> int | str:
    if value in {"open", "comment", "check-push"}:
        return value
    try:
        number = int(value)
        if number > 0:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("use a positive PR number, open, comment, or check-push")


def register_parser(commands: Any) -> None:
    parser = commands.add_parser("ship", help="Merge exact-head green CI and safely retire its worktree")
    parser.add_argument("pr", type=delivery_target, metavar="PR|open|comment|check-push")
    parser.add_argument("target", type=int, nargs="?", metavar="PR", help="PR for comment or check-push")
    parser.add_argument("--title-file", type=Path, help="Complete final PR title for open")
    parser.add_argument("--body-file", type=Path, help="Complete PR body for open, or later comment evidence")
    parser.add_argument("--needs-base", help="Required base commit SHA for an explicit same-patch exception")
    parser.add_argument("--reason", help="Reason for requiring that base commit")
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--state-dir", type=Path, default=Path.home() / ".local/state/evallab-wf/ship")
    parser.add_argument("--poll", type=float, default=15)
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--rerun-failed-once", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--smoke")
    parser.add_argument("--preflight-only", action="store_true", help="Check delivery hazards without waiting or merging")
    parser.set_defaults(func=run_command)
