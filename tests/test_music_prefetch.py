"""Behavioural tests for the music-prefetch-abcmidi@1 transform."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from evallab.music_prefetch import (
    ABCMIDI_VERSION,
    ANCHOR,
    GRADER_GUARD,
    MARKER,
    PREFETCH_BLOCK,
    SETUP_REL,
    TEST_REL,
    TRANSFORM_ID,
    build_changes,
    build_setup_sh,
)
from evallab.strip_future_history import pack_setup, unpack_setup_blob
from evallab.task_variants import VariantInvalid

# Compact mimo_harbor-shaped music setup tail: the anchor line is a literal
# copy of the generated shape (not the module constant) so the test pins the
# real contract instead of restating it.
SETUP_TEMPLATE = """#!/bin/bash
M=/var/lib/mimo
[ -f "$M/ready" ] && exit 0
chmod 700 "$M"
fail() { echo "setup: $*" >&2; exit 1; }
write_blocklist() { cp "$M/files/blocklist" "$M/blocklist"; }

mkdir -p /app

touch "$M/ready"
echo "setup done"
"""

# Literal copy of the grade-time install guard in the real tests/test.sh:
# the transform relies on this no-op instead of rewriting grader logic.
TEST_TEMPLATE = """#!/bin/bash
# Music grading: Xiaomi's scorer (abc2midi + 18 human-likeness features, with its validity gate). abc2midi is
# installed only now, at the version the explorer uses, so the agent could not have run it while composing.
mkdir -p /logs/verifier
if ! command -v abc2midi >/dev/null 2>&1; then
  apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends abcmidi=20250216+ds-1 >/dev/null \\
    || { echo "could not install abcmidi (testbed problem, not scored)" >&2; exit 1; }
fi
cd /tests && python3 /tests/grade.py
"""

TOML_TEMPLATE = """schema_version = "1.4"

[task]
name = "mimo-v2.6-rl/music-gk-0000"
description = "synthetic parent"

[agent]
timeout_sec = 900.0

[verifier]
timeout_sec = 600.0
user = "root"

[environment]
docker_image = "docker.io/library/python@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f"
workdir = "/app"
cpus = 1
memory_mb = 2048
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
        "FROM docker.io/library/python@sha256:f77a\n", encoding="utf-8"
    )
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test.sh").write_text(TEST_TEMPLATE, encoding="utf-8")
    (root / "instruction.md").write_text("compose it", encoding="utf-8")
    blob = pack_setup(setup)
    (root / "task.toml").write_text(
        TOML_TEMPLATE.replace("__BLOB__", blob), encoding="utf-8"
    )
    return root


def test_setup_sh_runs_block_before_sentinel(parent_dir: Path) -> None:
    parent_text = (parent_dir / SETUP_REL).read_text(encoding="utf-8")
    new_text = build_setup_sh(parent_text)

    assert new_text.count(ANCHOR) == 1
    assert new_text.index(MARKER) < new_text.index(ANCHOR)
    assert TRANSFORM_ID in new_text


def test_setup_sh_transform_is_idempotent(parent_dir: Path) -> None:
    parent_text = (parent_dir / SETUP_REL).read_text(encoding="utf-8")
    once = build_setup_sh(parent_text)
    assert build_setup_sh(once) == once


def test_refuses_setup_without_unique_sentinel() -> None:
    with pytest.raises(VariantInvalid, match="ready sentinel"):
        build_setup_sh("mkdir -p /app\n")
    with pytest.raises(VariantInvalid, match="ready sentinel"):
        build_setup_sh('touch "$M/ready"\nmid\ntouch "$M/ready"\n')


def test_only_setup_and_toml_change(parent_dir: Path) -> None:
    changes, _ = build_changes(parent_dir)
    assert set(changes) == {SETUP_REL, "task.toml"}
    assert not any(key.startswith("tests/") for key in changes)


def test_inputs_record_pinned_version_and_arch_note(parent_dir: Path) -> None:
    _, inputs = build_changes(parent_dir)
    assert inputs["abcmidi_version"] == ABCMIDI_VERSION == "20250216+ds-1"
    assert "amd64" in inputs["arch_note"] and "arm64" in inputs["arch_note"]
    assert ".deb" not in inputs["arch_note"]
    assert ABCMIDI_VERSION in PREFETCH_BLOCK
    assert "amd64" not in PREFETCH_BLOCK and "arm64" not in PREFETCH_BLOCK


def test_refuses_double_derive(parent_dir: Path) -> None:
    (parent_dir / SETUP_REL).write_text(
        build_setup_sh((parent_dir / SETUP_REL).read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    with pytest.raises(VariantInvalid, match="already carries"):
        build_changes(parent_dir)


def test_refuses_grader_without_presence_guard(parent_dir: Path) -> None:
    (parent_dir / TEST_REL).write_text(
        "#!/bin/bash\napt-get install -y abcmidi >/dev/null\n", encoding="utf-8"
    )
    with pytest.raises(VariantInvalid, match="presence guard"):
        build_changes(parent_dir)


def test_embedded_payload_carries_new_setup(parent_dir: Path) -> None:
    changes, _ = build_changes(parent_dir)
    new_setup = (changes[SETUP_REL] or b"").decode("utf-8")
    new_toml = (changes["task.toml"] or b"").decode("utf-8")

    blob = pack_setup(parent_dir / "environment" / "setup", setup_sh=new_setup.encode())
    members = unpack_setup_blob(blob)
    assert members["setup.sh"].decode("utf-8") == new_setup
    assert blob in new_toml


def test_grader_guard_matches_real_shape(parent_dir: Path) -> None:
    test_sh = (parent_dir / TEST_REL).read_text(encoding="utf-8")
    assert GRADER_GUARD in test_sh
    assert ABCMIDI_VERSION in test_sh


# --- Functional execution of the inserted shell block (stubbed apt) -------- #


def _run_block(tmp_path: Path, *, with_abc2midi: bool, apt_fails: bool) -> subprocess.CompletedProcess[str]:
    """Run the prefetch block with a stubbed PATH: no network, no apt."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if with_abc2midi:
        (bindir / "abc2midi").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (bindir / "abc2midi").chmod(0o755)
    marker = tmp_path / "apt-called"
    if apt_fails:
        apt_body = f"#!/bin/sh\ntouch {marker}\nexit 1\n"
    else:
        apt_body = (
            f"#!/bin/sh\ntouch {marker}\n"
            f"printf '#!/bin/sh\\nexit 0\\n' > {bindir}/abc2midi\nchmod +x {bindir}/abc2midi\nexit 0\n"
        )
    (bindir / "apt-get").write_text(apt_body, encoding="utf-8")
    (bindir / "apt-get").chmod(0o755)
    script = (
        f'M="{tmp_path}"\nCWD=/app\nfail() {{ echo "setup: $*" >&2; exit 1; }}\nPATH="{bindir}:/usr/bin:/bin"\n'
        + PREFETCH_BLOCK
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_block_is_noop_when_abc2midi_present(tmp_path: Path) -> None:
    proc = _run_block(tmp_path, with_abc2midi=True, apt_fails=True)
    assert proc.returncode == 0
    assert not (tmp_path / "apt-called").exists()


def test_block_installs_and_verifies(tmp_path: Path) -> None:
    proc = _run_block(tmp_path, with_abc2midi=False, apt_fails=False)
    assert proc.returncode == 0
    assert (tmp_path / "apt-called").exists()
    assert (tmp_path / "bin" / "abc2midi").exists()


def test_block_fails_closed_when_install_fails(tmp_path: Path) -> None:
    proc = _run_block(tmp_path, with_abc2midi=False, apt_fails=True)
    assert proc.returncode != 0
    assert "music-prefetch-abcmidi@1" in proc.stderr
