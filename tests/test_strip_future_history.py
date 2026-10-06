"""Behavioural tests for the strip-future-history@1 transform.

Covers the transform output contract only (setup.sh contents, idempotence,
base-commit preservation, task.toml re-embed, grading untouched) plus
functional execution of the inserted shell block against fixture git repos
for both HAR-161 leak shapes. No Docker, no Harbor runs.
"""

from __future__ import annotations

import base64
import gzip
import io
import os
import re
import shutil
import subprocess
import tarfile
import tomllib
from pathlib import Path

import pytest

from evallab.strip_future_history import (
    SETUP_REL,
    STRIP_BLOCK,
    STRIP_MARKER,
    STRIP_VERIFY,
    TRANSFORM_ID,
    build_changes,
    build_setup_sh,
    derive_strip_future_history,
    pack_setup,
    read_parent_info,
    render_task_toml,
    unpack_setup_blob,
)
from evallab.task_variants import VariantInvalid

NEEDS_GIT = shutil.which("git") is None

# Compact mimo_harbor-shaped Code setup: the anchor lines are literal copies of
# the generated shape (not the module constants) so the tests pin the real
# contract instead of restating it.
SETUP_TEMPLATE = """#!/bin/bash
M=/var/lib/mimo
[ -f "$M/ready" ] && exit 0
chmod 700 "$M"
fail() { echo "setup: $*" >&2; exit 1; }
write_blocklist() { cp "$M/files/blocklist" "$M/blocklist"; }
CWD=/testbed
git config --global --add safe.directory "$CWD"
cd "$CWD" || fail "no working directory $CWD"
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  git init -q && git add -A && git -c user.name=mimo -c user.email=mimo@localhost commit -q -m baseline --allow-empty
fi
BASE=$(git rev-parse HEAD 2>/dev/null)
[ ${#BASE} -eq 40 ] || fail "could not resolve the base commit in $CWD"
echo "$BASE" > "$M/base"
# Images are built with history truncated at the base. If one is not, the fix could be read out of git log,
# so .git is hidden while the agent works and put back for grading.
LATER=$(git rev-list --all --not "$BASE" 2>/dev/null | head -n 5 | grep -c . || true)
git clean -fdx >/dev/null 2>&1
if [ "$LATER" -gt 0 ]; then
  mv "$CWD/.git" "$M/git-hidden"
  echo "history not truncated at ${BASE:0:12}: .git hidden while the agent works"
fi
write_blocklist
touch "$M/ready"
echo "setup done"
"""

TOML_TEMPLATE = """schema_version = "1.4"

[task]
name = "mimo-v2.6-rl/format-code-task-000000"
description = "synthetic parent"

[agent]
timeout_sec = 3600.0

[verifier]
timeout_sec = 2100.0
user = "root"

[environment]
docker_image = "docker.io/example/repo@sha256:0000000000000000000000000000000000000000000000000000000000000000"
workdir = "/testbed"
cpus = 2
memory_mb = 8192
network_mode = "public"
build_timeout_sec = 1800.0

[environment.healthcheck]
command = "bash -c 'test -f /var/lib/mimo/ready || { mkdir -p /var/lib/mimo && echo __BLOB__ | base64 -d | tar -xzf - -C /var/lib/mimo && bash /var/lib/mimo/setup.sh; }'"
timeout_sec = 1200.0
retries = 0
interval_sec = 5.0
"""


@pytest.fixture()
def parent_dir(tmp_path: Path) -> Path:
    root = tmp_path / "parent"
    setup = root / "environment" / "setup"
    (setup / "files").mkdir(parents=True)
    (setup / "setup.sh").write_text(SETUP_TEMPLATE, encoding="utf-8")
    (setup / "files" / "blocklist").write_text("0.0.0.0 example.com\n", encoding="utf-8")
    (root / "environment" / "Dockerfile").write_text(
        "FROM docker.io/example/repo@sha256:0000\n", encoding="utf-8"
    )
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test.sh").write_text("#!/bin/bash\necho grading\n", encoding="utf-8")
    (root / "tests" / "test.patch").write_text("fake patch", encoding="utf-8")
    (root / "tests" / "test_command.sh").write_text("true\n", encoding="utf-8")
    (root / "instruction.md").write_text("fix it", encoding="utf-8")
    blob = pack_setup(setup)
    (root / "task.toml").write_text(
        TOML_TEMPLATE.replace("__BLOB__", blob), encoding="utf-8"
    )
    return root


def test_setup_sh_keeps_base_lines_and_adds_strip(parent_dir: Path) -> None:
    parent_text = (parent_dir / SETUP_REL).read_text(encoding="utf-8")
    new_text = build_setup_sh(parent_text)

    # The recorded-base lines survive byte-identical.
    for line in (
        'BASE=$(git rev-parse HEAD 2>/dev/null)',
        '[ ${#BASE} -eq 40 ] || fail "could not resolve the base commit in $CWD"',
        'echo "$BASE" > "$M/base"',
    ):
        assert parent_text.count(line) == 1
        assert new_text.count(line) == 1

    assert STRIP_MARKER in new_text
    assert "pack-objects --revs --stdout --no-reuse-delta --no-reuse-object" in new_text
    assert 'rm -rf "$CWD/.git" "$M/git-hidden" || fail' in new_text
    # Post-rebuild acceptance counts future objects, never the fsck exit code:
    # truncated-at-base images already lack some of BASE's ancestors.
    assert "fsck --full" not in new_text
    assert "base history failed fsck" not in new_text
    assert "fsck --unreachable --no-reflogs" in new_text
    assert "unexpected future commits in clean git directory" in new_text
    assert "unexpected refs beyond base in clean git directory" in new_text
    assert "LATER=" not in new_text
    assert 'mv "$CWD/.git" "$M/git-hidden"' not in new_text
    assert new_text.index(STRIP_MARKER) < new_text.index('touch "$M/ready"')


def test_setup_sh_transform_is_idempotent(parent_dir: Path) -> None:
    parent_text = (parent_dir / SETUP_REL).read_text(encoding="utf-8")
    once = build_setup_sh(parent_text)
    assert once != parent_text
    assert build_setup_sh(once) == once


def test_only_setup_and_toml_change(parent_dir: Path) -> None:
    changes, _ = build_changes(parent_dir)
    assert set(changes) == {SETUP_REL, "task.toml"}
    # Grading is byte-identical by construction: no tests/ key exists.
    assert not any(key.startswith("tests/") for key in changes)


def test_task_toml_reembed_only_swaps_payload(parent_dir: Path) -> None:
    changes, inputs = build_changes(parent_dir)
    parent_config = tomllib.loads((parent_dir / "task.toml").read_text(encoding="utf-8"))
    new_config = tomllib.loads(changes["task.toml"].decode("utf-8"))

    for section in ("task", "agent", "verifier", "metadata"):
        if section in parent_config:
            assert new_config[section] == parent_config[section]
    for key, value in parent_config["environment"].items():
        if key != "healthcheck":
            assert new_config["environment"][key] == value
    assert (
        new_config["environment"]["healthcheck"]["timeout_sec"]
        == parent_config["environment"]["healthcheck"]["timeout_sec"]
    )
    old_cmd = parent_config["environment"]["healthcheck"]["command"]
    new_cmd = new_config["environment"]["healthcheck"]["command"]
    assert len(new_cmd) != len(old_cmd)
    assert new_cmd.replace(new_cmd.split("echo ")[1].split(" |")[0], "") == old_cmd.replace(
        old_cmd.split("echo ")[1].split(" |")[0], ""
    )

    # The new payload unpacks to the repaired script plus untouched files.
    new_blob = new_cmd.split("echo ")[1].split(" |")[0]
    members = unpack_setup_blob(new_blob)
    assert members["setup.sh"] == changes[SETUP_REL]
    assert (
        members["files/blocklist"]
        == (parent_dir / "environment" / "setup" / "files" / "blocklist").read_bytes()
    )
    assert inputs["parent_task"] == "mimo-v2.6-rl/format-code-task-000000"
    assert inputs["workdir"] == "/testbed"


def test_pack_matches_mimo_harbor_convention(parent_dir: Path) -> None:
    blob = pack_setup(parent_dir / "environment" / "setup")
    raw = base64.b64decode(blob)
    assert raw[:2] == b"\x1f\x8b"  # gzip magic
    assert raw[4:8] == b"\x00\x00\x00\x00"  # gzip mtime 0
    data = gzip.decompress(raw)
    assert data[257:262] == b"ustar"  # ustar members
    names, modes, mtimes = [], [], []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r") as tar:
        for member in tar.getmembers():
            names.append(member.name)
            modes.append(member.mode)
            mtimes.append(member.mtime)
    assert names == sorted(names) == ["files/blocklist", "setup.sh"]
    assert modes == [0o700, 0o700]
    assert mtimes == [0, 0]


def test_refuses_non_git_setup(parent_dir: Path) -> None:
    terminal_setup = """#!/bin/bash
M=/var/lib/mimo
[ -f "$M/ready" ] && exit 0
rm -rf /tests
touch "$M/ready"
echo "setup done"
"""
    (parent_dir / SETUP_REL).write_text(terminal_setup, encoding="utf-8")
    with pytest.raises(VariantInvalid):
        build_changes(parent_dir)
    with pytest.raises(VariantInvalid):
        read_parent_info(parent_dir)


def test_refuses_double_derive(parent_dir: Path) -> None:
    (parent_dir / SETUP_REL).write_text(
        build_setup_sh((parent_dir / SETUP_REL).read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    with pytest.raises(VariantInvalid):
        build_changes(parent_dir)


def test_render_task_toml_needs_payload() -> None:
    with pytest.raises(VariantInvalid):
        render_task_toml("command = \"true\"\n", new_blob="abc")


def test_derive_records_transform_id(parent_dir: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    record = derive_strip_future_history(
        parent_dir,
        rationale="test",
        created_by="test",
        repo_root=tmp_path,
        parent_source={"kind": "local", "path": str(parent_dir)},
        variants_root=store,
    )
    assert record.transform == TRANSFORM_ID
    assert record.task_name == "mimo-v2.6-rl/format-code-task-000000"
    assert {change.path for change in record.files} == {"task.toml", SETUP_REL}
    assert set(record.components_changed) == {"task_toml", "environment"}
    expected_record = (
        tmp_path / "library" / "task-variants" / record.task_slug / f"{record.digest12}.json"
    )
    assert expected_record.is_file()
    assert (store / record.task_slug / record.digest12).is_dir()


# --- Functional execution of the inserted shell block ---------------------- #


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    merged = dict(os.environ, **(env or {}))
    merged["GIT_CONFIG_NOSYSTEM"] = "1"
    merged["GIT_CONFIG_GLOBAL"] = os.devnull
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env=merged,
    )
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def _fixture_repo(path: Path, *, leak: str) -> str:
    """One base commit plus a leak shape; returns the base sha.

    leak: "refs" (branches/tags beyond base, the git-hidden shape),
    "unreachable" (dangling fix commit), "kept-pack" (an unprunable pack),
    or "clean".
    """
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "mimo")
    _git(path, "config", "user.email", "mimo@localhost")
    (path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    base = _git(path, "rev-parse", "HEAD")
    if leak in ("refs", "unreachable", "kept-pack"):
        (path / "app.py").write_text("VALUE = 2  # upstream fix\n", encoding="utf-8")
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", "upstream fix")
        if leak == "refs":
            _git(path, "branch", "future")
            _git(path, "tag", "v2")
        # HEAD back to base on every shape: the fix commit is reachable only
        # from the extra refs (refs shape) or from nothing (unreachable shape).
        _git(path, "checkout", "-q", base)
        _git(path, "branch", "-f", "main", base)
        if leak == "kept-pack":
            _git(path, "repack", "-Ad")
            for pack in (path / ".git" / "objects" / "pack").glob("*.pack"):
                pack.with_suffix(".keep").touch()
        assert _git(path, "rev-parse", "HEAD") == base
    return base


def _execute_strip_block(repo: Path, base: str) -> subprocess.CompletedProcess[str]:
    """Execute STRIP_BLOCK + STRIP_VERIFY like the repaired setup.sh would."""
    state = repo.parent / "mimo-state"
    state.mkdir(exist_ok=True)
    script = (
        f'M="{state}"\nCWD="{repo}"\nBASE="{base}"\n'
        + 'fail() { echo "setup: $*" >&2; exit 1; }\n'
        + STRIP_BLOCK
        + STRIP_VERIFY
        + 'touch "$M/ready"\n'
    )
    return subprocess.run(
        ["bash", "-c", script],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )


def _execute_strip_verify(repo: Path, base: str) -> subprocess.CompletedProcess[str]:
    """Execute only the post-rebuild acceptance against an existing repo."""
    state = repo.parent / "mimo-state"
    state.mkdir(exist_ok=True)
    script = (
        f'M="{state}"\nCWD="{repo}"\nBASE="{base}"\n'
        + 'fail() { echo "setup: $*" >&2; exit 1; }\n'
        + STRIP_VERIFY
    )
    return subprocess.run(
        ["bash", "-c", script],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )


@pytest.mark.skipif(NEEDS_GIT, reason="git binary required")
@pytest.mark.parametrize("leak", ["refs", "unreachable", "kept-pack", "clean"])
def test_strip_block_removes_post_base_history(tmp_path: Path, leak: str) -> None:
    repo = tmp_path / "testbed"
    base = _fixture_repo(repo, leak=leak)
    if leak == "refs":
        assert _git(repo, "rev-list", "--all", "--not", base).strip() != ""
    if leak == "unreachable":
        assert "commit" in _git(repo, "fsck", "--unreachable", "--no-reflogs")
    base_objects = {
        line.split()[0] for line in _git(repo, "rev-list", "--objects", base).splitlines()
    }
    # Future metadata and a stale hidden copy must not survive replacement.
    (repo / ".git" / "FETCH_HEAD").write_text("future metadata\n", encoding="utf-8")
    state = repo.parent / "mimo-state"
    (state / "git-hidden").mkdir(parents=True)
    (state / "git-hidden" / "future").write_text("upstream fix\n", encoding="utf-8")

    proc = _execute_strip_block(repo, base)
    assert proc.returncode == 0, proc.stderr
    assert (state / "ready").is_file()
    assert not (state / "git-hidden").exists()
    assert not (repo / ".git" / "FETCH_HEAD").exists()

    # Base commit preserved: HEAD still resolves to it, worktree untouched.
    assert _git(repo, "rev-parse", "HEAD") == base
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    # No post-base history observable by either HAR-161 accounting.
    assert _git(repo, "rev-list", "--all", "--not", base) == ""
    assert "commit" not in _git(repo, "fsck", "--unreachable", "--no-reflogs")
    # Every retained object belongs to BASE's closure, not merely no future ref.
    retained = set(
        _git(repo, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").splitlines()
    )
    assert retained == base_objects
    assert _git(repo, "cat-file", "-t", base) == "commit"

    # Grading-equivalent git operations still work on the stripped repo.
    _git(repo, "add", "-A")
    assert _git(repo, "diff", "--cached", base) == ""
    _git(repo, "reset", "-q")
    (repo / "app.py").write_text("VALUE = 3  # agent edit\n", encoding="utf-8")
    _git(repo, "add", "-A")
    assert _git(repo, "diff", "--cached", base).strip() != ""
    _git(repo, "reset", "-q")
    _git(repo, "checkout", base, "--", "app.py")
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


@pytest.mark.skipif(NEEDS_GIT, reason="git binary required")
@pytest.mark.parametrize("unsupported", ["alternates", "worktree"])
def test_strip_fails_closed_on_external_object_storage(
    tmp_path: Path, unsupported: str
) -> None:
    repo = tmp_path / "testbed"
    base = _fixture_repo(repo, leak="refs")
    if unsupported == "alternates":
        (repo / ".git" / "objects" / "info" / "alternates").write_text(
            "/unchecked/object-store\n", encoding="utf-8"
        )
    else:
        _git(repo, "worktree", "add", "--detach", str(tmp_path / "linked"), base)
    proc = _execute_strip_block(repo, base)
    assert proc.returncode != 0
    assert "setup:" in proc.stderr
    assert not (tmp_path / "mimo-state" / "ready").exists()
    assert (repo / ".git").is_dir()


@pytest.mark.skipif(NEEDS_GIT, reason="git binary required")
def test_strip_preserves_base_ancestor(tmp_path: Path) -> None:
    repo = tmp_path / "testbed"
    ancestor = _fixture_repo(repo, leak="clean")
    (repo / "app.py").write_text("VALUE = 5\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "new base")
    base = _git(repo, "rev-parse", "HEAD")
    proc = _execute_strip_block(repo, base)
    assert proc.returncode == 0, proc.stderr
    assert _git(repo, "rev-parse", "HEAD^") == ancestor
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 5\n"


@pytest.mark.skipif(NEEDS_GIT, reason="git binary required")
def test_strip_tolerates_truncated_base_history(tmp_path: Path) -> None:
    """A shallow-boundary BASE (the 000076 shape) must not fail setup.

    BASE 90915883 is itself the shallow boundary of its image: its two
    parents are absent, ``pack-objects`` stops at the boundary, and the
    rebuilt repo (which carries no shallow marker) fails a full fsck with
    broken links. The old ``fsck --full`` exit-code gate failed setup with
    "base history failed fsck" on Daytona twice for exactly this.
    """
    src = tmp_path / "src"
    _fixture_repo(src, leak="clean")
    (src / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "second")
    repo = tmp_path / "testbed"
    proc = subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{src}", str(repo)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    assert proc.returncode == 0, proc.stderr
    base = _git(repo, "rev-parse", "HEAD")
    assert _git(repo, "rev-parse", "--is-shallow-repository") == "true"

    proc = _execute_strip_block(repo, base)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "mimo-state" / "ready").is_file()
    assert _git(repo, "rev-parse", "HEAD") == base
    assert (repo / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    # The rebuilt repo carries no shallow marker, so a full fsck reports
    # the missing ancestors as broken links -- the exact condition the old
    # exit-code gate tripped on -- while the acceptance counts stay clean.
    fsck = subprocess.run(
        ["git", "fsck", "--full", "--unreachable", "--no-reflogs"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    assert fsck.returncode != 0
    assert "broken link" in fsck.stdout or "missing" in fsck.stdout
    assert _git(repo, "rev-list", "--all", "--not", base) == ""
    # The plain (non-full) fsck used by the acceptance reports the same
    # broken links, but none match the anchored future-commit pattern.
    check = subprocess.run(
        ["git", "fsck", "--unreachable", "--no-reflogs"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    assert not re.search(r"^(unreachable|dangling) commit ", check.stdout, re.M)


@pytest.mark.skipif(NEEDS_GIT, reason="git binary required")
def test_strip_verify_rejects_surviving_ref(tmp_path: Path) -> None:
    """STRIP_VERIFY fails closed when a ref beyond BASE exists."""
    repo = tmp_path / "testbed"
    base = _fixture_repo(repo, leak="refs")
    # The fixture leaves branches/tags on the post-base fix commit, so the
    # acceptance must refuse it without any rebuild taking place.
    proc = _execute_strip_verify(repo, base)
    assert proc.returncode != 0
    assert "unexpected refs beyond base" in proc.stderr
    assert (repo / ".git").is_dir()


@pytest.mark.skipif(NEEDS_GIT, reason="git binary required")
def test_strip_verify_rejects_dangling_commit(tmp_path: Path) -> None:
    """STRIP_VERIFY fails closed when a future commit object survives."""
    repo = tmp_path / "testbed"
    base = _fixture_repo(repo, leak="unreachable")
    proc = _execute_strip_verify(repo, base)
    assert proc.returncode != 0
    assert "unexpected future commits" in proc.stderr


@pytest.mark.skipif(NEEDS_GIT, reason="git binary required")
def test_strip_verify_ignores_missing_ancestors(tmp_path: Path) -> None:
    """STRIP_VERIFY passes on truncated history with no future objects."""
    src = tmp_path / "src"
    _fixture_repo(src, leak="clean")
    (src / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "second")
    repo = tmp_path / "testbed"
    proc = subprocess.run(
        ["git", "clone", "-q", "--depth", "1", f"file://{src}", str(repo)],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    assert proc.returncode == 0, proc.stderr
    base = _git(repo, "rev-parse", "HEAD")
    (repo / ".git" / "shallow").unlink()
    # A full fsck now reports the missing ancestors, but none of its lines
    # may match the anchored future-commit pattern the acceptance counts.
    fsck = subprocess.run(
        ["git", "fsck", "--full", "--unreachable", "--no-reflogs"],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )
    assert fsck.returncode != 0
    assert "broken link" in fsck.stdout or "missing" in fsck.stdout
    assert not re.search(r"^(unreachable|dangling) commit ", fsck.stdout, re.M)
    assert _execute_strip_verify(repo, base).returncode == 0
