"""Behavioural tests for the mtime-normalize@1 and @2 transforms."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from evallab.mtime_normalize import (
    ANCHOR,
    FIXED_MTIME,
    MARKER,
    MARKER_V2,
    MAX_PASSES_V2,
    MTIME_BLOCK,
    MTIME_BLOCK_V2,
    SETUP_REL,
    TRANSFORM_ID,
    TRANSFORM_ID_V2,
    build_changes,
    build_changes_v2,
    build_setup_sh,
    build_setup_sh_v2,
)
from evallab.strip_future_history import pack_setup
from evallab.task_variants import VariantInvalid

# Compact mimo_harbor-shaped Code setup tail: the anchor line is a literal copy
# of the generated shape (not the module constant) so the test pins the real
# contract instead of restating it.
SETUP_TEMPLATE = """#!/bin/bash
M=/var/lib/mimo
:[ -f "$M/ready" ] && exit 0
fail() { echo "setup: $*" >&2; exit 1; }
write_blocklist() { cp "$M/files/blocklist" "$M/blocklist"; }
CWD=/testbed
BASE=$(git rev-parse HEAD 2>/dev/null)
echo "$BASE" > "$M/base"
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
    (root / "instruction.md").write_text("fix it", encoding="utf-8")
    blob = pack_setup(setup)
    (root / "task.toml").write_text(
        TOML_TEMPLATE.replace("__BLOB__", blob), encoding="utf-8"
    )
    return root


def test_setup_sh_runs_block_last_and_keeps_sentinel(parent_dir: Path) -> None:
    parent_text = (parent_dir / SETUP_REL).read_text(encoding="utf-8")
    new_text = build_setup_sh(parent_text)

    assert MARKER in new_text
    assert new_text.count(ANCHOR) == 1
    # The normalize block is the last thing before the ready sentinel, so it
    # runs after git clean / strip / purge work earlier in setup.
    assert new_text.index(MARKER) < new_text.index(ANCHOR)
    assert new_text.index("\nwrite_blocklist\n") < new_text.index(MARKER)
    assert TRANSFORM_ID in new_text


def test_setup_sh_transform_is_idempotent(parent_dir: Path) -> None:
    parent_text = (parent_dir / SETUP_REL).read_text(encoding="utf-8")
    once = build_setup_sh(parent_text)
    assert build_setup_sh(once) == once


def test_refuses_setup_without_unique_sentinel() -> None:
    with pytest.raises(VariantInvalid, match="ready sentinel"):
        build_setup_sh("#!/bin/bash\nfail() { exit 1; }\n")
    with pytest.raises(VariantInvalid, match="ready sentinel"):
        build_setup_sh('touch "$M/ready"\nmid\ntouch "$M/ready"\n')


def test_only_setup_and_toml_change(parent_dir: Path) -> None:
    changes, _ = build_changes(parent_dir)
    assert set(changes) == {SETUP_REL, "task.toml"}
    assert not any(key.startswith("tests/") for key in changes)


def test_inputs_record_fixed_mtime(parent_dir: Path) -> None:
    _, inputs = build_changes(parent_dir)
    assert inputs["fixed_mtime"] == FIXED_MTIME == "2000-01-01 00:00:00 UTC"
    assert inputs["workdir"] == "/testbed"


def test_refuses_double_derive(parent_dir: Path) -> None:
    (parent_dir / SETUP_REL).write_text(
        build_setup_sh((parent_dir / SETUP_REL).read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    with pytest.raises(VariantInvalid, match="already carries"):
        build_changes(parent_dir)


# --- Functional execution of the inserted shell block ---------------------- #


def _gnu_find_newermt() -> bool:
    """Whether this host's ``find`` supports ``-newermt`` (GNU; the container does)."""
    proc = subprocess.run(
        ["find", ".", "-maxdepth", "0", "-newermt", "2000-01-01", "-print"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd="/tmp",
    )
    return proc.returncode == 0


def _gnu_stat_epoch() -> bool:
    """Whether this host's ``stat`` reports ``-c %Y`` (GNU; the container does)."""
    proc = subprocess.run(
        ["stat", "-c", "%Y", "/tmp"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode == 0 and proc.stdout.strip().isdigit()


NEEDS_GNU_FIND = not _gnu_find_newermt()
NEEDS_GNU_TOOLCHAIN = not (_gnu_find_newermt() and _gnu_stat_epoch())


def _run_block(
    cwd: Path, script: str, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run shell text with the setup's ``fail``/``CWD`` contract stubbed."""
    harness = (
        'fail() { echo "setup: $*" >&2; exit 1; }\n'
        f'CWD="{cwd}"\n'
        + script
    )
    env = {"PATH": os.environ["PATH"]}
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def _touch(path: Path, epoch: int) -> None:
    os.utime(path, (epoch, epoch))


@pytest.mark.skipif(NEEDS_GNU_FIND, reason="GNU find -newermt required")
def test_block_collapses_staggered_mtimes(tmp_path: Path) -> None:
    cwd = tmp_path / "testbed"
    (cwd / "sub").mkdir(parents=True)
    old, bulk, fix = 946684800, 1760482140, 1760482753
    files = [cwd / "Makefile", cwd / "pkg" / "a.py", cwd / "sub" / "b.py"]
    (cwd / "pkg").mkdir()
    for path in files:
        path.write_text("x\n", encoding="utf-8")
    _touch(files[0], bulk)
    _touch(files[1], bulk)
    _touch(files[2], fix)  # the leak shape: one fix-touched file stands out
    _touch(cwd / "sub", old)
    link = cwd / "link.py"
    link.symlink_to(files[0])

    proc = _run_block(cwd, MTIME_BLOCK)
    assert proc.returncode == 0, proc.stderr

    stamps = {path.stat().st_mtime for path in files}
    assert len(stamps) == 1
    # touch -t sets local-midnight 2000-01-01, whatever the host zone is.
    expected = time.mktime((2000, 1, 1, 0, 0, 0, 0, 0, -1))
    assert stamps.pop() == pytest.approx(expected)
    assert cwd.joinpath("sub").stat().st_mtime == pytest.approx(expected)
    # The find -newermt acceptance inside the block passed; re-check directly
    # with the same bare-date literal the block uses.
    check = subprocess.run(
        ["find", str(cwd), "-newermt", "2000-01-01", "-print", "-quit"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert check.stdout == ""


@pytest.mark.skipif(NEEDS_GNU_FIND, reason="GNU find -newermt required")
def test_verify_fails_closed_on_newer_path(tmp_path: Path) -> None:
    cwd = tmp_path / "testbed"
    cwd.mkdir()
    planted = cwd / "new.py"
    planted.write_text("x\n", encoding="utf-8")
    verify_only = MTIME_BLOCK.splitlines()[-1] + "\n"
    assert "newermt" in verify_only
    proc = _run_block(cwd, verify_only)
    assert proc.returncode != 0
    assert "mtime-normalize@1" in proc.stderr


# --- mtime-normalize@2 ----------------------------------------------------- #


def test_v2_setup_runs_block_last_and_keeps_sentinel(parent_dir: Path) -> None:
    parent_text = (parent_dir / SETUP_REL).read_text(encoding="utf-8")
    new_text = build_setup_sh_v2(parent_text)

    assert MARKER_V2 in new_text
    assert MARKER not in new_text
    assert new_text.count(ANCHOR) == 1
    # The normalize block is the last thing before the ready sentinel, so it
    # runs after git clean / strip / purge work earlier in setup.
    assert new_text.index(MARKER_V2) < new_text.index(ANCHOR)
    assert new_text.index("\nwrite_blocklist\n") < new_text.index(MARKER_V2)
    assert TRANSFORM_ID_V2 in new_text


def test_v2_refuses_setup_without_unique_sentinel() -> None:
    with pytest.raises(VariantInvalid, match="ready sentinel"):
        build_setup_sh_v2("#!/bin/bash\nfail() { exit 1; }\n")
    with pytest.raises(VariantInvalid, match="ready sentinel"):
        build_setup_sh_v2('touch "$M/ready"\nmid\ntouch "$M/ready"\n')


def test_v2_only_setup_and_toml_change(parent_dir: Path) -> None:
    changes, _ = build_changes_v2(parent_dir)
    assert set(changes) == {SETUP_REL, "task.toml"}
    assert not any(key.startswith("tests/") for key in changes)


def test_v2_inputs_record_fixed_mtime_and_passes(parent_dir: Path) -> None:
    _, inputs = build_changes_v2(parent_dir)
    assert inputs["fixed_mtime"] == FIXED_MTIME == "2000-01-01 00:00:00 UTC"
    assert inputs["max_passes"] == MAX_PASSES_V2 == 3
    assert inputs["workdir"] == "/testbed"


def test_v2_refuses_double_derive(parent_dir: Path) -> None:
    (parent_dir / SETUP_REL).write_text(
        build_setup_sh_v2((parent_dir / SETUP_REL).read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    with pytest.raises(VariantInvalid, match="already carries"):
        build_changes_v2(parent_dir)


def test_v2_refuses_v1_parent(parent_dir: Path) -> None:
    (parent_dir / SETUP_REL).write_text(
        build_setup_sh((parent_dir / SETUP_REL).read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    with pytest.raises(VariantInvalid, match="supersedes"):
        build_changes_v2(parent_dir)


def _v2_verify_tail() -> str:
    """The @2 fail-closed tail: final newer-check through the equality check."""
    lines = MTIME_BLOCK_V2.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if "found paths newer" in line)
    tail = "".join(lines[start:])
    assert "differing from the fixed timestamp" in tail
    return tail


@pytest.mark.skipif(NEEDS_GNU_TOOLCHAIN, reason="GNU find -newermt and stat -c required")
def test_v2_block_collapses_staggered_mtimes(tmp_path: Path) -> None:
    cwd = tmp_path / "testbed"
    (cwd / "sub").mkdir(parents=True)
    old, bulk, fix = 900000000, 1760482140, 1760482753
    files = [cwd / "Makefile", cwd / "pkg" / "a.py", cwd / "sub" / "b.py"]
    (cwd / "pkg").mkdir()
    for path in files:
        path.write_text("x\n", encoding="utf-8")
    _touch(files[0], bulk)
    _touch(files[1], bulk)
    _touch(files[2], fix)  # the leak shape: one fix-touched file stands out
    _touch(cwd / "sub", old)
    link = cwd / "link.py"
    link.symlink_to(files[0])

    proc = _run_block(cwd, MTIME_BLOCK_V2)
    assert proc.returncode == 0, proc.stderr

    stamps = {path.stat().st_mtime for path in files}
    assert len(stamps) == 1
    # touch -t sets local-midnight 2000-01-01, whatever the host zone is.
    expected = time.mktime((2000, 1, 1, 0, 0, 0, 0, 0, -1))
    assert stamps.pop() == pytest.approx(expected)
    assert cwd.joinpath("sub").stat().st_mtime == pytest.approx(expected)
    assert os.lstat(link).st_mtime == pytest.approx(expected)


@pytest.mark.skipif(NEEDS_GNU_TOOLCHAIN, reason="GNU find -newermt and stat -c required")
def test_v2_verify_fails_closed_on_newer_path(tmp_path: Path) -> None:
    cwd = tmp_path / "testbed"
    cwd.mkdir()
    planted = cwd / "new.py"
    planted.write_text("x\n", encoding="utf-8")
    # Normalize the rest of the tree first so the planted file is the only
    # offender the tail can see (a fresh directory itself reads as newer).
    assert _run_block(cwd, MTIME_BLOCK_V2).returncode == 0
    planted.write_text("changed\n", encoding="utf-8")
    proc = _run_block(cwd, _v2_verify_tail())
    assert proc.returncode != 0
    assert "found paths newer than the fixed timestamp" in proc.stderr


@pytest.mark.skipif(NEEDS_GNU_TOOLCHAIN, reason="GNU find -newermt and stat -c required")
def test_v2_verify_fails_closed_on_older_path(tmp_path: Path) -> None:
    cwd = tmp_path / "testbed"
    cwd.mkdir()
    planted = cwd / "old.py"
    planted.write_text("x\n", encoding="utf-8")
    assert _run_block(cwd, MTIME_BLOCK_V2).returncode == 0
    _touch(planted, 900000000)  # older than local-midnight 2000-01-01 in any zone
    proc = _run_block(cwd, _v2_verify_tail())
    assert proc.returncode != 0
    assert "differing from the fixed timestamp" in proc.stderr
    # Contrast: the @1 newer-only check passes the same older file, which is
    # why @2 requires exact equality instead.
    verify_v1_only = MTIME_BLOCK.splitlines()[-1] + "\n"
    assert _run_block(cwd, verify_v1_only).returncode == 0


def _write_touch_shim(shim: Path, real_touch: str) -> None:
    """A ``touch`` that simulates lazy layer materialization re-bumping a path."""
    (shim / "touch").write_text(
        "#!/bin/bash\n"
        'countfile="$(dirname "$0")/count"\n'
        "# Count only tree-normalizing touches (-h); the reference-file stamp\n"
        "# and the simulated re-bump itself use -t/-d and stay out of the count.\n"
        'n=$(cat "$countfile" 2>/dev/null || echo 0)\n'
        'case " $* " in\n'
        '  *" -h "*) echo $((n + 1)) > "$countfile";;\n'
        "esac\n"
        f'"{real_touch}" "$@"\n'
        'if [ "${LAZY_ALWAYS:-0}" = "1" ] || { [ "${LAZY_ONCE:-0}" = "1" ] && [ "$n" = "0" ]; }; then\n'
        f'    "{real_touch}" -d "2020-06-01" "$REBUMP"\n'
        "fi\n",
        encoding="utf-8",
    )
    (shim / "touch").chmod(0o755)


@pytest.mark.skipif(NEEDS_GNU_TOOLCHAIN, reason="GNU find -newermt and stat -c required")
def test_v2_retry_converges_when_first_pass_rebumped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Modal shape: one late materialization after pass 1, clean after pass 2."""
    import shutil

    cwd = tmp_path / "testbed"
    cwd.mkdir()
    victim = cwd / "pkg" / "a.py"
    victim.parent.mkdir()
    victim.write_text("x\n", encoding="utf-8")
    shim = tmp_path / "shim"
    shim.mkdir()
    _write_touch_shim(shim, shutil.which("touch") or "/usr/bin/touch")
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    shim_env = {"LAZY_ONCE": "1", "REBUMP": str(victim)}

    proc = _run_block(cwd, MTIME_BLOCK_V2, extra_env=shim_env)
    assert proc.returncode == 0, proc.stderr
    touches = int((shim / "count").read_text(encoding="utf-8").strip())
    assert touches == 2  # pass 1 dirtied by the re-bump, pass 2 converged
    expected = time.mktime((2000, 1, 1, 0, 0, 0, 0, 0, -1))
    assert victim.stat().st_mtime == pytest.approx(expected)


@pytest.mark.skipif(NEEDS_GNU_TOOLCHAIN, reason="GNU find -newermt and stat -c required")
def test_v2_retry_fails_closed_when_every_pass_rebumped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistently rebumping layer fails closed after exactly 3 passes."""
    import shutil

    cwd = tmp_path / "testbed"
    cwd.mkdir()
    victim = cwd / "a.py"
    victim.write_text("x\n", encoding="utf-8")
    shim = tmp_path / "shim"
    shim.mkdir()
    _write_touch_shim(shim, shutil.which("touch") or "/usr/bin/touch")
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    shim_env = {"LAZY_ALWAYS": "1", "REBUMP": str(victim)}

    proc = _run_block(cwd, MTIME_BLOCK_V2, extra_env=shim_env)
    assert proc.returncode != 0
    assert "newer than the fixed timestamp" in proc.stderr
    touches = int((shim / "count").read_text(encoding="utf-8").strip())
    assert touches == MAX_PASSES_V2
