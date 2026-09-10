from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from evallab.dsh import (
    DEFAULT_SCHEMA_VERSION,
    DSH_AGENT_NAME,
    SESSION_GLOB,
    SessionUnreadable,
    create_fallback_atif_from_final_message,
    decompress_session,
    looks_compressed,
    newest_session_file,
    parse_session_to_atif,
    read_session_file,
    sanitize_session,
)
from evallab.evidence.atif import SUPPORTED_SCHEMA_VERSIONS
from evallab.interpretation.trajectory_quality import QualityStatus, evaluate_trial_quality

FIXTURE = Path(__file__).resolve().parent / "fixtures/dsh/session.v3.jsonl"


def fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def converted() -> dict:
    payload = parse_session_to_atif(
        fixture_text(),
        agent_version="0.1.5-rc.1",
        job_id="job-1",
        trial_id="trial-1",
    )
    assert payload is not None
    return payload


def test_session_converts_to_atif_with_calls_paired_to_observations() -> None:
    payload = converted()

    assert payload["schema_version"] == DEFAULT_SCHEMA_VERSION
    assert payload["session_id"] == "session-0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
    assert payload["agent"]["name"] == DSH_AGENT_NAME
    assert payload["agent"]["model_name"] == "deepseek-v4-flash"

    steps = payload["steps"]
    assert [step["step_id"] for step in steps] == list(range(1, len(steps) + 1))
    assert [step["source"] for step in steps] == ["user", "agent", "agent", "agent"]

    # The turn-1 assistant step carries its call and that call's observation.
    tool_step = next(step for step in steps if step.get("tool_calls"))
    assert tool_step["tool_calls"] == [
        {
            "tool_call_id": "call-0001",
            "function_name": "bash",
            "arguments": {"command": "wc -l data/events.jsonl"},
        }
    ]
    assert tool_step["observation"]["results"][0]["source_call_id"] == "call-0001"
    assert tool_step["observation"]["results"][0]["content"] == "42 data/events.jsonl"


def test_tool_error_becomes_an_observation_not_a_dropped_step() -> None:
    payload = converted()

    failed = next(
        result
        for step in payload["steps"]
        for result in step.get("observation", {}).get("results", [])
        if result.get("extra", {}).get("error")
    )
    assert failed["source_call_id"] == "call-0002"
    assert failed["extra"]["error_type"] == "NonZeroExit"
    assert "No such file or directory" in failed["content"]


def test_usage_is_summed_across_turns() -> None:
    payload = converted()

    metrics = payload["final_metrics"]
    assert metrics["total_prompt_tokens"] == 1200 + 1350 + 1500
    assert metrics["total_completion_tokens"] == 80 + 40 + 60
    assert metrics["total_cached_tokens"] == 0 + 1100 + 1300
    assert metrics["total_steps"] == len(payload["steps"])


def test_unknown_event_types_are_ignored_rather_than_fatal() -> None:
    """A newer DSH that adds events must not make an old reader blind."""
    payload = converted()

    assert payload["steps"], "unknown events must not empty the trajectory"
    assert payload["extra"]["turn_end_reason"] == {
        "1": {"kind": "completed"},
        "2": {"kind": "completed"},
    }


def test_schema_version_is_one_the_evidence_layer_accepts() -> None:
    assert DEFAULT_SCHEMA_VERSION in SUPPORTED_SCHEMA_VERSIONS


def test_sanitize_redacts_key_shaped_fields_and_bearer_tokens() -> None:
    raw = json.dumps(
        {
            "type": "tool/call",
            "seq": 1,
            "data": {
                "name": "bash",
                "arguments": json.dumps(
                    {
                        "command": "curl -H 'Authorization: Bearer sk-live-abcdef123456' url",
                        "apiKey": "sk-should-not-survive",
                    }
                ),
            },
        }
    )
    sanitized = sanitize_session(raw + "\n")

    assert "sk-should-not-survive" not in sanitized
    assert "sk-live-abcdef123456" not in sanitized
    assert "<redacted>" in sanitized
    assert "Bearer <redacted>" in sanitized
    # Still parseable: redaction must not cost the event.
    assert json.loads(sanitized.splitlines()[0])["data"]["name"] == "bash"


def test_sanitize_keeps_token_counts_while_dropping_credentials() -> None:
    """Redaction must not eat the metrics it is stored next to.

    ``inputTokens``/``outputTokens``/``cacheReadTokens`` all contain the
    substring "token". A name-only rule redacts them, and every DSH trial then
    reports zero tokens — a silent corruption, because the trajectory still
    parses and still validates.
    """
    raw = json.dumps(
        {
            "type": "assistant/message",
            "seq": 1,
            "data": {
                "turn": 1,
                "step": 1,
                "usage": {
                    "inputTokens": 1200,
                    "outputTokens": 80,
                    "cacheReadTokens": 1100,
                    "reasoningTokens": 12,
                },
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ok"}],
                    "source": {"kind": "model", "model": "deepseek-v4-flash"},
                },
            },
        }
    )

    sanitized = sanitize_session(raw + "\n")
    parsed = json.loads(sanitized.splitlines()[0])

    assert parsed["data"]["usage"] == {
        "inputTokens": 1200,
        "outputTokens": 80,
        "cacheReadTokens": 1100,
        "reasoningTokens": 12,
    }
    # ...and the counts still reach final_metrics through the converter.
    payload = parse_session_to_atif(sanitized)
    assert payload is not None
    assert payload["final_metrics"]["total_prompt_tokens"] == 1200
    assert payload["final_metrics"]["total_cached_tokens"] == 1100


def test_plain_jsonl_is_read_without_a_decoder() -> None:
    text = fixture_text()

    assert not looks_compressed(text.encode("utf-8"))
    assert decompress_session(text.encode("utf-8")) == text
    assert read_session_file(FIXTURE) == text


def test_zstd_sessions_are_decompressed_when_a_decoder_exists(tmp_path: Path) -> None:
    executable = shutil.which("zstd")
    if executable is None and _no_python_zstd():
        pytest.skip("no Zstandard decoder available on this host")

    raw = fixture_text().encode("utf-8")
    compressed = subprocess.run(
        [executable, "-c", "-q"],
        input=raw,
        capture_output=True,
        check=True,
    ).stdout
    assert looks_compressed(compressed)

    path = tmp_path / "session.v3.jsonl.zstd"
    path.write_bytes(compressed)
    assert read_session_file(path) == fixture_text()


def _no_python_zstd() -> bool:
    try:
        import zstandard  # noqa: F401

        return False
    except Exception:  # noqa: BLE001
        pass
    try:
        from compression import zstd  # noqa: F401

        return False
    except Exception:  # noqa: BLE001
        return True


def test_missing_decoder_names_every_way_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """The failure has to tell an operator how to fix it, not just that it failed."""
    import evallab.dsh as dsh

    monkeypatch.setattr(dsh.shutil, "which", lambda _name: None)
    monkeypatch.setitem(__import__("sys").modules, "zstandard", None)
    monkeypatch.setitem(__import__("sys").modules, "compression.zstd", None)

    with pytest.raises(SessionUnreadable) as excinfo:
        decompress_session(b"\x28\xb5\x2f\xfd" + b"\x00" * 16)

    message = str(excinfo.value)
    assert "compression: none" in message
    assert "zstandard" in message


def test_empty_or_unparseable_input_yields_no_trajectory() -> None:
    assert parse_session_to_atif("") is None
    assert parse_session_to_atif("not json\nstill not json\n") is None


def test_newest_session_file_picks_the_latest_write(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions/--workspace--/session-aaa"
    sessions.mkdir(parents=True)
    older = sessions / "session.v3.jsonl"
    older.write_text("{}\n")
    newer_dir = tmp_path / "sessions/--workspace--/session-bbb"
    newer_dir.mkdir(parents=True)
    newer = newer_dir / "session.v3.jsonl.zstd"
    newer.write_text("{}\n")

    import os

    os.utime(older, (1_000_000, 1_000_000))
    os.utime(newer, (2_000_000, 2_000_000))

    assert newest_session_file(tmp_path / "sessions") == newer
    assert list(tmp_path.joinpath("sessions").rglob(SESSION_GLOB))
    assert newest_session_file(tmp_path / "absent") is None


def test_fallback_marks_itself_degraded() -> None:
    payload = create_fallback_atif_from_final_message(
        "The file has 42 events.",
        model_name="deepseek-v4-flash",
        trial_id="trial-1",
    )

    assert payload is not None
    assert payload["extra"]["degraded"] is True
    assert payload["extra"]["transport"] == "final-message"
    assert len(payload["steps"]) == 1
    assert payload["steps"][0]["message"] == "The file has 42 events."
    assert create_fallback_atif_from_final_message("   \n ") is None


def test_converted_trajectory_survives_the_quality_screen(tmp_path: Path) -> None:
    """The point of the conversion: a DSH trial must be counted, not quarantined.

    ``trajectory_quality`` quarantines a billable trial that has no
    ``agent/trajectory.json``. This asserts the ATIF this module writes is the
    thing that gate is looking for.
    """
    trial = tmp_path / "job/trial"
    (trial / "agent").mkdir(parents=True)
    (trial / "result.json").write_text(
        json.dumps(
            {
                "id": "trial-1",
                "agent_info": {"name": DSH_AGENT_NAME},
                "agent_result": {"cost_usd": 0.01},
            }
        )
    )
    (trial / "agent/trajectory.json").write_text(json.dumps(converted(), indent=2) + "\n")

    report, _findings = evaluate_trial_quality(trial)

    assert report.quarantine_reason != "missing_trajectory_file"
    assert report.status != QualityStatus.QUARANTINE
    assert report.is_analysis_ready is True
