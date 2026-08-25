from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from evallab.profiles import ProbeResult

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT_PATH = ROOT / "scripts/tau_knowledge/preflight.py"
RUN_CONTROLS_PATH = ROOT / "scripts/tau_knowledge/run_controls.py"
RUN_YAML_PATH = ROOT / "library/benchmarks/tau-knowledge/config/run.yaml"
TAU2_BENCH_ROOT = Path("/tmp/tau2-bench-v101")


def _load_preflight() -> Any:
    spec = importlib.util.spec_from_file_location("tau_knowledge_preflight", PREFLIGHT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_run_controls() -> Any:
    spec = importlib.util.spec_from_file_location("tau_knowledge_run_controls", RUN_CONTROLS_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_temp_config(tmp_path: Path) -> tuple[dict[str, Any], Path]:
    """Copy run.yaml with absolute asset paths and a scratch output root."""
    config = yaml.safe_load(RUN_YAML_PATH.read_text(encoding="utf-8"))
    config["cohort_manifest"] = str(ROOT / "library/benchmarks/tau-knowledge/cohort.manifest.json")
    config["generated_tasks"] = str(ROOT / "library/benchmarks/tau-knowledge/generated")
    config["outputs"]["root"] = str(tmp_path / "evidence")
    config_path = tmp_path / "run.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return config, config_path


def _fake_auth_json(home: Path) -> None:
    auth_dir = home / ".codex"
    auth_dir.mkdir(parents=True, exist_ok=True)
    (auth_dir / "auth.json").write_text("{}", encoding="utf-8")


def test_oracle_proceeds_with_empty_env(tmp_path: Path) -> None:
    p = _load_preflight()
    decision = p.preflight_tau_phase("oracle", env={}, home=tmp_path)
    assert decision.proceed is True
    assert decision.reason_code is None
    assert "reward" not in decision.to_dict()


def test_reference_without_openai_is_blocked(tmp_path: Path) -> None:
    p = _load_preflight()
    decision = p.preflight_tau_phase("reference", env={}, home=tmp_path)
    assert decision.proceed is False
    assert decision.reason_code == "blocked:missing_openai_api_key_for_simulated_user"
    assert "harness" in decision.detail.lower()
    assert "not a model" in decision.detail.lower()
    assert "reward" not in decision.to_dict()
    assert decision.to_dict()["status"] == "blocked"
    assert any(consumer.get("name") == "simulated_user" for consumer in decision.consumers)


def test_luna_without_openai_is_blocked_even_with_auth_json(tmp_path: Path) -> None:
    p = _load_preflight()
    _fake_auth_json(tmp_path)
    decision = p.preflight_tau_phase(
        "luna",
        env={},
        home=tmp_path,
        agent=p.DEFAULT_LUNA_AGENT,
    )
    assert decision.proceed is False
    assert decision.reason_code == "blocked:missing_openai_api_key_for_simulated_user"
    assert "cannot be converted" in decision.detail


def test_luna_with_openai_but_missing_auth_json_is_blocked(tmp_path: Path) -> None:
    p = _load_preflight()
    env = {"OPENAI_API_KEY": "sk-test-not-a-real-key"}
    decision = p.preflight_tau_phase(
        "luna",
        env=env,
        home=tmp_path,
        agent=p.DEFAULT_LUNA_AGENT,
    )
    assert decision.proceed is False
    assert decision.reason_code == "blocked:missing_codex_auth_json"
    # Decision must never carry the secret value.
    assert "sk-test-not-a-real-key" not in json.dumps(decision.to_dict())


def test_luna_with_both_proceeds_and_builds_child_env(tmp_path: Path) -> None:
    p = _load_preflight()
    _fake_auth_json(tmp_path)
    env = {"OPENAI_API_KEY": " sk-test-not-a-real-key "}  # spaces test non-empty, stripped check
    decision = p.preflight_tau_phase(
        "luna",
        env=env,
        home=tmp_path,
        agent=p.DEFAULT_LUNA_AGENT,
    )
    assert decision.proceed is True

    child = p.build_child_env(
        "luna",
        env=env,
        home=tmp_path,
        repo_root=tmp_path,
        adapter_pythonpath="/fake/adapter",
        luna_agent=p.DEFAULT_LUNA_AGENT,
    )
    assert child["OPENAI_API_KEY"] == " sk-test-not-a-real-key "
    assert child["CODEX_FORCE_AUTH_JSON"] == "1"
    assert child["LUNA_AGENT"] == p.DEFAULT_LUNA_AGENT
    pythonpath = child["PYTHONPATH"].split(os.pathsep)
    assert str(tmp_path / "src") == pythonpath[0]
    assert "/fake/adapter" in pythonpath
    assert "sk-test-not-a-real-key" not in json.dumps(decision.to_dict())


def test_child_env_forces_codex_routing_only_for_luna(tmp_path: Path) -> None:
    p = _load_preflight()
    env = {"OPENAI_API_KEY": "sk-test-not-a-real-key"}
    child = p.build_child_env(
        "reference",
        env=env,
        home=tmp_path,
        repo_root=tmp_path,
        adapter_pythonpath="/fake/adapter",
    )
    assert child["OPENAI_API_KEY"] == env["OPENAI_API_KEY"]
    assert "CODEX_FORCE_AUTH_JSON" not in child


def test_reference_execute_without_key_never_runs_harbor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper must stop before Harbor when no simulated-user key is present."""
    run_controls = _load_run_controls()
    _, config_path = _make_temp_config(tmp_path)

    calls: list[dict[str, Any]] = []

    def recording_run(
        command: list[str],
        *,
        task_path: Path,
        timeout: int,
        env: dict[str, str],
    ) -> None:
        calls.append({"command": command, "task_path": str(task_path), "env": env})
        raise AssertionError("Harbor should not be invoked")

    run_controls._run = recording_run  # type: ignore[attr-defined]

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("TAU2_BENCH_ROOT", str(TAU2_BENCH_ROOT))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(RUN_CONTROLS_PATH),
            "--config",
            str(config_path),
            "--phase",
            "reference",
            "--execute",
        ],
    )

    with pytest.raises(RuntimeError, match="blocked:missing_openai_api_key_for_simulated_user"):
        run_controls.main()

    assert not calls
    preflight_path = tmp_path / "evidence" / "credential-preflight.json"
    assert preflight_path.is_file()
    payload = json.loads(preflight_path.read_text(encoding="utf-8"))
    assert payload["status"] == "blocked"
    assert payload["reason_code"] == "blocked:missing_openai_api_key_for_simulated_user"
    assert payload["created_trial"] is False
    assert "sk-test" not in json.dumps(payload)


def test_luna_child_env_passed_to_run_when_credentials_ok(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Luna proceeds and passes a child env with both routing flags set."""
    run_controls = _load_run_controls()
    _, config_path = _make_temp_config(tmp_path)

    # Pre-seed a passing control status so the Luna gate opens.
    status = {
        row["task_id"]: {
            "reference": "passed",
            "oracle": "passed",
            "clean_reset_repetition": "passed",
        }
        for row in json.loads(
            (ROOT / "library/benchmarks/tau-knowledge/cohort.manifest.json").read_text()
        )["tasks"]
    }
    status_path = tmp_path / "evidence" / "controls-status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(json.dumps(status, indent=2, sort_keys=True), encoding="utf-8")

    calls: list[dict[str, Any]] = []

    def recording_run(
        command: list[str],
        *,
        task_path: Path,
        timeout: int,
        env: dict[str, str],
    ) -> None:
        calls.append({"command": command, "task_path": str(task_path), "env": env})

    run_controls._run = recording_run  # type: ignore[attr-defined]
    run_controls._validate_pin = lambda *a, **k: Path(TAU2_BENCH_ROOT)  # type: ignore[attr-defined]
    # Inject a fake key and bypass the real auth.json probe.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("TAU2_BENCH_ROOT", str(TAU2_BENCH_ROOT))
    monkeypatch.setattr(
        run_controls.preflight,
        "probe_codex_auth_result",
        lambda home: ProbeResult(ok=True),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(RUN_CONTROLS_PATH),
            "--config",
            str(config_path),
            "--phase",
            "luna",
            "--execute",
        ],
    )

    assert run_controls.main() == 0

    assert len(calls) == 8
    for call in calls:
        assert call["env"]["CODEX_FORCE_AUTH_JSON"] == "1"
        assert call["env"]["OPENAI_API_KEY"] == "sk-test-not-a-real-key"
        assert call["env"]["LUNA_AGENT"] == run_controls.preflight.DEFAULT_LUNA_AGENT
        assert str(ROOT / "src") in call["env"].get("PYTHONPATH", "")

    # The fake key must not leak into the preflight status artifact.
    preflight_path = tmp_path / "evidence" / "credential-preflight.json"
    if preflight_path.exists():
        assert "sk-test" not in preflight_path.read_text(encoding="utf-8")
