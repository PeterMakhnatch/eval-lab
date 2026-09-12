"""Verifier test suite for release-branch-rescue.

Evaluates repository integrity, reflog recovery, upstream divergence integration,
branch configuration, and anti-tampering defenses in a separate container.
"""

import os
import shutil
import subprocess
import tempfile
import pytest

WORKSPACE = "/workspace/release"
ORIGIN_GIT = "/srv/origin.git"

SAFE_GIT_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_ALLOW_PROTOCOL": "file",
    "PAGER": "cat",
}


def safe_git(cwd, args, timeout=15):
    """Execute git with strict isolation and security flags."""
    cmd = [
        "/usr/bin/git",
        "-c", "core.fsmonitor=false",
        "-c", "core.hooksPath=/dev/null",
        "-c", f"safe.directory={cwd}",
        "-c", "core.pager=cat",
        "--no-replace-objects",
        "--no-pager",
        "-C", cwd,
        *args
    ]
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=SAFE_GIT_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def derive_expected_fixture():
    """Independently reconstruct expected fixture object identities in a trusted sandbox.

    Derivation reconstructs:
    1. Baseline commit A (tagged v1)
    2. Local commits L1 and L2 (the lost work recovered from reflog)
    3. Upstream commits R1 and R2 (authoritative upstream progression)
    4. Deterministic merged tree combining both lines of development
    """
    tmp_root = tempfile.mkdtemp(prefix="fixture-derive-")
    try:
        env = {
            "GIT_AUTHOR_NAME": "Release Bot",
            "GIT_AUTHOR_EMAIL": "release@example.com",
            "GIT_COMMITTER_NAME": "Release Bot",
            "GIT_COMMITTER_EMAIL": "release@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "PATH": SAFE_GIT_ENV["PATH"],
        }

        def run(args, cwd=tmp_root):
            return subprocess.check_output(
                ["/usr/bin/git", *args], cwd=cwd, env=env, text=True
            ).strip()

        repo = os.path.join(tmp_root, "ref-repo")
        os.makedirs(repo)
        run(["init", "-b", "main"], cwd=repo)
        os.makedirs(os.path.join(repo, "config"))
        os.makedirs(os.path.join(repo, "docs"))

        with open(os.path.join(repo, "config", "limits.conf"), "w") as f:
            f.write("# Service rate limits and retries\nmax_connections=100\nretries=1\n")
        with open(os.path.join(repo, "config", "timeouts.conf"), "w") as f:
            f.write("# Client and upstream timeout settings\nconnect_timeout=5\ntimeout=30\n")
        with open(os.path.join(repo, "config", "regions.txt"), "w") as f:
            f.write("us-east\n")
        with open(os.path.join(repo, "docs", "runbook.md"), "w") as f:
            f.write(
                "# Incident Response and Deployment Runbook\n\n"
                "## Deployment Verification\n"
                "1. Verify gateway connectivity.\n"
                "2. Confirm health check responses on all endpoints.\n"
            )

        run(["add", "."], cwd=repo)
        env["GIT_AUTHOR_DATE"] = "2026-01-01T10:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-01T10:00:00Z"
        run(["commit", "-m", "release: v1.0.0 initial baseline"], cwd=repo)
        run(["tag", "-a", "v1", "-m", "v1.0.0 release"], cwd=repo)
        a_hash = run(["rev-parse", "v1^{commit}"], cwd=repo)

        # Build L1 and L2 on local-line
        run(["checkout", "-b", "local-line"], cwd=repo)
        with open(os.path.join(repo, "config", "limits.conf"), "w") as f:
            f.write("# Service rate limits and retries\nmax_connections=100\nretries=3\n")
        run(["add", "config/limits.conf"], cwd=repo)
        env["GIT_AUTHOR_DATE"] = "2026-01-02T14:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-02T14:00:00Z"
        run(["commit", "-m", "release: increase retry count to 3"], cwd=repo)

        with open(os.path.join(repo, "config", "regions.txt"), "w") as f:
            f.write("us-east\neu-west\n")
        run(["add", "config/regions.txt"], cwd=repo)
        env["GIT_AUTHOR_DATE"] = "2026-01-03T14:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-03T14:00:00Z"
        run(["commit", "-m", "release: add eu-west deployment region"], cwd=repo)
        l2_hash = run(["rev-parse", "HEAD"], cwd=repo)

        # Build R1 and R2 on upstream-line from commit A
        run(["checkout", "-b", "upstream-line", "v1"], cwd=repo)
        with open(os.path.join(repo, "config", "timeouts.conf"), "w") as f:
            f.write("# Client and upstream timeout settings\nconnect_timeout=5\ntimeout=60\n")
        run(["add", "config/timeouts.conf"], cwd=repo)
        env["GIT_AUTHOR_DATE"] = "2026-01-02T10:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-02T10:00:00Z"
        run(["commit", "-m", "upstream: increase timeout to 60s for slow backends"], cwd=repo)

        with open(os.path.join(repo, "docs", "runbook.md"), "w") as f:
            f.write(
                "# Incident Response and Deployment Runbook\n\n"
                "## Deployment Verification\n"
                "1. Verify gateway connectivity.\n"
                "2. Confirm health check responses on all endpoints.\n"
                "3. Check upstream latency metrics in telemetry dashboard.\n"
            )
        run(["add", "docs/runbook.md"], cwd=repo)
        env["GIT_AUTHOR_DATE"] = "2026-01-03T10:00:00Z"
        env["GIT_COMMITTER_DATE"] = "2026-01-03T10:00:00Z"
        run(["commit", "-m", "upstream: document latency metrics check in runbook"], cwd=repo)
        r2_hash = run(["rev-parse", "HEAD"], cwd=repo)

        # Merge local and upstream lines to produce expected integrated tree
        run(["merge", "--no-edit", "local-line"], cwd=repo)
        tree_hash = run(["rev-parse", "HEAD^{tree}"], cwd=repo)

        return {
            "A": a_hash,
            "L2": l2_hash,
            "R2": r2_hash,
            "TREE": tree_hash,
        }
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


# Reconstruct fixture object identities dynamically
EXPECTED = derive_expected_fixture()


@pytest.fixture(scope="session", autouse=True)
def preflight_security_and_integrity_gate():
    """Security preflight gate: stops entire test run if artifacts or git mechanisms are tampered with.

    Prevents subsequent test assertions from running git commands on hostile repository structures.
    """
    # 1. Verify artifact existence and reject symlinks
    for path in [WORKSPACE, os.path.join(WORKSPACE, ".git"), ORIGIN_GIT]:
        if not os.path.exists(path):
            pytest.exit(f"Artifact directory missing: {path}", returncode=1)
        if os.path.islink(path):
            pytest.exit(f"Artifact path is a symlink: {path}", returncode=1)
        if not os.path.isdir(path):
            pytest.exit(f"Artifact path is not a directory: {path}", returncode=1)

    # 2. Check for unsafe git mechanisms in workspace
    git_dir = os.path.join(WORKSPACE, ".git")
    for bad_path in [
        os.path.join(git_dir, "refs", "replace"),
        os.path.join(git_dir, "info", "grafts"),
        os.path.join(git_dir, "shallow"),
        os.path.join(git_dir, "objects", "info", "alternates"),
    ]:
        if os.path.exists(bad_path):
            pytest.exit(f"Prohibited git mechanism detected: {bad_path}", returncode=1)

    config_path = os.path.join(git_dir, "config")
    if os.path.exists(config_path):
        with open(config_path, "r", errors="ignore") as f:
            cfg = f.read().lower()
            if "fsmonitor" in cfg:
                pytest.exit("Prohibited fsmonitor config detected in workspace", returncode=1)
            if "[include" in cfg:
                pytest.exit("Prohibited include directive in workspace git config", returncode=1)

    hooks_dir = os.path.join(git_dir, "hooks")
    if os.path.exists(hooks_dir):
        for fname in os.listdir(hooks_dir):
            fpath = os.path.join(hooks_dir, fname)
            if os.path.isfile(fpath) and not fname.endswith(".sample"):
                if os.access(fpath, os.X_OK):
                    pytest.exit(f"Custom executable hook in workspace: {fname}", returncode=1)

    # 3. Check for unsafe git mechanisms in origin repository
    for bad_path in [
        os.path.join(ORIGIN_GIT, "refs", "replace"),
        os.path.join(ORIGIN_GIT, "info", "grafts"),
        os.path.join(ORIGIN_GIT, "shallow"),
        os.path.join(ORIGIN_GIT, "objects", "info", "alternates"),
    ]:
        if os.path.exists(bad_path):
            pytest.exit(f"Prohibited git mechanism detected in origin: {bad_path}", returncode=1)

    origin_cfg_path = os.path.join(ORIGIN_GIT, "config")
    if os.path.exists(origin_cfg_path):
        with open(origin_cfg_path, "r", errors="ignore") as f:
            cfg = f.read().lower()
            if "fsmonitor" in cfg or "[include" in cfg:
                pytest.exit("Prohibited config directives in origin.git", returncode=1)

    # 4. Verify object database integrity with fsck
    res = safe_git(ORIGIN_GIT, ["fsck", "--full", "--strict"])
    if res.returncode != 0:
        pytest.exit(f"Upstream origin fsck failed: {res.stderr}", returncode=1)

    res = safe_git(WORKSPACE, ["fsck", "--full", "--strict"])
    if res.returncode != 0:
        pytest.exit(f"Workspace repository fsck failed: {res.stderr}", returncode=1)


def test_repo_security_invariants():
    """Assert security invariants pass for candidate workspace."""
    # Ensure no tracked files are symlinks (mode 120000)
    res = safe_git(WORKSPACE, ["ls-files", "-s"])
    assert res.returncode == 0, f"git ls-files failed: {res.stderr}"
    for line in res.stdout.splitlines():
        assert not line.startswith("120000"), f"Tracked symlink prohibited: {line}"


def test_upstream_origin_untouched():
    """Verify upstream origin.git was not modified or pushed to."""
    res = safe_git(ORIGIN_GIT, ["rev-parse", "refs/heads/main"])
    assert res.returncode == 0 and res.stdout.strip() == EXPECTED["R2"], (
        f"Upstream origin main branch was modified: {res.stdout.strip()}"
    )

    res = safe_git(ORIGIN_GIT, ["rev-parse", "refs/tags/v1^{commit}"])
    assert res.returncode == 0 and res.stdout.strip() == EXPECTED["A"], (
        f"Upstream origin v1 tag was modified: {res.stdout.strip()}"
    )

    res = safe_git(ORIGIN_GIT, ["for-each-ref", "--format=%(refname)"])
    upstream_refs = set(res.stdout.strip().splitlines())
    expected_refs = {"refs/heads/main", "refs/tags/v1"}
    assert upstream_refs == expected_refs, f"Unexpected refs in upstream repository: {upstream_refs}"


def test_v1_tag_preserved():
    """Verify historical release tag v1 still points to original baseline commit A."""
    res = safe_git(WORKSPACE, ["rev-parse", "refs/tags/v1^{commit}"])
    assert res.returncode == 0 and res.stdout.strip() == EXPECTED["A"], (
        f"Tag v1 does not point to baseline commit A: {res.stdout.strip()}"
    )


def test_rescue_tag_points_to_original_l2():
    """Verify rescue/pre-reset tag points to original pre-reset tip commit L2."""
    res = safe_git(WORKSPACE, ["rev-parse", "refs/tags/rescue/pre-reset^{commit}"])
    assert res.returncode == 0, "Required tag refs/tags/rescue/pre-reset does not exist"
    assert res.stdout.strip() == EXPECTED["L2"], (
        f"Tag rescue/pre-reset does not point to pre-reset commit L2: {res.stdout.strip()}"
    )


def test_head_symbolic_ref():
    """Verify HEAD is attached to refs/heads/main."""
    res = safe_git(WORKSPACE, ["symbolic-ref", "HEAD"])
    assert res.returncode == 0 and res.stdout.strip() == "refs/heads/main", (
        f"HEAD is not symbolically refs/heads/main: {res.stdout.strip()}"
    )


def test_main_tracks_origin_main():
    """Verify local main tracks origin/main and origin remote URL is intact."""
    res = safe_git(WORKSPACE, ["rev-parse", "--symbolic-full-name", "main@{upstream}"])
    assert res.returncode == 0 and res.stdout.strip() == "refs/remotes/origin/main", (
        f"main@{{upstream}} is not configured to refs/remotes/origin/main: {res.stdout.strip()}"
    )

    res = safe_git(WORKSPACE, ["remote", "get-url", "origin"])
    assert res.returncode == 0 and res.stdout.strip() == ORIGIN_GIT, (
        f"origin remote URL is not {ORIGIN_GIT}: {res.stdout.strip()}"
    )

    res = safe_git(WORKSPACE, ["rev-parse", "refs/remotes/origin/main"])
    assert res.returncode == 0 and res.stdout.strip() == EXPECTED["R2"], (
        f"origin/main remote tracking ref does not point to R2: {res.stdout.strip()}"
    )


def test_ancestry_and_history():
    """Verify upstream origin/main is an ancestor of local main."""
    res = safe_git(
        WORKSPACE,
        ["merge-base", "--is-ancestor", "refs/remotes/origin/main", "refs/heads/main"]
    )
    assert res.returncode == 0, "Upstream origin/main (R2) is not an ancestor of local main"


def test_worktree_clean_and_tree_matches():
    """Verify working tree is clean and matches the expected integrated tree."""
    # 1. Ensure no assume-unchanged or skip-worktree flags are masking modifications
    res = safe_git(WORKSPACE, ["ls-files", "-v"])
    assert res.returncode == 0, f"git ls-files -v failed: {res.stderr}"
    for line in res.stdout.splitlines():
        assert line.startswith("H "), f"Suspicious index flag detected (expected normal 'H '): {line}"

    # 2. Check for untracked or ignored files
    res = safe_git(WORKSPACE, ["status", "--porcelain", "-uall", "--ignored"])
    assert res.returncode == 0 and res.stdout.strip() == "", (
        f"Working tree has untracked, ignored, or modified files:\n{res.stdout.strip()}"
    )

    # 3. Plumbing check: diff working files against index
    res = safe_git(WORKSPACE, ["diff-files", "--quiet", "--no-ext-diff"])
    assert res.returncode == 0, "Working tree has unstaged changes relative to index"

    # 4. Plumbing check: diff index against HEAD
    res = safe_git(WORKSPACE, ["diff-index", "--quiet", "--cached", "--no-ext-diff", "HEAD"])
    assert res.returncode == 0, "Index has uncommitted changes relative to HEAD"

    # 5. Plumbing check: diff working tree against HEAD
    res = safe_git(WORKSPACE, ["diff-index", "--quiet", "--no-ext-diff", "HEAD"])
    assert res.returncode == 0, "Working tree differs from HEAD commit"

    # 6. Verify exact tree object hash
    res = safe_git(WORKSPACE, ["rev-parse", "HEAD^{tree}"])
    assert res.returncode == 0 and res.stdout.strip() == EXPECTED["TREE"], (
        f"HEAD tree hash does not match expected integrated tree: {res.stdout.strip()}"
    )


def test_file_contents_and_integration():
    """Verify tracked files contain both local and upstream changes."""
    limits_path = os.path.join(WORKSPACE, "config", "limits.conf")
    with open(limits_path, "r") as f:
        limits_content = f.read()
        assert "retries=3" in limits_content, "config/limits.conf missing retries=3 (from L1)"
        assert "max_connections=100" in limits_content, "config/limits.conf corrupted"

    timeouts_path = os.path.join(WORKSPACE, "config", "timeouts.conf")
    with open(timeouts_path, "r") as f:
        timeouts_content = f.read()
        assert "timeout=60" in timeouts_content, "config/timeouts.conf missing timeout=60 (from R1)"

    regions_path = os.path.join(WORKSPACE, "config", "regions.txt")
    with open(regions_path, "r") as f:
        regions_content = f.read()
        assert "us-east" in regions_content, "config/regions.txt missing us-east (from A)"
        assert "eu-west" in regions_content, "config/regions.txt missing eu-west (from L2)"

    runbook_path = os.path.join(WORKSPACE, "docs", "runbook.md")
    with open(runbook_path, "r") as f:
        runbook_content = f.read()
        assert "telemetry dashboard" in runbook_content, (
            "docs/runbook.md missing upstream update (from R2)"
        )
