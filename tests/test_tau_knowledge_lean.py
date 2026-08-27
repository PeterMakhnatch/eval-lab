from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "library/benchmarks/tau-knowledge/cohort.manifest.json"
PREFLIGHT = ROOT / "scripts/tau_knowledge/preflight.py"
MATERIALIZER = ROOT / "scripts/tau_knowledge/materialize.py"
CONTROLS = ROOT / "scripts/tau_knowledge/controls.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_cohort_preserves_immutable_pins_and_selected_order() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert manifest["immutable"] is True
    assert manifest["selection"]["task_ids"] == [row["task_id"] for row in manifest["tasks"]]
    assert manifest["required_upstream"]["commit"] == "fc0055dc4e0a316c3f83133267fbd6faaa770992"
    assert manifest["required_upstream"]["license"] == "MIT"
    assert manifest["adapter_evidence"]["commit"] == "636a2d0295d3ee233666bcd7d77fa81f7f090a19"
    # Ensure TASTE tau-c is excluded
    assert manifest["benchmark_family"] == "tau3-bench"
    assert "tau-c" not in json.dumps(manifest).lower()
    simulator = manifest["credentials"]["simulated_user"]
    assert simulator["provider"] == "openai"
    assert simulator["model"] == "gpt-4o-mini-2024-07-18"
    assert simulator["required_env"] == ["OPENAI_API_KEY"]
    assert simulator["required_phases"] == ["reference", "evaluation"]
    assert set(manifest["credentials"]) == {"simulated_user"}


def test_source_digest_is_stable_and_bounded() -> None:
    materializer = _load(MATERIALIZER, "tau_knowledge_materialize")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    digest = materializer.source_digest(manifest)
    assert digest == "2519b16fa4ffc1b755a7b0ae63d0fa2b363450ccdff2fd284e1e5c60f1a4864c"
    assert len(digest) == 64


def test_missing_source_and_credentials_fail_closed_without_trial() -> None:
    preflight = _load(PREFLIGHT, "tau_knowledge_preflight")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    source = preflight.preflight_tau_phase(
        "oracle", env={}, source_root=Path("/tmp/missing"), manifest=manifest
    )
    assert source.proceed is False
    assert source.reason_code == "blocked:missing_source_checkout"
    credential = preflight.credential_preflight("reference", env={})
    assert credential.reason_code == "blocked:missing_openai_api_key_for_simulated_user"
    assert credential.to_dict()["created_trial"] is False


def test_user_simulator_credential_and_oracle_boundary_isolation(tmp_path: Path) -> None:
    """Ensure user-simulator credentials & oracle payload never leak into agent-visible bytes or decisions."""
    preflight = _load(PREFLIGHT, "tau_knowledge_preflight_leak")
    secret_key = "sk-proj-secret-user-simulator-token-12345"
    decision = preflight.credential_preflight(
        "reference",
        env={"OPENAI_API_KEY": secret_key},
        simulator_provider="openai",
        simulator_model="gpt-4o-mini-2024-07-18",
        simulator_credential_env="OPENAI_API_KEY",
    )
    assert decision.proceed is True
    decision_dict = decision.to_dict()
    # The key string MUST NEVER appear anywhere in the serialized decision dict/detail
    assert secret_key not in json.dumps(decision_dict)
    assert secret_key not in decision.detail
    assert decision_dict["simulator"]["provider"] == "openai"
    assert decision_dict["simulator"]["model"] == "gpt-4o-mini-2024-07-18"
    assert decision_dict["simulator"]["credential_env"] == "OPENAI_API_KEY"


def test_materialized_agent_package_boundary_rejects_credentials_and_oracle(
    tmp_path: Path,
) -> None:
    materializer = _load(MATERIALIZER, "tau_knowledge_boundary")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    task_dir = tmp_path / "tau3-banking_knowledge-task-001"
    environment = task_dir / "environment"
    tests = task_dir / "tests"
    solution = task_dir / "solution"
    environment.mkdir(parents=True)
    tests.mkdir()
    solution.mkdir()
    task_toml = task_dir / "task.toml"
    dockerfile = environment / "Dockerfile"
    task_toml.write_text(
        '[task]\nname = "tau3-banking_knowledge-task-001"\n',
        encoding="utf-8",
    )
    dockerfile.write_text("FROM python:3.12-slim\n", encoding="utf-8")
    hidden_payload = '{"ground_truth":"hidden-database-state-for-task-001"}'
    (tests / "config.json").write_text(hidden_payload + "\n", encoding="utf-8")
    (solution / "solve.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    materializer.validate_agent_boundary(task_dir, manifest)

    dockerfile.write_text(hidden_payload + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="oracle_boundary_leak"):
        materializer.validate_agent_boundary(task_dir, manifest)

    dockerfile.write_text("FROM python:3.12-slim\n", encoding="utf-8")
    task_toml.write_text("OPENAI_API_KEY must be set\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="simulator_credential_boundary_leak"):
        materializer.validate_agent_boundary(task_dir, manifest)


def test_harbor_repository_layout_resolves_nested_tau_adapter(tmp_path: Path) -> None:
    materializer = _load(MATERIALIZER, "tau_knowledge_nested_adapter")
    package = tmp_path / "harbor/adapters/tau3-bench/src/tau3_bench"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "adapter.py").write_text(
        "class Tau3BenchAdapter: pass\n", encoding="utf-8"
    )
    sys.modules.pop("tau3_bench", None)
    sys.modules.pop("tau3_bench.adapter", None)
    adapter = materializer._load_adapter(tmp_path / "harbor")
    assert adapter.__name__ == "Tau3BenchAdapter"


def test_adapter_digest_pins_are_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    materializer = _load(MATERIALIZER, "tau_knowledge_adapter_digests")
    package = tmp_path / "adapters/tau3-bench"
    (package / "src/tau3_bench").mkdir(parents=True)
    for relative in ("pyproject.toml", "README.md", "src/tau3_bench/adapter.py"):
        (package / relative).write_text("fixture\n", encoding="utf-8")
    root = tmp_path
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    monkeypatch.setattr(
        materializer.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=manifest["adapter_evidence"]["commit"] + "\n"
        ),
    )
    digests = {
        "pyproject.toml": manifest["adapter_evidence"]["adapter_pyproject_sha256"],
        "README.md": manifest["adapter_evidence"]["adapter_readme_sha256"],
        "adapter.py": manifest["adapter_evidence"]["adapter_source_sha256"],
    }
    monkeypatch.setattr(materializer, "sha256", lambda path: digests[path.name])
    assert materializer._validate_adapter(root, manifest) == root.resolve()
    monkeypatch.setattr(materializer, "sha256", lambda path: "sha256:wrong")
    with pytest.raises(RuntimeError, match="adapter_digest_mismatch"):
        materializer._validate_adapter(root, manifest)


def test_control_reads_persisted_reward(tmp_path: Path) -> None:
    controls = _load(CONTROLS, "tau_knowledge_reward")
    result = tmp_path / "trial/result.json"
    result.parent.mkdir()
    result.write_text(
        json.dumps({"verifier_result": {"rewards": {"reward": 0.0}}}),
        encoding="utf-8",
    )
    assert controls._persisted_reward(tmp_path) == 0.0


def test_controls_have_observable_oracle_nop_and_mutant_plans(tmp_path: Path) -> None:
    controls = _load(CONTROLS, "tau_knowledge_controls")
    task = tmp_path / "tau3-banking_knowledge-task-001"
    (task / "solution").mkdir(parents=True)
    (task / "task.toml").write_text("[task]\n", encoding="utf-8")
    for mode in ("oracle", "nop", "mutant"):
        command = controls.run_control(task, mode, dry_run=True)
        assert command[:6] == ["uv", "run", "harbor", "trial", "start", "-p"]
        assert command[-1] == ("oracle" if mode in {"oracle", "mutant"} else "nop")


def test_generated_corpus_is_not_tracked() -> None:
    tracked = subprocess.check_output(["git", "ls-files"], cwd=ROOT, text=True).splitlines()
    forbidden = (
        "library/benchmarks/tau-knowledge/generated/",
        "library/benchmarks/tau-knowledge/evidence/trials/",
        "library/benchmarks/tau-knowledge/evidence/luna/",
    )
    assert not [
        path
        for path in tracked
        if path.startswith(forbidden)
        or (path.endswith(".parquet") and "tau-knowledge" in path)
    ]
