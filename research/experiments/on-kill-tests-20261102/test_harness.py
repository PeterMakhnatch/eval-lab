"""Unit tests for engine-neutral kill-test fixtures and ledger parsing."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

EXPERIMENT_DIR = Path(__file__).resolve().parent
FAKE_AGENT_PATH = EXPERIMENT_DIR / "fake_agent.py"
sys.path.insert(0, str(EXPERIMENT_DIR))
from ledger_reader import analyze_quota_parking, summarize_ledger  # noqa: E402


def test_fake_agent_execution_and_idempotence(tmp_path: Path):
    ledger = tmp_path / "ledger.jsonl"
    report = tmp_path / "report.md"

    # First execution
    cmd1 = [
        sys.executable, str(FAKE_AGENT_PATH),
        "--id", "T1", "--ledger", str(ledger), "--report", str(report),
        "--seconds", "0.2"
    ]
    r1 = subprocess.run(cmd1, capture_output=True, text=True)
    assert r1.returncode == 0
    assert report.exists()
    assert "DS-DONE: T1" in report.read_text()

    # Second execution (re-run of finished task)
    r2 = subprocess.run(cmd1, capture_output=True, text=True)
    assert r2.returncode == 0

    # Summary: 2 execs, exactly 1 effect
    summary = summarize_ledger(ledger, ["T1"])
    assert summary["exec_counts"]["T1"] == 2
    assert summary["effect_counts"]["T1"] == 1
    assert summary["exact_one_effect"] is True


def test_fake_agent_quota_refusal(tmp_path: Path):
    ledger = tmp_path / "ledger.jsonl"
    report = tmp_path / "report.md"
    reset_epoch = 2000000000.0  # far future

    cmd = [
        sys.executable, str(FAKE_AGENT_PATH),
        "--id", "QX", "--ledger", str(ledger), "--report", str(report),
        "--seconds", "0.1", "--quota-until", str(reset_epoch)
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 29
    assert "quota exhausted" in r.stderr

    summary = summarize_ledger(ledger, ["QX"])
    assert summary["refusal_counts"]["QX"] == 1
    assert summary["exec_counts"]["QX"] == 0


def test_quota_parking_analysis(tmp_path: Path):
    ledger = tmp_path / "ledger.jsonl"
    t_reset = 100.0
    entries = [
        {"id": "X", "kind": "quota-refusal", "t": 10.0},
        {"id": "X", "kind": "exec", "t": 100.5},
        {"id": "X", "kind": "effect", "t": 101.0},
    ]
    ledger.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    res = analyze_quota_parking(ledger, "X", t_reset)
    assert res["clean_parking"] is True
    assert res["attempts_before_t"] == 0
    assert res["tolerance_sec"] == 0.5
