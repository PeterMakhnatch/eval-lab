from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from harbor_lab.tracing import (
    TraceError,
    convert_atif,
    convert_source,
    instrument_openinference,
    is_control_trial,
    load_trajectory,
    summarize_otel,
    trace_path,
    validate_atif,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "research/explorations/harbor-021/fixtures/trajectory.json"
)


def test_fixture_exists() -> None:
    assert FIXTURE.is_file(), "RECON atif2otel fixture is required"


def test_convert_fixture_has_root_agent_span() -> None:
    trajectory = json.loads(FIXTURE.read_text())
    _resource_spans, payload = convert_atif(trajectory)
    summary = summarize_otel(payload)
    assert validate_atif(trajectory) == []
    assert summary.n_spans > 0
    assert summary.has_root_agent
    assert "AGENT" in summary.span_kinds
    assert summary.n_root_spans == 1
    assert "codex" in summary.root_names


def test_cli_dry_run_on_fixture_does_not_need_phoenix() -> None:
    batch = trace_path(FIXTURE, dry_run=True, include_controls=True)
    assert batch.failed == 0
    assert batch.shipped == 1
    assert batch.results[0].summary is not None
    assert batch.results[0].summary.has_root_agent
    assert batch.results[0].message == "dry-run"


def test_missing_trajectory_is_clear_error(tmp_path: Path) -> None:
    trial = tmp_path / "empty-trial"
    trial.mkdir()
    (trial / "trial.log").write_text("started\n")
    with pytest.raises(TraceError, match="no ATIF trajectory") as exc:
        load_trajectory(trial / "agent" / "trajectory.json")
    assert "Traceback" not in str(exc.value)
    batch = trace_path(trial, dry_run=True, include_controls=True)
    assert batch.shipped == 0
    assert batch.skipped == 1
    assert "no ATIF trajectory" in batch.results[0].message


def test_invalid_trajectory_is_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "trajectory.json"
    path.write_text("{}\n")
    with pytest.raises(TraceError, match="invalid ATIF trajectory") as exc:
        convert_source(path)
    message = str(exc.value)
    assert "schema_version" in message or "missing" in message
    assert "Traceback" not in message


def test_malformed_json_is_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "trajectory.json"
    path.write_text("{not-json")
    with pytest.raises(TraceError, match="not valid JSON"):
        load_trajectory(path)


def test_oracle_control_is_skipped_unless_flagged(tmp_path: Path) -> None:
    trial = tmp_path / "event-summary__abc"
    agent = trial / "agent"
    agent.mkdir(parents=True)
    (trial / "trial.log").write_text("ok\n")
    (trial / "result.json").write_text(
        json.dumps({"agent_info": {"name": "oracle"}, "id": "1", "trial_name": "t"})
    )
    (agent / "trajectory.json").write_text(FIXTURE.read_text())
    assert is_control_trial(trial)
    skipped = trace_path(trial, dry_run=True, include_controls=False)
    assert skipped.skipped == 1
    assert skipped.shipped == 0
    included = trace_path(trial, dry_run=True, include_controls=True)
    assert included.shipped == 1


def test_instrument_openinference_with_stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class LiteLLMInstrumentor:
        def instrument(self) -> None:
            calls.append("litellm")

    class DSPyInstrumentor:
        def instrument(self) -> None:
            calls.append("dspy")

    import sys

    for name in list(sys.modules):
        if name == "openinference" or name.startswith("openinference."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(
        sys.modules,
        "openinference.instrumentation.litellm",
        SimpleNamespace(LiteLLMInstrumentor=LiteLLMInstrumentor),
    )
    monkeypatch.setitem(
        sys.modules,
        "openinference.instrumentation.dspy",
        SimpleNamespace(DSPyInstrumentor=DSPyInstrumentor),
    )
    monkeypatch.setitem(sys.modules, "openinference.instrumentation", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "openinference", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "dspy", SimpleNamespace())

    wired = instrument_openinference()
    assert wired == {"litellm": True, "dspy": True}
    assert calls == ["litellm", "dspy"]
    assert instrument_openinference(enabled=False) == {"litellm": False, "dspy": False}
