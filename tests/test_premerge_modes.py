"""Exercise local checkpoint selection with real pytest and isolated gate tools."""

from __future__ import annotations

import json
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
        "elif 'evallab.lessons' in args and os.environ.get('FAIL_LESSONS'):\n"
        "    print('lessons unavailable', file=sys.stderr)\n"
        "    sys.exit(26)\n"
        "elif 'ship' in args:\n"
        "    from evallab.entry import run_ship\n"
        "    sys.exit(run_ship(args[args.index('ship'):]))\n"
        "elif 'pytest' in args:\n"
        "    offset = args.index('pytest') + 1\n"
        "    os.execv(sys.executable, [sys.executable, '-m', 'pytest', "
        "'-p', 'xdist.plugin', *args[offset:]])\n"
        "elif 'devloop' in args:\n"
        "    from evallab.devloop import run_devloop\n"
        "    sys.exit(run_devloop(Path.cwd(), since=args[args.index('--since') + 1],\n"
        "                         checkpoint='--checkpoint' in args, run='--run' in args))\n"
        "elif 'evallab.smoke' in args:\n"
        "    if not Path('test-ran').exists():\n"
        "        sys.exit(24)\n"
        "    Path('smoke-ran').touch()\n"
        "    sys.exit(25)\n"
    )
    uv.chmod(0o755)
    uvx = bin_dir / "uvx"
    uvx.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "${CHECKER_OUTPUT:-clean checker}"\n'
        'exit "${CHECKER_EXIT:-0}"\n'
    )
    mktemp = bin_dir / "mktemp"
    mktemp.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        f"path = subprocess.check_output([{shutil.which('mktemp')!r}, *sys.argv[1:]], "
        "text=True).strip()\n"
        "Path('receipt-path').write_text(path)\n"
        "print(path)\n"
    )
    mktemp.chmod(0o755)
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
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    env.pop("FAIL_STATIC", None)
    env.pop("EVALLAB_PREPUSH_BASE", None)
    for name in ("FAIL_LESSONS", "CHECKER_OUTPUT", "CHECKER_EXIT",
                 "EVALLAB_PREPUSH_NEEDS_BASE", "EVALLAB_PREPUSH_REASON"):
        env.pop(name, None)
    fake = ROOT / "tests/fixtures/ship_fake_gh.py"
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n" + fake.read_text().split("\n", 1)[1])
    gh.chmod(0o755)
    state = tmp_path / "github.json"
    state.write_text(json.dumps({"pr": {}, "open_prs": []}))
    env["SHIP_FAKE_STATE"] = str(state)
    for args in (["init", "-qb", "topic"], ["add", "--", "test_selected.py"],
                 ["commit", "-qm", "base"],
                 ["update-ref", "refs/remotes/origin/main", "HEAD"]):
        subprocess.run(["git", *args], cwd=tmp_path, env=env, check=True, capture_output=True)
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


def _make_prepush(
    checkpoint: tuple[Path, dict[str, str]], selectors: str = ""
) -> subprocess.CompletedProcess[str]:
    root, env = checkpoint
    (root / "scripts").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "scripts/premerge.sh", root / "scripts/premerge.sh")
    return subprocess.run(
        ["make", "--no-print-directory", "-f", str(ROOT / "Makefile"), "prepush",
         f"TESTS={selectors}"],
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
    assert "Push check passed: no open PR" in result.stdout
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


def test_invalid_focused_selection_refuses_before_installation(
    checkpoint: tuple[Path, dict[str, str]],
) -> None:
    root, _ = checkpoint
    result = _run(checkpoint, "--focused", "absent.py")
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


@pytest.mark.parametrize(
    ("selectors", "expected_status"),
    [("test_selected.py::test_selected -q", 0), ("absent.py", 2)],
)
def test_make_prepush_preserves_explicit_selection(
    checkpoint: tuple[Path, dict[str, str]], selectors: str, expected_status: int
) -> None:
    root, _ = checkpoint
    (root / "test_unselected.py").write_text("def test_unselected(): assert False\n")

    result = _make_prepush(checkpoint, selectors)

    assert result.returncode == expected_status, result.stdout + result.stderr
    assert (root / "test-ran").exists() == (expected_status == 0)
    assert (root / "installed").exists() == (expected_status == 0)
    assert not (root / "smoke-ran").exists()


def test_make_prepush_without_selector_runs_automatic_checkpoint(
    checkpoint: tuple[Path, dict[str, str]],
) -> None:
    root, _ = checkpoint
    (root / "tests").mkdir()
    (root / "tests/test_automatic.py").write_text(
        "from pathlib import Path\n"
        "def test_automatic(): Path('automatic-ran').touch()\n"
    )
    (root / "test_unselected.py").write_text("def test_unselected(): assert False\n")

    result = _make_prepush(checkpoint)

    assert result.returncode == 0, result.stdout + result.stderr
    assert (root / "automatic-ran").exists()
    assert not (root / "test-ran").exists()
    assert not (root / "smoke-ran").exists()


@pytest.mark.parametrize("explicit", [False, True])
def test_focused_checkpoint_leaves_lessons_to_static_and_ci(checkpoint, explicit) -> None:
    root, env = checkpoint
    env["FAIL_LESSONS"] = "1"
    if explicit:
        args = ("test_selected.py",)
    else:
        # The fixture already has a committed base and origin/main.
        (root / "tests").mkdir()
        (root / "tests/test_changed.py").write_text("def test_changed(): assert True\n")
        args = ()

    result = _run(checkpoint, "--focused", *args)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("mode", ["--static", "--full"])
def test_static_and_full_checkpoints_preserve_lessons_failure(checkpoint, mode) -> None:
    root, env = checkpoint
    env["FAIL_LESSONS"] = "1"

    result = _run(checkpoint, mode)

    assert result.returncode == 26
    assert "lessons unavailable" in result.stderr
    assert not (root / "test-ran").exists()


@pytest.mark.parametrize("checker_exit", [0, 2])
def test_typecheck_receipt_is_private_ephemeral_and_outside_checkout(checkpoint, checker_exit) -> None:
    root, env = checkpoint
    env["CHECKER_OUTPUT"] = "complete checker diagnostic"
    env["CHECKER_EXIT"] = str(checker_exit)
    env["TMPDIR"] = str(root)

    result = _run(checkpoint, "--focused", "test_selected.py")

    assert result.returncode == checker_exit, result.stdout + result.stderr
    assert "complete checker diagnostic" in result.stdout
    receipt = Path((root / "receipt-path").read_text())
    assert not receipt.resolve().is_relative_to(root.resolve())
    assert not receipt.exists()
    assert not (root / "runs").exists()
    assert (root / "test-ran").exists() == (checker_exit == 0)


def _open_checkpoint_pr(checkpoint):
    root, env = checkpoint
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root, env=env, text=True).strip()
    base = git("rev-parse", "HEAD")
    git("init", "--bare", str(root / "origin.git"))
    git("remote", "add", "origin", str(root / "origin.git"))
    git("push", "origin", "HEAD:refs/heads/main")
    (root / "consumer.txt").write_text("change\n")
    git("add", "consumer.txt")
    git("commit", "-qm", "change")
    head = git("rev-parse", "HEAD")
    git("push", "origin", "HEAD:refs/heads/topic", "HEAD:refs/pull/42/head")
    Path(env["SHIP_FAKE_STATE"]).write_text(json.dumps({"pr": {
        "number": 42, "state": "OPEN", "headRefOid": head, "headRefName": "topic",
        "baseRefName": "main", "isCrossRepository": False,
    }}))
    return git, base


@pytest.mark.parametrize("change", ["same", "changed", "unstaged", "staged", "override"])
def test_focused_push_check_compares_commits_and_preserves_explicit_exception(checkpoint, change):
    root, env = checkpoint
    git, base = _open_checkpoint_pr(checkpoint)
    if change != "same":
        (root / "consumer.txt").write_text("  change\n" if change == "override" else "needed fix\n")
        if change != "unstaged":
            git("add", "consumer.txt")
        if change in {"changed", "override"}:
            git("commit", "-qm", "fix")
    if change == "override":
        env["EVALLAB_PREPUSH_NEEDS_BASE"] = base
        env["EVALLAB_PREPUSH_REASON"] = "The indentation fix needs this base."

    result = _make_prepush(checkpoint, "test_selected.py")

    assert result.returncode == (1 if change == "same" else 0), result.stdout + result.stderr
    assert (root / "test-ran").exists() == (change != "same")
    if change in {"unstaged", "staged"}:
        assert "push check skipped: uncommitted tracked changes" in result.stdout
        assert "compares committed HEAD" in result.stdout
        assert not json.loads(Path(env["SHIP_FAKE_STATE"]).read_text()).get("calls")
    elif change == "same":
        assert "Push refused: same patch" in result.stdout
    elif change == "override":
        assert f"explicit base dependency {base}" in result.stdout
        assert env["EVALLAB_PREPUSH_REASON"] in result.stdout
    else:
        assert "Push check passed: the patch differs" in result.stdout


@pytest.mark.parametrize("remote_error", ["github", "fetch"])
def test_focused_push_check_allows_offline_local_checks(checkpoint, remote_error):
    root, env = checkpoint
    if remote_error == "github":
        (root / "bin/gh").write_text("#!/bin/sh\necho 'GitHub unavailable' >&2\nexit 7\n")
    else:
        git, _ = _open_checkpoint_pr(checkpoint)
        git("remote", "set-url", "origin", str(root / "missing.git"))

    result = _make_prepush(checkpoint, "test_selected.py")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "remote push check unavailable" in result.stderr
    assert "rerun ship check-push before pushing" in result.stderr
    assert (root / "test-ran").exists()


@pytest.mark.parametrize("variable", ["EVALLAB_PREPUSH_NEEDS_BASE", "EVALLAB_PREPUSH_REASON"])
def test_focused_push_check_refuses_incomplete_exception(checkpoint, variable):
    root, env = checkpoint
    env[variable] = "incomplete"

    result = _make_prepush(checkpoint, "test_selected.py")

    assert result.returncode == 2
    assert "set both EVALLAB_PREPUSH_NEEDS_BASE and EVALLAB_PREPUSH_REASON" in result.stderr
    assert not (root / "test-ran").exists()
