"""Behaviour tests for scripts/ops/launchd/install-sync-primary.sh.

The installer runs for real against a throwaway ``$HOME`` with a stub
``launchctl`` first on ``PATH``, so no real launchd domain, ``~/Library``
directory or LaunchAgent is touched. Both Linux CI and macOS run these: the
stub records calls, and the rendered plist is inspected with ``plistlib``.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ops" / "launchd" / "install-sync-primary.sh"
LABEL = "com.petermakhnatch.evallab.sync-primary"

STUB_LAUNCHCTL = f"""#!{sys.executable}
import json, os, plistlib, sys
from pathlib import Path

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
state_path = Path(os.environ["STUB_LAUNCHD_STATE"])
loaded = set(json.loads(state_path.read_text())) if state_path.exists() else set()


def label_of(target):
    if target.startswith("gui/"):
        return target.rsplit("/", 1)[-1]
    return plistlib.loads(Path(target).read_bytes())["Label"]


sub = argv[0]
if sub == "list":
    sys.exit(0 if argv[1] in loaded else 1)
if sub == "load":
    loaded.add(label_of(argv[1]))
elif sub == "unload":
    loaded.discard(label_of(argv[1]))
elif sub == "bootout":
    target = label_of(argv[1])
    if target not in loaded:
        print(f"Could not find service {{argv[1]}}", file=sys.stderr)
        sys.exit(113)
    loaded.discard(target)
else:
    sys.exit(f"stub launchctl: unexpected {{argv}}")
state_path.write_text(json.dumps(sorted(loaded)))
"""


class World:
    """A throwaway HOME, a stub launchctl, and a call log."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp.resolve()
        self.home = self.tmp / "home"
        self.stub = self.tmp / "stub"
        self.home.mkdir()
        self.stub.mkdir()
        launcher = self.stub / "launchctl"
        launcher.write_text(STUB_LAUNCHCTL)
        launcher.chmod(0o755)
        self.log = self.tmp / "launchctl.jsonl"
        self.launchd_state = self.tmp / "launchd-state.json"

    def run(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            "HOME": str(self.home),
            "PATH": os.pathsep.join(
                [
                    str(self.stub),
                    os.path.dirname(sys.executable),
                    "/opt/homebrew/bin",
                    "/usr/local/bin",
                    "/usr/bin",
                    "/bin",
                    "/usr/sbin",
                    "/sbin",
                ]
            ),
            "STUB_LOG": str(self.log),
            "STUB_LAUNCHD_STATE": str(self.launchd_state),
        }
        return subprocess.run(
            ["bash", str(SCRIPT), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    @property
    def installed_script(self) -> Path:
        return (
            self.home
            / "Library"
            / "Application Support"
            / "evallab"
            / "bin"
            / "sync-primary-checkout.sh"
        )

    def calls(self, subcommand: str | None = None) -> list[list[str]]:
        if not self.log.exists():
            return []
        recorded = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [c for c in recorded if subcommand is None or c[0] == subcommand]

    def loaded(self) -> list[str]:
        if not self.launchd_state.exists():
            return []
        return json.loads(self.launchd_state.read_text())


def test_install_renders_plist_and_prints_installed_digest(tmp_path: Path) -> None:
    world = World(tmp_path)
    repo = tmp_path / "primary"

    done = world.run("--repo", str(repo), "--interval", "120")

    assert done.returncode == 0, done.stderr
    assert world.plist.is_file()
    assert (
        world.installed_script.read_bytes()
        == (ROOT / "scripts" / "sync-primary-checkout.sh").read_bytes()
    )
    assert world.installed_script.stat().st_mode & 0o111

    config = plistlib.loads(world.plist.read_bytes())
    assert config["Label"] == LABEL
    assert config["ProgramArguments"] == ["/bin/bash", str(world.installed_script)]
    assert config["StartInterval"] == 120
    assert config["EnvironmentVariables"]["EVALLAB_SYNC_REPO"] == str(repo)
    assert config["EnvironmentVariables"]["EVALLAB_SYNC_STATE_DIR"] == str(
        world.home / "Library" / "Application Support" / "evallab"
    )

    digest = hashlib.sha256(world.installed_script.read_bytes()).hexdigest()
    assert f"sha256={digest}" in done.stdout.splitlines()
    assert world.calls() == []  # No --load, so launchd is never touched.


def test_uninstall_boots_out_label_and_removes_plist(tmp_path: Path) -> None:
    world = World(tmp_path)
    assert world.run("--load").returncode == 0
    assert world.loaded() == [LABEL]

    done = world.run("--uninstall")

    assert done.returncode == 0, done.stderr
    assert not world.plist.exists()
    assert world.loaded() == []
    assert world.calls("bootout") == [["bootout", f"gui/{os.getuid()}/{LABEL}"]]


def test_uninstall_twice_is_a_noop(tmp_path: Path) -> None:
    world = World(tmp_path)
    assert world.run("--load").returncode == 0
    assert world.run("--uninstall").returncode == 0

    # The job and plist are gone, so launchd rejects the bootout; that is not an error.
    again = world.run("--uninstall")

    assert again.returncode == 0
    assert again.stderr == ""
    assert f"removed: {world.plist}" in again.stdout.splitlines()
    assert not world.plist.exists()


def test_uninstall_without_install_creates_no_state(tmp_path: Path) -> None:
    world = World(tmp_path)

    done = world.run("--uninstall")

    assert done.returncode == 0
    assert not world.plist.exists()
    assert not (world.home / "Library" / "Application Support" / "evallab").exists()
    assert not (world.home / "Library" / "Logs").exists()


def test_uninstall_honours_custom_label_and_dest(tmp_path: Path) -> None:
    world = World(tmp_path)
    dest = tmp_path / "agents" / "custom.plist"
    flags = ["--label", "com.example.sync", "--dest", str(dest)]
    assert world.run(*flags, "--load").returncode == 0
    assert dest.is_file()
    assert world.loaded() == ["com.example.sync"]

    done = world.run(*flags, "--uninstall")

    assert done.returncode == 0, done.stderr
    assert not dest.exists()
    assert not world.plist.exists()
    assert world.calls("bootout") == [["bootout", f"gui/{os.getuid()}/com.example.sync"]]


def test_usage_header_documents_uninstall() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    usage = text.split("set -euo pipefail", 1)[0]

    assert "[--uninstall]" in usage
    assert "--uninstall boots the label out" in usage
