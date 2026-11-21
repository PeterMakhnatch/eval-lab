"""runner.py: the only module that starts a process. A small fake `harbor` stands in for the real one."""

import json
import os
import signal
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from muster import runner
from muster.model import InputError

FAKE = """#!{python}
import json, os, signal, sys, time
record = {{"argv": sys.argv[1:], "pythonpath": os.environ.get("PYTHONPATH"), "extra": os.environ.get("FAKE_EXTRA"),
          "stdin": sys.stdin.read(), "session": os.getsid(0) == os.getpid()}}
if "--version" in sys.argv:
    print("some banner")
    print("0.26.0")
    sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
config = sys.argv[sys.argv.index("-c") + 1]
with open(config + ".seen", "w") as seen:
    json.dump(record, seen)
print("fake harbor ran", config)
print("a warning", file=sys.stderr)
if os.environ.get("FAKE_SLEEP"):
    def stop(signum, frame):
        open(config + ".stopped", "w").write("SIGTERM")
        if os.environ.get("FAKE_STUBBORN"):
            return
        sys.exit(143)
    signal.signal(signal.SIGTERM, stop)
    deadline = time.time() + float(os.environ["FAKE_SLEEP"])
    while time.time() < deadline:
        time.sleep(0.05)
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


@pytest.fixture
def harbor(tmp_path):
    path = tmp_path / "bin" / "harbor"
    path.parent.mkdir()
    path.write_text(FAKE.format(python=sys.executable))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_find_harbor_takes_the_option_then_the_environment(harbor, tmp_path, monkeypatch):
    other = tmp_path / "bin" / "other"
    other.write_text(harbor.read_text())
    other.chmod(0o755)
    monkeypatch.setenv(runner.ENV_HARBOR, str(other))
    assert runner.find_harbor(str(harbor)) == harbor
    assert runner.find_harbor(None) == other
    monkeypatch.chdir(tmp_path)
    assert runner.find_harbor("bin/harbor") == harbor  # a relative path becomes absolute


def test_find_harbor_never_uses_path(harbor, monkeypatch):
    monkeypatch.delenv(runner.ENV_HARBOR, raising=False)
    monkeypatch.setenv("PATH", f"{harbor.parent}{os.pathsep}{os.environ['PATH']}")
    with pytest.raises(InputError, match="no harbor command: pass --harbor PATH or set MUSTER_HARBOR"):
        runner.find_harbor(None)


def test_find_harbor_refuses_what_it_cannot_run(tmp_path):
    (tmp_path / "plain").write_text("not a program")
    for value in (tmp_path / "missing", tmp_path / "plain", tmp_path):
        with pytest.raises(InputError, match="is not an executable file"):
            runner.find_harbor(str(value))


def test_run_job_writes_the_config_and_starts_harbor(harbor, tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/already/there")
    config = {"job_name": "j", "participants": [{"seat": "p1", "name": "oracle"}], "n_attempts": 2}
    out = tmp_path / "out" / "p1=oracle"
    outcome = runner.run_job(config, out, harbor, {"FAKE_EXTRA": "from muster"}, timeout_sec=30)
    assert outcome.config == out / "job.json" and json.loads(outcome.config.read_text()) == config
    assert outcome.log == out / "harbor.log"
    assert (outcome.returncode, outcome.timed_out) == (0, False) and outcome.seconds > 0
    assert outcome.command == (str(harbor), "run", "-c", str(out / "job.json"), "--quiet", "--yes")
    seen = json.loads((out / "job.json.seen").read_text())
    assert seen["argv"] == ["run", "-c", str(out / "job.json"), "--quiet", "--yes"]
    package_parent = str(Path(runner.__file__).resolve().parents[1])
    assert seen["pythonpath"] == f"{package_parent}{os.pathsep}/already/there"
    assert (Path(package_parent) / "muster" / "agents" / "script.py").is_file()
    assert seen["extra"] == "from muster"
    assert seen["stdin"] == "" and seen["session"] is True
    log = outcome.log.read_text()
    assert log.startswith(f"$ {harbor} run -c {out / 'job.json'} --quiet --yes\n")
    assert "fake harbor ran" in log and "a warning" in log  # stdout and stderr


def test_the_config_bytes_are_stable(tmp_path):
    first = runner.write_config({"b": 1, "a": {"d": [1, 2], "c": None}}, tmp_path / "x")
    second = runner.write_config({"a": {"c": None, "d": [1, 2]}, "b": 1}, tmp_path / "y")
    assert first.read_bytes() == second.read_bytes() and first.read_text().endswith("}\n")


def test_secrets_stay_in_the_environment(harbor, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_EXTRA", "sk-inherited-secret")
    outcome = runner.run_job({"job_name": "j"}, tmp_path, harbor, timeout_sec=30)
    assert json.loads((tmp_path / "job.json.seen").read_text())["extra"] == "sk-inherited-secret"
    assert "sk-inherited-secret" not in " ".join(outcome.command)
    for path in (outcome.config, outcome.log):
        assert "sk-inherited-secret" not in path.read_text()


def test_a_failed_harbor_gives_its_status_and_the_log_grows(harbor, tmp_path):
    first = runner.run_job({"job_name": "j"}, tmp_path, harbor, {"FAKE_EXIT": "7"}, timeout_sec=30)
    assert (first.returncode, first.timed_out) == (7, False)
    runner.run_job({"job_name": "j"}, tmp_path, harbor, timeout_sec=30)
    assert first.log.read_text().count("fake harbor ran") == 2  # a second run adds to the log


def test_the_stem_puts_the_files_next_to_a_job_directory(harbor, tmp_path):
    outcome = runner.run_job({"job_name": "team-3"}, tmp_path, harbor, timeout_sec=30, stem="team-3.")
    assert (outcome.config, outcome.log) == (tmp_path / "team-3.job.json", tmp_path / "team-3.harbor.log")
    assert outcome.config.is_file() and outcome.log.is_file()


def test_timeout_stops_harbor_with_sigterm(harbor, tmp_path):
    started = time.monotonic()
    outcome = runner.run_job({"job_name": "j"}, tmp_path, harbor, {"FAKE_SLEEP": "60"}, timeout_sec=1.0)
    assert outcome.timed_out and outcome.returncode == 143
    assert time.monotonic() - started < 20
    assert (tmp_path / "job.json.stopped").read_text() == "SIGTERM"  # harbor had the chance to clean up


def test_a_harbor_that_ignores_sigterm_is_killed(harbor, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "GRACE_SEC", 0.5)
    env = {"FAKE_SLEEP": "60", "FAKE_STUBBORN": "1"}
    started = time.monotonic()
    outcome = runner.run_job({"job_name": "j"}, tmp_path, harbor, env, timeout_sec=1.0)
    assert outcome.timed_out and outcome.returncode == -9
    assert time.monotonic() - started < 20


def test_a_sigterm_to_muster_stops_harbor_and_goes_on_up(harbor, tmp_path):
    def harmless(_signum, _frame):  # keeps the test run alive if muster's handler is not there
        pass

    before = signal.signal(signal.SIGTERM, harmless)
    try:
        threading.Timer(1.0, os.kill, (os.getpid(), signal.SIGTERM)).start()
        with pytest.raises(KeyboardInterrupt):
            runner.run_job({"job_name": "j"}, tmp_path, harbor, {"FAKE_SLEEP": "60"}, timeout_sec=30)
        assert (tmp_path / "job.json.stopped").read_text() == "SIGTERM"
        assert signal.getsignal(signal.SIGTERM) is harmless  # the handler is put back
    finally:
        signal.signal(signal.SIGTERM, before)


def test_harbor_version_is_the_last_line(harbor, monkeypatch):
    assert runner.harbor_version(harbor) == "0.26.0"
    monkeypatch.setenv("FAKE_EXIT", "2")
    assert runner.harbor_version(harbor) is None
    assert runner.harbor_version(harbor.parent / "missing") is None


def test_an_installed_muster_is_not_put_on_pythonpath(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/already/there")
    monkeypatch.setattr(runner, "PACKAGE_PARENT", Path("/venv/lib/python3.13/site-packages"))
    assert runner.child_env({"A": "1"})["PYTHONPATH"] == "/already/there"
    monkeypatch.delenv("PYTHONPATH")
    assert "PYTHONPATH" not in runner.child_env()
    monkeypatch.setattr(runner, "PACKAGE_PARENT", Path("/checkout/src"))
    assert runner.child_env()["PYTHONPATH"] == "/checkout/src"
