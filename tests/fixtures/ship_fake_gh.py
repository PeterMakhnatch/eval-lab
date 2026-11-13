#!/usr/bin/env python3
"""Network-free gh used with real disposable git repositories."""
import json
import os
import subprocess
import sys
from pathlib import Path

path = Path(os.environ["SHIP_FAKE_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
code = 0
output = ""
if args[:2] == ["repo", "view"]:
    output = json.dumps({"nameWithOwner": "test/lab"})
elif args[0] == "api":
    if "--slurp" in args:
        output = json.dumps([[{"filename": row["path"]} for row in state["pr"]["files"]]])
    elif "graphql" in args:
        output = json.dumps({"data": {"repository": {"pullRequest": {"timelineItems": {
            "nodes": [{"createdAt": state["retarget"]}] if state.get("retarget") else []
        }}}}})
    elif "/actions/runs/" in args[-1]:
        pr = state["pr"]
        repo = {"url": "https://api.github.com/repos/test/lab"}
        base = state.get("event_base", state.get("tested_base", pr["baseRefOid"]))
        output = json.dumps({
            "head_sha": pr["headRefOid"], "head_branch": pr["headRefName"],
            "event": "pull_request", "status": "completed", "conclusion": "success",
            "created_at": "2026-11-13T12:00:00Z", "run_attempt": state.get("attempt", 1),
            "pull_requests": [{"number": 42, "base": {"ref": "main", "sha": base, "repo": repo},
                               "head": {"sha": pr["headRefOid"], "repo": repo}}],
            **state.get("run_override", {})
        })
    elif "/commits/" in args[-1]:
        short = args[-1].rsplit("/", 1)[-1]
        output = json.dumps({**state["tested_commits"][short], **state.get("commit_override", {})})
    else:
        output = json.dumps({"contexts": ["quality-required", "typecheck-required"]})
elif args[:2] == ["pr", "view"]:
    state["views"] = state.get("views", 0) + 1
    if state.get("move") and state["views"] >= 3:
        state["pr"]["headRefOid"] = "b" * 40
    if state.get("pending") and state["views"] == 2:
        view = {**state["pr"], "statusCheckRollup": []}
    else:
        view = state["pr"]
    if state["views"] <= state.get("unknown_views", 0):
        # GitHub computes mergeability lazily after a push or a base change.
        view = {**view, "mergeable": "UNKNOWN"}
    output = json.dumps(view)
elif args[:2] == ["pr", "merge"]:
    if "--match-head-commit" not in args or "--admin" in args:
        raise RuntimeError("unguarded merge")
    root = state["root"]
    def git(*command):
        return subprocess.check_output(["git", "-C", root, *command], text=True).strip()
    tree = git("rev-parse", state["pr"]["headRefOid"] + "^{tree}")
    commit = git("commit-tree", tree, "-p", "origin/main", "-m", "squash delivery")
    git("push", "origin", commit + ":refs/heads/main")
    state["pr"]["state"] = "MERGED"
    state["pr"]["mergeCommit"] = {"oid": commit}
    state["merges"] = state.get("merges", 0) + 1
    # Model gh failing its local switch after a successful remote merge.
    code, output = 1, "main is already used by worktree"
elif args[:2] == ["run", "view"]:
    if "--log-failed" in args:
        output = "FAILED tests/test_consumer.py::test_boundary - AssertionError"
    elif "--log" in args:
        pr = state["pr"]
        head = state.get("tested_head", pr["headRefOid"])
        base = state.get("tested_base", pr["baseRefOid"])
        def git(*command):
            return subprocess.check_output(["git", "-C", state["root"], *command], text=True).strip()
        tree = git("rev-parse", head + "^{tree}")
        sha = git("commit-tree", tree, "-p", base, "-p", head, "-m", f"Merge {head} into {base}")
        state.setdefault("tested_commits", {})[sha[:9]] = {
            "sha": sha, "parents": [{"sha": base}, {"sha": head}]}
        attempt = args[args.index("--attempt") + 1]
        output = state.get("attempt_logs", {}).get(attempt, state.get(
            "run_log", f"lint\tCheckout\t2026-11-13T12:00:00Z HEAD is now at {sha[:9]} Merge {head} into {base}\n"))
    else:
        output = json.dumps({"status": "completed", "attempt": state.get("attempt", 1)})
elif args[:2] == ["run", "rerun"]:
    state["attempt"] = state.get("attempt", 1) + 1
    if state.get("rerun_pass"):
        for row in state["pr"]["statusCheckRollup"]:
            row["conclusion"] = "SUCCESS"
else:
    raise RuntimeError(f"unexpected fake gh call: {args}")
path.write_text(json.dumps(state))
print(output)
sys.exit(code)
