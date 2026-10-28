"""Offline native-seal boundaries: synthetic inputs, no model, quota, or campaign IO."""

from __future__ import annotations

import asyncio
import builtins
import importlib.util
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.fixture
def runner(monkeypatch, tmp_path):
    here = Path(__file__).resolve().parent
    monkeypatch.syspath_prepend(str(here.parents[3] / "src"))
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("tt4_native_seal_test", here / "run.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    for name, relative in {
        "ROOT": "repo",
        "BRIDGE": "historical-bridge",
        "STATE": "state",
        "SPEND": "spend.jsonl",
        "FROZEN": "historical-campaign",
        "CAMPAIGN": "sealed-campaign",
        "RECEIPTS": "sealed-campaign/receipts",
        "SAMPLE": "historical-bridge/sample.json",
        "CASCADE": "historical-bridge/cascade.json",
        "SCANS": "historical-scans",
        "HALT": "sealed-campaign/halt.json",
    }.items():
        monkeypatch.setattr(module, name, tmp_path / relative)
    return module


def count_denials(monkeypatch, runner):
    guard = runner.require_native_omp_transport
    reasons = []

    def deny():
        try:
            guard()
        except runner.OmpTransportSealedError as exc:
            reasons.append(str(exc))
            raise

    monkeypatch.setattr(runner, "require_native_omp_transport", deny)
    return reasons


@contextmanager
def side_effect_sentinels(monkeypatch, runner, *, allow_quota=False):
    from evallab.readers import omp_model

    effects = []

    def forbidden(label):
        def fail(*args, **kwargs):
            effects.append(label)
            raise AssertionError(f"sealed execution reached {label}")

        return fail

    runner_calls = (
        "prepare", "score", "connect", "jsonl", "save", "append", "collect",
        "flush_spend", "source_abstention", "locate_scan",
    )
    if not allow_quota:
        runner_calls += ("quota",)
    with monkeypatch.context() as patch:
        for target, names, label in (
            (runner, runner_calls, "runner"),
            (runner.fcntl, ("flock",), "lock"),
            (omp_model, ("register_omp_provider", "call_omp", "subscription_env"), "provider"),
            (subprocess, ("run", "Popen", "check_output", "check_call"), "subprocess"),
            (asyncio, ("create_subprocess_exec",), "async-subprocess"),
            (
                Path,
                (
                    "mkdir", "open", "read_text", "read_bytes", "write_text", "write_bytes",
                    "glob", "rglob", "iterdir", "exists", "stat", "unlink", "replace",
                ),
                "filesystem",
            ),
        ):
            for name in names:
                patch.setattr(target, name, forbidden(f"{label}.{name}"))
        original_import = builtins.__import__
        native_modules = (
            "inspect_ai", "inspect_scout", "metr_scanners", "paid_projection",
            "evallab.readers.scout_clouddb", "evallab.readers.clouddb_blind",
        )

        def guarded_import(name, *args, **kwargs):
            if any(name == prefix or name.startswith(prefix + ".") for prefix in native_modules):
                forbidden(f"import.{name}")()
            return original_import(name, *args, **kwargs)

        patch.setattr(builtins, "__import__", guarded_import)
        yield effects


class UnreadInputs:
    """A synthetic fixed-200 source that fails on any attempt to inspect it."""

    def __init__(self):
        self.rows = tuple(
            {"run_id": f"synthetic-trace-{i}", "opaque_id": f"synthetic-run-{i}", "ordinal": i}
            for i in range(1, 201)
        )
        self.accesses = []

    def __iter__(self):
        self.accesses.append("iteration")
        raise AssertionError("native denial must precede source iteration")

    def __len__(self):
        self.accesses.append("length")
        raise AssertionError("native denial must precede source inspection")

    def __getitem__(self, index):
        self.accesses.append("index")
        raise AssertionError("native denial must precede source access")


@pytest.mark.parametrize("arguments", [[], ["--limit", "1"], ["--limit", "200"]])
def test_run_command_fails_once_before_any_native_or_campaign_activity(
    runner, monkeypatch, capsys, arguments,
):
    monkeypatch.setattr(sys, "argv", ["tt4-runner", "run", *arguments])
    reasons = count_denials(monkeypatch, runner)
    with (
        side_effect_sentinels(monkeypatch, runner) as effects,
        pytest.raises(SystemExit) as stopped,
    ):
        runner.main()
    output = capsys.readouterr()
    assert stopped.value.code == 1
    assert len(reasons) == 1 and "sealed" in reasons[0].lower()
    assert output.err == reasons[0] + "\n"
    assert output.out == ""
    assert effects == []
    assert not runner.CAMPAIGN.exists()


@pytest.mark.parametrize("limit", [None, 0, 1, 200])
def test_direct_run_denies_once_before_reading_even_a_fixed_200_source(
    runner, monkeypatch, limit,
):
    keys = UnreadInputs()
    reasons = count_denials(monkeypatch, runner)
    with (
        side_effect_sentinels(monkeypatch, runner) as effects,
        pytest.raises(runner.OmpTransportSealedError, match="(?i)sealed"),
    ):
        runner.run(keys, limit)
    assert len(reasons) == 1
    assert keys.accesses == []
    assert effects == []
    assert not runner.CAMPAIGN.exists()


def test_direct_quota_denies_without_refreshing_usage_or_writing_ledgers(runner, monkeypatch):
    reasons = count_denials(monkeypatch, runner)
    with (
        side_effect_sentinels(monkeypatch, runner, allow_quota=True) as effects,
        pytest.raises(runner.OmpTransportSealedError, match="(?i)sealed"),
    ):
        runner.quota()
    assert len(reasons) == 1
    assert effects == []
    assert not runner.CAMPAIGN.exists()
    assert not runner.SPEND.exists()


@pytest.mark.parametrize("command", ["prepare", "score"])
def test_offline_cli_dispatch_remains_available_with_mocked_handlers(
    runner, monkeypatch, command,
):
    events = []
    keys = [{"run_id": "synthetic-trace", "opaque_id": "synthetic-run", "ordinal": 1}]

    def forbidden(*args, **kwargs):
        raise AssertionError("offline CLI dispatch must not enter a native route")

    def prepare():
        events.append("prepare")
        return keys

    def score(received):
        assert received is keys
        events.append("score")

    def lock(stream, flags):
        assert Path(stream.name) == runner.CAMPAIGN / "runner.lock"
        assert flags == runner.fcntl.LOCK_EX | runner.fcntl.LOCK_NB
        events.append("lock")

    monkeypatch.setattr(sys, "argv", ["tt4-runner", command])
    monkeypatch.setattr(runner, "prepare", prepare)
    monkeypatch.setattr(runner, "score", score)
    monkeypatch.setattr(runner, "require_native_omp_transport", forbidden)
    monkeypatch.setattr(runner, "quota", forbidden)
    monkeypatch.setattr(runner, "run", forbidden)
    monkeypatch.setattr(runner.fcntl, "flock", lock)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    from evallab.readers import omp_model

    monkeypatch.setattr(omp_model, "register_omp_provider", forbidden)
    monkeypatch.setattr(omp_model, "call_omp", forbidden)
    runner.main()
    assert events == ["lock", "prepare", *(["score"] if command == "score" else [])]
    assert (runner.CAMPAIGN / "runner.lock").is_file()
    assert list(runner.CAMPAIGN.iterdir()) == [runner.CAMPAIGN / "runner.lock"]


def test_historical_metadata_parser_and_metrics_need_no_native_activity(runner, monkeypatch):
    observed = 1_798_560_000_000
    report = {
        "generatedAt": observed - 100,
        "reports": [{
            "provider": "kimi-code",
            "fetchedAt": observed - 200,
            "limits": [{
                "status": "ok",
                "scope": {"provider": "kimi-code", "windowId": window},
                "amount": {"usedFraction": 0.2, "remainingFraction": 0.8},
                "window": {"id": window, "resetsAt": observed + 3_600_000},
            } for window in ("5h", "limit_month_total", "limit_month_code")],
        }],
    }
    records = [{
        "run_id": "synthetic-positive", "tool": "scout_metr", "detector": runner.DETECTOR,
        "detector_version": runner.DETECTOR_VERSION, "value": 7,
    }]
    labels = {"synthetic-positive": True, "synthetic-unmeasured": False}
    baseline = {
        trace: {"alarmed": False, "stage1_any": False}
        for trace in labels
    }
    with side_effect_sentinels(monkeypatch, runner) as effects:
        parsed = runner.parse_quota_report(report, observed_at_ms=observed)
        values = runner.scores(records)
        measured = runner.metrics(values, labels, baseline, threshold=7)
    assert parsed["available"] is True
    assert parsed["stop_used_fraction"] == 0.9
    assert parsed["freshness_ttl_ms"] == 60_000
    assert parsed["reset_at"] == []
    assert values == {"synthetic-positive": 7}
    assert (measured["TP"], measured["FP"], measured["abstentions"]) == (1, 0, 1)
    assert effects == []
    assert not runner.CAMPAIGN.exists()
