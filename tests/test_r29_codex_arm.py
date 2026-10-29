import importlib.util
import json
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1] / "research/experiments/r29-oq1-codex-cross-arm"
SPEC = importlib.util.spec_from_file_location("r29_codex_arm", BASE / "codex_arm.py")
codex_arm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(codex_arm)


def _report(*limits, provider="openai-codex"):
    return {"reports": [{"provider": provider, "limits": list(limits)}]}


def _limit(window_id, fraction):
    return {"window": {"id": window_id, "durationMs": 604800000 if window_id == "7d" else 18000000},
            "amount": {"usedFraction": fraction}}


def test_weekly_fraction_uses_only_the_seven_day_window():
    report = _report(_limit("5h", 0.99), _limit("7d", 0.69))
    assert codex_arm.Quota.weekly(report) == pytest.approx(0.69)


def test_weekly_fraction_fails_closed_without_a_weekly_window_or_on_error():
    with pytest.raises(RuntimeError):
        codex_arm.Quota.weekly(_report(_limit("5h", 0.1)))
    with pytest.raises(RuntimeError):
        codex_arm.Quota.weekly({"reports": [{"provider": "openai-codex", "error": "x", "limits": []}]})


def test_admission_stops_at_the_quota_threshold_and_on_a_stop_file(tmp_path, monkeypatch):
    stop = tmp_path / "STOP"
    quota = codex_arm.Quota(stop)

    class Done:
        stdout = json.dumps(_report(_limit("7d", 0.86)))

    monkeypatch.setattr(codex_arm.subprocess, "run", lambda *a, **k: Done())
    with pytest.raises(codex_arm.Stopped):
        quota.admit()
    quota = codex_arm.Quota(stop)
    stop.write_text("")
    with pytest.raises(codex_arm.Stopped):
        quota.admit()


def test_unanswered_receipt_becomes_a_terminal_missing_native_result(tmp_path):
    receipt = {"status": "invalid", "error": "session identity, fallback or tool protocol violation", "answer": None}
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))
    result = codex_arm.native_result(receipt, path)
    assert (result["disposition"], result["terminal"], result["sent"]) == ("missing", True, True)
    assert result["answer"] is None and result["findings_count"] is None
