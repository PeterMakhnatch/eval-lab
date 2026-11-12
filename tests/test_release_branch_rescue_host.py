"""Host replay of the release-branch-rescue fixture, oracle, and shipped verifier.

The container runs the full recovery contract: rescue tag, attached ``main``,
upstream tracking, and the exact integrated tree. On the host only data-view
regressions run (``tests/test_release_repository_view.py``). This module closes
that gap in about 5 seconds so oracle or verifier drift fails on every pull
request.

The fixture script writes system paths by design, so it is never run unchanged:
every system literal is redirected into one temporary root first, and the copy
is asserted clean before the script executes. The oracle runs with a redirected
``cd`` and an explicit ``GIT_CONFIG_GLOBAL``. The shipped verifier runs in a
child pytest (``-I -c /dev/null --noconftest``) through an adapter that sets
``WORKSPACE`` and ``ORIGIN_GIT``. Controls are never executed here.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

TASK = Path(__file__).resolve().parents[1] / "library/tasks/release-branch-rescue"
FIXTURE = TASK / "environment" / "build_fixture.sh"
ORACLE = TASK / "solution" / "solve.sh"
VERIFIER = TASK / "tests" / "test_rescue.py"

BASH = Path("/bin/bash")
PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CHILD_TIMEOUT_SECONDS = 300

# The five system path literals in the fixture script: four write targets and
# the final ``cd /`` cleanup. Each is prefixed with the temporary root.
SYSTEM_TARGETS = re.compile(r"/etc/gitconfig|/srv|/workspace|/tmp/upstream-dev")
SYSTEM_LEFTOVER = re.compile(r"(?<![\w>])/(?:etc|srv|workspace|tmp)(?:/|\b)")

# Host replay adapter: load the shipped verifier from the adjacent copy, point
# it at the temporary fixture, and re-export its tests and fixtures.
ADAPTER = '''\
"""Host replay adapter for the shipped release-branch-rescue verifier."""

import importlib.util
import os
from pathlib import Path

VERIFIER = Path(__file__).with_name("test_rescue.py")
FIXTURE_ATTRIBUTES = ("_pytestfixturefunction", "_fixture_function_marker", "_fixture_function")

spec = importlib.util.spec_from_file_location("release_branch_rescue_verifier", VERIFIER)
assert spec is not None and spec.loader is not None, VERIFIER
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

module.WORKSPACE = os.environ["WORKSPACE"]
module.ORIGIN_GIT = os.environ["ORIGIN_GIT"]

for _name, _value in vars(module).items():
    if _name.startswith("test_") or any(
        hasattr(_value, attribute) for attribute in FIXTURE_ATTRIBUTES
    ):
        globals()[_name] = _value
'''


def _host_env(root: Path) -> dict[str, str]:
    """A scrubbed environment: no host Git configuration or temporary state."""
    return {
        "PATH": PATH,
        "HOME": str(root / "home"),
        "TMPDIR": str(root / "tmp"),
        "GIT_CONFIG_GLOBAL": str(root / "etc" / "gitconfig"),
        "GIT_CONFIG_SYSTEM": os.devnull,
        "LANG": "C",
    }


def _run(argv: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=root,
        env=_host_env(root),
        text=True,
        capture_output=True,
        timeout=CHILD_TIMEOUT_SECONDS,
        check=False,
    )


def _temporary_root(tmp_path_factory: pytest.TempPathFactory, name: str) -> Path:
    root = tmp_path_factory.mktemp(name).resolve()
    for child in ("etc", "home", "tmp"):
        (root / child).mkdir()
    return root


def _redirected_fixture(root: Path) -> Path:
    """Copy the fixture script with every system path inside ``root``."""
    text = FIXTURE.read_text()
    assert set(SYSTEM_TARGETS.findall(text)) == {
        "/etc/gitconfig",
        "/srv",
        "/workspace",
        "/tmp/upstream-dev",
    }, "the fixture script changed shape; update the redirect list before running it"
    text = SYSTEM_TARGETS.sub(lambda match: f"{root}{match.group(0)}", text)
    assert text.count("\ncd /\n") == 1, "expected the single final `cd /` cleanup line"
    text = text.replace("\ncd /\n", f"\ncd {root}\n")
    copy = root / "build_fixture.sh"
    copy.write_text(text)
    leftovers = SYSTEM_LEFTOVER.findall(text.replace(str(root), "<ROOT>"))
    assert leftovers == [], f"system path left in the fixture copy: {leftovers}"
    return copy


@pytest.fixture(scope="module")
def redirected(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The redirected fixture script, not yet executed."""
    return _redirected_fixture(_temporary_root(tmp_path_factory, "rescue-fixture"))


@pytest.fixture(scope="module")
def built(redirected: Path) -> Path:
    """The untouched fixture: detached HEAD at v1, lost L1/L2 reachable by reflog."""
    result = _run([str(BASH), str(redirected)], redirected.parent)
    assert result.returncode == 0, f"fixture build failed:\n{result.stderr}{result.stdout}"
    assert (redirected.parent / "srv" / "origin.git").is_dir()
    assert (redirected.parent / "workspace" / "release" / ".git").is_dir()
    assert (redirected.parent / "etc" / "gitconfig").is_file()
    return redirected.parent


@pytest.fixture(scope="module")
def recovered(built: Path) -> Path:
    """The fixture after the shipped oracle solution runs."""
    text = ORACLE.read_text()
    assert text.count("cd /workspace/release") == 1, "the oracle changed shape"
    copy = built / "solve.sh"
    copy.write_text(text.replace("cd /workspace/release", f"cd {built}/workspace/release"))
    result = _run([str(BASH), str(copy)], built)
    assert result.returncode == 0, f"oracle failed:\n{result.stderr}{result.stdout}"
    return built


@pytest.fixture(scope="module")
def untouched(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A second fresh fixture that the oracle never touches (the no-op control)."""
    root = _temporary_root(tmp_path_factory, "rescue-nop")
    result = _run([str(BASH), str(_redirected_fixture(root))], root)
    assert result.returncode == 0, f"fixture build failed:\n{result.stderr}{result.stdout}"
    return root


@pytest.fixture(scope="module")
def adapter(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The child-run adapter beside its own copy of the shipped verifier."""
    directory = tmp_path_factory.mktemp("rescue-verifier")
    (directory / "test_rescue.py").write_text(VERIFIER.read_text())
    (directory / "test_release_host_adapter.py").write_text(ADAPTER)
    return directory


def _child(root: Path, adapter: Path) -> subprocess.CompletedProcess[str]:
    """Run the shipped verifier against ``root`` in an isolated pytest process."""
    env = _host_env(root)
    env["WORKSPACE"] = str(root / "workspace" / "release")
    env["ORIGIN_GIT"] = str(root / "srv" / "origin.git")
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-m",
            "pytest",
            "-c",
            os.devnull,
            "--noconftest",
            "-p",
            "no:cacheprovider",
            "-q",
            str(adapter / "test_release_host_adapter.py"),
        ],
        cwd=adapter,
        env=env,
        text=True,
        capture_output=True,
        timeout=CHILD_TIMEOUT_SECONDS,
        check=False,
    )


def test_redirected_fixture_copy_has_no_system_path(redirected: Path) -> None:
    masked = redirected.read_text().replace(str(redirected.parent), "<ROOT>")
    assert SYSTEM_LEFTOVER.findall(masked) == []
    assert "\ncd /\n" not in masked


def test_oracle_passes_the_shipped_verifier(recovered: Path, adapter: Path) -> None:
    result = _child(recovered, adapter)
    assert result.returncode == 0, f"{result.stdout}{result.stderr}"
    assert re.search(r"\b9 passed\b", result.stdout), result.stdout


def test_untouched_fixture_fails_the_rescue_tag_check(untouched: Path, adapter: Path) -> None:
    result = _child(untouched, adapter)
    assert result.returncode == 1, f"{result.stdout}{result.stderr}"
    assert "test_rescue_tag_points_to_original_l2" in result.stdout, result.stdout
