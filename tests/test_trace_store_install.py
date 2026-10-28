"""Behavioral tests for scripts/ops/launchd/install-trace-store-sync.sh.

The real script runs against a throwaway primary checkout (real git, a bare local
"origin") with stub `uv`, `launchctl` and `keys` on the command boundary. Nothing
touches the real launchd domain, key store, network or user state.
"""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/ops/launchd/install-trace-store-sync.sh"
LABEL = "com.petermakhnatch.evallab.trace-store-sync"
SECRET = "sk-test-secret-value-9f3a"
WATCH_REL = "src/evallab/trace_store/watch.py"

STUB_UV = f"""#!{sys.executable}
import json, os, re, subprocess, sys
from pathlib import Path

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({{"tool": "uv", "argv": argv, "cwd": os.getcwd()}}) + "\\n")


def opt(name):
    return argv[argv.index(name) + 1]


def dist(site, name, version):
    path = site / f"{{name.replace('-', '_')}}-{{version}}.dist-info"
    path.mkdir(exist_ok=True)
    (path / "METADATA").write_text(f"Metadata-Version: 2.1\\nName: {{name}}\\nVersion: {{version}}\\n")


cmd = argv[0]
if cmd == "export":
    for flag in ("--frozen", "--no-dev", "--no-default-groups", "--no-hashes",
                 "--no-editable", "--no-emit-project"):
        if flag not in argv:
            sys.exit(f"stub uv: missing {{flag}}")
    if argv[argv.index("--extra") + 1] != "laminar":
        sys.exit("stub uv: missing laminar extra")
    if not (Path(opt("--project")) / "uv.lock").is_file():
        sys.exit("stub uv: no uv.lock in project")
    Path(opt("-o")).write_text("lmnr==0.7.64\\nopentelemetry-proto==1.38.0\\n")
elif cmd == "venv":
    subprocess.check_call([sys.executable, "-m", "venv", "--without-pip", argv[-1]])
elif cmd == "pip" and argv[1] == "check":
    if not Path(opt("--python")).is_file():
        sys.exit("stub uv: missing venv interpreter")
elif cmd == "pip" and argv[1] == "install":
    venv = Path(opt("--python")).parent.parent
    site = next(venv.glob("lib/python*/site-packages"))
    if "-r" in argv:
        for line in Path(opt("-r")).read_text().splitlines():
            match = re.match(r"([A-Za-z0-9_.-]+)==([^\\s;]+)", line)
            if match:
                name, version = match.groups()
                if name == "lmnr":
                    version = os.environ.get("UV_STUB_LMNR_VERSION", version)
                dist(site, name, version)
    elif "-e" in argv:
        if "--no-deps" not in argv:
            sys.exit("stub uv: editable install must use --no-deps")
        (site / "evallab.pth").write_text(str(Path(opt("-e")) / "src") + "\\n")
        dist(site, "evallab", "0.0.0")
    elif argv[-1].startswith("docent=="):
        if os.environ.get("UV_STUB_FAIL_DOCENT"):
            sys.exit(1)
        dist(site, "docent", argv[-1].split("==")[1])
    else:
        sys.exit("stub uv: unexpected pip install")
else:
    sys.exit(f"stub uv: unexpected command {{argv}}")
"""

STUB_LAUNCHCTL = f"""#!{sys.executable}
import json, os, plistlib, sys
from pathlib import Path

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps({{"tool": "launchctl", "argv": argv}}) + "\\n")
state_path = Path(os.environ["STUB_LAUNCHD_STATE"])
loaded = set(json.loads(state_path.read_text())) if state_path.exists() else set()
sub = argv[0]
if sub == "print":
    sys.exit(0 if argv[1] in loaded else 113)
if sub == "bootout":
    loaded.discard(argv[1])
elif sub == "bootstrap":
    label = plistlib.loads(Path(argv[2]).read_bytes())["Label"]
    target = f"{{argv[1]}}/{{label}}"
    if target in loaded:
        sys.exit(5)
    loaded.add(target)
elif sub != "enable":
    sys.exit(f"stub launchctl: unexpected {{argv}}")
state_path.write_text(json.dumps(sorted(loaded)))
"""

GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


class World:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp.resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.repo = self.tmp / "repo"
        self.origin = self.tmp / "origin.git"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.log = self.tmp / "calls.jsonl"
        self.launchd_state = self.tmp / "launchd.json"
        self.state = self.tmp / "state"
        self.dest = self.tmp / "agents" / f"{LABEL}.plist"
        self.sync_state = self.tmp / "sync-state"
        self.db = self.tmp / "cloud.sqlite"
        self.keys = self.tmp / "keybin" / "keys"
        self.logs = self.home / "Library/Logs/evallab"
        self.wt = self.repo / ".worktrees" / "topic"

        for name, body in (("uv", STUB_UV), ("launchctl", STUB_LAUNCHCTL)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        (self.bin / "python3").symlink_to(sys.executable)
        self.keys.parent.mkdir()
        self.keys.write_text("#!/bin/sh\nexit 99\n")
        self.keys.chmod(0o755)

        self.env = {
            **os.environ,
            **GIT_ENV,
            "HOME": str(self.home),
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "STUB_LOG": str(self.log),
            "STUB_LAUNCHD_STATE": str(self.launchd_state),
            # Credentials and disposable-checkout hints must never reach the plist.
            "LMNR_PROJECT_API_KEY": SECRET,
            "DOCENT_API_KEY": SECRET,
            "VIRTUAL_ENV": str(self.tmp / "disposable/.venv"),
            "PYTHONPATH": str(self.tmp / "disposable/src"),
        }

        self.git("init", "-q", "-b", "main", str(self.repo), cwd=self.tmp)
        files = {
            "pyproject.toml": "[project]\nname = 'evallab'\n",
            "README.md": "readme\n",
            "uv.lock": "version = 1\n",
            "src/evallab/__init__.py": "",
            "src/evallab/trace_store/__init__.py": "",
            "src/evallab/production/__init__.py": "",
            "src/evallab/production/corpus_queue.py": (
                "from pathlib import Path\n"
                "class CorpusRegistry:\n"
                "    def __init__(self, root):\n"
                "        (Path(root) / 'registry-input.json').read_bytes()\n"
            ),
            "registry-input.json": "{}\n",
            WATCH_REL: 'MARK = "committed"\n',
        }
        for rel, text in files.items():
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        script = self.repo / "scripts/ops/launchd/install-trace-store-sync.sh"
        script.parent.mkdir(parents=True)
        shutil.copy2(SCRIPT, script)
        script.chmod(0o755)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "initial")
        self.git("init", "-q", "--bare", str(self.origin), cwd=self.tmp)
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "-q", "origin", "main")
        self.git("fetch", "-q", "origin")
        self.git("worktree", "add", "-q", "-b", "topic", str(self.wt))

    def git(self, *args: str, cwd: Path | None = None) -> str:
        done = subprocess.run(
            ["git", *args], cwd=cwd or self.repo, env=self.env,
            capture_output=True, text=True, check=True,
        )
        return done.stdout.strip()

    def commit_and_push(self, rel: str, text: str) -> str:
        (self.repo / rel).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", f"change {rel}")
        self.git("push", "-q", "origin", "main")
        return self.git("rev-parse", "HEAD")

    def args(self) -> list[str]:
        return [
            "--state-dir", str(self.state), "--dest", str(self.dest),
            "--sync-state-root", str(self.sync_state), "--db-path", str(self.db),
            "--keys", str(self.keys),
        ]

    def run(self, *extra: str, base: bool = True, env: dict[str, str] | None = None,
            script: Path | None = None) -> subprocess.CompletedProcess[str]:
        script = script or self.wt / "scripts/ops/launchd/install-trace-store-sync.sh"
        return subprocess.run(
            [str(script), *(self.args() if base else []), *extra],
            cwd=self.wt, env={**self.env, **(env or {})}, capture_output=True, text=True,
        )

    def calls(self, tool: str | None = None) -> list[dict]:
        if not self.log.exists():
            return []
        rows = [json.loads(line) for line in self.log.read_text().splitlines()]
        return [row for row in rows if tool is None or row["tool"] == tool]

    def loaded(self) -> list[str]:
        return json.loads(self.launchd_state.read_text()) if self.launchd_state.exists() else []

    def releases(self) -> list[Path]:
        root = self.state / "releases"
        return sorted(root.iterdir()) if root.exists() else []

    def plist(self) -> dict:
        return plistlib.loads(self.dest.read_bytes())

    def untouched(self) -> None:
        assert not self.state.exists()
        assert not self.dest.exists()
        assert not self.logs.exists()
        assert self.calls("uv") == []
        assert [c for c in self.calls("launchctl") if c["argv"][0] != "print"] == []


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.items() for part in item for s in strings(part)]
    if isinstance(value, list):
        return [s for part in value for s in strings(part)]
    return []


def test_install_builds_durable_snapshot_and_plist(world: World) -> None:
    # Dirty primary state and a dirty invoking worktree must never reach the snapshot.
    (world.repo / WATCH_REL).write_text('MARK = "dirty"\n')
    (world.repo / "src/evallab/untracked.py").write_text("LEAK = 1\n")
    (world.wt / "src/evallab/wt_untracked.py").write_text("LEAK = 1\n")

    done = world.run()
    assert done.returncode == 0, done.stderr

    (release,) = world.releases()
    source, venv = release / "source", release / "venv"
    assert (source / WATCH_REL).read_text() == 'MARK = "committed"\n'
    assert not (source / "src/evallab/untracked.py").exists()
    assert not (source / "src/evallab/wt_untracked.py").exists()

    plist = world.plist()
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == [
        str(world.keys), "run", "--", str(venv / "bin/python"),
        "-m", "evallab.trace_store.watch",
        "--repo-root", str(source),
        "--db-path", str(world.db),
        "--state-root", str(world.sync_state),
    ]
    assert plist["WorkingDirectory"] == str(source)
    assert plist["RunAtLoad"] is True
    assert plist["StartInterval"] == 600
    assert not plist.get("KeepAlive")
    assert set(plist["EnvironmentVariables"]) == {"HOME", "PATH"}
    assert plist["EnvironmentVariables"]["HOME"] == str(world.home)

    # Neither runtime code nor registry authority depends on any checkout.
    for text in strings(plist):
        assert ".worktrees" not in text
        assert str(world.repo) not in text
        assert "disposable" not in text
    assert "--resume" not in plist["ProgramArguments"]
    assert not any("cap" in a.lower() or "force" in a.lower() for a in plist["ProgramArguments"])

    # The venv imports evallab from the snapshot and the build used the snapshot only.
    (site,) = venv.glob("lib/python*/site-packages")
    assert (site / "evallab.pth").read_text().strip() == str(source / "src")
    for call in world.calls("uv"):
        flat = " ".join(call["argv"])
        assert str(world.repo) not in flat and str(world.wt) not in flat
        assert "disposable" not in flat
    assert [c for c in world.calls("uv") if c["argv"][0] == "export"][0]["argv"].count(
        str(source)
    ) == 1

    # Receipt: commit, time, packages; docent installed; no secrets anywhere.
    receipt = (release / "INSTALLED").read_text()
    assert f"commit={world.git('rev-parse', 'origin/main')}" in receipt
    assert "status=ready" in receipt and "installed_utc=" in receipt
    assert "lmnr==0.7.64" in receipt and "docent==0.1.88" in receipt
    assert (world.state / "INSTALLED").read_text() == receipt
    for text in (receipt, world.dest.read_text(), done.stdout, done.stderr):
        assert SECRET not in text

    # Without --load a fresh install never bootstraps.
    assert world.loaded() == []
    assert not [c for c in world.calls("launchctl") if c["argv"][0] == "bootstrap"]


def test_docent_install_failure_never_loads_partial_runtime(world: World) -> None:
    done = world.run(env={"UV_STUB_FAIL_DOCENT": "1"})
    assert done.returncode != 0
    (release,) = world.releases()
    assert "status=ready" not in _receipt(release)
    assert not world.dest.exists()
    assert world.loaded() == []
    assert not [c for c in world.calls("launchctl") if c["argv"][0] == "bootstrap"]


def test_custom_interval_and_label_and_default_authority(world: World) -> None:
    dest = world.tmp / "agents" / "custom.plist"
    done = world.run("--interval", "0090", "--label", "com.example.other-sync",
                     "--dest", str(dest))
    assert done.returncode == 0, done.stderr
    plist = plistlib.loads(dest.read_bytes())
    assert plist["StartInterval"] == 90
    assert plist["Label"] == "com.example.other-sync"
    (release,) = world.releases()
    assert plist["ProgramArguments"][plist["ProgramArguments"].index("--repo-root") + 1] == str(
        release / "source"
    )


def test_missing_primary_registry_asset_uses_committed_snapshot(world: World) -> None:
    (world.repo / "registry-input.json").unlink()
    done = world.run()
    assert done.returncode == 0, done.stderr
    (release,) = world.releases()
    assert (release / "source/registry-input.json").read_text() == "{}\n"
    assert f"registry={release / 'source'}" in done.stdout


def test_default_revision_is_origin_main_not_local_head(world: World) -> None:
    origin_sha = world.git("rev-parse", "origin/main")
    (world.repo / WATCH_REL).write_text('MARK = "local-only"\n')
    world.git("commit", "-q", "-am", "unpushed")
    done = world.run()
    assert done.returncode == 0, done.stderr
    (release,) = world.releases()
    assert f"commit={origin_sha}" in (release / "INSTALLED").read_text()
    assert (release / "source" / WATCH_REL).read_text() == 'MARK = "committed"\n'


def test_unmerged_revision_is_refused_without_side_effects(world: World) -> None:
    world.git("checkout", "-q", "-b", "feature")
    (world.repo / WATCH_REL).write_text('MARK = "feature"\n')
    world.git("commit", "-q", "-am", "feature")
    world.git("checkout", "-q", "main")
    done = world.run("--revision", "feature")
    assert done.returncode != 0
    assert "origin/main" in done.stderr
    world.untouched()


def test_load_then_reload_waits_for_new_runtime_and_keeps_old_release(world: World) -> None:
    assert world.run("--load").returncode == 0
    assert world.loaded() == [f"gui/{os.getuid()}/{LABEL}"]
    (first,) = world.releases()

    # Same commit: verified snapshot is reused, job reloaded (own job was loaded).
    assert world.run().returncode == 0
    assert world.releases() == [first]
    assert len([c for c in world.calls("uv") if c["argv"][0] == "venv"]) == 1
    assert world.loaded() == [f"gui/{os.getuid()}/{LABEL}"]

    # New commit: a second release is built first, then the own job is bounced.
    world.log.write_text("")
    world.commit_and_push("README.md", "changed\n")
    assert world.run().returncode == 0
    second = [r for r in world.releases() if r != first]
    assert len(second) == 1 and first.exists()
    calls = world.calls()
    last_build = max(i for i, c in enumerate(calls) if c["tool"] == "uv")
    bootout = next(i for i, c in enumerate(calls) if c["argv"][0] == "bootout")
    bootstrap = next(i for i, c in enumerate(calls) if c["argv"][0] == "bootstrap")
    assert last_build < bootout < bootstrap
    assert str(second[0] / "venv/bin/python") in world.plist()["ProgramArguments"]
    assert world.loaded() == [f"gui/{os.getuid()}/{LABEL}"]


def test_failed_sdk_pin_check_leaves_loaded_job_and_plist_alone(world: World) -> None:
    assert world.run("--load").returncode == 0
    before = world.dest.read_bytes()
    world.log.write_text("")
    world.commit_and_push("README.md", "changed\n")
    done = world.run(env={"UV_STUB_LMNR_VERSION": "0.7.0"})
    assert done.returncode != 0
    assert "lmnr" in done.stderr
    assert world.dest.read_bytes() == before
    assert world.loaded() == [f"gui/{os.getuid()}/{LABEL}"]
    assert not [c for c in world.calls("launchctl") if c["argv"][0] in ("bootout", "bootstrap")]
    # The half-built release is preserved but never marked ready.
    unready = [r for r in world.releases() if "status=ready" not in _receipt(r)]
    assert len(unready) == 1


def _receipt(release: Path) -> str:
    path = release / "INSTALLED"
    return path.read_text() if path.exists() else ""


@pytest.mark.parametrize(
    "bad",
    [
        ["--interval", "0"],
        ["--interval", "-5"],
        ["--interval", "abc"],
        ["--interval", "1.5"],
        ["--interval", ""],
        ["--label", "../evil"],
        ["--label", "a b"],
        ["--dest", "relative.plist"],
        ["--state-dir", "relative/state"],
        ["--interval"],
        ["--bogus"],
        ["--load", "--uninstall"],
        ["--resume", "all"],
    ],
)
def test_invalid_arguments_fail_before_any_side_effect(world: World, bad: list[str]) -> None:
    done = world.run(*bad)
    assert done.returncode != 0
    world.untouched()


def test_repo_root_must_be_primary_checkout(world: World) -> None:
    done = world.run("--repo-root", str(world.wt))
    assert done.returncode != 0
    assert "primary" in done.stderr
    world.untouched()


def test_state_dir_inside_a_worktree_is_refused(world: World) -> None:
    done = world.run("--state-dir", str(world.wt / "state"))
    assert done.returncode != 0
    assert not (world.wt / "state").exists()
    assert world.calls("uv") == []


def test_foreign_plist_is_never_replaced_or_uninstalled(world: World) -> None:
    world.dest.parent.mkdir(parents=True)
    foreign = plistlib.dumps({"Label": LABEL, "ProgramArguments": ["/bin/true"]})
    world.dest.write_bytes(foreign)
    world.launchd_state.write_text(json.dumps([f"gui/{os.getuid()}/{LABEL}"]))

    install = world.run()
    assert install.returncode != 0
    uninstall = world.run("--uninstall")
    assert uninstall.returncode != 0

    assert world.dest.read_bytes() == foreign
    assert world.loaded() == [f"gui/{os.getuid()}/{LABEL}"]
    assert world.calls("uv") == []
    assert not world.state.exists()
    assert not [c for c in world.calls("launchctl") if c["argv"][0] in ("bootout", "bootstrap")]


def test_foreign_loaded_job_without_plist_is_not_touched(world: World) -> None:
    world.launchd_state.write_text(json.dumps([f"gui/{os.getuid()}/{LABEL}"]))
    assert world.run().returncode != 0
    assert world.run("--uninstall").returncode == 0  # nothing of ours to remove
    assert world.loaded() == [f"gui/{os.getuid()}/{LABEL}"]
    assert world.calls("uv") == []
    assert not [c for c in world.calls("launchctl") if c["argv"][0] in ("bootout", "bootstrap")]


def test_uninstall_removes_only_own_plist_and_job(world: World) -> None:
    assert world.run("--load").returncode == 0
    (release,) = world.releases()
    world.sync_state.mkdir(parents=True)
    (world.sync_state / "receipts.json").write_text("{}")
    (world.logs / "trace-store-sync.log").write_text("tick\n")
    other = f"gui/{os.getuid()}/com.example.other-lane"
    world.launchd_state.write_text(json.dumps([*world.loaded(), other]))

    done = world.run("--uninstall")
    assert done.returncode == 0, done.stderr
    assert not world.dest.exists()
    assert world.loaded() == [other]
    # Runtimes, receipts, sync state and logs are preserved.
    assert (release / "venv/bin/python").exists()
    assert (release / "INSTALLED").exists()
    assert (world.state / "INSTALLED").exists()
    assert (world.sync_state / "receipts.json").exists()
    assert (world.logs / "trace-store-sync.log").exists()
    booted_out = [c["argv"][1] for c in world.calls("launchctl") if c["argv"][0] == "bootout"]
    assert booted_out == [f"gui/{os.getuid()}/{LABEL}"]