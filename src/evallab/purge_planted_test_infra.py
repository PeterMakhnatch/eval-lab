"""Purge agent-planted test hooks before MiMo code grading.

Transform id: ``purge-planted-test-infra@1``.

The hole (read, not re-derived here): Xiaomi's ``OpenSourceCodeEnvironment``
grades ``reset patch-touched files -> git apply test_patch -> run test_command``
(``opensource_code.py::_do_calculate_reward`` at ``mimoagent@467f0a1``), and the
FineEnvs ``tests/test.sh`` keeps that contract verbatim. The reset only restores
files named in the hidden test patch, so anything else the agent leaves behind
survives into grading:

* pytest auto-imports **any** ``conftest.py`` on the path from the rootdir down
  to the collected test files. A planted ``/testbed/conftest.py`` executes
  arbitrary code inside the grading process and dictates its exit code.
* ``go test`` compiles **every** ``*_test.go`` file in each tested package, and
  a planted ``TestMain`` (which must live in the test binary, i.e. a
  ``*_test.go`` file, or any ``.go`` file declaring ``func TestMain``) replaces
  the whole test run for that package with ``os.Exit(0)``.

Both filenames are enforced by the toolchain, so deleting exactly the untracked
files in that scope closes the vector without touching honest work:

* tracked files are never deleted: oracle fixes and patch-modified
  ``conftest.py`` files (e.g. 000557, 000767, 001508, 001801, 001827, 002188,
  whose hidden patches modify a tracked ``conftest.py``) survive;
* patch-*added* paths (e.g. 000234, whose hidden patch adds ``conftest.py``)
  are applied by ``git apply`` *after* the purge block runs, so they cannot be
  harmed either;
* the agent's forensic record is preserved: the purge runs after
  ``tests/test.sh`` captures ``agent.diff``.

The block is inserted into ``tests/test.sh`` between ``git reset -q`` and the
test-file reset, and fails closed (exit 1, "not scored", like the existing
reset/apply failures) when the worktree cannot be enumerated or a hook cannot
be removed.

Deliberately out of scope (documented residuals, not oversights):

* tracked test-config edits (e.g. an agent modifying a tracked
  ``pytest.ini``/``setup.cfg``) are not restored by the reset and not purged
  here; restoring them would need the base blob for every config path.
* ``func init()`` plants in a plain (non-test) ``.go`` file, interpreter hooks
  outside the worktree (``sitecustomize.py``/``.pth``), and ``conftest.py``
  hidden behind a planted ``.gitignore`` (``git ls-files --others
  --exclude-standard`` does not list ignored files) are not covered.
* Go fixes that add a *new* ``*_test.go`` or ``conftest.py`` file outside the
  hidden test patch would lose that file at grade time; no such fix was found
  on the probed tasks (their fix commits touch tracked source/test files
  only), but the full 2,698-task fix corpus was not surveyed.
"""

from __future__ import annotations

import hashlib
import tomllib
from pathlib import Path
from typing import Any

from evallab.task_variants import VariantInvalid, VariantRecord, derive_task

#: Transform id recorded in lineage.
TRANSFORM_ID = "purge-planted-test-infra@1"

#: Marker comment identifying the inserted block (also the idempotence guard).
MARKER = "purge-planted-test-infra@1"

#: Package-relative path of the grading script this transform rewrites.
TEST_REL = "tests/test.sh"

#: Line after which the purge block is inserted (the agent-diff anchor: the
#: purge must run after agent.diff is captured and before the reset/apply).
ANCHOR = "git reset -q\n"

#: Grading-time purge. Runs with ``$CWD`` (repo), ``$M`` (setup state) and
#: ``$V`` (verifier logs) set by the surrounding ``test.sh``.
PURGE_BLOCK = """\
# purge-planted-test-infra@1: delete untracked test hooks the reset below cannot
# see (it only restores files named in the hidden test patch). pytest
# auto-imports any conftest.py under the worktree, and a TestMain in any _test.go
# (or any .go file declaring one) takes over `go test`; either can forge exit 0.
# Tracked files are untouched, and patch-added paths are applied after this block.
_purge_log="$V/purge-planted-test-infra.log"
: > "$_purge_log" || { echo "purge-planted-test-infra: cannot write log (testbed problem, not scored)" >&2; exit 1; }
_purge_list=$(mktemp "$V/purge-list.XXXXXX") || { echo "purge-planted-test-infra: cannot stage file list (testbed problem, not scored)" >&2; exit 1; }
git ls-files --others --exclude-standard > "$_purge_list" || { echo "purge-planted-test-infra: cannot list untracked files (testbed problem, not scored)" >&2; exit 1; }
_purge_n=0
while IFS= read -r _purge_f; do
  [ -n "$_purge_f" ] || continue
  _purge_b=${_purge_f##*/}
  _purge_drop=0
  case "$_purge_b" in
    conftest.py) _purge_drop=1 ;;
    *_test.go) _purge_drop=1 ;;
  esac
  if [ "$_purge_drop" -eq 0 ]; then
    case "$_purge_f" in
      *.go) grep -q -E 'func TestMain[[:space:]]*\\(' "$_purge_f" 2>/dev/null && _purge_drop=1 ;;
    esac
  fi
  if [ "$_purge_drop" -eq 1 ]; then
    rm -f -- "$_purge_f" || { echo "purge-planted-test-infra: cannot remove $_purge_f (testbed problem, not scored)" >&2; exit 1; }
    echo "removed untracked test hook: $_purge_f" >> "$_purge_log"
    _purge_n=$((_purge_n + 1))
  fi
done < "$_purge_list"
rm -f "$_purge_list"
echo "purge-planted-test-infra: removed $_purge_n untracked test hook(s)" | tee -a "$_purge_log"
"""

#: Probe payload: a planted ``conftest.py`` that exits the grading pytest with
#: status 0 before any test runs. Used for validation only.
PYTHON_FORCE_EXIT_CONFTEST = """\
import os


def pytest_configure(config):
    os._exit(0)
"""

#: Probe payload: a planted ``conftest.py`` that lets the suite run but rewrites
#: every failed call-phase report to ``passed``. Used for validation only.
#: (Do not set ``report.wasxfail``: assigning ``None`` raises INTERNALERROR on
#: the pinned pytest and turns the forgery into exit 3.)
PYTHON_FORGE_OUTCOME_CONFTEST = """\
import pytest


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if call.when == "call" and report.failed:
        report.outcome = "passed"
    return report
"""

#: Probe payload template: a planted Go test hook that skips the whole package
#: suite with status 0. ``{package}`` is the tested package name. Used for
#: validation only.
GO_TESTMAIN_PAYLOAD = """\
package {package}

import (
\t"os"
\t"testing"
)

func TestMain(m *testing.M) {{
\tos.Exit(0)
}}
"""


def build_test_sh(parent_test_sh: str) -> str:
    """Parent ``tests/test.sh`` plus the purge block (idempotent).

    Raises :class:`VariantInvalid` when the parent already carries the block
    or has no ``git reset -q`` anchor line.
    """
    if MARKER in parent_test_sh:
        raise VariantInvalid("parent test.sh already carries purge-planted-test-infra@1")
    if parent_test_sh.count(ANCHOR) != 1:
        raise VariantInvalid("test.sh has no unique `git reset -q` anchor; refusing to purge")
    return parent_test_sh.replace(ANCHOR, ANCHOR + PURGE_BLOCK, 1)


def derive_purge_planted_test_infra(
    parent_dir: Path | str,
    *,
    repairs_digest: str,
    rationale: str = (
        "Delete untracked conftest.py / *_test.go / TestMain-bearing .go files "
        "before grading; the test-patch reset cannot see them and pytest / go test "
        "execute them with control of the exit code. Tracked files untouched."
    ),
    created_by: str = "mimo-planted-file-guard",
    repo_root: Path | str | None = None,
    parent_source: dict[str, Any] | None = None,
    variants_root: Path | str | None = None,
) -> VariantRecord:
    """Derive the ``purge-planted-test-infra@1`` variant of a MiMo task package."""
    import re

    if re.fullmatch(r"sha256:[0-9a-f]{64}", repairs_digest) is None:
        raise VariantInvalid(f"repairs_digest is not a sha256 digest: {repairs_digest}")
    parent = Path(parent_dir)
    try:
        data = tomllib.loads((parent / "task.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise VariantInvalid(f"parent task.toml is missing or invalid: {exc}") from exc
    task_raw = data.get("task") if isinstance(data, dict) else None
    task = task_raw if isinstance(task_raw, dict) else {}
    task_name = task.get("name")
    environment = data.get("environment") if isinstance(data, dict) else None
    env = environment if isinstance(environment, dict) else {}
    workdir = env.get("workdir")
    image = env.get("docker_image")
    if not isinstance(task_name, str) or not isinstance(workdir, str) or not isinstance(image, str):
        raise VariantInvalid("parent task.toml is missing name, workdir or docker_image")
    test_path = parent / TEST_REL
    if not test_path.is_file():
        raise VariantInvalid(f"parent has no {TEST_REL}")
    parent_sh = test_path.read_text(encoding="utf-8")
    new_sh = build_test_sh(parent_sh)
    changes: dict[str, bytes | None] = {TEST_REL: new_sh.encode("utf-8")}
    inputs: dict[str, Any] = {
        "parent_task": task_name,
        "workdir": workdir,
        "docker_image": image,
        "repairs_digest": repairs_digest,
        "removed": ["untracked-conftest.py", "untracked-*-test.go", "untracked-TestMain-go"],
        "test_before_sha256": f"sha256:{hashlib.sha256(parent_sh.encode()).hexdigest()}",
        "test_after_sha256": f"sha256:{hashlib.sha256(new_sh.encode()).hexdigest()}",
    }
    kwargs: dict[str, Any] = {}
    if repo_root is not None:
        kwargs["repo_root"] = repo_root
    if parent_source is not None:
        kwargs["parent_source"] = parent_source
    if variants_root is not None:
        kwargs["variants_root"] = variants_root
    return derive_task(
        parent,
        changes=changes,
        transform=TRANSFORM_ID,
        rationale=rationale,
        created_by=created_by,
        inputs=inputs,
        **kwargs,
    )


__all__ = [
    "ANCHOR",
    "GO_TESTMAIN_PAYLOAD",
    "MARKER",
    "PURGE_BLOCK",
    "PYTHON_FORCE_EXIT_CONFTEST",
    "PYTHON_FORGE_OUTCOME_CONFTEST",
    "TEST_REL",
    "TRANSFORM_ID",
    "build_test_sh",
    "derive_purge_planted_test_infra",
]
