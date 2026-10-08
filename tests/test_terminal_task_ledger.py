"""Tests for the terminal task ledger builder (deterministic, no Docker)."""

from __future__ import annotations

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

EXPERIMENT = Path(__file__).resolve().parents[1] / "research" / "experiments" / "terminal-task-ledger"


def _load_build():
    spec = importlib.util.spec_from_file_location("terminal_ledger_build", EXPERIMENT / "build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_decide_branches() -> None:
    build = _load_build()
    decision, _ = build.decide("candidate-0674-ml-evaluation", has_compiler_test=False)
    assert decision == "discard"
    decision, reason = build.decide("candidate-0260-security-appsec", has_compiler_test=False)
    assert decision == "fix"
    assert "stevedore" in reason
    decision, reason = build.decide("candidate-0109-science-robotics", has_compiler_test=True)
    assert decision == "fix"
    assert "g++" in reason
    decision, _ = build.decide("candidate-0036-software-data-engineering", has_compiler_test=False)
    assert decision == "keep"


def _synthetic_task(root: Path, task_id: str, *, compiler: bool) -> None:
    task = root / task_id
    (task / "tests").mkdir(parents=True)
    (task / "environment" / "setup").mkdir(parents=True)
    (task / "environment" / "setup" / "setup.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    body = ""
    if compiler:
        body = "import subprocess\nx = ['g++', '-c', 'a.cpp']\n"
    (task / "tests" / "test_outputs.py").write_text(body + "def test_x():\n    pass\n", encoding="utf-8")
    (task / "task.toml").write_text(
        'schema_version = "1.4"\n\n[task]\nname = "mimo-v2.6-rl/'
        + task_id
        + '"\n\n[environment]\ndocker_image = "docker.io/example/img@sha256:'
        + "0" * 64
        + '"\nworkdir = "/app"\n',
        encoding="utf-8",
    )


def _record(records: Path, task_id: str, digest12: str) -> None:
    dest = records / f"mimo-v2.6-rl__{task_id}"
    dest.mkdir(parents=True)
    (dest / f"{digest12}.json").write_text(
        json.dumps(
            {
                "task_name": f"mimo-v2.6-rl/{task_id}",
                "transform": "terminal-guard-extend@1",
                "variant_digest": digest12 + "0" * (64 - 12),
                "created_at": "2026-10-08T00:00:00Z",
                "status": "candidate",
            }
        ),
        encoding="utf-8",
    )


def test_build_end_to_end(tmp_path: Path) -> None:
    snapshot = tmp_path / "tasks"
    snapshot.mkdir()
    _synthetic_task(snapshot, "candidate-0000-plain", compiler=False)
    _synthetic_task(snapshot, "candidate-0001-native", compiler=True)
    records = tmp_path / "records"
    _record(records, "candidate-0000-plain", "aaaabbbbcccc")
    _record(records, "candidate-0001-native", "ddddeeeeffff")
    validation = tmp_path / "validation.json"
    validation.write_text(json.dumps({"candidate-0000-plain": "nop:0 (1F)"}), encoding="utf-8")
    out = tmp_path / "ledger.csv"
    proc = subprocess.run(
        [
            sys.executable,
            str(EXPERIMENT / "build.py"),
            "--snapshot",
            str(snapshot),
            "--records",
            str(records),
            "--validation",
            str(validation),
            "--output",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(EXPERIMENT.parents[2]),
    )
    assert proc.returncode == 0, proc.stderr
    rows = {row["task_id"]: row for row in csv.DictReader(out.read_text(encoding="utf-8").splitlines())}
    assert set(rows) == {"candidate-0000-plain", "candidate-0001-native"}
    assert rows["candidate-0000-plain"]["decision"] == "keep"
    assert rows["candidate-0000-plain"]["guard_variant"] == "aaaabbbbcccc"
    assert rows["candidate-0000-plain"]["validation"] == "nop:0 (1F)"
    assert rows["candidate-0001-native"]["decision"] == "fix"
    assert "static-only" in rows["candidate-0001-native"]["validation"]
