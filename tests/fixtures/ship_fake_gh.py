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
    elif "headSha,event,headBranch,status,conclusion,jobs,url,attempt" in args:
        output = json.dumps({"headSha": state["pr"]["headRefOid"], "event": "pull_request",
                             "status": "completed", "conclusion": "success", "jobs": []})
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
