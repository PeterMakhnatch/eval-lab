"""Behaviour tests for scripts/ops/launchd/install-nightly-refresh.sh.

The installer runs for real against a throwaway ``$HOME`` and a throwaway git
checkout holding a copy of the installer and its plist template, with a stub
``launchctl`` first on ``PATH``. No real launchd domain, ``~/Library``
directory or LaunchAgent is touched, and the checkout is committed so the
installer's clean-tree gate is satisfied without reading the developer's
working tree.

The runtime snapshot is pre-seeded (``runtime/<commit>/READY``) because that
marker is the installer's only seam between rendering a plist and building a
``uv`` environment; building one is not what these tests are about.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "ops" / "launchd" / "install-nightly-refresh.sh"
TEMPLATE = ROOT / "scripts" / "ops" / "launchd" / "com.petermakhnatch.evallab.nightly.plist"
LABEL = "com.petermakhnatch.evallab.nightly"

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
if sub == "enable":
    sys.exit(0)
if sub == "bootstrap":
    loaded.add(label_of(argv[2]))
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
    """A throwaway HOME, a throwaway checkout, a stub launchctl and a call log."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp.resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.stub = self.tmp / "stub"
        self.stub.mkdir()
        launcher = self.stub / "launchctl"
        launcher.write_text(STUB_LAUNCHCTL)
        launcher.chmod(0o755)
        self.log = self.tmp / "launchctl.jsonl"
        self.launchd_state = self.tmp / "launchd-state.json"
        self.repo = self.tmp / "checkout"
        launchd = self.repo / "scripts" / "ops" / "launchd"
        launchd.mkdir(parents=True)
        shutil.copy2(SCRIPT, launchd / SCRIPT.name)
        shutil.copy2(TEMPLATE, launchd / TEMPLATE.name)
        self.git("init", "-q", "-b", "main")
        self.git("add", str(launchd / SCRIPT.name), str(launchd / TEMPLATE.name))
        self.git("commit", "-qm", "installer and template")
        self.commit = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "git",
                "-c",
                "user.name=Installer Test",
                "-c",
                "user.email=installer-test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

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
            ["bash", str(self.repo / "scripts" / "ops" / "launchd" / SCRIPT.name), *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )

    @property
    def state(self) -> Path:
        return self.home / ".local" / "state" / "evallab-nightly"

    @property
    def snapshot(self) -> Path:
        return self.state / "runtime" / self.commit

    @property
    def plist(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"

    def prepare_runtime(self) -> Path:
        self.snapshot.mkdir(parents=True)
        (self.snapshot / "READY").write_text(f"{self.commit}\n")
        return self.snapshot

    def calls(self, subcommand: str | None = None) -> list[list[str]]:
        if not self.log.exists():
            return []
        recorded = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [c for c in recorded if subcommand is None or c[0] == subcommand]

    def loaded(self) -> list[str]:
        if not self.launchd_state.exists():
            return []
        return json.loads(self.launchd_state.read_text())


def test_install_renders_plist_without_touching_launchd(tmp_path: Path) -> None:
    world = World(tmp_path)
    snapshot = world.prepare_runtime()

    done = world.run()

    assert done.returncode == 0, done.stderr
    assert world.plist.is_file()
    config = plistlib.loads(world.plist.read_bytes())
    assert config["Label"] == LABEL
    assert config["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}
    assert config["ProgramArguments"][:3] == [
        str(snapshot / ".venv" / "bin" / "python"),
        "-I",
        "-c",
    ]
    assert config["ProgramArguments"][4:] == [
        "nightly",
        "--refresh",
        "--data-root",
        str(world.repo),
        "--state-dir",
        str(world.state),
        "--facts-root",
        str(world.home / ".local" / "state" / "daily-report"),
    ]
    assert config["WorkingDirectory"] == str(snapshot)
    assert config["StandardOutPath"] == str(
        world.home / "Library" / "Logs" / "evallab" / "nightly-refresh.log"
    )
    assert config["EnvironmentVariables"]["HOME"] == str(world.home)
    assert f"installed: {world.plist}" in done.stdout.splitlines()
    assert world.calls() == []  # No --load, so launchd is never touched.


def test_install_load_bootstraps_the_label(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.prepare_runtime()

    done = world.run("--load")

    assert done.returncode == 0, done.stderr
    assert world.loaded() == [LABEL]
    assert world.calls("bootstrap") == [["bootstrap", f"gui/{os.getuid()}", str(world.plist)]]


def test_uninstall_boots_out_label_and_removes_plist(tmp_path: Path) -> None:
    world = World(tmp_path)
    snapshot = world.prepare_runtime()
    assert world.run("--load").returncode == 0
    assert world.loaded() == [LABEL]

    done = world.run("--uninstall")

    assert done.returncode == 0, done.stderr
    assert not world.plist.exists()
    assert world.loaded() == []
    assert world.calls("bootout")[-1] == ["bootout", f"gui/{os.getuid()}/{LABEL}"]
    # State and the runtime snapshot are evidence, not installer baggage.
    assert (snapshot / "READY").is_file()


def test_uninstall_twice_is_a_noop(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.prepare_runtime()
    assert world.run("--load").returncode == 0
    assert world.run("--uninstall").returncode == 0

    # The job and plist are gone, so launchd rejects the bootout; that is not an error.
    again = world.run("--uninstall")

    assert again.returncode == 0
    assert again.stderr == ""
    assert f"removed: {world.plist} (runtime snapshot and state retained)" in (
        again.stdout.splitlines()
    )
    assert not world.plist.exists()


def test_uninstall_without_install_creates_nothing(tmp_path: Path) -> None:
    world = World(tmp_path)

    done = world.run("--uninstall")

    assert done.returncode == 0
    assert not world.plist.exists()
    assert not (world.home / "Library").exists()
    assert not (world.home / ".local").exists()


def test_uninstall_ignores_a_dirty_checkout(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.prepare_runtime()
    assert world.run("--load").returncode == 0
    (world.repo / "uncommitted.txt").write_text("work in progress\n")

    uninstall = world.run("--uninstall")
    install = world.run()

    assert uninstall.returncode == 0, uninstall.stderr
    assert not world.plist.exists()
    # The same dirty tree still blocks installing a snapshot it cannot name.
    assert install.returncode != 0
    assert "commit changes before installing" in install.stderr


def test_usage_header_documents_uninstall() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    usage = text.split("set -euo pipefail", 1)[0]

    assert "[--uninstall]" in usage
    assert "--uninstall boots the label out" in usage
