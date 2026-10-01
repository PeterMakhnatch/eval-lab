"""Unit tests for evallab.run_telemetry (HAR-126).

Covers:
- Timestamp parsing and percentile calculations
- Prometheus metric parsing for SGLang gauges
- Modal container list and GPU stats extraction
- Live sampler execution into JSONL
- Post-hoc extractor with synthetic multi-trial fixtures:
  - Per-call latency derivation from trajectory step timestamps
  - Concurrency calculation (peak, mean, and per-concurrency-level buckets)
  - Trial startup duration
  - Trajectory step vs proxy ledger call reconciliation
  - Handling of corrupt/missing files
- CLI integration via evallab telemetry
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.cli import run_cli
from evallab.run_telemetry import (
    CallRecord,
    _concurrency_at,
    extract_round,
    extract_trial,
    parse_sglang_metrics,
    parse_ts,
    percentile,
    run_sampler,
    sample_once,
    summarize,
)

# ---------------------------------------------------------------------------
# 1. Basic parsing utilities
# ---------------------------------------------------------------------------


def test_parse_ts() -> None:
    assert parse_ts(None) is None
    assert parse_ts("") is None
    assert parse_ts("not-a-timestamp") is None

    # UTC timestamps
    parsed = parse_ts("2026-10-01T01:50:16.512204Z")
    assert parsed is not None
    assert parsed.tzinfo == UTC
    assert parsed.year == 2026 and parsed.month == 10 and parsed.day == 1

    parsed_offset = parse_ts("2026-10-01T01:50:16.512204+00:00")
    assert parsed_offset == parsed


def test_percentile() -> None:
    assert percentile([], 0.5) is None
    assert percentile([10.0], 0.5) == 10.0
    assert percentile([10.0], 0.9) == 10.0

    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert percentile(xs, 0.5) == 6.0
    assert percentile(xs, 0.9) == 10.0


# ---------------------------------------------------------------------------
# 2. Prometheus metrics parsing
# ---------------------------------------------------------------------------


def test_parse_sglang_metrics() -> None:
    sample_prometheus = """
# HELP sglang:prompt_tokens_total Number of prefill tokens processed.
# TYPE sglang:prompt_tokens_total counter
sglang:prompt_tokens_total{model_name="mimo"} 8128902.0
# HELP sglang:num_running_reqs The number of running requests
# TYPE sglang:num_running_reqs gauge
sglang:num_running_reqs{model_name="mimo"} 14.0
# HELP sglang:num_queue_reqs The number of requests in the waiting queue
# TYPE sglang:num_queue_reqs gauge
sglang:num_queue_reqs{model_name="mimo"} 3.0
# HELP sglang:gen_throughput The generate throughput (token/s)
# TYPE sglang:gen_throughput gauge
sglang:gen_throughput{model_name="mimo"} 86.5
# HELP sglang:token_usage The token usage
# TYPE sglang:token_usage gauge
sglang:token_usage{model_name="mimo"} 0.28
# HELP sglang:cache_hit_rate The cache hit rate
# TYPE sglang:cache_hit_rate gauge
sglang:cache_hit_rate{model_name="mimo"} 0.0075
# HELP sglang:num_used_tokens The number of used tokens
# TYPE sglang:num_used_tokens gauge
sglang:num_used_tokens{model_name="mimo"} 123859.0
"""
    gauges = parse_sglang_metrics(sample_prometheus)
    assert gauges["num_running_reqs"] == 14.0
    assert gauges["num_queue_reqs"] == 3.0
    assert gauges["gen_throughput_tok_s"] == 86.5
    assert gauges["token_usage"] == 0.28
    assert gauges["cache_hit_rate"] == 0.0075
    assert gauges["num_used_tokens"] == 123859.0
    # Unmonitored counter not present in gauges
    assert "prompt_tokens_total" not in gauges


# ---------------------------------------------------------------------------
# 3. Live sampler unit tests
# ---------------------------------------------------------------------------


def test_sample_once_success(tmp_path: Path) -> None:
    fake_metrics = "sglang:num_running_reqs 5.0\nsglang:num_queue_reqs 2.0\n"

    def fake_fetch(_url: str) -> str:
        return fake_metrics

    def fake_modal(_app: str | None) -> list[dict[str, Any]]:
        return [{"container_id": "ta-12345"}]

    def fake_gpu(_cid: str) -> dict[str, Any]:
        return {
            "ok": True,
            "gpu_util_pct": 78.0,
            "mem_util_pct": 45.0,
            "mem_used_mib": 36000.0,
            "mem_total_mib": 80000.0,
        }

    # Setup temporary runs and queue directory
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    trial_dir = runs_dir / "trial-1"
    trial_dir.mkdir()
    (trial_dir / "lab-metadata.json").write_text(
        json.dumps({"started_at": "2026-10-01T05:00:00Z"}), encoding="utf-8"
    )

    queue_dir = tmp_path / "queue"
    (queue_dir / "running").mkdir(parents=True)
    (queue_dir / "approved").mkdir(parents=True)
    (queue_dir / "running" / "spec1.json").write_text("{}", encoding="utf-8")

    sample = sample_once(
        metrics_url="http://fake/metrics",
        runs_dir=runs_dir,
        modal_app="test-app",
        queue_dir=queue_dir,
        fetch_fn=fake_fetch,
        modal_containers_fn=fake_modal,
        modal_gpu_fn=fake_gpu,
    )

    assert "ts" in sample
    assert sample["sglang"]["ok"] is True
    assert sample["sglang"]["num_running_reqs"] == 5.0
    assert sample["sglang"]["num_queue_reqs"] == 2.0
    assert sample["modal"]["ok"] is True
    assert sample["modal"]["container_count"] == 1
    assert sample["modal"]["containers"] == ["ta-12345"]
    assert sample["gpu"]["ok"] is True
    assert sample["gpu"]["gpu_util_pct"] == 78.0
    assert sample["lab"]["runs_running"] == 1
    assert sample["lab"]["runs_finished"] == 0
    assert sample["lab"]["queue"]["running"] == 1
    assert sample["lab"]["queue"]["approved"] == 0


def test_sample_once_error_resilience() -> None:
    def failing_fetch(_url: str) -> str:
        raise OSError("connection refused")

    def failing_modal(_app: str | None) -> list[dict[str, Any]]:
        raise RuntimeError("modal CLI not found")

    sample = sample_once(
        metrics_url="http://unreachable/metrics",
        runs_dir=None,
        modal_app=None,
        fetch_fn=failing_fetch,
        modal_containers_fn=failing_modal,
    )

    assert sample["sglang"]["ok"] is False
    assert "OSError" in sample["sglang"]["error"]
    assert sample["modal"]["ok"] is False
    assert "RuntimeError" in sample["modal"]["error"]


def test_run_sampler_writes_jsonl(tmp_path: Path) -> None:
    out_file = tmp_path / "telemetry.jsonl"

    def fake_fetch(_url: str) -> str:
        return "sglang:num_running_reqs 1.0\n"

    def fake_modal(_app: str | None) -> list[dict[str, Any]]:
        return []

    sleep_calls: list[float] = []

    def mock_sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    written = run_sampler(
        out_file,
        metrics_url="http://fake/metrics",
        runs_dir=None,
        interval_s=10.0,
        samples=3,
        sleep_fn=mock_sleep,
        fetch_fn=fake_fetch,
        modal_containers_fn=fake_modal,
    )

    assert written == 3
    assert len(sleep_calls) == 2  # sleeps between samples 1-2 and 2-3
    assert sleep_calls[0] == 10.0

    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    records = [json.loads(line) for line in lines]
    assert all(r["interval_s"] == 10.0 for r in records)
    assert all(r["sglang"]["num_running_reqs"] == 1.0 for r in records)


# ---------------------------------------------------------------------------
# 4. Post-hoc extractor unit tests
# ---------------------------------------------------------------------------


def test_concurrency_at() -> None:
    t0 = datetime(2026, 10, 1, 1, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 10, 1, 1, 0, 5, tzinfo=UTC)
    t2 = datetime(2026, 10, 1, 1, 0, 10, tzinfo=UTC)
    t3 = datetime(2026, 10, 1, 1, 0, 15, tzinfo=UTC)

    calls = [
        CallRecord("job1", 1, t0, t2, 10.0),
        CallRecord("job2", 1, t1, t3, 10.0),
    ]

    # At t0: job1 is active, job2 hasn't started yet -> concurrency 1
    assert _concurrency_at(calls, t0) == 1
    # At t1: both job1 and job2 are active -> concurrency 2
    assert _concurrency_at(calls, t1) == 2
    # At t2: job1 finished [start <= t < end), job2 still active -> concurrency 1
    assert _concurrency_at(calls, t2) == 1
    # At t3: both finished -> concurrency 0
    assert _concurrency_at(calls, t3) == 0


def test_extractor_synthetic_fixtures(tmp_path: Path) -> None:
    # Build two synthetic trial directories
    # Trial A:
    #   Started at 01:00:00, finished at 01:02:00
    #   Step 1: user at 01:00:10
    #   Step 2: agent at 01:00:20 (latency 10s, tokens: 100/20)
    #   Step 3: agent at 01:00:30 (latency 10s, tokens: 150/30)
    # Trial B:
    #   Started at 01:00:05, finished at 01:01:30
    #   Step 1: user at 01:00:15
    #   Step 2: agent at 01:00:25 (latency 10s, tokens: 200/40)

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    job_a = runs_dir / "job-a"
    job_a.mkdir()
    (job_a / "lab-metadata.json").write_text(
        json.dumps(
            {
                "started_at": "2026-10-01T01:00:00+00:00",
                "finished_at": "2026-10-01T01:02:00+00:00",
                "provider_usage": {
                    "calls": [
                        {"call_id": 1, "status": 200},
                        {"call_id": 2, "status": 200},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    traj_a_dir = job_a / "trial_hash" / "agent"
    traj_a_dir.mkdir(parents=True)
    (traj_a_dir / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "step_id": 1,
                        "timestamp": "2026-10-01T01:00:10+00:00",
                        "source": "user",
                    },
                    {
                        "step_id": 2,
                        "timestamp": "2026-10-01T01:00:20+00:00",
                        "source": "agent",
                        "metrics": {"prompt_tokens": 100, "completion_tokens": 20},
                    },
                    {
                        "step_id": 3,
                        "timestamp": "2026-10-01T01:00:30+00:00",
                        "source": "agent",
                        "metrics": {"prompt_tokens": 150, "completion_tokens": 30},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    job_b = runs_dir / "job-b"
    job_b.mkdir()
    (job_b / "lab-metadata.json").write_text(
        json.dumps(
            {
                "started_at": "2026-10-01T01:00:05+00:00",
                "finished_at": "2026-10-01T01:01:30+00:00",
                "provider_usage": {
                    "calls": [
                        {"call_id": 1, "status": 200},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    traj_b_dir = job_b / "trial_hash" / "agent"
    traj_b_dir.mkdir(parents=True)
    (traj_b_dir / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "step_id": 1,
                        "timestamp": "2026-10-01T01:00:15+00:00",
                        "source": "user",
                    },
                    {
                        "step_id": 2,
                        "timestamp": "2026-10-01T01:00:25+00:00",
                        "source": "agent",
                        "metrics": {"prompt_tokens": 200, "completion_tokens": 40},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    trials, report = extract_round([job_a, job_b])
    assert len(trials) == 2
    assert report["n_trials"] == 2
    assert report["n_trials_with_trajectory"] == 2
    assert report["n_calls"] == 3

    # Check latencies: all 3 calls had exactly 10s latency
    assert report["latency_s"]["n"] == 3
    assert report["latency_s"]["mean"] == 10.0
    assert report["latency_s"]["p50"] == 10.0
    assert report["latency_s"]["p90"] == 10.0

    # Overlaps:
    # Call A1: 01:00:10 -> 01:00:20 (midpoint 01:00:15)
    # Call B1: 01:00:15 -> 01:00:25 (midpoint 01:00:20)
    # Call A2: 01:00:20 -> 01:00:30 (midpoint 01:00:25)
    # At Call A1 midpoint 01:00:15: Call B1 starts at 01:00:15, so concurrency is 2
    # Peak call concurrency is 2
    assert report["peak_call_concurrency"] == 2
    assert report["peak_trial_concurrency"] == 2

    # Startup times:
    # Job A first call ended at 01:00:20, started at 01:00:00 -> 20s
    # Job B first call ended at 01:00:25, started at 01:00:05 -> 20s
    assert report["trial_startup_s"]["p50"] == 20.0

    # Reconciliation
    assert report["trajectory_vs_ledger_matched"] == 2
    assert report["trajectory_vs_ledger_mismatched"] == []

    # Queue wait
    assert report["queue_wait_s"] is None
    assert "per-call server queue wait" in report["queue_wait_note"]


def test_extractor_handles_missing_or_corrupt_files(tmp_path: Path) -> None:
    job = tmp_path / "broken-job"
    job.mkdir()
    # Missing lab-metadata.json
    trial = extract_trial(job)
    assert trial.job == "broken-job"
    assert trial.started_at is None
    assert trial.calls == []
    assert trial.trajectory_found is False

    # Corrupt trajectory.json
    agent_dir = job / "sub" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "trajectory.json").write_text("{invalid json", encoding="utf-8")
    trial_corrupt = extract_trial(job)
    assert trial_corrupt.trajectory_found is False
    assert trial_corrupt.calls == []

    summary = summarize([trial, trial_corrupt])
    assert summary["n_trials"] == 2
    assert summary["n_calls"] == 0
    assert summary["latency_s"]["mean"] is None


# ---------------------------------------------------------------------------
# 5. CLI invocation tests
# ---------------------------------------------------------------------------


def test_cli_telemetry_extract(tmp_path: Path, capsys) -> None:
    job = tmp_path / "cli-job"
    job.mkdir()
    (job / "lab-metadata.json").write_text(
        json.dumps(
            {
                "started_at": "2026-10-01T01:00:00+00:00",
                "finished_at": "2026-10-01T01:01:00+00:00",
                "provider_usage": {"calls": [{"call_id": 1}]},
            }
        ),
        encoding="utf-8",
    )
    agent_dir = job / "run1" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {"step_id": 1, "timestamp": "2026-10-01T01:00:10+00:00", "source": "user"},
                    {
                        "step_id": 2,
                        "timestamp": "2026-10-01T01:00:15+00:00",
                        "source": "agent",
                        "metrics": {"prompt_tokens": 10},
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    out_json = tmp_path / "summary.json"
    rc = run_cli(
        ["telemetry", "extract", str(job), "--out", str(out_json)],
        workspace=tmp_path,
    )
    assert rc == 0
    assert out_json.is_file()
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["n_calls"] == 1
    assert data["latency_s"]["mean"] == 5.0

    captured = capsys.readouterr()
    assert "calls=1" in captured.out
