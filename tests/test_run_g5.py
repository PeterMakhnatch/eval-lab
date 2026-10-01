"""Run the G5 shell driver against local stubs; never contact paid services."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "research/experiments/ovn-sft-v0/run_g5.sh"

STUB = r"""import json, os, pathlib, signal, sys, time
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ["G5_TEST_ROOT"])
with (root / "commands.jsonl").open("a") as f:
    f.write(json.dumps([name, *args]) + "\n")
def arg(flag):
    return args[args.index(flag) + 1]
def idle():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:
        time.sleep(1)
if name == "git":
    if args == ["rev-parse", "--show-toplevel"]:
        print(root)
    elif args == ["rev-parse", "HEAD"]:
        print("a" * 40)
    elif args[:2] != ["status", "--porcelain"]:
        sys.exit(1)
elif name == "curl":
    print("200", end="")
elif name == "lsof":
    pid = root / "capture.pid"
    if not pid.exists():
        sys.exit(1)
    print(pid.read_text().strip())
elif name == "sleep":
    time.sleep(0.02 if args != ["180"] else 1)
elif name == "uv":
    if "deploy" in args:
        print("https://g5-test.modal.direct")
    elif "billing" in args:
        print("[]")
    elif "list" in args:
        print('[{"description":"g5-test-app","state":"stopped"}]')
elif name == "evallab":
    if args[:2] == ["capture", "smoke"]:
        if "--help" in args:
            print("--model")
        else:
            out = pathlib.Path(arg("--out"))
            out.mkdir(parents=True)
            (out / "smoke.json").write_text(json.dumps({"status": 200, "model": arg("--model").removeprefix("selfhosted/"), "route_token": "test"}))
    elif args[:2] == ["spend", "check"]:
        print('{"allowed":true}')
    elif args[0] == "submit":
        print("01M" + "A" * 23)
    elif args[:2] == ["capture", "serve"]:
        out = pathlib.Path(arg("--out"))
        (out / "calls.jsonl").write_text('{"model":"test"}\n')
        (root / "capture.pid").write_text(str(os.getpid()))
        idle()
    elif args[:2] == ["telemetry", "sample"]:
        idle()
    elif args[0] == "tick":
        for spec in (root / "specs").glob("ovn-g5-*.json"):
            (root / "runs" / spec.stem).mkdir(parents=True, exist_ok=True)
        print("stub tick complete")
        sys.exit(int(os.environ["G5_TICK_STATUS"]))
    elif args[:2] == ["capture", "link"]:
        print('{"linked":true}')
    elif args[:2] == ["modal", "billing-reconcile"]:
        print("billing reconciled")
    elif args[:2] == ["spend", "day"]:
        print("spend day complete")
    elif args[0] != "approve":
        sys.exit(1)
"""


def _executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def _run_round(tmp_path: Path, *, tick_status: int, outcome_status: int, waves: int = 1):
    root = tmp_path / "repo"
    root.mkdir()
    fake_bin = root / "bin"
    stub = f"#!{sys.executable}\n{STUB}"
    for command in ("git", "curl", "lsof", "sleep", "uv"):
        _executable(fake_bin / command, stub)
    _executable(root / ".venv/bin/evallab", stub)
    _executable(
        root / ".venv/bin/python",
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "args = sys.argv[1:]\n"
        "if args and args[0].endswith('make_g5_specs.py'):\n"
        "    print('check ok'); sys.exit(0)\n"
        "if args and args[0].endswith('g5_wave_outcome.py'):\n"
        "    print('{\"gate_test\":true}'); sys.exit(int(os.environ['G5_OUTCOME_STATUS']))\n"
        "if args[:1] == ['-']:\n"
        "    source = sys.stdin.read()\n"
        "    if 'validate_model_pin' in source:\n"
        "        print('test profile ok'); sys.exit(0)\n"
        "    sys.argv = ['-', *args[1:]]\n"
        "    exec(compile(source, '<g5-heredoc>', 'exec')); sys.exit(0)\n"
        f"os.execv({sys.executable!r}, [{sys.executable!r}, *args])\n",
    )
    app = root / "tools/modal-mimo-serve/serve_lora.py"
    app.parent.mkdir(parents=True)
    app.write_text('APP_NAME = "g5-test-app"\n')
    specs = root / "specs"
    specs.mkdir()
    arms = [f"arm-{i}" for i in range(waves)]
    (specs / "cohort.json").write_text(
        json.dumps({"cohort": [{"task_id": "format-code-task-1", "order": arms}]})
    )
    for arm in arms:
        (specs / f"ovn-g5-1-{arm}.json").write_text("{}")
    home = tmp_path / "home"
    key = home / ".local/state/evallab-har116/key"
    key.parent.mkdir(parents=True)
    key.write_text("test-key-not-a-credential")
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT),
            "--specs-dir",
            str(specs),
            "--adapter",
            "test",
            "--candidate-usd",
            "1",
            "--actor",
            "test",
            "--modal-app-day-limit-usd",
            "35",
            "--label",
            "test",
        ],
        cwd=root,
        env={
            **os.environ,
            "HOME": str(home),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "G5_TEST_ROOT": str(root),
            "G5_TICK_STATUS": str(tick_status),
            "G5_OUTCOME_STATUS": str(outcome_status),
        },
        text=True,
        capture_output=True,
        timeout=30,
    )
    manifest_path = next(
        (home / "Developer/eval-lab-results").glob("*/test-round/round-manifest.json")
    )
    commands = [json.loads(line) for line in (root / "commands.jsonl").read_text().splitlines()]
    return result, json.loads(manifest_path.read_text()), commands, manifest_path.parent


@pytest.mark.parametrize("tick_status,outcome_status", [(0, 1), (7, 0), (7, 1)])
def test_last_wave_failed_gate_preserves_capture_and_billing(
    tmp_path: Path, tick_status: int, outcome_status: int
) -> None:
    result, manifest, commands, out = _run_round(
        tmp_path, tick_status=tick_status, outcome_status=outcome_status
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert manifest["gate_failure"]["wave"] == "wave-1"
    assert manifest["gate_failure"]["tick_status"] == tick_status
    assert manifest["gate_failure"]["outcome_status"] == outcome_status
    assert manifest["gate_failure"]["last_wave"] is True
    capture = manifest["capture"]
    raw = Path(capture["file"]).read_bytes()
    assert capture["sha256"] == hashlib.sha256(raw).hexdigest()
    assert capture["bytes"] == len(raw)
    assert capture["lines"] == 1
    assert not Path(capture["file"]).stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    assert any(c[:3] == ["evallab", "capture", "link"] for c in commands)
    assert (out / "billing-reconcile-after.txt").read_text() == "billing reconciled\n"
    assert (out / "spend-day-after.txt").read_text() == "spend day complete\n"
    assert manifest["teardown"]["state"] == "stopped"
    assert manifest["teardown"]["exit_status"] == 3
    assert any("stop" in c and "g5-test-app" in c for c in commands)
    assert "GATE FAILED" in (out / "round.log").read_text()
    print(
        f"tick={tick_status} outcome={outcome_status}: exit={result.returncode}; "
        f"capture link=yes frozen=yes sha256={capture['sha256']} bytes={capture['bytes']} "
        f"lines={capture['lines']}; billing reconcile=yes spend day=yes; teardown=stopped"
    )


@pytest.mark.parametrize("tick_status,outcome_status", [(0, 1), (7, 0)])
def test_earlier_wave_failed_gate_never_ticks_next_wave(
    tmp_path: Path,
    tick_status: int,
    outcome_status: int,
) -> None:
    result, manifest, commands, out = _run_round(
        tmp_path, tick_status=tick_status, outcome_status=outcome_status, waves=2
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert sum(c[:2] == ["evallab", "tick"] for c in commands) == 1
    assert "wave-2" not in manifest
    assert "capture" not in manifest
    assert not (out / "billing-reconcile-after.txt").exists()
    assert manifest["teardown"]["state"] == "stopped"


def test_successful_last_wave_finalizes_without_failure(tmp_path: Path) -> None:
    result, manifest, _, out = _run_round(tmp_path, tick_status=0, outcome_status=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "gate_failure" not in manifest
    assert manifest["capture"]["lines"] == 1
    assert manifest["teardown"]["exit_status"] == 0
    assert (out / "spend-day-after.txt").read_text() == "spend day complete\n"
