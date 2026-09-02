#!/usr/bin/env python3
"""Materialize selected Tau tasks under ignored derived/harbor-tasks only."""

from __future__ import annotations

import argparse
import email
import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_MANIFEST = Path("library/benchmarks/tau-knowledge/cohort.manifest.json")
DEFAULT_OUTPUT = Path("derived/harbor-tasks/tau")
PYTHON_BASE_IMAGE = (
    "python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217"
)
DATA_PACKAGE_NAME = "tau2-bench-data"
DATA_PACKAGE_DIR = "tau2_bench_data"
SIDECAR_NAME = "tau3-runtime"
VOLUME_NAME = "tau3-logs"
MOUNT_PATH = "/logs/agent"
RUNTIME_STATE_PATH = "/logs/agent/tau3_runtime_state.json"
ARTIFACT_PATH = "/app/tau3_runtime_state.json"
SIMULATOR_MODEL = "gpt-4o-mini-2024-07-18"

AGENT_DOCKERFILE = f"""FROM {PYTHON_BASE_IMAGE}

WORKDIR /app
"""

ORACLE_TOOL_RESULTS: dict[str, str] = {
    "apply_for_credit_card": (
        "Credit card application submitted:\n"
        "Your application has been successfully submitted. "
        "You will receive a decision within 5-7 business days via email."
    )
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


try:
    from preflight import REGISTERED_SIMULATOR_BASE_URL, sha256, validate_source
except ImportError as exc:
    _spec = importlib.util.spec_from_file_location(
        "tau_knowledge_preflight", Path(__file__).with_name("preflight.py")
    )
    if _spec is None or _spec.loader is None:
        raise RuntimeError("cannot load Tau preflight") from exc
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)
    REGISTERED_SIMULATOR_BASE_URL = _module.REGISTERED_SIMULATOR_BASE_URL
    sha256, validate_source = _module.sha256, _module.validate_source


def source_digest(manifest: Mapping[str, Any]) -> str:
    payload = {
        "required_upstream": manifest["required_upstream"],
        "adapter_evidence": manifest["adapter_evidence"],
        "tasks": manifest["tasks"],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _validate_adapter(root: Path, manifest: Mapping[str, Any]) -> Path:
    root = root.expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"blocked:missing_adapter_checkout:{root}")
    expected = manifest["adapter_evidence"]["commit"]
    try:
        actual = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"blocked:unreadable_adapter_checkout:{root}") from exc
    if actual != expected:
        raise RuntimeError(f"blocked:adapter_commit_mismatch:expected={expected}:actual={actual}")
    package_root = root / "adapters" / "tau3-bench"
    if not (package_root / "pyproject.toml").is_file():
        package_root = root / "harbor" / "adapters" / "tau3-bench"
    if not (package_root / "pyproject.toml").is_file():
        package_root = root
    expected_files = {
        "adapter_pyproject_sha256": package_root / "pyproject.toml",
        "adapter_source_sha256": package_root / "src" / "tau3_bench" / "adapter.py",
        "adapter_readme_sha256": package_root / "README.md",
    }
    evidence = manifest["adapter_evidence"]
    for evidence_key, path in expected_files.items():
        if not path.is_file() or sha256(path) != evidence[evidence_key]:
            raise RuntimeError(f"blocked:adapter_digest_mismatch:{path}")
    return root


def _load_adapter(root: Path) -> type[Any]:
    candidates = [
        root / "src",
        root / "adapters" / "tau3-bench" / "src",
        root / "harbor" / "adapters" / "tau3-bench" / "src",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            sys.path.insert(0, str(candidate))
            try:
                return importlib.import_module("tau3_bench.adapter").Tau3BenchAdapter
            except (ImportError, AttributeError):
                continue
    raise RuntimeError(f"blocked:adapter_package_missing:{root}")


def _adapter_package_root(adapter_root: Path) -> Path:
    """Return the directory that contains the tau3-bench adapter pyproject.toml."""
    candidates = [
        adapter_root / "adapters" / "tau3-bench",
        adapter_root / "harbor" / "adapters" / "tau3-bench",
        adapter_root,
    ]
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError(f"blocked:adapter_package_missing:{adapter_root}")


def _python_for_wheelhouse() -> Path:
    """Locate a Python 3.12 interpreter to build Linux cp312 wheels."""
    exe = Path(sys.executable).resolve()
    version = subprocess.run(
        [str(exe), "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if version.startswith("Python 3.12"):
        return exe
    for name in ("python3.12", "python3"):
        path = shutil.which(name)
        if not path:
            continue
        version = subprocess.run(
            [path, "--version"], check=True, capture_output=True, text=True
        ).stdout.strip()
        if version.startswith("Python 3.12"):
            return Path(path)
    raise RuntimeError(
        "blocked:no_python_3.12_for_wheelhouse: building Linux wheels "
        "requires a CPython 3.12 interpreter"
    )


def _build_tau2_data_package(source_root: Path, temp_dir: Path) -> Path:
    """Build an offline data package that exposes tau2_bench_data.DATA_DIR."""
    data_pkg = temp_dir / "tau2-data-pkg"
    src = data_pkg / "src" / DATA_PACKAGE_DIR
    src.mkdir(parents=True)
    data_src = source_root / "data" / "tau2"
    if not data_src.is_dir():
        raise RuntimeError(f"blocked:tau2_data_dir_missing:{data_src}")
    shutil.copytree(data_src, src / "data" / "tau2")
    (src / "__init__.py").write_text(
        'from pathlib import Path\n\nDATA_DIR = Path(__file__).parent / "data"\n',
        encoding="utf-8",
    )
    (data_pkg / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n\n'
        "[project]\n"
        f'name = "{DATA_PACKAGE_NAME}"\n'
        'version = "1.0.1"\n'
        'requires-python = ">=3.12"\n\n'
        "[tool.hatch.build.targets.wheel]\n"
        'packages = ["src/tau2_bench_data"]\n',
        encoding="utf-8",
    )
    return data_pkg


def _wheel_metadata(whl: Path) -> tuple[str, str]:
    """Extract Name and Version from a wheel's METADATA."""
    with zipfile.ZipFile(whl) as zf:
        names = [n for n in zf.namelist() if n.endswith(".dist-info/METADATA")]
        if not names:
            raise RuntimeError(f"blocked:wheel_metadata_missing:{whl.name}")
        raw = zf.read(names[0])
    msg = email.message_from_bytes(raw)
    name = msg.get("Name", "").strip()
    version = msg.get("Version", "").strip()
    if not name or not version:
        raise RuntimeError(f"blocked:wheel_metadata_missing:{whl.name}")
    return name, version


def _prepare_wheelhouse(wheelhouse: Path) -> None:
    """Write a sorted requirements.txt with per-wheel hashes."""
    entries: list[tuple[str, str, str, str]] = []
    for whl in sorted(wheelhouse.glob("*.whl"), key=lambda p: p.name.lower()):
        name, version = _wheel_metadata(whl)
        entries.append((name, version, _sha256_file(whl), whl.name))
    entries.sort(key=lambda x: x[0].lower())
    lines = [f"{name}=={version} --hash={digest}" for name, version, digest, _ in entries]
    (wheelhouse / "requirements.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_wheelhouse(source_root: Path, adapter_root: Path, temp_dir: Path) -> Path:
    """Create a Linux wheelhouse with tau2, data, the adapter, and fastmcp."""
    data_pkg = _build_tau2_data_package(source_root, temp_dir)
    adapter_pkg = _adapter_package_root(adapter_root)
    wheelhouse = temp_dir / "wheelhouse"
    wheelhouse.mkdir()
    build_venv = temp_dir / "build-venv"
    python = _python_for_wheelhouse()
    subprocess.run(
        [str(python), "-m", "venv", str(build_venv)],
        check=True,
    )
    pip = build_venv / "bin" / "pip"
    env = {**os.environ, "SOURCE_DATE_EPOCH": "315532800"}
    cmd = [
        str(pip),
        "wheel",
        f"{source_root}[knowledge]",
        str(data_pkg),
        str(adapter_pkg),
        "fastmcp",
        "--wheel-dir",
        str(wheelhouse),
        "--no-cache-dir",
    ]
    subprocess.run(cmd, check=True, env=env, stdout=sys.stderr, stderr=sys.stderr)
    _prepare_wheelhouse(wheelhouse)
    return wheelhouse


def _copy_wheelhouse(source: Path, destination: Path) -> None:
    """Copy the wheel directory to a build context, replacing if needed."""
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination)


def _write_build_proof(context_dir: Path, wheelhouse_dir: Path, lockfile_rel: str) -> None:
    """Write a v2 offline build proof for the supplied build context."""
    lockfile_path = context_dir / lockfile_rel
    if not lockfile_path.is_file():
        raise RuntimeError(f"blocked:lockfile_missing:{lockfile_path}")
    pinned: list[dict[str, Any]] = []
    for whl in sorted(wheelhouse_dir.glob("*.whl"), key=lambda p: p.name.lower()):
        name, version = _wheel_metadata(whl)
        pinned.append(
            {
                "name": name,
                "version": version,
                "wheel": whl.name,
            }
        )
    if not pinned:
        raise RuntimeError("blocked:empty_wheelhouse")
    proof = {
        "schema_version": "1.0",
        "kind": "offline_build_proof",
        "ecosystem": "python",
        "lockfile": lockfile_rel,
        "lockfile_digest": _sha256_file(lockfile_path),
        "pinned_dependencies": pinned,
        "reviewed_by": "tau-knowledge-materializer",
    }
    (context_dir / "build-proof.json").write_text(
        json.dumps(proof, indent=2) + "\n", encoding="utf-8"
    )


def _rewrite_env_in_section(text: str, section: str, replacement: str) -> str:
    """Replace the first env = ... line inside a TOML section."""
    lines = text.splitlines()
    current = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped
        if current == section and stripped.startswith("env ="):
            lines[i] = replacement
            break
    return "\n".join(lines)


def _add_after_section(text: str, section: str, insert: str) -> str:
    """Insert a line immediately after a TOML section header."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == section:
            lines.insert(i + 1, insert)
            break
    return "\n".join(lines)


def _normalize_task_metadata(task_dir: Path) -> None:
    """Ensure task.toml carries the required v2 metadata and Harbor package name."""
    task_toml = task_dir / "task.toml"
    text = task_toml.read_text(encoding="utf-8")
    safe_name = "evallab/tau3-banking-knowledge-task-001"
    description = (
        "Rho-Bank customer-service task requiring KB retrieval and a credit-card application."
    )
    text = re.sub(
        r'^name\s*=\s*"[^"]*"',
        (f'name = "{safe_name}"\nversion = "1.0.1"\ndescription = "{description}"'),
        text,
        flags=re.MULTILINE,
        count=1,
    )
    text = re.sub(
        r"^(\[metadata\]\s*\n)",
        r'\1tags = ["tau3", "banking", "knowledge"]\n',
        text,
        flags=re.MULTILINE,
        count=1,
    )
    task_toml.write_text(text, encoding="utf-8")


def harden_agent_environment(task_dir: Path) -> None:
    """Remove simulator secrets and benchmark source data from the agent service."""
    task_config = task_dir / "task.toml"
    text = task_config.read_text(encoding="utf-8")
    text = _rewrite_env_in_section(text, "[environment]", "env = {}")
    text = _add_after_section(text, "[environment]", 'network_mode = "no-network"')
    task_config.write_text(text + "\n", encoding="utf-8")

    agent_dockerfile = task_dir / "environment/Dockerfile"
    agent_dockerfile.write_text(AGENT_DOCKERFILE, encoding="utf-8")


def _registered_simulator_base_url(manifest: Mapping[str, Any]) -> str:
    try:
        simulator_base_url = manifest["credentials"]["simulated_user"]["base_url"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError("blocked:missing_registered_simulator_base_url") from exc
    if simulator_base_url != REGISTERED_SIMULATOR_BASE_URL:
        raise RuntimeError("blocked:unregistered_simulator_base_url")
    return simulator_base_url


def _generate_docker_compose(manifest: Mapping[str, Any]) -> str:
    _registered_simulator_base_url(manifest)
    return (
        f"services:\n"
        f"  main:\n"
        f"    build: .\n"
        f"    volumes:\n"
        f"      - {VOLUME_NAME}:{MOUNT_PATH}:ro\n"
        f"  {SIDECAR_NAME}:\n"
        f"    build: ./runtime-server\n"
        f"    environment:\n"
        f"      - OPENAI_API_KEY=${{TAU3_SIMULATOR_API_KEY}}\n"
        f"    volumes:\n"
        f"      - {VOLUME_NAME}:{MOUNT_PATH}:rw\n"
        f"volumes:\n"
        f"  {VOLUME_NAME}:\n"
    )


def harden_oracle_solution(task_dir: Path) -> None:
    """Build the oracle runtime log without installing Tau data in the agent image."""
    config = json.loads((task_dir / "tests/config.json").read_text(encoding="utf-8"))
    task = config["task"]
    unsupported_actions = sorted(
        {
            str(action["name"])
            for action in config.get("expected_actions") or []
            if action["name"] not in ORACLE_TOOL_RESULTS
        }
    )
    if unsupported_actions:
        raise RuntimeError(
            "blocked:tau_oracle_result_contract_missing:" + ",".join(unsupported_actions)
        )
    simulation = {
        "actions": config.get("expected_actions") or [],
        "communicate_info": config.get("expected_communicate_info") or [],
        "initialization_data": task.get("initial_state"),
        "initialization_actions": task.get("initialization_actions") or [],
        "tool_results": ORACLE_TOOL_RESULTS,
    }
    script = f"""#!/bin/bash
set -euo pipefail
python3 - <<'PYEOF'
import json
from pathlib import Path

simulation = json.loads({json.dumps(json.dumps(simulation, sort_keys=True))})
messages = []
for index, action in enumerate(simulation["actions"]):
    requestor = action.get("requestor", "assistant")
    call_id = f"oracle_{{index}}"
    messages.append(
        {{
            "role": "user" if requestor == "user" else "assistant",
            "content": None,
            "tool_calls": [
                {{
                    "id": call_id,
                    "name": action["name"],
                    "arguments": action.get("arguments") or {{}},
                    "requestor": requestor,
                }}
            ],
        }}
    )
    messages.append(
        {{
            "id": call_id,
            "role": "tool",
            "content": simulation["tool_results"][action["name"]],
            "requestor": requestor,
            "error": False,
        }}
    )
for content in simulation["communicate_info"]:
    messages.append({{"role": "assistant", "content": content}})

state_path = Path({json.dumps(ARTIFACT_PATH)})
state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(
    json.dumps(
        {{
            "domain": {json.dumps(config["domain"])},
            "task_id": {json.dumps(config["source_task_id"])},
            "termination_reason": "agent_stop",
            "bootstrap_complete": True,
            "start_tool_called": True,
            "messages": messages,
        }},
        indent=2,
    ),
    encoding="utf-8",
)
PYEOF
"""
    path = task_dir / "solution/solve.sh"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def _state_script(state: dict[str, Any]) -> str:
    """Wrap a runtime-state dict as an executable /bin/bash script."""
    return f"""#!/bin/bash
set -euo pipefail
python3 - <<'PYEOF'
import json
from pathlib import Path

state = json.loads({json.dumps(json.dumps(state, sort_keys=True))})
state_path = Path({json.dumps(ARTIFACT_PATH)})
state_path.parent.mkdir(parents=True, exist_ok=True)
state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
PYEOF
"""


def _create_workbench_controls(task_dir: Path) -> None:
    """Write oracle alternatives and adversarial control probes."""
    config = json.loads((task_dir / "tests/config.json").read_text(encoding="utf-8"))
    (task_dir / "workbench").mkdir(parents=True, exist_ok=True)

    oracle_script = (task_dir / "solution/solve.sh").read_text(encoding="utf-8")
    fair_script = oracle_script.replace("indent=2,", "indent=4,", 1)
    if fair_script == oracle_script:
        raise RuntimeError("blocked:tau_fair_alternative_contract_drift")
    fair_path = task_dir / "workbench/fair-alternative.sh"
    fair_path.write_text(fair_script, encoding="utf-8")
    fair_path.chmod(0o755)

    please_hack = {
        "domain": config["domain"],
        "task_id": config["source_task_id"],
        "termination_reason": "agent_stop",
        "bootstrap_complete": True,
        "start_tool_called": True,
        "actions": [],
        "communicate_info": [],
    }
    hack_path = task_dir / "workbench/please-hack.sh"
    hack_path.write_text(_state_script(please_hack), encoding="utf-8")
    hack_path.chmod(0o755)

    adversarial = task_dir / "workbench/adversarial"
    adversarial.mkdir(exist_ok=True)

    def _write_adversarial(name: str, actions: list[dict[str, Any]]) -> None:
        bad = {
            "domain": config["domain"],
            "task_id": config["source_task_id"],
            "termination_reason": "agent_stop",
            "bootstrap_complete": True,
            "start_tool_called": True,
            "actions": actions,
            "communicate_info": [],
        }
        path = adversarial / f"{name}.sh"
        path.write_text(_state_script(bad), encoding="utf-8")
        path.chmod(0o755)

    if config.get("expected_actions"):
        action = config["expected_actions"][0]
        args = dict(action.get("arguments") or {})
        _write_adversarial("no-action", [])
        wrong_args = {**args}
        for key in ("card_type", "customer_name"):
            if key in wrong_args:
                wrong_args[key] = "Unknown"
                break
        _write_adversarial("wrong-arguments", [{**action, "arguments": wrong_args}])
        _write_adversarial(
            "wrong-tool",
            [{**action, "name": "transfer_to_human_agents"}],
        )


def harden_verifier_environment(
    task_dir: Path, manifest: Mapping[str, Any], wheelhouse: Path | None = None
) -> None:
    """Move Tau evaluator dependencies into a verifier-only container."""
    task_config = task_dir / "task.toml"
    text = task_config.read_text(encoding="utf-8")
    text = _rewrite_env_in_section(text, "[verifier]", "env = {}")
    if "artifacts =" not in text:
        text = text.replace(
            'schema_version = "1.1"\n',
            f'schema_version = "1.1"\nartifacts = [{json.dumps(ARTIFACT_PATH)}]\n',
            1,
        )
    text = text.replace(
        "[verifier]\n",
        '[verifier]\nenvironment_mode = "separate"\n',
        1,
    )
    text = text.replace(
        "\n[agent]\n",
        "\n[[verifier.collect]]\n"
        f'command = "if [ ! -f {ARTIFACT_PATH} ] '
        f"&& [ -f {RUNTIME_STATE_PATH} ]; then "
        f'cp {RUNTIME_STATE_PATH} {ARTIFACT_PATH}; fi"\n'
        'service = "main"\n\n'
        '[verifier.environment]\nnetwork_mode = "no-network"\n\n[agent]\n',
        1,
    )
    task_config.write_text(text + "\n", encoding="utf-8")

    commit = manifest["required_upstream"]["commit"]
    if wheelhouse is None:
        dockerfile = f"""FROM {PYTHON_BASE_IMAGE}

ARG TAU2_BENCH_REPO="https://github.com/sierra-research/tau2-bench.git"
ARG TAU2_BENCH_COMMIT="{commit}"
ENV TAU2_BENCH_ROOT=/opt/tau2-bench
ENV TAU2_DATA_DIR=/opt/tau2-bench/data
RUN apt-get update \\
    && apt-get install -y --no-install-recommends ca-certificates git \\
    && git clone "${{TAU2_BENCH_REPO}}" "${{TAU2_BENCH_ROOT}}" \\
    && git -C "${{TAU2_BENCH_ROOT}}" checkout "${{TAU2_BENCH_COMMIT}}" \\
    && pip install --no-cache-dir "${{TAU2_BENCH_ROOT}}[knowledge]" \\
    && rm -rf "${{TAU2_BENCH_ROOT}}/.git" /var/lib/apt/lists/*
WORKDIR /tests
COPY config.json /tests/config.json
COPY evaluate.py /tests/evaluate.py
COPY test.sh /tests/test.sh
RUN chmod +x /tests/test.sh
"""
    else:
        dest = task_dir / "tests" / "wheelhouse"
        _copy_wheelhouse(wheelhouse, dest)
        _write_build_proof(task_dir / "tests", dest, "wheelhouse/requirements.txt")
        dockerfile = f"""FROM {PYTHON_BASE_IMAGE}

ARG TAU2_BENCH_COMMIT="{commit}"
ENV TAU2_DATA_DIR=/usr/local/lib/python3.12/site-packages/tau2_bench_data/data
WORKDIR /tests
COPY wheelhouse /wheelhouse
RUN pip install --no-index --find-links=/wheelhouse -r /wheelhouse/requirements.txt --require-hashes
COPY config.json /tests/config.json
COPY evaluate.py /tests/evaluate.py
COPY test.sh /tests/test.sh
RUN chmod +x /tests/test.sh
"""
    (task_dir / "tests/Dockerfile").write_text(dockerfile, encoding="utf-8")

    evaluate_path = task_dir / "tests/evaluate.py"
    evaluate_text = evaluate_path.read_text(encoding="utf-8")
    runtime_log_declaration = (
        'DEFAULT_RUNTIME_LOG_PATH = Path("/logs/agent/tau3_runtime_state.json")'
    )
    runtime_log_resolution = f"DEFAULT_RUNTIME_LOG_PATH = Path({json.dumps(ARTIFACT_PATH)})"
    if runtime_log_declaration not in evaluate_text:
        raise RuntimeError("blocked:tau_verifier_runtime_log_contract_drift")
    evaluate_text = evaluate_text.replace(runtime_log_declaration, runtime_log_resolution, 1)
    if _OLD_TAU2_SETUP in evaluate_text:
        evaluate_text = evaluate_text.replace(_OLD_TAU2_SETUP, _OFFLINE_TAU2_SETUP, 1)
    else:
        evaluate_text += "\n\n" + _OFFLINE_TAU2_SETUP
    evaluate_path.write_text(evaluate_text, encoding="utf-8")

    test_path = task_dir / "tests/test.sh"
    test_text = test_path.read_text(encoding="utf-8")
    runtime_log_argument = "--runtime-log /logs/agent/tau3_runtime_state.json"
    if runtime_log_argument not in test_text:
        raise RuntimeError("blocked:tau_verifier_test_entrypoint_contract_drift")
    test_text = test_text.replace(runtime_log_argument, f"--runtime-log {ARTIFACT_PATH}", 1)
    command_start = "python3 /tests/evaluate.py \\\n"
    command_end = '  --result "${LOG_DIR}/result.json"'
    if command_start not in test_text or command_end not in test_text:
        raise RuntimeError("blocked:tau_verifier_command_contract_drift")
    test_text = test_text.replace(command_start, "if ! " + command_start, 1)
    test_text = test_text.replace(
        command_end,
        command_end
        + " >/tmp/tau-evaluator.log 2>&1; then\n"
        + "  cat /tmp/tau-evaluator.log >&2\n"
        + "  exit 1\n"
        + "fi",
        1,
    )
    test_path.write_text(test_text, encoding="utf-8")


_OLD_TAU2_SETUP = """def _setup_tau2_path(config: dict[str, Any]) -> bool:
    candidates: list[Path] = []

    env_root = os.getenv("TAU2_BENCH_ROOT")
    if env_root:
        candidates.append(Path(env_root))

    configured_root = config.get("tau2_root")
    if configured_root:
        candidates.append(Path(configured_root))

    candidates.append(TAU2_RUNTIME_ROOT)

    for root in candidates:
        src = root / "src"
        if (src / "tau2").exists():
            sys.path.insert(0, str(src))
            if not os.getenv("TAU2_DATA_DIR"):
                data_dir = root / "data"
                if data_dir.exists():
                    os.environ["TAU2_DATA_DIR"] = str(data_dir)
            return True
    return False
"""

_OFFLINE_TAU2_SETUP = """def _setup_tau2_path(config: dict[str, Any]) -> bool:
    import importlib.util

    if importlib.util.find_spec("tau2") is not None:
        if not os.getenv("TAU2_DATA_DIR"):
            try:
                import tau2_bench_data
                os.environ["TAU2_DATA_DIR"] = str(tau2_bench_data.DATA_DIR)
            except Exception:
                env_root = os.getenv("TAU2_BENCH_ROOT")
                configured_root = config.get("tau2_root")
                for root in (Path(p) for p in (env_root, configured_root) if p):
                    data_dir = root / "data"
                    if data_dir.exists():
                        os.environ["TAU2_DATA_DIR"] = str(data_dir)
                        break
                else:
                    data_dir = TAU2_RUNTIME_ROOT / "data"
                    if data_dir.exists():
                        os.environ["TAU2_DATA_DIR"] = str(data_dir)
        return True

    candidates: list[Path] = []
    env_root = os.getenv("TAU2_BENCH_ROOT")
    if env_root:
        candidates.append(Path(env_root))
    configured_root = config.get("tau2_root")
    if configured_root:
        candidates.append(Path(configured_root))
    candidates.append(TAU2_RUNTIME_ROOT)
    for root in candidates:
        src = root / "src"
        if (src / "tau2").exists():
            sys.path.insert(0, str(src))
            if not os.getenv("TAU2_DATA_DIR"):
                data_dir = root / "data"
                if data_dir.exists():
                    os.environ["TAU2_DATA_DIR"] = str(data_dir)
            return True
    return False
"""


def harden_sidecar_environment(
    task_dir: Path, manifest: Mapping[str, Any], wheelhouse: Path
) -> None:
    """Package the runtime sidecar with an offline wheelhouse and public config only."""
    runtime_dir = task_dir / "environment" / "runtime-server"
    if not runtime_dir.is_dir():
        raise RuntimeError("blocked:missing_runtime_server_directory")

    full_config_path = runtime_dir / "task_config.json"
    if not full_config_path.is_file():
        raise RuntimeError("blocked:missing_runtime_task_config")
    full_config = json.loads(full_config_path.read_text(encoding="utf-8"))
    minimal_config = {
        "domain": full_config.get("domain"),
        "source_task_id": full_config.get("source_task_id"),
        "retrieval_variant": full_config.get("retrieval_variant", "bm25"),
        "max_steps": full_config.get("max_steps", 200),
        "max_errors": full_config.get("max_errors", 10),
    }
    full_config_path.write_text(json.dumps(minimal_config, indent=2) + "\n", encoding="utf-8")

    server_path = runtime_dir / "server.py"
    server_text = server_path.read_text(encoding="utf-8")
    old_prepare = """def _prepare_tau2_imports() -> None:
    src = TAU2_RUNTIME_ROOT / "src"
    if not (src / "tau2").exists():
        raise ImportError(f"Could not locate tau2 sources under {src}")

    sys.path.insert(0, str(src))
    if not os.getenv("TAU2_DATA_DIR"):
        data_dir = TAU2_RUNTIME_ROOT / "data"
        if data_dir.exists():
            os.environ["TAU2_DATA_DIR"] = str(data_dir)
"""
    new_prepare = """def _prepare_tau2_imports() -> None:
    import importlib.util

    if importlib.util.find_spec("tau2") is None:
        src = TAU2_RUNTIME_ROOT / "src"
        if not (src / "tau2").exists():
            raise ImportError(f"Could not locate tau2 sources under {src}")
        sys.path.insert(0, str(src))
    if not os.getenv("TAU2_DATA_DIR"):
        try:
            import tau2_bench_data
            os.environ["TAU2_DATA_DIR"] = str(tau2_bench_data.DATA_DIR)
        except Exception:
            data_dir = TAU2_RUNTIME_ROOT / "data"
            if data_dir.exists():
                os.environ["TAU2_DATA_DIR"] = str(data_dir)
"""
    if old_prepare in server_text:
        server_text = server_text.replace(old_prepare, new_prepare, 1)
    old_task = 'self.task = self.Task.model_validate(self.config["task"])'
    new_task = """task_payload = self.config.get("task")
        if task_payload is None:
            import tau2_bench_data
            task_path = (
                Path(tau2_bench_data.DATA_DIR)
                / "tau2"
                / "domains"
                / self.domain
                / "tasks"
                / f"{self.config['source_task_id']}.json"
            )
            task_payload = json.loads(task_path.read_text(encoding="utf-8"))
        self.task = self.Task.model_validate(task_payload)"""
    if old_task in server_text:
        server_text = server_text.replace(old_task, new_task, 1)
    old_override = """        if not isinstance(decoded, dict):
            raise ValueError("User LLM args override must decode to a JSON object.")
        llm_args: dict[str, Any] = decoded"""
    new_override = """        if not isinstance(decoded, dict):
            raise ValueError("User LLM args override must decode to a JSON object.")
        forbidden = {"api_key", "api_base", "base_url", "model"} & decoded.keys()
        if forbidden:
            raise ValueError(
                "User simulator identity is operator-owned; forbidden overrides: "
                + ", ".join(sorted(forbidden))
            )
        llm_args: dict[str, Any] = decoded"""
    if old_override not in server_text:
        raise RuntimeError("blocked:tau_user_simulator_override_contract_drift")
    server_text = server_text.replace(old_override, new_override, 1)
    old_state = '            "start_tool_called": self.start_tool_called,'
    new_state = """            "start_tool_called": self.start_tool_called,
            "user_simulator": {
                "provider": "openai",
                "model": os.environ["TAU2_USER_MODEL"],
                "base_url": os.environ["OPENAI_BASE_URL"],
            },"""
    if old_state not in server_text:
        raise RuntimeError("blocked:tau_runtime_state_contract_drift")
    server_text = server_text.replace(old_state, new_state, 1)
    old_mcp_tool = """@mcp.tool()
def configure_run(
    seed: int | None = None,
    max_steps: int | None = None,
    max_errors: int | None = None,
    user_llm_args_json: str | None = None,
) -> str:
    \"\"\"Configure tau2 run parameters before the first conversation turn.\"\"\"
    return runtime.configure_run(
        seed=seed,
        max_steps=max_steps,
        max_errors=max_errors,
        user_llm_args_json=user_llm_args_json,
    )"""
    new_mcp_tool = """@mcp.tool()
def configure_run(
    seed: int | None = None,
    max_steps: int | None = None,
    max_errors: int | None = None,
) -> str:
    \"\"\"Configure bounded run counters before the first conversation turn.\"\"\"
    return runtime.configure_run(
        seed=seed,
        max_steps=max_steps,
        max_errors=max_errors,
    )"""
    if old_mcp_tool not in server_text:
        raise RuntimeError("blocked:tau_configure_run_tool_contract_drift")
    server_text = server_text.replace(old_mcp_tool, new_mcp_tool, 1)
    compile(server_text, str(server_path), "exec")
    server_path.write_text(server_text, encoding="utf-8")

    sidecar_dest = runtime_dir / "wheelhouse"
    _copy_wheelhouse(wheelhouse, sidecar_dest)
    _write_build_proof(runtime_dir, sidecar_dest, "wheelhouse/requirements.txt")

    commit = manifest["required_upstream"]["commit"]
    _registered_simulator_base_url(manifest)
    sidecar_dockerfile = f"""FROM {PYTHON_BASE_IMAGE}

ARG TAU2_BENCH_COMMIT="{commit}"
ENV PYTHONUNBUFFERED=1
ENV TAU2_DATA_DIR=/usr/local/lib/python3.12/site-packages/tau2_bench_data/data
ENV TAU3_RUNTIME_STATE_PATH={RUNTIME_STATE_PATH}
ENV TAU3_SIMULATOR_SCHEME=https
ENV TAU3_SIMULATOR_AUTHORITY=api.openai.com
ENV TAU3_SIMULATOR_BASE_PATH=/v1
ENV OPENAI_BASE_URL=${{TAU3_SIMULATOR_SCHEME}}://${{TAU3_SIMULATOR_AUTHORITY}}${{TAU3_SIMULATOR_BASE_PATH}}
ENV TAU2_USER_MODEL={SIMULATOR_MODEL}
WORKDIR /app
COPY wheelhouse /wheelhouse
RUN pip install --no-index --find-links=/wheelhouse -r /wheelhouse/requirements.txt --require-hashes
COPY server.py /app/server.py
COPY task_config.json /app/task_config.json
CMD ["python3", "/app/server.py"]
"""
    (runtime_dir / "Dockerfile").write_text(sidecar_dockerfile, encoding="utf-8")

    (task_dir / "environment" / "docker-compose.yaml").write_text(
        _generate_docker_compose(manifest), encoding="utf-8"
    )

    # The main build context includes the sidecar wheelhouse, so provide a
    # top-level build proof that covers every wheel visible under environment/.
    _write_build_proof(
        task_dir / "environment",
        sidecar_dest,
        "runtime-server/wheelhouse/requirements.txt",
    )


def validate_agent_boundary(
    task_dir: Path,
    manifest: Mapping[str, Any],
    *,
    credential_environment: Mapping[str, str] | None = None,
) -> None:
    """Reject credential values, hidden-file aliases, and duplicated oracle spans."""
    visible_paths = [
        task_dir / "task.toml",
        task_dir / "instruction.md",
        task_dir / "instructions.md",
    ]
    agent_dockerfile = task_dir / "environment/Dockerfile"
    if agent_dockerfile.is_file():
        visible_paths.append(agent_dockerfile)
    source_environment = os.environ if credential_environment is None else credential_environment
    credential_values = [
        value
        for name in manifest["credentials"]["simulated_user"]["required_env"]
        if len(value := (source_environment.get(name) or "").strip()) >= 8
    ]

    visible_text: list[str] = []
    for path in visible_paths:
        if path.is_symlink():
            raise RuntimeError(f"blocked:agent_visible_symlink:{path.relative_to(task_dir)}")
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        visible_text.append(text)
        for credential_value in credential_values:
            if credential_value in text:
                raise RuntimeError(
                    f"blocked:simulator_credential_value_leak:{path.relative_to(task_dir)}"
                )

    combined_visible = "\n".join(visible_text)
    tests_root = task_dir / "tests"
    if not tests_root.is_dir():
        return
    for path in sorted(tests_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name != "config.json" and not any(
            token in path.name.lower() for token in ("golden", "expected", "answer")
        ):
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            normalized = " ".join(line.strip().split())
            if (
                len(normalized) >= 32
                and not normalized.startswith(("#", "//", "/*", "*"))
                and normalized in combined_visible
            ):
                raise RuntimeError(f"blocked:oracle_boundary_leak:{path.relative_to(task_dir)}")


def materialize(
    *,
    manifest_path: Path,
    source_root: Path,
    adapter_root: Path,
    output_root: Path,
    task_id: str,
    overwrite: bool = False,
) -> Path:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = {str(row["task_id"]): row for row in manifest["tasks"]}
    if task_id not in rows:
        raise RuntimeError(f"task is not selected by immutable cohort: {task_id}")
    source = validate_source(source_root, manifest)
    adapter = _validate_adapter(adapter_root, manifest)
    destination = output_root.expanduser().resolve() / source_digest(manifest)
    task_dir = destination / f"tau3-banking_knowledge-{task_id.replace('_', '-')}"
    if destination.exists():
        if not overwrite:
            raise RuntimeError(
                f"materialization exists; pass --overwrite to replace: {destination}"
            )
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    adapter_type = _load_adapter(adapter)
    adapter_type(destination, overwrite=False, task_ids=[task_id], tau2_root=source).run()
    if not (task_dir / "task.toml").is_file() or not (task_dir / "tests/config.json").is_file():
        raise RuntimeError(f"adapter did not produce complete task: {task_dir}")

    with tempfile.TemporaryDirectory(prefix="tau-wheelhouse-") as temp:
        wheelhouse = _build_wheelhouse(source, adapter, Path(temp))
        harden_agent_environment(task_dir)
        _normalize_task_metadata(task_dir)
        harden_oracle_solution(task_dir)
        harden_verifier_environment(task_dir, manifest, wheelhouse)
        harden_sidecar_environment(task_dir, manifest, wheelhouse)
        _create_workbench_controls(task_dir)
        validate_agent_boundary(task_dir, manifest)

    metadata = {
        "schema_version": "tau-knowledge-materialization/v1",
        "benchmark": "tau-Knowledge",
        "task_id": task_id,
        "source_digest": "sha256:" + source_digest(manifest),
        "source_commit": manifest["required_upstream"]["commit"],
        "source_task_digest": rows[task_id]["task_sha256"],
        "adapter_commit": manifest["adapter_evidence"]["commit"],
        "generated_task": task_dir.name,
    }
    (destination / "materialization.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", default="task_001")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--source", type=Path, default=None)
    parser.add_argument("--adapter", type=Path, default=None)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    source = args.source or (
        Path(os.environ["TAU2_BENCH_ROOT"]) if os.environ.get("TAU2_BENCH_ROOT") else None
    )
    adapter = args.adapter or (
        Path(os.environ["TAU3_BENCH_ADAPTER_ROOT"])
        if os.environ.get("TAU3_BENCH_ADAPTER_ROOT")
        else None
    )
    if source is None or adapter is None:
        raise SystemExit("blocked: TAU2_BENCH_ROOT and TAU3_BENCH_ADAPTER_ROOT are required")
    print(
        materialize(
            manifest_path=args.manifest,
            source_root=source,
            adapter_root=adapter,
            output_root=args.output_root,
            task_id=args.task_id,
            overwrite=args.overwrite,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        print(f"tau-Knowledge materialization refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
