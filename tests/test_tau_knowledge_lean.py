from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import zipfile
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
    assert simulator["base_url"] == "https://api.openai.com/v1"
    assert simulator["required_env"] == [
        "TAU3_SIMULATOR_API_KEY",
        "TAU3_SIMULATOR_BASE_URL",
    ]
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
    assert credential.reason_code == "blocked:missing_tau3_simulator_api_key_for_simulated_user"
    assert credential.to_dict()["created_trial"] is False


def test_user_simulator_credential_and_oracle_boundary_isolation(tmp_path: Path) -> None:
    """Ensure user-simulator credentials & oracle payload never leak into agent-visible bytes or decisions."""
    preflight = _load(PREFLIGHT, "tau_knowledge_preflight_leak")
    secret_key = "simulator-credential-value-marker-12345"
    decision = preflight.credential_preflight(
        "reference",
        env={
            "TAU3_SIMULATOR_API_KEY": secret_key,
            "TAU3_SIMULATOR_BASE_URL": "https://api.openai.com/v1",
        },
        simulator_provider="openai",
        simulator_model="gpt-4o-mini-2024-07-18",
        simulator_credential_env="TAU3_SIMULATOR_API_KEY",
    )
    assert decision.proceed is True
    decision_dict = decision.to_dict()
    # The key string MUST NEVER appear anywhere in the serialized decision dict/detail
    assert secret_key not in json.dumps(decision_dict)
    assert secret_key not in decision.detail
    assert decision_dict["simulator"]["provider"] == "openai"
    assert decision_dict["simulator"]["model"] == "gpt-4o-mini-2024-07-18"
    assert decision_dict["simulator"]["credential_env"] == "TAU3_SIMULATOR_API_KEY"
    assert decision_dict["simulator"]["base_url"] == "https://api.openai.com/v1"
    refused = preflight.credential_preflight(
        "evaluation",
        env={
            "TAU3_SIMULATOR_API_KEY": secret_key,
            "TAU3_SIMULATOR_BASE_URL": "http://localhost:11434/v1",
        },
    )
    assert refused.reason_code == "blocked:unregistered_simulated_user_route"


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
        'schema_version = "1.1"\n'
        '[task]\nname = "tau3-banking_knowledge-task-001"\n'
        "[verifier]\ntimeout_sec = 300.0\n"
        "[agent]\ntimeout_sec = 3600.0\n"
        '[environment]\nenv = { OPENAI_API_KEY = "${OPENAI_API_KEY}" }\n',
        encoding="utf-8",
    )
    materializer._normalize_task_metadata(task_dir)
    assert 'name = "evallab/tau3-banking-knowledge-task-001"' in task_toml.read_text(
        encoding="utf-8"
    )
    dockerfile.write_text(
        "FROM python:3.12-slim\nRUN git clone tau2-bench /opt/tau2-bench\n",
        encoding="utf-8",
    )
    hidden_payload = json.dumps(
        {
            "domain": "banking_knowledge",
            "source_task_id": "task_001",
            "task": {"initial_state": None},
            "expected_actions": [
                {
                    "name": "apply_for_credit_card",
                    "arguments": {"card_type": "Gold Rewards Card"},
                    "requestor": "user",
                }
            ],
            "expected_communicate_info": [],
            "ground_truth": "hidden-database-state-for-task-001",
        },
        separators=(",", ":"),
    )
    (tests / "config.json").write_text(hidden_payload + "\n", encoding="utf-8")
    (tests / "evaluate.py").write_text(
        "from pathlib import Path\n"
        'DEFAULT_RUNTIME_LOG_PATH = Path("/logs/agent/tau3_runtime_state.json")\n',
        encoding="utf-8",
    )
    (tests / "test.sh").write_text(
        "#!/bin/bash\n"
        'LOG_DIR="/logs/verifier"\n'
        "python3 /tests/evaluate.py \\\n"
        "  --config /tests/config.json \\\n"
        "  --runtime-log /logs/agent/tau3_runtime_state.json \\\n"
        '  --reward "${LOG_DIR}/reward.txt" \\\n'
        '  --result "${LOG_DIR}/result.json"\n',
        encoding="utf-8",
    )
    (solution / "solve.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    materializer.harden_agent_environment(task_dir)
    materializer.harden_oracle_solution(task_dir)
    materializer.harden_verifier_environment(task_dir, manifest)
    materializer._create_workbench_controls(task_dir)
    assert "env = {}" in task_toml.read_text(encoding="utf-8")
    assert "tau2-bench" not in dockerfile.read_text(encoding="utf-8")
    assert "/opt/tau2-bench" not in (solution / "solve.sh").read_text(encoding="utf-8")
    assert 'state_path = Path("/app/tau3_runtime_state.json")' in (solution / "solve.sh").read_text(
        encoding="utf-8"
    )
    verifier_config = task_toml.read_text(encoding="utf-8")
    assert 'environment_mode = "separate"' in verifier_config
    assert '[verifier.environment]\nnetwork_mode = "no-network"' in verifier_config
    assert 'artifacts = ["/app/tau3_runtime_state.json"]' in verifier_config
    assert "[[verifier.collect]]" in verifier_config
    assert (
        "cp /logs/agent/tau3_runtime_state.json /app/tau3_runtime_state.json"
    ) in verifier_config
    assert manifest["required_upstream"]["commit"] in (tests / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert 'Path("/app/tau3_runtime_state.json")' in (tests / "evaluate.py").read_text(
        encoding="utf-8"
    )
    assert "--runtime-log /app/tau3_runtime_state.json" in (tests / "test.sh").read_text(
        encoding="utf-8"
    )
    test_entrypoint = (tests / "test.sh").read_text(encoding="utf-8")
    assert ">/tmp/tau-evaluator.log 2>&1" in test_entrypoint
    assert "cat /tmp/tau-evaluator.log >&2" in test_entrypoint
    oracle_script = (solution / "solve.sh").read_text(encoding="utf-8")
    fair_script = (task_dir / "workbench/fair-alternative.sh").read_text(encoding="utf-8")
    assert fair_script != oracle_script
    assert "indent=4," in fair_script

    materializer.validate_agent_boundary(task_dir, manifest)

    dockerfile.write_text(hidden_payload + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="oracle_boundary_leak"):
        materializer.validate_agent_boundary(task_dir, manifest)

    dockerfile.write_text(materializer.AGENT_DOCKERFILE, encoding="utf-8")
    credential_value = "simulator-credential-value-marker-12345"
    task_toml.write_text("credential_env = TAU3_SIMULATOR_API_KEY\n", encoding="utf-8")
    materializer.validate_agent_boundary(
        task_dir,
        manifest,
        credential_environment={
            "TAU3_SIMULATOR_API_KEY": credential_value,
            "TAU3_SIMULATOR_BASE_URL": "https://api.openai.com/v1",
        },
    )
    task_toml.write_text(credential_value + "\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="simulator_credential_value_leak"):
        materializer.validate_agent_boundary(
            task_dir,
            manifest,
            credential_environment={
                "TAU3_SIMULATOR_API_KEY": credential_value,
                "TAU3_SIMULATOR_BASE_URL": "https://api.openai.com/v1",
            },
        )


def test_harbor_repository_layout_resolves_nested_tau_adapter(tmp_path: Path) -> None:
    materializer = _load(MATERIALIZER, "tau_knowledge_nested_adapter")
    package = tmp_path / "harbor/adapters/tau3-bench/src/tau3_bench"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "adapter.py").write_text("class Tau3BenchAdapter: pass\n", encoding="utf-8")
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
        assert command[:4] == ["harbor", "trial", "start", "-p"]
        assert Path(command[4]).name == task.name
        assert command[5:] == [
            "-a",
            "oracle" if mode in {"oracle", "mutant"} else "nop",
            "--force-build",
        ]


def test_oracle_nop_gate_plans_both_free_controls(tmp_path: Path) -> None:
    controls = _load(CONTROLS, "tau_knowledge_control_gate")
    task = tmp_path / "tau3-banking_knowledge-task-001"
    (task / "solution").mkdir(parents=True)
    (task / "task.toml").write_text("[task]\n", encoding="utf-8")

    commands = controls.run_oracle_nop_gate(
        task,
        trials_dir=tmp_path / "controls",
        dry_run=True,
    )

    assert list(commands) == ["oracle", "nop"]
    assert commands["oracle"][-2:] == ["--trials-dir", str(tmp_path / "controls/oracle")]
    assert commands["nop"][-2:] == ["--trials-dir", str(tmp_path / "controls/nop")]


def test_hardened_sidecar_runtime_is_syntactically_valid(tmp_path: Path) -> None:
    materializer = _load(MATERIALIZER, "tau_knowledge_sidecar_syntax")
    task = tmp_path / "task"
    runtime = task / "environment/runtime-server"
    runtime.mkdir(parents=True)
    (runtime / "task_config.json").write_text(
        json.dumps(
            {
                "domain": "banking_knowledge",
                "source_task_id": "task_001",
                "task": {"hidden": "oracle"},
            }
        ),
        encoding="utf-8",
    )
    (runtime / "server.py").write_text(
        "import json\n"
        "import os\n"
        "from typing import Any\n\n"
        "def _build_user_llm_args(override_json=None):\n"
        "    if override_json:\n"
        "        decoded = json.loads(override_json)\n"
        "        if not isinstance(decoded, dict):\n"
        '            raise ValueError("User LLM args override must decode to a JSON object.")\n'
        "        llm_args: dict[str, Any] = decoded\n"
        "    else:\n"
        "        llm_args = {}\n"
        "    return llm_args\n\n"
        "class Runtime:\n"
        "    def initialize(self):\n"
        '        self.task = self.Task.model_validate(self.config["task"])\n'
        "    def _write_state(self):\n"
        "        payload = {\n"
        '            "start_tool_called": self.start_tool_called,\n'
        "        }\n\n"
        "class MCP:\n"
        "    def tool(self):\n"
        "        return lambda function: function\n\n"
        "mcp = MCP()\n"
        "runtime = Runtime()\n\n"
        "@mcp.tool()\n"
        "def configure_run(\n"
        "    seed: int | None = None,\n"
        "    max_steps: int | None = None,\n"
        "    max_errors: int | None = None,\n"
        "    user_llm_args_json: str | None = None,\n"
        ") -> str:\n"
        '    """Configure tau2 run parameters before the first conversation turn."""\n'
        "    return runtime.configure_run(\n"
        "        seed=seed,\n"
        "        max_steps=max_steps,\n"
        "        max_errors=max_errors,\n"
        "        user_llm_args_json=user_llm_args_json,\n"
        "    )\n",
        encoding="utf-8",
    )
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    wheel = wheelhouse / "valid_pkg-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr(
            "valid_pkg-1.0.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: valid-pkg\nVersion: 1.0.0\n",
        )
    materializer._prepare_wheelhouse(wheelhouse)

    materializer.harden_sidecar_environment(
        task,
        {"required_upstream": {"commit": "a" * 40}},
        wheelhouse,
    )

    generated = (runtime / "server.py").read_text(encoding="utf-8")
    compile(generated, str(runtime / "server.py"), "exec")
    assert '"task"' not in (runtime / "task_config.json").read_text(encoding="utf-8")
    assert '"api_key", "api_base", "base_url", "model"' in generated
    assert '"user_simulator"' in generated
    assert '"model": os.environ["TAU2_USER_MODEL"]' in generated
    exposed_tool = generated.split("@mcp.tool()", 1)[1]
    assert "user_llm_args_json" not in exposed_tool


def test_wheelhouse_metadata_inspection_and_dummy_rejection(tmp_path: Path) -> None:
    """Ensure wheel metadata parser rejects corrupt/empty wheels and extracts valid distribution info."""
    materializer = _load(MATERIALIZER, "tau_knowledge_wheel_inspection")
    empty_whl = tmp_path / "dummy-0.0.1-py3-none-any.whl"
    with zipfile.ZipFile(empty_whl, "w") as zf:
        zf.writestr("dummy.py", "# empty\n")
    with pytest.raises(RuntimeError, match="wheel_metadata_missing"):
        materializer._wheel_metadata(empty_whl)

    corrupt_whl = tmp_path / "corrupt-0.0.1-py3-none-any.whl"
    with zipfile.ZipFile(corrupt_whl, "w") as zf:
        zf.writestr("dummy-0.0.1.dist-info/METADATA", "InvalidMetadataHeaderWithoutName\n")
    with pytest.raises(RuntimeError, match="wheel_metadata_missing"):
        materializer._wheel_metadata(corrupt_whl)

    valid_whl = tmp_path / "valid_pkg-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(valid_whl, "w") as zf:
        zf.writestr(
            "valid_pkg-1.0.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: valid-pkg\nVersion: 1.0.0\n",
        )
    name, version = materializer._wheel_metadata(valid_whl)
    assert name == "valid-pkg"
    assert version == "1.0.0"


def test_wheelhouse_requirements_hash_locking(tmp_path: Path) -> None:
    """Ensure _prepare_wheelhouse generates a hash-locked requirements file."""
    materializer = _load(MATERIALIZER, "tau_knowledge_req_locking")
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    whl1 = wheelhouse / "alpha-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(whl1, "w") as zf:
        zf.writestr(
            "alpha-1.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: alpha\nVersion: 1.0.0\n"
        )
    whl2 = wheelhouse / "beta-2.0.0-py3-none-any.whl"
    with zipfile.ZipFile(whl2, "w") as zf:
        zf.writestr(
            "beta-2.0.0.dist-info/METADATA", "Metadata-Version: 2.1\nName: beta\nVersion: 2.0.0\n"
        )

    materializer._prepare_wheelhouse(wheelhouse)
    reqs_path = wheelhouse / "requirements.txt"
    assert reqs_path.is_file()
    lines = reqs_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("alpha==1.0.0 --hash=sha256:")
    assert lines[1].startswith("beta==2.0.0 --hash=sha256:")


def test_docker_compose_structure_preserves_task_local_named_volume() -> None:
    """Ensure generated docker-compose.yaml satisfies single named volume topology."""
    materializer = _load(MATERIALIZER, "tau_knowledge_compose_gen")
    compose_yaml = materializer._generate_docker_compose()
    assert "volumes:\n  tau3-logs:\n" in compose_yaml
    assert "tau3-logs:/logs/agent:ro" in compose_yaml
    assert "tau3-logs:/logs/agent:rw" in compose_yaml
    assert "tau3-runtime" in compose_yaml
    assert (
        "OPENAI_API_KEY=${TAU3_SIMULATOR_API_KEY:?TAU3_SIMULATOR_API_KEY is required}"
        in compose_yaml
    )
    assert (
        "OPENAI_BASE_URL=${TAU3_SIMULATOR_BASE_URL:?TAU3_SIMULATOR_BASE_URL is required}"
        in compose_yaml
    )
    assert "OPENAI_API_KEY=${OPENAI_API_KEY" not in compose_yaml


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
        if path.startswith(forbidden) or (path.endswith(".parquet") and "tau-knowledge" in path)
    ]
