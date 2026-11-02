"""test_killtests.py: Automated unit and contract tests for DBOS squad runner and kill tests."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from dbos import DBOS, DBOSConfig, SetWorkflowID
from dbos_squad import (
    DBOSSquadRunner,
    QuotaExhaustedError,
    TransientWorkerError,
)


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def test_fake_agent_execution_and_effect(temp_dir):
    ledger = temp_dir / "ledger.jsonl"
    report = temp_dir / "report.md"
    fa_script = Path(__file__).parent.parent / "src" / "fake_agent.py"

    cmd = [
        sys.executable,
        str(fa_script),
        "--id", "FA-1",
        "--ledger", str(ledger),
        "--report", str(report),
        "--seconds", "0.1",
    ]
    res1 = subprocess.run(cmd, capture_output=True, text=True)
    assert res1.returncode == 0
    assert report.exists()
    assert "DS-DONE: FA-1" in report.read_text(encoding="utf-8")

    # Run second time: exec should increment, effect should NOT duplicate
    res2 = subprocess.run(cmd, capture_output=True, text=True)
    assert res2.returncode == 0

    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    execs = [row for row in lines if row.get("kind") == "exec"]
    effects = [row for row in lines if row.get("kind") == "effect"]
    assert len(execs) == 2
    assert len(effects) == 1


def test_fake_agent_quota_refusal(temp_dir):
    ledger = temp_dir / "ledger.jsonl"
    report = temp_dir / "report.md"
    fa_script = Path(__file__).parent.parent / "src" / "fake_agent.py"

    future_t = time.time() + 100.0
    cmd = [
        sys.executable,
        str(fa_script),
        "--id", "FA-QUOTA",
        "--ledger", str(ledger),
        "--report", str(report),
        "--seconds", "0.1",
        "--quota-until", str(future_t),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    assert res.returncode == 29
    assert "quota exhausted" in res.stdout

    lines = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    refusals = [row for row in lines if row.get("kind") == "quota-refusal"]
    assert len(refusals) == 1
    assert not report.exists()


def test_dbos_step_transient_retry(temp_dir):
    db_path = temp_dir / "test_transient.sqlite"
    cfg = DBOSConfig(name="test_transient_app", system_database_url=f"sqlite:///{db_path}")
    DBOS(config=cfg)

    attempts = {"count": 0}

    def should_retry_fn(err):
        return isinstance(err, TransientWorkerError)

    @DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=0.05, backoff_rate=1.2, should_retry=should_retry_fn)
    def flaky_step():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise TransientWorkerError("Temporary failure")
        return "success"

    @DBOS.workflow(name="transient_wf")
    def transient_wf():
        return flaky_step()

    DBOS.launch()
    try:
        res = transient_wf()
        assert res == "success"
        assert attempts["count"] == 3
    finally:
        DBOS.destroy()


def test_dbos_quota_parking(temp_dir):
    db_path = temp_dir / "test_quota.sqlite"
    cfg = DBOSConfig(name="test_quota_app", system_database_url=f"sqlite:///{db_path}")
    DBOS(config=cfg)

    attempts = {"count": 0}

    @DBOS.step()
    def quota_step():
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise QuotaExhaustedError(reset_at=time.time() + 0.3)
        return "recovered"

    @DBOS.workflow(name="quota_wf")
    def quota_wf():
        while True:
            try:
                return quota_step()
            except QuotaExhaustedError as qe:
                DBOS.sleep(max(0.0, qe.reset_at - time.time()))

    DBOS.launch()
    try:
        t0 = time.time()
        res = quota_wf()
        elapsed = time.time() - t0
        assert res == "recovered"
        assert attempts["count"] == 2
        assert elapsed >= 0.25
    finally:
        DBOS.destroy()


def test_dbos_gate_approval_signal(temp_dir):
    db_path = temp_dir / "test_gate.sqlite"
    cfg = DBOSConfig(name="test_gate_app", system_database_url=f"sqlite:///{db_path}")
    DBOS(config=cfg)

    @DBOS.workflow(name="gate_wf")
    def gate_wf():
        msg = DBOS.recv(topic="test_topic", timeout_seconds=10.0)
        return msg

    DBOS.launch()
    try:
        import threading
        def sender():
            time.sleep(0.5)
            DBOS.send("gate-wf-1", {"approved": True, "by": "operator"}, topic="test_topic")

        t = threading.Thread(target=sender)
        t.start()

        with SetWorkflowID("gate-wf-1"):
            res = gate_wf()

        t.join()
        assert isinstance(res, dict)
        assert res.get("approved") is True
    finally:
        DBOS.destroy()


def test_dbos_caps_wallclock(temp_dir):
    db_path = temp_dir / "test_caps.sqlite"
    runner = DBOSSquadRunner(db_path=db_path, app_name="test_caps_app")
    runner.launch(max_parallel=1)

    plan = {
        "model": "fake-model",
        "budget_usd": 10.0,
        "max_parallel": 1,
        "ledger_path": str(temp_dir / "spend.jsonl"),
        "items": [
            {
                "id": "CAP-TIME",
                "title": "Item timing out",
                "seconds": 10.0,
                "max_seconds": 1.0,
                "fake_agent": True,
                "needs": [],
            }
        ],
    }

    try:
        res = runner.run_squad(plan, temp_dir, workflow_id="wf-cap-time")
        assert res["items"]["CAP-TIME"]["status"] == "killed-time"
    finally:
        runner.destroy()


def test_dbos_caps_iterations(temp_dir):
    db_path = temp_dir / "test_iter_caps.sqlite"
    runner = DBOSSquadRunner(db_path=db_path, app_name="test_iter_app")
    runner.launch(max_parallel=1)

    plan = {
        "model": "fake-model",
        "budget_usd": 10.0,
        "max_parallel": 1,
        "ledger_path": str(temp_dir / "spend.jsonl"),
        "items": [
            {
                "id": "CAP-ITER",
                "title": "Item failing transiently",
                "seconds": 0.1,
                "fake_agent": True,
                "fail_transient": True,
                "retry": 1,
                "needs": [],
            }
        ],
    }

    try:
        res = runner.run_squad(plan, temp_dir, workflow_id="wf-cap-iter")
        assert res["items"]["CAP-ITER"]["status"] == "failed"
        assert "iteration cap reached" in res["items"]["CAP-ITER"]["note"]
    finally:
        runner.destroy()
