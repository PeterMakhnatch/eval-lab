"""Consumer-visible ship boundaries, with fake gh and real isolated git."""
from __future__ import annotations

import argparse
import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from evallab.ship import cleanup, run_command


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repo"
    root.mkdir()
    origin = tmp_path / "origin.git"
    def git(*args, cwd=root):
        return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()
    git("init", "--bare", str(origin))
    git("init", "-b", "main")
    monkeypatch.setattr(cleanup, "sys", SimpleNamespace(platform="darwin"))
    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Ship test")
    (root / "agents").mkdir()
    (root / "agents/CHECKS.md").write_text("| `quality-required` | lint |\n| `typecheck-required` | ty |\n")
    (root / "agents/ship-gates.toml").write_text("")
    (root / "consumer.txt").write_text("base\n")
    git("add", "agents", "consumer.txt")
    git("commit", "-m", "base")
    base = git("rev-parse", "HEAD")
    git("remote", "add", "origin", str(origin))
    git("push", "-u", "origin", "main")
    tree = tmp_path / "topic"
    git("worktree", "add", "-b", "topic", str(tree))
    (tree / "consumer.txt").write_text("change\n")
    git("commit", "-am", "change", cwd=tree)
    head = git("rev-parse", "HEAD", cwd=tree)
    git("push", "-u", "origin", "topic")
    git("update-ref", "refs/pull/42/head", head, cwd=origin)
    state = tmp_path / "github.json"
    state.write_text(json.dumps({"root": str(root), "pr": {
        "number": 42, "state": "OPEN", "headRefOid": head, "headRefName": "topic",
        "changedFiles": 1,
        "baseRefName": "main", "baseRefOid": base, "isDraft": False,
        "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN", "labels": [],
        "files": [{"path": "consumer.txt"}], "mergeCommit": None,
        "isCrossRepository": False, "statusCheckRollup": [
            {"name": name, "conclusion": "SUCCESS", "detailsUrl": "https://github.com/test/lab/actions/runs/123/job/456"}
            for name in ("quality-required", "typecheck-required")]
    }}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = Path(__file__).parent / "fixtures/ship_fake_gh.py"
    (bin_dir / "gh").write_text(f"#!{sys.executable}\n" + fake.read_text().split("\n", 1)[1])
    (bin_dir / "lsof").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "launchctl").write_text("#!/bin/sh\nprintf 'no references\\n'\n")
    for executable in bin_dir.iterdir():
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + ":" + str(Path(shutil.which("git")).parent) + ":/usr/bin:/bin")
    monkeypatch.setenv("SHIP_FAKE_STATE", str(state))
    args = argparse.Namespace(repo_root=root, pr=42, state_dir=tmp_path / "receipts", poll=0,
                              timeout=2, rerun_failed_once=True, smoke="test -f consumer.txt")
    def change(**values):
        content = json.loads(state.read_text())
        content.update(values)
        state.write_text(json.dumps(content))
    return args, root, tree, state, git, change


def test_merge_remote_success_despite_gh_exit_and_resume(delivery):
    args, root, tree, state, git, _ = delivery
    assert run_command(args, root) == 0
    receipt = json.loads((args.state_dir / "42.json").read_text())
    assert receipt["step"] == "complete"
    assert receipt["smoke_passed"]["commit"] == receipt["merge_commit"]
    assert not tree.exists()
    assert not git("ls-remote", "--heads", "origin", "topic")
    assert "refs/heads/topic" not in git("show-ref")
    assert run_command(args, root) == 0
    assert json.loads(state.read_text())["merges"] == 1


def test_head_movement_never_merges(delivery):
    args, root, tree, state, _, change = delivery
    change(move=True)
    assert run_command(args, root) == 1
    receipt = json.loads((args.state_dir / "42.json").read_text())
    assert "moved" in receipt["error"]
    assert not json.loads(state.read_text()).get("merges")
    assert tree.exists()


def test_failure_names_test_and_reruns_only_once(delivery, capsys):
    args, root, tree, state, _, change = delivery
    pr = json.loads(state.read_text())["pr"]
    pr["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    change(pr=pr)
    assert run_command(args, root) == 1
    assert "tests/test_consumer.py::test_boundary" in capsys.readouterr().out
    assert run_command(args, root) == 1
    calls = json.loads(state.read_text())["calls"]
    assert sum(call[:2] == ["run", "rerun"] for call in calls) == 1
    assert not json.loads(state.read_text()).get("merges")
    assert tree.exists()


def test_rerun_can_finish_between_polls(delivery):
    args, root, _, state, _, change = delivery
    pr = json.loads(state.read_text())["pr"]
    pr["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    change(pr=pr, rerun_pass=True)
    assert run_command(args, root) == 0
    assert json.loads(state.read_text())["merges"] == 1


def test_dirty_worktree_deletes_nothing(delivery):
    args, root, tree, state, git, _ = delivery
    (tree / "consumer.txt").write_text("unique uncommitted work\n")
    assert run_command(args, root) == 1
    assert "dirty worktree" in json.loads((args.state_dir / "42.json").read_text())["error"]
    assert (tree / "consumer.txt").read_text() == "unique uncommitted work\n"
    assert git("ls-remote", "--heads", "origin", "topic")
    assert "refs/heads/topic" in git("show-ref")
    assert not json.loads(state.read_text()).get("merges")


@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_exact_head_path_gate(delivery, exit_code):
    args, root, tree, state, git, change = delivery
    # Put the trusted policy on the base, then rebase the topic onto that base.
    config = ('[[gate]]\nname = "consumer"\npaths = ["consumer.*"]\n'
              f'command = ["/bin/sh", "-c", "test $(git rev-parse HEAD) = $EVALLAB_SHIP_HEAD && exit {exit_code}"]\n')
    (root / "agents/ship-gates.toml").write_text(config)
    git("commit", "-am", "gate policy")
    git("push", "origin", "main")
    git("rebase", "main", cwd=tree)
    head = git("rev-parse", "HEAD", cwd=tree)
    git("push", "--force-with-lease", "origin", "topic")
    git("update-ref", "refs/pull/42/head", head, cwd=root.parent / "origin.git")
    pr = json.loads(state.read_text())["pr"]
    pr.update(headRefOid=head, baseRefOid=git("rev-parse", "main"))
    change(pr=pr)
    # A candidate cannot weaken or remove the base-owned gate.
    (tree / "agents/ship-gates.toml").write_text(
        '[[gate]]\nname = "consumer"\npaths = ["unrelated/**"]\n'
        'command = ["/bin/sh", "-c", "exit 0"]\n'
    )
    git("commit", "-am", "attempt to bypass gate", cwd=tree)
    head = git("rev-parse", "HEAD", cwd=tree)
    git("push", "--force-with-lease", "origin", "topic")
    git("update-ref", "refs/pull/42/head", head, cwd=root.parent / "origin.git")
    pr.update(headRefOid=head, changedFiles=2)
    pr["files"].append({"path": "agents/ship-gates.toml"})
    change(pr=pr)
    assert run_command(args, root) == (0 if exit_code == 0 else 1)
    if exit_code:
        assert not json.loads(state.read_text()).get("merges")
        assert tree.exists()


@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_new_gate_runs_before_its_registration_merges(delivery, exit_code):
    args, root, tree, state, git, change = delivery
    config = ('[[gate]]\nname = "consumer"\npaths = ["consumer.*"]\n'
              f'command = ["/bin/sh", "-c", "test $(git rev-parse HEAD) = $EVALLAB_SHIP_HEAD && exit {exit_code}"]\n')
    (tree / "agents/ship-gates.toml").write_text(config)
    git("commit", "-am", "register gate", cwd=tree)
    head = git("rev-parse", "HEAD", cwd=tree)
    git("push", "origin", "topic")
    git("update-ref", "refs/pull/42/head", head, cwd=root.parent / "origin.git")
    pr = json.loads(state.read_text())["pr"]
    pr.update(headRefOid=head, changedFiles=2)
    pr["files"].append({"path": "agents/ship-gates.toml"})
    change(pr=pr)
    assert run_command(args, root) == (0 if exit_code == 0 else 1)
    receipt = json.loads((args.state_dir / "42.json").read_text())
    if exit_code:
        assert not json.loads(state.read_text()).get("merges")
        assert tree.exists()
    else:
        assert receipt["gates"] == ["consumer"]


def test_pending_waits_for_required_checks(delivery):
    args, root, _, state, _, change = delivery
    change(pending=True)
    assert run_command(args, root) == 0
    assert json.loads(state.read_text())["views"] >= 4


@pytest.mark.parametrize("hazard", ["ignored", "unpushed", "process", "launchd"])
def test_cleanup_preserves_unsafe_tree(delivery, hazard):
    args, root, tree, state, git, _ = delivery
    if hazard == "ignored":
        exclude = root / git("rev-parse", "--git-common-dir") / "info/exclude"
        exclude.write_text("private/\n")
        (tree / "private").mkdir()
        (tree / "private/evidence").write_text("unique evidence")
    elif hazard == "unpushed":
        (tree / "consumer.txt").write_text("unpushed\n")
        git("commit", "-am", "unpushed", cwd=tree)
    else:
        binary = state.parent / "bin" / ("lsof" if hazard == "process" else "launchctl")
        binary.write_text(f"#!/bin/sh\nprintf '%s\\n' '{tree}'\nexit 0\n")
    assert run_command(args, root) == 1
    assert tree.exists()
    assert git("ls-remote", "--heads", "origin", "topic")
    assert not json.loads(state.read_text()).get("merges")


def test_clean_ignored_dependency_cache_can_retire(delivery):
    args, root, tree, _, git, _ = delivery
    exclude = root / git("rev-parse", "--git-common-dir") / "info/exclude"
    exclude.write_text(".venv/\n")
    (tree / ".venv").mkdir()
    (tree / ".venv/pyvenv.cfg").write_text("rebuildable dependency cache")
    assert run_command(args, root) == 0
    assert not tree.exists()


def test_nested_tool_cache_can_retire(delivery):
    args, root, tree, _, git, _ = delivery
    exclude = root / git("rev-parse", "--git-common-dir") / "info/exclude"
    exclude.write_text(".ruff_cache/\n")
    (tree / "library/benchmarks/.ruff_cache/0.17.2").mkdir(parents=True)
    (tree / "library/benchmarks/.ruff_cache/0.17.2/123").write_text("lint cache")
    assert run_command(args, root) == 0
    assert not tree.exists()


def test_unknown_mergeability_is_asked_again_not_treated_as_conflict(delivery):
    args, root, _, state, _, change = delivery
    change(unknown_views=3)
    assert run_command(args, root) == 0
    assert json.loads(state.read_text())["merges"]


@pytest.mark.parametrize("pause", ["draft", "conflict", "hold", "neutral", "missing"])
def test_pauses_and_incomplete_checks_cannot_merge(delivery, pause):
    args, root, tree, state, _, change = delivery
    pr = json.loads(state.read_text())["pr"]
    if pause == "draft":
        pr["isDraft"] = True
    elif pause == "conflict":
        pr["mergeable"] = "CONFLICTING"
    elif pause == "hold":
        pr["labels"] = [{"name": "operator-hold"}]
    elif pause == "neutral":
        pr["statusCheckRollup"][0]["conclusion"] = "NEUTRAL"
    else:
        pr["statusCheckRollup"] = []
    change(pr=pr)
    args.timeout = 0
    args.rerun_failed_once = False
    assert run_command(args, root) == 1
    assert tree.exists()
    assert not json.loads(state.read_text()).get("merges")


def test_failed_smoke_resume_preserves_dirty_tree_and_never_remerges(delivery):
    args, root, tree, state, git, _ = delivery
    args.smoke = "exit 1"
    assert run_command(args, root) == 1
    assert json.loads(state.read_text())["merges"] == 1
    assert tree.exists()
    (tree / "consumer.txt").write_text("uncommitted after merge\n")
    args.smoke = "test -f consumer.txt"
    assert run_command(args, root) == 1
    assert "dirty worktree" in json.loads((args.state_dir / "42.json").read_text())["error"]
    assert git("ls-remote", "--heads", "origin", "topic")
    assert "refs/heads/topic" in git("show-ref")
    (tree / "consumer.txt").write_text("change\n")
    assert run_command(args, root) == 0
    assert json.loads(state.read_text())["merges"] == 1


def test_interrupted_merge_intent_reconciles_open_pr_before_delivery(delivery):
    args, root, _, state, _, _ = delivery
    args.state_dir.mkdir()
    snapshot = json.loads(state.read_text())["pr"]
    (args.state_dir / "42.json").write_text(json.dumps({"pr": snapshot, "merge_attempted": True}))
    assert run_command(args, root) == 0
    assert json.loads(state.read_text())["merges"] == 1


def test_killed_smoke_resumes_its_checkout_without_remerging(delivery):
    args, root, _, state, git, _ = delivery
    ready = state.parent / "ready.fifo"
    os.mkfifo(ready)
    descriptor = os.open(ready, os.O_RDONLY | os.O_NONBLOCK)
    command = [sys.executable, "-c", "from evallab.entry import main; main()", "ship", "42",
               "--repo-root", str(root), "--state-dir", str(args.state_dir), "--poll", "0",
               "--smoke", f"printf ready > {shlex.quote(str(ready))}; sleep 60"]
    with (state.parent / "interrupted.log").open("w") as stream:
        process = subprocess.Popen(command, cwd=root, stdout=stream, stderr=stream, start_new_session=True)
        try:
            assert select.select([descriptor], [], [], 120)[0], "smoke did not reach its readiness boundary"
            assert os.read(descriptor, 5) == b"ready"
        finally:
            os.close(descriptor)
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=30)
    checkout = args.state_dir / "42-smoke-worktree"
    assert checkout.exists()
    assert json.loads(state.read_text())["merges"] == 1
    assert run_command(args, root) == 0
    assert json.loads(state.read_text())["merges"] == 1
    assert not checkout.exists()
    assert "detached" not in git("worktree", "list", "--porcelain")
