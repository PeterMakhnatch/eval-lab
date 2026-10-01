"""Daytona capacity evidence on the run-report and trial-page surfaces.

Behavioral coverage only: the monitor record must reach the structured
run-report JSON, the CLI markdown, and the published trial page with
under-limit / missing / pressure / unavailable kept distinct. A
near-limit disappearance reads as capacity correlation, never as a
confirmed provider cause; absent evidence reads as unavailable, never zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evallab.interpretation.run_report import build_run_report, render_run_report_markdown
from evallab.process_job import process_job

MESSAGE = json.dumps({"analysis": "", "plan": "", "commands": [{"keystrokes": "echo hi"}]})


def _snapshot(**overrides: Any) -> dict[str, Any]:
    snap: dict[str, Any] = {
        "schema_version": 1,
        "observed_at": "2026-10-01T20:45:55Z",
        "organization_id": "75ae5c7a-cc37-40e9-8f96-9bd4e8798e7b",
        "api_url": "https://app.daytona.io/api",
        "region": "us",
        "sandbox_class": "container",
        "source": "GET /organizations/{id}/usage",
        "limits": {
            "cpu": 100,
            "memory_gib": 200,
            "disk_gib": 300,
            "gpu": 0,
            "concurrent_sandboxes": None,
        },
        "per_sandbox_limits": {"cpu": 4, "memory_gib": 8, "disk_gib": 10},
        "safety_fraction": 0.8,
        "used": {"cpu": 10, "memory_gib": 20, "disk_gib": 30, "gpu": 0, "concurrent_sandboxes": 2},
        "pending": {"cpu": 0, "memory_gib": 0, "disk_gib": 0, "gpu": 0, "concurrent_sandboxes": 0},
        "requested": {"cpu": 4, "memory_gib": 8, "disk_gib": 10, "gpu": 0, "concurrent_sandboxes": 1},
        "projected": {"cpu": 14, "memory_gib": 28, "disk_gib": 40, "gpu": 0, "concurrent_sandboxes": 3},
        "inventory": [
            {
                "id": "sbx-1",
                "name": "evallab-aaa",
                "region": "us",
                "sandbox_class": "container",
                "state": "started",
                "cpu": 4,
                "memory_gib": 8,
                "disk_gib": 10,
                "gpu": 0,
            },
            {
                "id": "sbx-2",
                "name": "evallab-bbb",
                "region": "us",
                "sandbox_class": "container",
                "state": "started",
                "cpu": 4,
                "memory_gib": 8,
                "disk_gib": 10,
                "gpu": 0,
            },
        ],
        "at_or_near_limit": [],
        "admitted": True,
        "reasons": [],
    }
    snap.update(overrides)
    return snap


def _pressure_usage() -> dict[str, Any]:
    near = _snapshot(
        observed_at="2026-10-01T20:50:10Z",
        used={"cpu": 85, "memory_gib": 40, "disk_gib": 30, "gpu": 0, "concurrent_sandboxes": 3},
        at_or_near_limit=["cpu"],
        admitted=False,
        reasons=["cpu at 85% of raw quota (safety cap 80%)"],
    )
    return {
        "schema_version": 1,
        "admission": _snapshot(),
        "last_observation": near,
        "disappearance": {
            "observed_at": "2026-10-01T20:50:25Z",
            "sandbox_id": "sbx-9",
            "sandbox_name": "evallab-abc123",
            "cause": "usage_limit_pressure",
            "evidence_strength": "observed_capacity_pressure_not_provider_event",
            "pressure_source": "last_observation",
            "current_usage": None,
            "last_observation": near,
            "error_type": "SandboxNotFound",
            "read_error": None,
        },
    }


def _unknown_usage() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "admission": _snapshot(),
        "last_observation": _snapshot(),
        "disappearance": {
            "observed_at": "2026-10-01T20:51:25Z",
            "sandbox_id": "sbx-9",
            "sandbox_name": "evallab-abc123",
            "cause": "unknown",
            "evidence_strength": "no_capacity_evidence",
            "pressure_source": None,
            "current_usage": None,
            "last_observation": None,
            "error_type": "SandboxNotFound",
            "read_error": "usage telemetry unavailable: quota endpoint refused",
        },
    }


def _run_trial(root: Path, name: str, usage: dict[str, Any] | None = None) -> Path:
    trial = root / name
    agent = trial / "agent"
    agent.mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "trial_name": name,
                "task_name": "lab/task",
                "config": {"agent": {"name": "mini-swe-agent", "model_name": "zai/glm"}},
                "agent_info": {"name": "mini-swe-agent", "version": "2.4.6"},
                "verifier_result": {"rewards": {"reward": 0.0}},
                "started_at": "2026-10-01T00:00:00Z",
                "finished_at": "2026-10-01T00:10:00Z",
            }
        ),
        encoding="utf-8",
    )
    call_id = "call-1"
    (agent / "trajectory.json").write_text(
        json.dumps(
            {
                "schema_version": "ATIF-v1.7",
                "session_id": "s",
                "agent": {"name": "mini-swe-agent", "version": "2.4.6"},
                "steps": [
                    {"step_id": 0, "source": "user", "message": "Fix it"},
                    {
                        "step_id": 1,
                        "timestamp": "2026-10-01T00:02:00Z",
                        "source": "agent",
                        "message": "",
                        "tool_calls": [
                            {
                                "tool_call_id": call_id,
                                "function_name": "bash",
                                "arguments": {"command": "echo hi"},
                            }
                        ],
                        "observation": {
                            "results": [
                                {
                                    "source_call_id": call_id,
                                    "content": json.dumps({"returncode": 0, "output": "hi"}),
                                }
                            ]
                        },
                        "metrics": {"prompt_tokens": 100, "completion_tokens": 10},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    if usage is not None:
        (trial / "daytona-usage.json").write_text(json.dumps(usage), encoding="utf-8")
    return trial


def _job_trial(job: Path, name: str, usage: dict[str, Any] | None = None) -> Path:
    trial = job / name
    agent = trial / "agent"
    agent.mkdir(parents=True)
    (agent / "trajectory.json").write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "step_id": 1,
                        "source": "agent",
                        "message": MESSAGE,
                        "observation": "output\n",
                        "metrics": {"prompt_tokens": 1000, "completion_tokens": 10},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (trial / "result.json").write_text(
        json.dumps(
            {
                "task_name": "task",
                "trial_name": name,
                "verifier_result": {"rewards": {"reward": 0.0}},
            }
        ),
        encoding="utf-8",
    )
    if usage is not None:
        (trial / "daytona-usage.json").write_text(json.dumps(usage), encoding="utf-8")
    return trial


def test_run_report_json_exposes_full_daytona_usage_record(tmp_path: Path) -> None:
    usage = _pressure_usage()
    report = build_run_report(_run_trial(tmp_path, "trial", usage))
    assert report["daytona_usage"] == usage
    assert report["daytona_usage"]["disappearance"]["cause"] == "usage_limit_pressure"
    assert (
        report["daytona_usage"]["disappearance"]["evidence_strength"]
        == "observed_capacity_pressure_not_provider_event"
    )


def test_missing_evidence_is_unavailable_never_zero(tmp_path: Path) -> None:
    report = build_run_report(_run_trial(tmp_path, "trial"))
    assert report["daytona_usage"] is None
    assert "Daytona" not in render_run_report_markdown(report)
    assert all("daytona-usage.json" not in source["path"] for source in report["sources"])


def test_markdown_renders_quota_safety_used_pending_requested_live(tmp_path: Path) -> None:
    markdown = render_run_report_markdown(build_run_report(_run_trial(tmp_path, "trial", _pressure_usage())))
    # Safety cap is 80% of the raw quota (100 -> 80), distinct from quota/used.
    assert "| cpu | 100 | 80 | 85 | 0 | 4 |" in markdown
    # A null concurrent-sandbox quota reads as unavailable, never 0.
    assert "| concurrent_sandboxes | unavailable | unavailable |" in markdown
    assert "GET /organizations/{id}/usage" in markdown
    assert "2026-10-01T20:50:10Z" in markdown
    assert "Live sandboxes in provider inventory: 2." in markdown


def test_pressure_vs_unknown_disappearance_stays_separable(tmp_path: Path) -> None:
    pressure = render_run_report_markdown(
        build_run_report(_run_trial(tmp_path, "pressure", _pressure_usage()))
    )
    assert "usage_limit_pressure" in pressure
    assert "observed_capacity_pressure_not_provider_event" in pressure
    assert "at/near limit: cpu" in pressure
    assert "pressure basis: last_observation" in pressure
    assert "not a confirmed provider deletion cause" in pressure

    unknown = render_run_report_markdown(
        build_run_report(_run_trial(tmp_path, "unknown", _unknown_usage()))
    )
    assert "usage_limit_pressure" not in unknown
    assert "cause unknown" in unknown
    assert "no_capacity_evidence" in unknown
    assert "No capacity evidence either way" in unknown
    assert "pressure unknown (no snapshot recorded)" in unknown
    assert "usage telemetry unavailable: quota endpoint refused" in unknown


def test_monitor_gap_renders_unavailable_not_zero(tmp_path: Path) -> None:
    usage = {
        "schema_version": 1,
        "admission": _snapshot(),
        "monitor_error": "quota endpoint refused: HTTPS 429",
    }
    markdown = render_run_report_markdown(build_run_report(_run_trial(tmp_path, "trial", usage)))
    assert "Monitor gap: quota endpoint refused: HTTPS 429" in markdown
    assert "unavailable, never zero" in markdown
    # With no later sample the admission snapshot names the provenance.
    assert "snapshot: admission snapshot" in markdown


def test_process_job_trial_page_shows_daytona_pressure(tmp_path: Path) -> None:
    job = tmp_path / "job"
    job.mkdir()
    (job / "lab-metadata.json").write_text(json.dumps({}), encoding="utf-8")
    _job_trial(job, "trial-pressure", _pressure_usage())
    _job_trial(job, "trial-plain")
    out = tmp_path / "out"
    process_job(job, output_dir=out, ingest=False, publish=False)

    pressured = (out / "trial-trial-pressure.md").read_text(encoding="utf-8")
    assert "Environment capacity (Daytona)" in pressured
    assert "usage_limit_pressure" in pressured
    assert "at/near limit: cpu" in pressured
    saved = json.loads((out / "trial-trial-pressure.json").read_text(encoding="utf-8"))
    assert saved["daytona_usage"]["disappearance"]["cause"] == "usage_limit_pressure"

    plain = (out / "trial-trial-plain.md").read_text(encoding="utf-8")
    assert "Daytona" not in plain
    saved_plain = json.loads((out / "trial-trial-plain.json").read_text(encoding="utf-8"))
    assert saved_plain["daytona_usage"] is None
