"""Exercise local checkpoint selection with real pytest and isolated gate tools."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "/bin/bash"


@pytest.fixture
def checkpoint(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args == ['--version']:\n"
        "    print('uv 0.9.24')\n"
        "elif args and args[0] == 'sync':\n"
        "    Path('installed').touch()\n"
        "elif 'ruff' in args and os.environ.get('FAIL_STATIC'):\n"
        "    sys.exit(23)\n"
        "elif 'pytest' in args:\n"
        "    offset = args.index('pytest') + 1\n"
        "    os.execv(sys.executable, [sys.executable, '-m', 'pytest', *args[offset:]])\n"
        "elif 'evallab.smoke' in args:\n"
        "    if not Path('test-ran').exists():\n"
        "        sys.exit(24)\n"
        "    Path('smoke-ran').touch()\n"
        "    sys.exit(25)\n"
    )
    uv.chmod(0o755)
    uvx = bin_dir / "uvx"
    uvx.write_text("#!/bin/sh\nexit 0\n")
    uvx.chmod(0o755)
    (tmp_path / "test_selected.py").write_text(
        "from pathlib import Path\ndef test_selected():\n    Path('test-ran').touch()\n"
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.defpath}",
        "HOME": str(tmp_path),
        "PYTEST_ADDOPTS": "",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    env.pop("FAIL_STATIC", None)
    return tmp_path, env


def _run(checkpoint: tuple[Path, dict[str, str]], *args: str) -> subprocess.CompletedProcess[str]:
    root, env = checkpoint
    return subprocess.run(
        [BASH, str(ROOT / "scripts/premerge.sh"), *args],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def test_focused_checkpoint_excludes_unselected_failures(
    checkpoint: tuple[Path, dict[str, str]],
) -> None:
    root, _ = checkpoint
    (root / "test_unselected.py").write_text("def test_unselected():\n    assert False\n")
    result = _run(checkpoint, "--focused", "test_selected.py::test_selected", "-q")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "test-ran").exists()
    assert not (root / "smoke-ran").exists()


def test_static_checkpoint_does_not_execute_tests(
    checkpoint: tuple[Path, dict[str, str]],
) -> None:
    root, _ = checkpoint
    result = _run(checkpoint, "--static")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (root / "test-ran").exists()
    assert not (root / "smoke-ran").exists()


def test_missing_focused_selection_refuses_before_installation(
    checkpoint: tuple[Path, dict[str, str]],
) -> None:
    root, _ = checkpoint
    result = _run(checkpoint, "--focused")
    assert result.returncode == 2
    assert not (root / "installed").exists()
    assert not (root / "test-ran").exists()


def test_focused_test_failure_cannot_pass(
    checkpoint: tuple[Path, dict[str, str]],
) -> None:
    root, _ = checkpoint
    (root / "test_selected.py").write_text("def test_selected():\n    assert False\n")
    result = _run(checkpoint, "--focused", "test_selected.py")
    assert result.returncode == 1, result.stdout + result.stderr
    assert not (root / "smoke-ran").exists()


def test_static_failure_prevents_focused_test_execution(
    checkpoint: tuple[Path, dict[str, str]],
) -> None:
    root, env = checkpoint
    env["FAIL_STATIC"] = "1"
    result = _run(checkpoint, "--focused", "test_selected.py")
    assert result.returncode == 23
    assert not (root / "test-ran").exists()


def test_full_checkpoint_includes_smoke_and_preserves_its_failure(
    checkpoint: tuple[Path, dict[str, str]],
) -> None:
    root, _ = checkpoint
    result = _run(checkpoint, "--full")
    assert result.returncode == 25, result.stdout + result.stderr
    assert (root / "test-ran").exists()
    assert (root / "smoke-ran").exists()
