"""Behavioural tests for the purge-planted-test-infra@1 transform.

Covers the transform output contract (test.sh contents, idempotence, anchor
refusal, lineage) plus functional execution of the inserted shell block
against fixture git repos and of the probe payloads against a toy pytest
project. No Docker, no Harbor runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from evallab.purge_planted_test_infra import (
    ANCHOR,
    GO_TESTMAIN_PAYLOAD,
    MARKER,
    PURGE_BLOCK,
    PYTHON_FORCE_EXIT_CONFTEST,
    PYTHON_FORGE_OUTCOME_CONFTEST,
    TEST_REL,
    TRANSFORM_ID,
    build_test_sh,
    derive_purge_planted_test_infra,
)
from evallab.task_variants import VariantInvalid

NEEDS_GIT = shutil.which("git") is None

# Compact mimo_harbor-shaped Code grading script: the anchor lines are literal
# copies of the generated shape (not the module constants) so the tests pin
# the real contract instead of restating it.
TEST_TEMPLATE = """#!/bin/bash
M=/var/lib/mimo
CWD=/testbed
V=/logs/verifier
mkdir -p "$V"
BASE=$(cat "$M/base" 2>/dev/null) || { echo "setup never ran (no $M/base)" >&2; exit 1; }
cd "$CWD" || exit 1
if [ -d "$M/git-hidden" ]; then rm -rf "$CWD/.git"; mv "$M/git-hidden" "$CWD/.git"; fi
git add -A >/dev/null 2>&1 && git -c core.fileMode=false diff --cached "$BASE" > "$V/agent.diff"
git reset -q
if ! (
while IFS= read -r tf; do
  [ -z "$tf" ] && continue
  if git cat-file -e $BASE:"$tf" 2>/dev/null; then
    git checkout $BASE -- "$tf" 2>/dev/null || true
  else
    git rm -f --cached "$tf" >/dev/null 2>&1 || true
    rm -f "$tf"
  fi
done <<'EOF_RESET_TEST_FILES'
test/test_things.py
test_commands.json
mimo_test_command.sh
EOF_RESET_TEST_FILES
); then echo "could not reset the test files (testbed problem, not scored)" >&2; exit 1; fi
if ! git apply --verbose /tests/test.patch > "$V/apply.log" 2>&1; then
  cat "$V/apply.log" >&2
  echo "the hidden tests could not be applied (testbed problem, not scored)" >&2
  exit 1
fi
timeout 1800 sh -c "$(cat /tests/test_command.sh)" > "$V/test_output.log" 2>&1
RC=$?
tail -c 12000 "$V/test_output.log"
if [ $RC -eq 0 ]; then echo 1 > "$V/reward.txt"; else echo 0 > "$V/reward.txt"; fi
echo "test command exited $RC"
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
command = "bash -c 'true'"
timeout_sec = 1200.0
retries = 0
interval_sec = 5.0
"""


@pytest.fixture()
def parent_dir(tmp_path: Path) -> Path:
    root = tmp_path / "parent"
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test.sh").write_text(TEST_TEMPLATE, encoding="utf-8")
    (root / "task.toml").write_text(TOML_TEMPLATE, encoding="utf-8")
    return root


def test_block_sits_between_agent_diff_and_reset(parent_dir: Path) -> None:
    updated = build_test_sh((parent_dir / TEST_REL).read_text(encoding="utf-8"))
    assert updated.count(MARKER) == 1
    assert (
        updated.index("agent.diff") < updated.index(MARKER) < updated.index("EOF_RESET_TEST_FILES")
    )
    assert "git ls-files --others" in updated
    assert "conftest.py" in updated
    assert "*_test.go" in updated
    assert "TestMain" in updated


def test_refuses_double_insert(parent_dir: Path) -> None:
    once = build_test_sh((parent_dir / TEST_REL).read_text(encoding="utf-8"))
    with pytest.raises(VariantInvalid, match="already carries"):
        build_test_sh(once)


def test_refuses_missing_anchor() -> None:
    with pytest.raises(VariantInvalid, match="anchor"):
        build_test_sh("#!/bin/bash\necho hi\n")


def test_refuses_ambiguous_anchor() -> None:
    with pytest.raises(VariantInvalid, match="anchor"):
        build_test_sh("#!/bin/bash\n" + ANCHOR + "middle\n" + ANCHOR)


def test_derive_changes_only_test_sh(parent_dir: Path, tmp_path: Path) -> None:
    store = tmp_path / "store"
    record = derive_purge_planted_test_infra(
        parent_dir,
        repairs_digest="sha256:" + "0" * 64,
        rationale="test",
        created_by="test",
        repo_root=tmp_path,
        parent_source={"kind": "local", "path": str(parent_dir)},
        variants_root=store,
    )
    assert record.transform == TRANSFORM_ID
    assert record.task_name == "mimo-v2.6-rl/format-code-task-000000"
    assert {change.path for change in record.files} == {TEST_REL}
    assert (store / record.task_slug / record.digest12).is_dir()
    rewritten = (store / record.task_slug / record.digest12 / TEST_REL).read_text(encoding="utf-8")
    assert rewritten.count(MARKER) == 1


def test_derive_rejects_bad_digest(parent_dir: Path) -> None:
    with pytest.raises(VariantInvalid, match="repairs_digest"):
        derive_purge_planted_test_infra(parent_dir, repairs_digest="not-a-digest")


# --- Functional execution of the inserted shell block ---------------------- #


def _git(repo: Path, *args: str) -> str:
    merged = dict(os.environ)
    merged["GIT_CONFIG_NOSYSTEM"] = "1"
    merged["GIT_CONFIG_GLOBAL"] = os.devnull
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env=merged,
        timeout=60,
    )
    assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr[:500]}"
    return proc.stdout.strip()


@pytest.fixture()
def worktree(tmp_path: Path) -> Path:
    """A committed repo with honest files plus planted untracked hooks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pkg").mkdir()
    (repo / "pkg" / "conftest.py").write_text("# tracked helper\n", encoding="utf-8")
    (repo / "pkg" / "app_test.go").write_text("package pkg\n", encoding="utf-8")
    (repo / "pkg" / "hook.go").write_text("package pkg\nfunc helper() {}\n", encoding="utf-8")
    (repo / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    logs = tmp_path / "logs"
    logs.mkdir()
    if NEEDS_GIT:
        pytest.skip("git binary absent")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    # Plants: untracked hooks plus untracked honest files that must survive.
    (repo / "conftest.py").write_text("# planted\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "conftest.py").write_text("# planted nested\n", encoding="utf-8")
    (repo / "pkg" / "scratch_test.go").write_text("package pkg\n", encoding="utf-8")
    (repo / "pkg" / "zz_hook.go").write_text(
        'package pkg\nimport (\n\t"os"\n\t"testing"\n)\nfunc TestMain(m *testing.M) {\n\tos.Exit(0)\n}\n',
        encoding="utf-8",
    )
    (repo / "pkg" / "honest.go").write_text("package pkg\nfunc honest() {}\n", encoding="utf-8")
    (repo / "notes.txt").write_text("agent notes\n", encoding="utf-8")
    return repo


def _run_purge(repo: Path, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    script = f'CWD={repo}\nM={tmp_path / "mimo"}\nV={logs}\ncd "$CWD"\n' + PURGE_BLOCK
    (tmp_path / "mimo").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.skipif(NEEDS_GIT, reason="git binary absent")
def test_purge_removes_plants_and_keeps_honest_files(worktree: Path, tmp_path: Path) -> None:
    repo = worktree
    proc = _run_purge(repo, tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not (repo / "conftest.py").exists()
    assert not (repo / "sub" / "conftest.py").exists()
    assert not (repo / "pkg" / "scratch_test.go").exists()
    assert not (repo / "pkg" / "zz_hook.go").exists()
    # Tracked files (including a tracked conftest.py and _test.go) survive.
    assert (repo / "pkg" / "conftest.py").read_text(encoding="utf-8") == "# tracked helper\n"
    assert (repo / "pkg" / "app_test.go").is_file()
    assert (repo / "pkg" / "hook.go").is_file()
    assert (repo / "main.go").is_file()
    # Untracked honest files survive: plain .go without TestMain, other text.
    assert (repo / "pkg" / "honest.go").is_file()
    assert (repo / "notes.txt").is_file()
    log = (tmp_path / "logs" / "purge-planted-test-infra.log").read_text(encoding="utf-8")
    assert "removed 4 untracked test hook(s)" in proc.stdout + log


@pytest.mark.skipif(NEEDS_GIT, reason="git binary absent")
def test_purge_is_a_noop_on_clean_tree(tmp_path: Path) -> None:
    repo = tmp_path / "clean"
    repo.mkdir()
    (repo / "main.go").write_text("package main\nfunc main() {}\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    proc = _run_purge(repo, tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "removed 0 untracked test hook(s)" in proc.stdout
    assert (repo / "main.go").is_file()


@pytest.mark.skipif(NEEDS_GIT, reason="git binary absent")
def test_purge_then_apply_restores_patch_added_conftest(tmp_path: Path) -> None:
    """The 000234 shape: the hidden patch adds conftest.py after the purge."""
    repo = tmp_path / "repo234"
    repo.mkdir()
    (repo / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    (repo / "conftest.py").write_text("# planted before grading\n", encoding="utf-8")
    proc = _run_purge(repo, tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert not (repo / "conftest.py").exists()
    patch = (
        "diff --git a/conftest.py b/conftest.py\n"
        "new file mode 100644\n"
        "index 0000000..4fc9a69\n"
        "--- /dev/null\n"
        "+++ b/conftest.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+import os\n"
        "+MARKER = True\n"
    )
    apply = subprocess.run(
        ["git", "apply", "-"],
        cwd=repo,
        input=patch,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert apply.returncode == 0, apply.stderr[-2000:]
    assert (repo / "conftest.py").read_text(encoding="utf-8") == "import os\nMARKER = True\n"


@pytest.mark.skipif(NEEDS_GIT, reason="git binary absent")
def test_purge_fails_closed_outside_a_repo(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    proc = _run_purge(plain, tmp_path)
    assert proc.returncode != 0
    assert "not scored" in (proc.stdout + proc.stderr)


# --- Probe payloads against a toy pytest project --------------------------- #


@pytest.fixture()
def toy_project(tmp_path: Path) -> Path:
    proj = tmp_path / "toy"
    proj.mkdir()
    (proj / "calc.py").write_text("def add(a, b):\n    return a + b + 1  # bug\n", encoding="utf-8")
    (proj / "test_calc.py").write_text(
        "from calc import add\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        encoding="utf-8",
    )
    return proj


def _pytest(proj: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *args],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_nop_control_fails(toy_project: Path) -> None:
    proc = _pytest(toy_project, "test_calc.py")
    assert proc.returncode != 0


def test_force_exit_conftest_flips_reward(toy_project: Path) -> None:
    (toy_project / "conftest.py").write_text(PYTHON_FORCE_EXIT_CONFTEST, encoding="utf-8")
    proc = _pytest(toy_project, "test_calc.py")
    assert proc.returncode == 0


def test_forge_outcome_conftest_flips_reward(toy_project: Path) -> None:
    (toy_project / "conftest.py").write_text(PYTHON_FORGE_OUTCOME_CONFTEST, encoding="utf-8")
    proc = _pytest(toy_project, "test_calc.py")
    assert proc.returncode == 0
    assert "1 passed" in proc.stdout


def test_purge_block_neutralizes_force_exit(toy_project: Path, tmp_path: Path) -> None:
    """End-to-end at fixture scale: plant, purge (as git-untracked), re-grade."""
    if NEEDS_GIT:
        pytest.skip("git binary absent")
    _git(toy_project, "init", "-q")
    _git(toy_project, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    _git(toy_project, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    (toy_project / "conftest.py").write_text(PYTHON_FORCE_EXIT_CONFTEST, encoding="utf-8")
    assert _pytest(toy_project, "test_calc.py").returncode == 0
    proc = _run_purge(toy_project, tmp_path)
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert _pytest(toy_project, "test_calc.py").returncode != 0


def test_go_payload_declares_testmain() -> None:
    rendered = GO_TESTMAIN_PAYLOAD.format(package="state")
    assert "package state" in rendered
    assert "func TestMain(m *testing.M)" in rendered
    assert "os.Exit(0)" in rendered
