"""Behavioural tests for the mtime-normalize@1 transform."""

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
    MTIME_BLOCK,
    SETUP_REL,
    TRANSFORM_ID,
    build_changes,
    build_setup_sh,
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


NEEDS_GNU_FIND = not _gnu_find_newermt()


def _run_block(cwd: Path, script: str) -> subprocess.CompletedProcess[str]:
    """Run shell text with the setup's ``fail``/``CWD`` contract stubbed."""
    harness = (
        'fail() { echo "setup: $*" >&2; exit 1; }\n'
        f'CWD="{cwd}"\n'
        + script
    )
    return subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": os.environ["PATH"]},
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
