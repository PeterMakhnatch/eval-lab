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

from evallab.ship import Ship, cleanup, run_command


@pytest.fixture
def delivery(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-11-13T00:00:00Z")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-11-13T00:00:00Z")
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


def test_gate_timeout_blocks_merge_and_preserves_topic(delivery):
    args, root, tree, state, git, change = delivery
    args.timeout = 0.01
    command = [sys.executable, "-c", "import time; time.sleep(10)"]
    (tree / "agents/ship-gates.toml").write_text(
        '[[gate]]\nname = "consumer"\npaths = ["consumer.*"]\n'
        f"command = {json.dumps(command)}\n"
    )
    git("commit", "-am", "register bounded verification", cwd=tree)
    head = git("rev-parse", "HEAD", cwd=tree)
    git("push", "origin", "topic")
    git("update-ref", "refs/pull/42/head", head, cwd=root.parent / "origin.git")
    pr = json.loads(state.read_text())["pr"]
    pr.update(headRefOid=head, changedFiles=2)
    pr["files"].append({"path": "agents/ship-gates.toml"})
    change(pr=pr)
    assert run_command(args, root) == 1
    receipt = json.loads((args.state_dir / "42.json").read_text())
    assert "timed out" in receipt["error"]
    assert not json.loads(state.read_text()).get("merges")
    assert tree.exists()
    assert git("ls-remote", "--heads", "origin", "topic")


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


@pytest.mark.parametrize("tested_current_base", [False, True])
def test_main_advancement_needs_no_rebase_or_new_ci(delivery, tested_current_base):
    args, root, tree, state, git, change = delivery
    pr = json.loads(state.read_text())["pr"]
    head, old_base = pr["headRefOid"], pr["baseRefOid"]
    (root / "other.txt").write_text("main moves independently\n")
    git("add", "other.txt")
    git("commit", "-m", "advance main")
    git("push", "origin", "main")
    current = git("rev-parse", "main")
    pr["baseRefOid"] = current
    change(pr=pr, tested_base=current if tested_current_base else old_base, event_base=old_base)
    assert run_command(args, root) == 0
    receipt = json.loads((args.state_dir / "42.json").read_text())
    assert receipt["checked_head"] == head
    assert receipt["ci_runs"]["123"]["tested_merges"][0]["base"] == (
        current if tested_current_base else old_base)
    assert receipt["gates_inputs"] == {"head": head, "base": current}
    assert not tree.exists()
    assert not any(call[:2] == ["run", "rerun"] for call in json.loads(state.read_text())["calls"])


@pytest.mark.parametrize("hazard", [
    "head", "event", "branch", "base", "association", "retarget", "merge_head", "merge_missing", "merge_base",
    "merge_parents",
])
def test_stale_or_wrong_combined_evidence_never_merges(delivery, hazard):
    args, root, tree, state, git, change = delivery
    pr = json.loads(state.read_text())["pr"]
    if hazard == "head":
        change(run_override={"head_sha": "b" * 40})
    elif hazard == "event":
        change(run_override={"event": "workflow_dispatch"})
    elif hazard == "branch":
        change(run_override={"head_branch": "other-topic"})
    elif hazard in {"base", "association"}:
        repo = {"url": "https://api.github.com/repos/test/lab"}
        change(run_override={"pull_requests": [{
            "number": 43 if hazard == "association" else 42,
            "head": {"sha": pr["headRefOid"], "repo": repo},
            "base": {"ref": "stack" if hazard == "base" else "main", "sha": pr["baseRefOid"], "repo": repo},
        }]})
    elif hazard == "retarget":
        # Retargeting away and back invalidates an old run, including a rerun.
        change(retarget="2026-11-13T12:01:00Z", attempt=2)
    elif hazard == "merge_head":
        change(tested_head=pr["baseRefOid"])
    elif hazard == "merge_missing":
        change(run_log="no merge checkout evidence\n")
    elif hazard == "merge_parents":
        change(commit_override={"parents": [{"sha": pr["headRefOid"]}, {"sha": pr["baseRefOid"]}]})
    else:
        git("checkout", "-b", "other-base")
        git("commit", "--allow-empty", "-m", "unrelated base advancement")
        base = git("rev-parse", "HEAD")
        git("push", "origin", "other-base")
        change(tested_base=base)
    assert run_command(args, root) == 1
    assert not json.loads(state.read_text()).get("merges")
    assert tree.exists()


def test_base_retarget_during_delivery_never_merges(delivery, monkeypatch):
    args, root, tree, state, _, _ = delivery
    original = Ship.view
    views = []
    def view(ship, **kwargs):
        pr = original(ship, **kwargs)
        views.append(pr)
        if len(views) >= 2:
            pr["baseRefName"] = "stack"
        return pr
    monkeypatch.setattr(Ship, "view", view)
    assert run_command(args, root) == 1
    assert "baseRefName moved" in json.loads((args.state_dir / "42.json").read_text())["error"]
    assert not json.loads(state.read_text()).get("merges")
    assert tree.exists()


@pytest.mark.parametrize("block_changed_base", [False, True])
def test_gate_restart_reuses_only_unchanged_current_base(delivery, block_changed_base):
    args, root, tree, state, git, _ = delivery
    counter = state.parent / "gate-bases.txt"
    command = [sys.executable, "-c",
               f"import os, sys; from pathlib import Path; p=Path({str(counter)!r}); "
               "previous=p.read_text().splitlines() if p.exists() else []; "
               "base=os.environ['EVALLAB_SHIP_BASE']; p.open('a').write(base+'\\n'); "
               f"sys.exit(int(bool(previous) and previous[0] != base and {block_changed_base!r}))"]
    (root / "agents/ship-gates.toml").write_text(
        '[[gate]]\nname = "consumer"\npaths = ["consumer.*"]\n'
        f"command = {json.dumps(command)}\n")
    git("commit", "-am", "current base gate")
    git("push", "origin", "main")
    first_base = git("rev-parse", "main")
    blocker = tree / "untracked.txt"
    blocker.write_text("preserve work\n")
    assert run_command(args, root) == 1
    assert counter.read_text().splitlines() == [first_base]
    assert run_command(args, root) == 1
    assert counter.read_text().splitlines() == [first_base]
    git("commit", "--allow-empty", "-m", "new replay base")
    git("push", "origin", "main")
    current = git("rev-parse", "main")
    blocker.unlink()
    assert run_command(args, root) == (1 if block_changed_base else 0)
    assert counter.read_text().splitlines() == [first_base, current]
    assert json.loads(state.read_text()).get("merges", 0) == (0 if block_changed_base else 1)
    assert tree.exists() == block_changed_base


def test_base_movement_after_gate_blocks_then_recovers_without_rebase(delivery, monkeypatch):
    args, root, tree, state, git, _ = delivery
    original = Ship.gates
    moved = []
    def gates(ship, pr):
        original(ship, pr)
        if not moved:
            git("commit", "--allow-empty", "-m", "main advances during gate")
            git("push", "origin", "main")
            moved.append(True)
    monkeypatch.setattr(Ship, "gates", gates)
    assert run_command(args, root) == 1
    assert "Gate base moved" in json.loads((args.state_dir / "42.json").read_text())["error"]
    assert not json.loads(state.read_text()).get("merges")
    assert tree.exists()
    assert run_command(args, root) == 0
    assert json.loads(state.read_text())["merges"] == 1


@pytest.mark.parametrize("hazard", ["retarget", "pending"])
def test_new_remote_evidence_after_gate_blocks_merge(delivery, monkeypatch, hazard):
    args, root, tree, state, _, change = delivery
    original = Ship.gates
    def gates(ship, pr):
        original(ship, pr)
        if hazard == "retarget":
            change(retarget="2026-11-13T12:01:00Z")
        else:
            current = json.loads(state.read_text())["pr"]
            current["statusCheckRollup"].append({"name": "new-check", "status": "IN_PROGRESS"})
            change(pr=current)
    monkeypatch.setattr(Ship, "gates", gates)
    assert run_command(args, root) == 1
    assert not json.loads(state.read_text()).get("merges")
    assert tree.exists()


def test_gate_only_ci_rerun_keeps_exact_event_checkout_evidence(delivery):
    args, root, _, state, _, change = delivery
    change(attempt=2, attempt_logs={"2": "quality-required succeeded; no checkout job reran\n"})
    assert run_command(args, root) == 0
    receipt = json.loads((args.state_dir / "42.json").read_text())
    run = receipt["ci_runs"]["123"]
    assert run["checkout_attempts"] == [1]
    assert run["tested_merges"][0]["head"] == receipt["pr"]["headRefOid"]
    assert json.loads(state.read_text())["merges"] == 1


def test_fresh_ci_after_retarget_can_merge(delivery):
    args, root, _, state, _, change = delivery
    change(retarget="2026-11-13T12:01:00Z", run_override={"created_at": "2026-11-13T12:01:01Z"})
    assert run_command(args, root) == 0
    assert json.loads(state.read_text())["merges"] == 1


def test_preflight_only_passes_without_wait_or_merge(delivery, monkeypatch):
    args, root, tree, state, git, change = delivery
    (root / "unrelated.txt").write_text("unrelated delivery commit\n")
    git("add", "unrelated.txt")
    git("commit", "-m", "unrelated local commit")
    args.preflight_only = True
    change(unknown_views=100, pending=True)
    monkeypatch.setattr("evallab.ship.time", SimpleNamespace(
        sleep=lambda _: pytest.fail("preflight must not wait")))
    assert run_command(args, root) == 0
    remote = json.loads(state.read_text())
    assert remote["views"] == 1
    assert not any(call[0] == "run" or call[:2] == ["pr", "merge"] for call in remote["calls"])
    assert tree.exists()
    assert json.loads((args.state_dir / "42-preflight.json").read_text())["step"] == "delivery-preflight-passed"
    assert not (args.state_dir / "42.json").exists()


@pytest.mark.parametrize("preflight_only", [False, True])
def test_preflight_refuses_cwd_before_ci_with_exact_fix(delivery, preflight_only):
    args, root, tree, state, _, _ = delivery
    args.preflight_only = preflight_only
    (state.parent / "bin/lsof").write_text(
        f"#!/bin/sh\nprintf 'COMMAND PID USER FD TYPE NAME\\npython 4321 owner cwd DIR {tree}\\n'\n")
    assert run_command(args, root) == 1
    receipt = args.state_dir / ("42-preflight.json" if preflight_only else "42.json")
    error = json.loads(receipt.read_text())["error"]
    assert error == (
        f"Delivery preflight refused: process 4321 (python) uses {tree}; start ship outside the topic: "
        f"cd {args.state_dir.parent} && uv run --directory {root} --no-sync evallab ship 42 "
        f"--state-dir {args.state_dir} --no-rerun-failed-once --smoke 'test -f consumer.txt'"
        + (" --preflight-only" if preflight_only else "")
    )
    assert json.loads(state.read_text())["views"] == 1
    assert not json.loads(state.read_text()).get("merges")


@pytest.mark.parametrize(("path", "preflight_only"), [
    ("src/evallab/ship/stale.py", False), ("agents/ship-gates.toml", True),
])
def test_preflight_refuses_stale_delivery_inputs(delivery, path, preflight_only):
    args, root, tree, state, git, _ = delivery
    args.preflight_only = preflight_only
    source = root / path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("stale delivery input\n")
    git("add", path)
    git("commit", "-m", "local stale input")
    assert run_command(args, root) == 1
    receipt = args.state_dir / ("42-preflight.json" if preflight_only else "42.json")
    error = json.loads(receipt.read_text())["error"]
    prefix = f"Delivery preflight refused: delivery inputs differ from origin/main: {path}; use a fresh delivery checkout: "
    repair = (
        f"mkdir -p {args.state_dir.parent} && "
        f"delivery=$(mktemp -d {args.state_dir.parent}/delivery.XXXXXX) && "
        f'git -C {root} worktree add --detach "$delivery" origin/main && '
        'uv sync --directory "$delivery" --locked --no-group observability --group benchmarks --group inspect && '
        f'cd {args.state_dir.parent} && uv run --directory "$delivery" --no-sync '
        f"evallab ship 42 --state-dir {args.state_dir} --no-rerun-failed-once --smoke 'test -f consumer.txt'"
    )
    if preflight_only:
        repair += " --preflight-only"
    assert error == prefix + repair
    # Execute the exact repair through setup. The final ship is preflight-only
    # to prove that the repaired checkout passes without merging the fixture PR.
    setup = repair.split(" && uv sync", 1)[0]
    subprocess.run(["/bin/sh", "-c", setup], check=True, cwd=state.parent)
    fresh = next(args.state_dir.parent.glob("delivery.*"))
    args.repo_root = fresh
    args.preflight_only = True
    assert run_command(args, fresh) == 0
    assert tree.exists()
    assert not json.loads(state.read_text()).get("merges")


@pytest.mark.parametrize("name", ["private/evidence", "tools/mimoagent-harbor/.venv/local-source.py"])
def test_preflight_preserves_ignored_bytes_with_executable_fix(delivery, name):
    args, root, tree, state, git, _ = delivery
    exclude = root / git("rev-parse", "--git-common-dir") / "info/exclude"
    exclude.write_text("private/\ntools/mimoagent-harbor/.venv/\n")
    source = tree / name
    source.parent.mkdir(parents=True)
    source.write_text("unique evidence\n")
    move_path = "tools/mimoagent-harbor/.venv" if name.startswith("tools/") else name
    if name.startswith("tools/"):
        (source.parent / "second.py").write_text("second unique source\n")
        (source.parent / "__pycache__").mkdir()
        (source.parent / "__pycache__/cache.pyc").write_bytes(b"cache")
    assert run_command(args, root) == 1
    error = json.loads((args.state_dir / "42.json").read_text())["error"]
    known = (" (includes locked tool environment tools/mimoagent-harbor/.venv; preserve unverified local bytes)"
             if name.startswith("tools/") else "")
    names = f"{name}, tools/mimoagent-harbor/.venv/second.py" if name.startswith("tools/") else name
    prefix = f"Delivery preflight refused: ignored files in {tree}: {names}{known}; preserve them: "
    repair = (
        f"backup=$(mktemp -d {args.state_dir}/preserved.XXXXXX) && "
        f'mkdir -p "$backup"/{Path(move_path).parent} && mv {tree / move_path} "$backup"/{move_path}'
    )
    assert error == prefix + repair
    subprocess.run(["/bin/sh", "-c", repair], check=True)
    preserved = next(args.state_dir.glob("preserved.*")) / name
    assert preserved.read_text() == "unique evidence\n"
    if name.startswith("tools/"):
        assert (preserved.parent / "second.py").read_text() == "second unique source\n"
        assert (preserved.parent / "__pycache__/cache.pyc").read_bytes() == b"cache"
    args.preflight_only = True
    assert run_command(args, root) == 0
    assert not json.loads(state.read_text()).get("merges")


@pytest.mark.parametrize("location", ["cwd", "root", "receipts"])
def test_preflight_refuses_own_topic_paths(delivery, monkeypatch, location):
    args, root, tree, state, _, _ = delivery
    monkeypatch.setenv("HOME", str(state.parent / "home"))
    expected_root = root
    expected_state = args.state_dir
    if location == "cwd":
        monkeypatch.chdir(tree)
        label = "ship working directory"
    elif location == "root":
        args.repo_root = tree
        expected_root = tree
        label = "delivery checkout"
    else:
        args.state_dir = tree / "receipts"
        expected_state = Path.home() / ".local/state/evallab-wf/ship"
        label = "receipt directory"
    assert run_command(args, root) == 1
    error = json.loads((args.state_dir / "42.json").read_text())["error"]
    assert error == (
        f"Delivery preflight refused: {label} uses {tree}; start ship outside the topic: "
        f"mkdir -p {expected_state.parent} && "
        f"delivery=$(mktemp -d {expected_state.parent}/delivery.XXXXXX) && "
        f'git -C {expected_root} worktree add --detach "$delivery" origin/main && '
        'uv sync --directory "$delivery" --locked --no-group observability --group benchmarks --group inspect && '
        f'cd {expected_state.parent} && uv run --directory "$delivery" --no-sync '
        f"evallab ship 42 --state-dir {expected_state} --no-rerun-failed-once --smoke 'test -f consumer.txt'"
    )
    assert json.loads(state.read_text())["views"] == 1
    assert not json.loads(state.read_text()).get("merges")


def test_preflight_does_not_freeze_head_or_replace_delivery_receipt(delivery):
    args, root, _, state, _, change = delivery
    args.state_dir.mkdir()
    committed = args.state_dir / "42.json"
    committed.write_text(json.dumps({"step": "complete", "merge_commit": "a" * 40}))
    args.preflight_only = True
    assert run_command(args, root) == 0
    pr = json.loads(state.read_text())["pr"]
    pr["headRefOid"] = "b" * 40
    change(pr=pr)
    assert run_command(args, root) == 0
    assert json.loads(committed.read_text()) == {"step": "complete", "merge_commit": "a" * 40}


def publish_args(*arguments):
    from evallab.ship import register_parser

    parser = argparse.ArgumentParser()
    register_parser(parser.add_subparsers())
    return parser.parse_args(["ship", *arguments])


def publication_files(state):
    title = state.parent / "title.txt"
    body = state.parent / "body.md"
    title.write_text("fix: preserve complete delivery evidence\n")
    body.write_text("## Change\nPreserve the original PR text and add later evidence as comments.\n\n## Proof\nThe focused delivery cases pass.\n")
    return title, body


def test_publication_opens_once_and_comments_without_edit(delivery, capsys, monkeypatch):
    _, root, tree, state, _, change = delivery
    monkeypatch.setenv("HOME", str(state.parent / "home"))
    title, body = publication_files(state)
    args = publish_args("open", "--title-file", str(title), "--body-file", str(body))
    change(open_prs=[])
    for content, message in [
        (" \n", "Publication refused: empty body; write the complete text in --body-file."),
        ("## Summary\n## Proof\n", "Publication refused: placeholder body; replace every placeholder in --body-file with complete PR details."),
        ("## Summary\n- [ ] TODO\n<!-- complete later -->", "Publication refused: placeholder body; replace every placeholder in --body-file with complete PR details."),
        ("A real summary.\n\n## Proof\nTBD", "Publication refused: placeholder body; replace every placeholder in --body-file with complete PR details."),
    ]:
        body.write_text(content)
        assert run_command(args, tree) == 1
        assert capsys.readouterr().out.strip() == message
    body.write_text("Remove obsolete TODO checks.\n\nProof: the delivery cases pass.\n")
    assert run_command(args, tree) == 0
    original = json.loads(state.read_text())["created"]
    capsys.readouterr()
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Publication refused: PR 42 already exists; add evidence with evallab ship comment 42 "
        "--body-file <file>. Do not edit its title or body."
    )
    remote = json.loads(state.read_text())["pr"]
    remote["state"] = "MERGED"
    change(pr=remote)
    body.write_text("Merged revision CLI smoke passes.\n")
    assert run_command(publish_args("comment", "42", "--body-file", str(body)), root) == 0
    result = json.loads(state.read_text())
    assert result["created"] == original
    assert result["comments"] == [{"pr": "42", "body": "Merged revision CLI smoke passes.\n"}]
    assert not any(call[:2] == ["pr", "edit"] for call in result["calls"])
    assert not args.state_dir.exists()


def test_publication_refuses_unpushed_head(delivery, capsys):
    _, _, tree, state, git, change = delivery
    title, body = publication_files(state)
    args = publish_args("open", "--title-file", str(title), "--body-file", str(body))
    change(open_prs=[])
    (tree / "consumer.txt").write_text("needed commit\n")
    git("commit", "-am", "needed fix", cwd=tree)
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Publication refused: topic HEAD is not pushed; run make prepush, then git push -u origin topic."
    )
    assert not json.loads(state.read_text()).get("created")
    git("push", "origin", "topic", cwd=tree)
    assert run_command(args, tree) == 0


def test_check_push_refuses_unchanged_patch_and_accepts_needed_change(delivery, capsys):
    _, root, tree, state, git, _ = delivery
    args = publish_args("check-push", "42")
    remote = json.loads(state.read_text())["pr"]["headRefOid"]
    message = (f"Push refused: same patch as PR head {remote}; main advancing does not require a new head "
               "(ship accepts it). Push only for a real conflict or a needed commit: add --needs-base <sha> with a reason.")
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == message
    (root / "unrelated.txt").write_text("main advances without overlap\n")
    git("add", "unrelated.txt")
    git("commit", "-m", "main advancement")
    git("push", "origin", "main")
    base = git("rev-parse", "HEAD")
    git("rebase", "main", cwd=tree)
    assert git("rev-parse", "HEAD", cwd=tree) != remote
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == message
    args.needs_base = "not-a-sha"
    args.reason = "The change needs the new base API."
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Push refused: --needs-base must name a commit SHA reachable from HEAD and origin/main."
    )
    args.needs_base = base
    assert run_command(args, tree) == 0
    assert capsys.readouterr().out.strip() == f"Push check passed: explicit base dependency {base}; reason: {args.reason}"
    (tree / "consumer.txt").write_text("needed behavior fix\n")
    git("commit", "-am", "real fix", cwd=tree)
    assert run_command(publish_args("check-push", "42"), tree) == 0
    assert capsys.readouterr().out.strip() == "Push check passed: the patch differs from the current PR head."
    assert not json.loads(state.read_text()).get("merges")


def test_check_push_discovers_no_pr_and_refuses_ambiguous_prs(delivery, capsys):
    _, _, tree, state, _, change = delivery
    args = publish_args("check-push")
    change(open_prs=[])
    assert run_command(args, tree) == 0
    assert capsys.readouterr().out.strip() == "Push check passed: no open PR for this topic branch."
    pr = json.loads(state.read_text())["pr"]
    change(open_prs=[pr, {**pr, "number": 43}])
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == "Push refused: multiple open PRs match this branch; pass the intended PR number."
    change(open_prs=[{**pr, "isCrossRepository": True}])
    assert run_command(args, tree) == 0
    assert capsys.readouterr().out.strip() == "Push check passed: no open PR for this topic branch."


def test_publication_rejects_invalid_mode_flags_before_io(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cases = [
        ("open", "42"), ("comment",), ("check-push", "--needs-base", "abcdef0"),
        ("check-push", "--needs-base", "abcdef0", "--reason", "   "),
        ("check-push", "--reason", "   "), ("comment", "0"),
    ]
    for words in cases:
        assert run_command(publish_args(*words), tmp_path) == 1
        assert capsys.readouterr().out.strip() == (
            "Publication refused: use ship open --title-file <file> --body-file <file>, "
            "ship comment <PR> --body-file <file>, or ship check-push [PR] [--needs-base <sha> --reason <text>]."
        )
    assert run_command(publish_args("42", "43"), tmp_path) == 1
    assert capsys.readouterr().out.strip() == "ship blocked: publication arguments require open, comment, or check-push."
    assert not (tmp_path / "home").exists()


def test_publication_file_and_delivery_flag_refusals(delivery, capsys):
    _, _, tree, state, _, _ = delivery
    title, body = publication_files(state)
    assert run_command(publish_args("open"), tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Publication refused: --title-file is required; write the final PR title to a UTF-8 file."
    )
    args = publish_args("open", "--title-file", str(title), "--body-file", str(body))
    for content in ("", "TODO", "first line\nsecond line\n"):
        title.write_text(content)
        assert run_command(args, tree) == 1
        assert capsys.readouterr().out.strip() == (
            "Publication refused: title is empty, multiline, or a placeholder; write the final title in --title-file."
        )
    title.write_text("fix: complete title")
    assert run_command(publish_args("open", "--title-file", str(title)), tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Publication refused: --body-file is required; write the complete text to a UTF-8 file."
    )
    body.write_text(" \n")
    assert run_command(publish_args("comment", "42", "--body-file", str(body)), tree) == 1
    assert capsys.readouterr().out.strip() == "Publication refused: empty body; write the complete text in --body-file."
    for flag in (("--preflight-only",), ("--smoke", "true")):
        assert run_command(publish_args("check-push", *flag), tree) == 1
        assert capsys.readouterr().out.strip() == (
            "Publication refused: --preflight-only and --smoke apply only to numeric PR delivery."
        )
    assert not json.loads(state.read_text()).get("created")
    assert not json.loads(state.read_text()).get("comments")


def test_check_push_refuses_wrong_pr_and_stale_fetched_head(delivery, capsys):
    _, _, tree, state, _, change = delivery
    args = publish_args("check-push", "42")
    original = json.loads(state.read_text())["pr"]
    for wrong in ({"state": "MERGED"}, {"baseRefName": "stack"},
                  {"headRefName": "other"}, {"isCrossRepository": True}):
        change(pr={**original, **wrong})
        assert run_command(args, tree) == 1
        assert capsys.readouterr().out.strip() == (
            "Push refused: PR must be open, target main, and match this origin topic branch."
        )
    change(pr={**original, "headRefOid": "b" * 40})
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Push refused: fetched PR head differs from GitHub; repeat check-push before pushing."
    )


def test_check_push_keeps_stable_whitespace_contract_and_binary_changes(delivery, capsys):
    _, _, tree, state, git, _ = delivery
    for key, value in (("patchid.verbatim", "true"), ("diff.context", "0"),
                       ("diff.algorithm", "histogram"), ("diff.relative", "true"),
                       ("diff.ignoreSubmodules", "all"), ("diff.noprefix", "true")):
        git("config", key, value, cwd=tree)
    original = json.loads(state.read_text())["pr"]
    (tree / "consumer.txt").write_text(" change\n")
    git("commit", "-am", "needed indentation fix", cwd=tree)
    args = publish_args("check-push", "42")
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == (
        f"Push refused: same patch as PR head {original['headRefOid']}; main advancing does not require a new head "
        "(ship accepts it). Push only for a real conflict or a needed commit: add --needs-base <sha> with a reason."
    )
    args.needs_base = git("rev-parse", "HEAD", cwd=tree)
    args.reason = "The indentation change affects Python behavior."
    assert run_command(args, tree) == 1
    assert capsys.readouterr().out.strip() == (
        "Push refused: --needs-base must name a commit SHA reachable from HEAD and origin/main."
    )
    args.needs_base = original["baseRefOid"]
    assert run_command(args, tree) == 0
    assert capsys.readouterr().out.strip() == (
        f"Push check passed: explicit base dependency {args.needs_base}; reason: {args.reason}"
    )
    (tree / "binary.dat").write_bytes(b"\x00\xff\xfe")
    git("add", "binary.dat", cwd=tree)
    git("commit", "-m", "needed binary change", cwd=tree)
    assert run_command(publish_args("check-push", "42"), tree) == 0
    assert capsys.readouterr().out.strip() == "Push check passed: the patch differs from the current PR head."
