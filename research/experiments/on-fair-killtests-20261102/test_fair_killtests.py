"""test_fair_killtests.py: Targeted pytest suite for F11 fair kill-test fixtures and parsers."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

EXPERIMENT_DIR = Path(__file__).resolve().parent
FAKE_AGENT_PATH = EXPERIMENT_DIR / "fake_agent.py"

sys.path.insert(0, str(EXPERIMENT_DIR))
from ledger_reader import analyze_kt1_ledger, analyze_kt3_ledger, read_ledger, wilson_interval  # noqa: E402


def test_fake_agent_guarded_idempotency():
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        ledger = tdp / "ledger.jsonl"
        rep1 = tdp / "rep1.md"
        rep2 = tdp / "rep2.md"

        # First run
        cmd1 = [sys.executable, str(FAKE_AGENT_PATH), "--id", "TASK_G", "--ledger", str(ledger), "--report", str(rep1), "--seconds", "0.1", "--mode", "guarded"]
        subprocess.run(cmd1, check=True)

        # Second run with same id
        cmd2 = [sys.executable, str(FAKE_AGENT_PATH), "--id", "TASK_G", "--ledger", str(ledger), "--report", str(rep2), "--seconds", "0.1", "--mode", "guarded"]
        subprocess.run(cmd2, check=True)

        rows = read_ledger(ledger)
        starts = [r for r in rows if r.get("kind") == "start" and r.get("id") == "TASK_G"]
        effects = [r for r in rows if r.get("kind") == "effect" and r.get("id") == "TASK_G"]
        unguarded = [r for r in rows if r.get("kind") == "unguarded-effect" and r.get("id") == "TASK_G"]

        assert len(starts) == 2
        assert len(effects) == 1  # Exactly-once idempotent effect
        assert len(unguarded) == 0


def test_fake_agent_unguarded_mode():
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        ledger = tdp / "ledger.jsonl"
        rep1 = tdp / "rep1.md"
        rep2 = tdp / "rep2.md"

        cmd1 = [sys.executable, str(FAKE_AGENT_PATH), "--id", "TASK_U", "--ledger", str(ledger), "--report", str(rep1), "--seconds", "0.1", "--mode", "unguarded"]
        subprocess.run(cmd1, check=True)

        cmd2 = [sys.executable, str(FAKE_AGENT_PATH), "--id", "TASK_U", "--ledger", str(ledger), "--report", str(rep2), "--seconds", "0.1", "--mode", "unguarded"]
        subprocess.run(cmd2, check=True)

        rows = read_ledger(ledger)
        starts = [r for r in rows if r.get("kind") == "start" and r.get("id") == "TASK_U"]
        unguarded = [r for r in rows if r.get("kind") == "unguarded-effect" and r.get("id") == "TASK_U"]

        assert len(starts) == 2
        assert len(unguarded) == 2  # Duplicates recorded in side-effects


def test_analyze_kt1_ledger_metrics():
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        ledger = tdp / "ledger.jsonl"

        rows = [
            {"kind": "start", "id": "A1", "pid": 100, "t": 0.0, "seconds": 2.0},
            {"kind": "exit", "id": "A1", "pid": 100, "t": 2.0, "rc": 0},
            {"kind": "effect", "id": "A1", "pid": 100, "t": 2.0},
            {"kind": "start", "id": "A2", "pid": 101, "t": 2.0, "seconds": 2.0},
            # Kill occurs at t=3.0: A1 completed, A2 in flight
            # Case 1: duplicate A2 started while orphan 101 is still alive until t=4.0
            {"kind": "start", "id": "A2", "pid": 102, "t": 3.5, "seconds": 2.0},
            {"kind": "exit", "id": "A2", "pid": 101, "t": 4.0, "rc": 0},
            {"kind": "exit", "id": "A2", "pid": 102, "t": 5.5, "rc": 0},
            {"kind": "effect", "id": "A2", "pid": 101, "t": 4.0},
        ]
        with open(ledger, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

        analysis = analyze_kt1_ledger(ledger, ["A1", "A2"], t_kill=3.0)
        assert analysis["completed_before_kill"] == ["A1"]
        assert analysis["in_flight_at_kill"] == ["A2"]
        assert analysis["completed_re_executed"] == 0
        assert analysis["in_flight_duplicated"] == 1
        # Overlap between pid 101 [2.0, 4.0] and pid 102 [3.5, 5.5] is min(4.0, 5.5) - max(2.0, 3.5) = 4.0 - 3.5 = 0.5s
        assert abs(analysis["overlap_seconds"] - 0.5) < 1e-3


def test_analyze_kt3_ledger_metrics():
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        ledger = tdp / "ledger.jsonl"

        t_reset = 90.0
        rows = [
            {"kind": "start", "id": "QUOTA", "pid": 200, "t": 0.0},
            {"kind": "quota-refusal", "id": "QUOTA", "pid": 200, "t": 0.0, "reset_at": t_reset},
            {"kind": "exit", "id": "QUOTA", "pid": 200, "t": 0.1, "rc": 29},
            # Second attempt cleanly after t_reset
            {"kind": "start", "id": "QUOTA", "pid": 201, "t": 90.05},
            {"kind": "effect", "id": "QUOTA", "pid": 201, "t": 91.0},
            {"kind": "exit", "id": "QUOTA", "pid": 201, "t": 91.0, "rc": 0},
        ]
        with open(ledger, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

        analysis = analyze_kt3_ledger(ledger, t_reset)
        assert analysis["passed"] is True
        assert analysis["attempts_before_t"] == 0
        assert analysis["attempts_after_t"] == 1
        assert abs(analysis["first_start_delay_sec"] - 0.05) < 1e-3


def test_wilson_interval():
    ci_10_10 = wilson_interval(10, 10)
    assert ci_10_10 == (72.2, 100.0)

    ci_0_10 = wilson_interval(0, 10)
    assert ci_0_10 == (0.0, 27.8)

    ci_3_3 = wilson_interval(3, 3)
    assert ci_3_3 == (43.9, 100.0)
