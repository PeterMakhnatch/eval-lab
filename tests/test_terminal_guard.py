"""Behavioural tests for the terminal-guard-extend@1 transform.

Covers consumer-visible behaviour only: the setup snapshot block, the
grading-time env scrub / plugin pinning, the site-hook quarantine logic
(including the exact code embedded into the container scripts), and the
derivation refusal cases. No Docker, no network, no clock dependence.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evallab.task_variants import VariantInvalid
from evallab.terminal_guard import (
    GUARD_REL,
    MARKER,
    SETUP_ANCHOR,
    SETUP_REL,
    SNAPSHOT_SCHEMA,
    TEST_ENV_ANCHOR,
    TEST_ENV_BLOCK,
    TEST_GUARD_CALL,
    TEST_GUARD_CALL_HARDENED,
    TEST_PYTEST_ANCHOR,
    TEST_REL,
    _embedded_snippet,
    build_guard_changes,
    build_guard_py,
    build_prefetch_changes,
    build_setup_prefetch,
    build_setup_sh,
    build_task_test_sh,
    check_snapshot,
    collect_snapshot,
)

# Literal copies of the real contract shapes (not the module constants) so
# the tests pin the real task format instead of restating it.
SETUP_TEMPLATE = """#!/bin/bash
# Runs once, as root, before the agent starts.
M=/var/lib/mimo
[ -f "$M/ready" ] && exit 0
chmod 700 "$M"
fail() { echo "setup: $*" >&2; exit 1; }
write_blocklist() { cp "$M/files/blocklist" "$M/blocklist"; }

rm -rf /tests
ls -A /app 2>/dev/null > "$M/before" || true

touch "$M/ready"
echo "setup done"
"""

TASK_TEST_TEMPLATE = """#!/bin/sh
set -u
mkdir -p /logs/verifier
# tbench-anti-hack-guard:v1
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
# Fail open when the verifier image has no python3 (node-only tests images are
# excluded from the RL set but still verifiable natively): a missing
# interpreter must not swallow the grader's own reward write.
if command -v python3 >/dev/null 2>&1 && [ -f /tests/anti_hack_guard.py ]; then
  if ! python3 /tests/anti_hack_guard.py; then
    # Guard already wrote reward=0; the reward file is the grading contract.
    exit 0
  fi
fi


python3 -m pytest --ctrf /logs/verifier/ctrf.json -q /tests/test_outputs.py
status=$?
if [ "$status" -eq 0 ]; then printf '1\\n' > /logs/verifier/reward.txt; else printf '0\\n' > /logs/verifier/reward.txt; fi
exit "$status"
"""

# The 2951 shape: relative test path plus "$@" passthrough.
TASK_TEST_RELATIVE = """#!/usr/bin/env bash
set -uo pipefail
# tbench-anti-hack-guard:v1
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
if command -v python3 >/dev/null 2>&1 && [ -f /tests/anti_hack_guard.py ]; then
  if ! python3 /tests/anti_hack_guard.py; then
    exit 0
  fi
fi



mkdir -p /logs/verifier
cd /tests
python3 -m pytest --ctrf /logs/verifier/ctrf.json -q test_outputs.py "$@"
status=$?
exit 0
"""

GUARD_TEMPLATE = '''#!/usr/bin/env python3
"""TBench in-pod anti-hack guard."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def scan(manifest, app=None):
    return None


def reject(reason, reward=None):
    return 1


def main(app=None, reward=None):
    manifest = None
    reason = scan(manifest, app=app)
    if reason:
        return reject(reason, reward=reward)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

TOML_TEMPLATE = """schema_version = "1.4"

[task]
name = "mimo-v2.6-rl/candidate-0109-science-robotics"

[agent]
timeout_sec = 900.0

[verifier]
timeout_sec = 240.0
user = "root"

[environment]
docker_image = "docker.io/xiaomimimo/mimo-v2.6-rl-oss@sha256:92e1760ed72a11016f8792e3f7c8ec97943ad4f16c591164e8d5d4e0b81c9ab5"
workdir = "/app"
cpus = 1
memory_mb = 2048

[environment.healthcheck]
command = "bash -c 'test -f /var/lib/mimo/ready || { mkdir -p /var/lib/mimo && echo __BLOB__ | base64 -d | tar -xzf - -C /var/lib/mimo && bash /var/lib/mimo/setup.sh; }'"
timeout_sec = 1200.0
retries = 0
interval_sec = 5.0
"""


@pytest.fixture()
def parent_dir(tmp_path: Path) -> Path:
    from evallab.strip_future_history import pack_setup

    root = tmp_path / "parent"
    setup = root / "environment" / "setup"
    setup.mkdir(parents=True)
    (setup / "setup.sh").write_text(SETUP_TEMPLATE, encoding="utf-8")
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "anti_hack_guard.py").write_text(GUARD_TEMPLATE, encoding="utf-8")
    (root / "tests" / "task_test.sh").write_text(TASK_TEST_TEMPLATE, encoding="utf-8")
    blob = pack_setup(setup)
    (root / "task.toml").write_text(
        TOML_TEMPLATE.replace("__BLOB__", blob), encoding="utf-8"
    )
    return root


def test_setup_inserts_snapshot_before_ready(parent_dir: Path) -> None:
    updated = build_setup_sh((parent_dir / SETUP_REL).read_text(encoding="utf-8"))
    assert updated.count(SETUP_ANCHOR) == 1
    assert updated.index(MARKER) < updated.index(SETUP_ANCHOR)
    assert "terminal-guard-snapshot.json" in updated
    assert "python3 -S" in updated


def test_setup_is_idempotent(parent_dir: Path) -> None:
    once = build_setup_sh((parent_dir / SETUP_REL).read_text(encoding="utf-8"))
    assert build_setup_sh(once) == once


def test_setup_refuses_missing_or_ambiguous_anchor() -> None:
    with pytest.raises(VariantInvalid, match="ready sentinel"):
        build_setup_sh("#!/bin/bash\necho hi\n")
    with pytest.raises(VariantInvalid, match="ready sentinel"):
        build_setup_sh('touch "$M/ready"\nmid\ntouch "$M/ready"\n')


def test_task_test_sh_scrubs_env_and_pins_plugins(parent_dir: Path) -> None:
    updated = build_task_test_sh((parent_dir / TEST_REL).read_text(encoding="utf-8"))
    env_pos = updated.index(TEST_ENV_ANCHOR)
    scrub_pos = updated.index("unset PYTEST_PLUGINS PYTHONPATH PYTHONSTARTUP")
    guard_pos = updated.index("anti_hack_guard.py")
    pytest_pos = updated.index(TEST_PYTEST_ANCHOR)
    assert env_pos < scrub_pos < guard_pos
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1" in updated
    assert TEST_GUARD_CALL_HARDENED in updated
    assert TEST_GUARD_CALL not in updated.replace(TEST_GUARD_CALL_HARDENED, "")
    assert "$_terminal_guard_plugins --noconftest" in updated[pytest_pos:]
    assert updated.count(MARKER) >= 1


def test_task_test_sh_relative_shape() -> None:
    updated = build_task_test_sh(TASK_TEST_RELATIVE)
    pytest_pos = updated.index(TEST_PYTEST_ANCHOR)
    tail = updated[pytest_pos:]
    assert "$_terminal_guard_plugins --noconftest" in tail
    assert 'test_outputs.py "$@"' in tail


def test_task_test_sh_is_idempotent(parent_dir: Path) -> None:
    once = build_task_test_sh((parent_dir / TEST_REL).read_text(encoding="utf-8"))
    assert build_task_test_sh(once) == once


def test_task_test_sh_refuses_bad_anchors() -> None:
    with pytest.raises(VariantInvalid, match="env anchor"):
        build_task_test_sh("#!/bin/sh\npython3 -m pytest x\npython3 /tests/anti_hack_guard.py\n")


def test_guard_py_wires_site_check() -> None:
    updated = build_guard_py(GUARD_TEMPLATE)
    assert "extended = run_site_check()" in updated
    assert "return reject(extended, reward=reward)" in updated
    assert "def check_snapshot(" in updated
    assert "def collect_snapshot(" in updated
    assert "def guard_site_dirs(" in updated
    # The appended snippet must be valid at the END of a file that already
    # has code (no __future__ imports): this caught a real SyntaxError.
    compile(updated, "anti_hack_guard.py", "exec")
    # New defs must precede the entry-point footer: main() calls
    # run_site_check(), which must already exist (NameError caught live).
    assert updated.index("def run_site_check(") < updated.index('if __name__ == "__main__":')


def test_built_guard_runs_under_python_S_without_snapshot(tmp_path: Path) -> None:
    """Execute the built guard with -S: fail-open pass when no snapshot exists."""
    guard = tmp_path / "anti_hack_guard.py"
    guard.write_text(build_guard_py(GUARD_TEMPLATE), encoding="utf-8")
    env = {
        "PATH": os.environ["PATH"],
        "TBENCH_APP_ROOT": str(tmp_path / "no-app"),
        "TBENCH_FIXTURES": str(tmp_path / "no-fixtures"),
        "TBENCH_REWARD_FILE": str(tmp_path / "reward.txt"),
        "TERMINAL_GUARD_SNAPSHOT": str(tmp_path / "missing.json"),
    }
    proc = subprocess.run(
        [sys.executable, "-S", str(guard)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "no snapshot" in proc.stderr
    assert not (tmp_path / "reward.txt").exists()


def test_guard_py_is_idempotent() -> None:
    once = build_guard_py(GUARD_TEMPLATE)
    assert build_guard_py(once) == once


def test_guard_py_refuses_unknown_shape() -> None:
    with pytest.raises(VariantInvalid, match="main anchor"):
        build_guard_py("#!/usr/bin/env python3\nprint('hi')\n")


def test_guard_changes_touch_only_four_files(parent_dir: Path) -> None:
    changes, _ = build_guard_changes(parent_dir)
    assert set(changes) == {SETUP_REL, GUARD_REL, TEST_REL, "task.toml"}


def test_guard_changes_refuse_double_derive(parent_dir: Path) -> None:
    (parent_dir / SETUP_REL).write_text(
        build_setup_sh((parent_dir / SETUP_REL).read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    with pytest.raises(VariantInvalid, match="already carries"):
        build_guard_changes(parent_dir)


def test_prefetch_inserts_pip_install(parent_dir: Path) -> None:
    updated = build_setup_prefetch(
        (parent_dir / SETUP_REL).read_text(encoding="utf-8"), "stevedore"
    )
    assert "python3 -m pip install stevedore" in updated
    assert updated.index("env-prefetch-network@1") < updated.index(SETUP_ANCHOR)
    assert build_setup_prefetch(updated, "stevedore") == updated


def test_prefetch_changes_touch_only_setup_and_toml(parent_dir: Path) -> None:
    changes, inputs = build_prefetch_changes(parent_dir, "cryptography")
    assert set(changes) == {SETUP_REL, "task.toml"}
    assert inputs["pip_spec"] == "cryptography"


# --- Snapshot / quarantine behaviour on fake site trees -------------------- #


@pytest.fixture()
def site_tree(tmp_path: Path) -> Path:
    """A fake site-packages dir with a baseline hook, a .pth and one plugin dist."""
    site_dir = tmp_path / "site-packages"
    (site_dir / "ctrf").mkdir(parents=True)
    (site_dir / "honest-nspkg.pth").write_text("import sys, types;\n", encoding="utf-8")
    (site_dir / "ctrf" / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    (site_dir / "ctrf" / "main.py").write_text("def pytest_addoption(p):\n    pass\n", encoding="utf-8")
    dist = site_dir / "ctrf-1.0.dist-info"
    dist.mkdir()
    (dist / "METADATA").write_text("Metadata-Version: 2.1\nName: ctrf\nVersion: 1.0\n", encoding="utf-8")
    (dist / "entry_points.txt").write_text("[pytest11]\nctrf = ctrf.main\n", encoding="utf-8")
    return site_dir


def _snapshot_of(site_dir: Path) -> dict:
    return {
        "schema": SNAPSHOT_SCHEMA,
        **collect_snapshot([str(site_dir)]),
    }


def test_collect_snapshot_records_hooks_and_plugins(site_tree: Path) -> None:
    doc = collect_snapshot([str(site_tree)])
    assert set(doc["hooks"]) == {str(site_tree / "honest-nspkg.pth")}
    assert set(doc["plugins"]) == {"ctrf"}
    plugin = doc["plugins"]["ctrf"]
    assert plugin["value"] == "ctrf.main"
    assert plugin["version"] == "1.0"
    assert set(plugin["files"]) == {
        str(site_tree / "ctrf" / "main.py"),
        str(site_tree / "ctrf" / "__init__.py"),
    }


def test_check_clean_tree_passes(site_tree: Path, tmp_path: Path) -> None:
    quarantined, reason = check_snapshot(
        _snapshot_of(site_tree), [str(site_tree)], str(tmp_path / "q")
    )
    assert quarantined == []
    assert reason is None


def test_check_quarantines_plant_but_grades_on(site_tree: Path, tmp_path: Path) -> None:
    """The proven attack shape: sitecustomize + skip plugin + new pytest11 dist."""
    snap = _snapshot_of(site_tree)
    (site_tree / "sitecustomize.py").write_text("import os\nos.environ['PYTEST_PLUGINS']='zzskip'\n", encoding="utf-8")
    (site_tree / "zzskip.py").write_text("def pytest_collection_modifyitems(i):\n    pass\n", encoding="utf-8")
    evil = site_tree / "evil-9.9.dist-info"
    evil.mkdir()
    (evil / "METADATA").write_text("Metadata-Version: 2.1\nName: evil\nVersion: 9.9\n", encoding="utf-8")
    (evil / "entry_points.txt").write_text("[pytest11]\nevil = zzskip\n", encoding="utf-8")
    quarantine = tmp_path / "q"
    quarantined, reason = check_snapshot(snap, [str(site_tree)], str(quarantine))
    assert quarantined == [str(site_tree / "sitecustomize.py")]
    assert reason is None  # plant neutralized; grading continues honestly
    assert not (site_tree / "sitecustomize.py").exists()
    assert (quarantine / "new_pytest_plugins.log").is_file()


def test_check_rejects_mutated_baseline_hook(site_tree: Path, tmp_path: Path) -> None:
    snap = _snapshot_of(site_tree)
    (site_tree / "honest-nspkg.pth").write_text("import os\nos._exit(1)\n", encoding="utf-8")
    _, reason = check_snapshot(snap, [str(site_tree)], str(tmp_path / "q"))
    assert reason == f"protected_interpreter_hook_mutated:{site_tree / 'honest-nspkg.pth'}"


def test_check_rejects_deleted_baseline_hook(site_tree: Path, tmp_path: Path) -> None:
    snap = _snapshot_of(site_tree)
    (site_tree / "honest-nspkg.pth").unlink()
    _, reason = check_snapshot(snap, [str(site_tree)], str(tmp_path / "q"))
    assert reason == f"protected_interpreter_hook_deleted:{site_tree / 'honest-nspkg.pth'}"


def test_check_rejects_mutated_plugin_file(site_tree: Path, tmp_path: Path) -> None:
    snap = _snapshot_of(site_tree)
    (site_tree / "ctrf" / "main.py").write_text("def pytest_collection_modifyitems(i):\n    pass\n", encoding="utf-8")
    _, reason = check_snapshot(snap, [str(site_tree)], str(tmp_path / "q"))
    assert reason == "protected_pytest_plugin_mutated:ctrf"


def test_check_rejects_deleted_plugin(site_tree: Path, tmp_path: Path) -> None:
    import shutil

    snap = _snapshot_of(site_tree)
    shutil.rmtree(site_tree / "ctrf-1.0.dist-info")
    _, reason = check_snapshot(snap, [str(site_tree)], str(tmp_path / "q"))
    assert reason == "protected_pytest_plugin_deleted:ctrf"


def test_embedded_snippet_behaves_like_module(site_tree: Path) -> None:
    """The container-embedded code is exactly what these tests exercise."""
    namespace: dict = {}
    exec(_embedded_snippet(), namespace)
    assert set(namespace) >= {
        "guard_site_dirs",
        "collect_snapshot",
        "check_snapshot",
        "_read_entry_points",
        "_read_dist_version",
        "_plugin_module_files",
    }
    assert namespace["collect_snapshot"]([str(site_tree)]) == collect_snapshot([str(site_tree)])


def test_snapshot_block_runs_and_writes_valid_snapshot(tmp_path: Path) -> None:
    """Run the real setup block under bash with M/fail stubbed (no Docker)."""
    from evallab.terminal_guard import build_setup_sh

    state = tmp_path / "mimo"
    state.mkdir()
    stub = 'fail() { echo "setup: $*" >&2; exit 1; }\nCWD=/nonexistent\ntouch "$M/ready"\n'
    built = build_setup_sh(stub)
    assert "SNIPPET_PLACEHOLDER" not in built
    assert "SNAPSHOT_WRITER_PLACEHOLDER" not in built
    harness = f'M="{state}"\n' + built.replace("CWD=/nonexistent\n", "")
    proc = subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        timeout=180,
        env={"PATH": os.environ["PATH"]},
    )
    assert proc.returncode == 0, proc.stderr
    out = state / "terminal-guard-snapshot.json"
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema"] == SNAPSHOT_SCHEMA
    assert isinstance(doc["hooks"], dict)
    assert isinstance(doc["plugins"], dict)
    assert isinstance(doc["site_dirs"], list) and doc["site_dirs"]


# --- Shell fragment execution (bash only; no Docker/network) ---------------- #


def _run_env_block(tmp_path: Path, snapshot: dict | None) -> subprocess.CompletedProcess[str]:
    if snapshot is not None:
        snap_path = tmp_path / "snapshot.json"
        snap_path.write_text(json.dumps(snapshot), encoding="utf-8")
    else:
        snap_path = tmp_path / "missing.json"
    harness = (
        "set -u\n"
        f'TERMINAL_GUARD_SNAPSHOT="{snap_path}"\n'
        'PYTEST_PLUGINS=zzskip\nPYTHONPATH=/evil\nPYTHONSTARTUP=/evil/startup.py\n'
        + TEST_ENV_BLOCK.replace(
            '${TERMINAL_GUARD_SNAPSHOT:-/var/lib/mimo/terminal-guard-snapshot.json}',
            '"$TERMINAL_GUARD_SNAPSHOT"',
        )
        + '\nprintf "plugins=<%s>\\n" "$_terminal_guard_plugins"\n'
        'printf "autoload=<%s>\\n" "${PYTEST_DISABLE_PLUGIN_AUTOLOAD-unset}"\n'
        'printf "ppt=<%s>\\n" "${PYTEST_PLUGINS-unset}"\n'
        'printf "pp=<%s>\\n" "${PYTHONPATH-unset}"\n'
        'printf "ps=<%s>\\n" "${PYTHONSTARTUP-unset}"\n'
    )
    return subprocess.run(
        ["bash", "-c", harness],
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": os.environ["PATH"]},
    )


def test_env_block_pins_snapshot_plugins(tmp_path: Path, site_tree: Path) -> None:
    proc = _run_env_block(tmp_path, _snapshot_of(site_tree))
    assert proc.returncode == 0, proc.stderr
    assert "plugins=<-p ctrf.main>" in proc.stdout
    assert "autoload=<1>" in proc.stdout
    assert "ppt=<unset>" in proc.stdout
    assert "pp=<unset>" in proc.stdout
    assert "ps=<unset>" in proc.stdout


def test_env_block_fails_open_without_snapshot(tmp_path: Path) -> None:
    proc = _run_env_block(tmp_path, None)
    assert proc.returncode == 0, proc.stderr
    assert "plugins=<>" in proc.stdout
    assert "autoload=<unset>" in proc.stdout
    assert "without plugin pinning" in proc.stderr
