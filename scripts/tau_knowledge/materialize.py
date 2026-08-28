#!/usr/bin/env python3
"""Materialize selected Tau tasks under ignored derived/harbor-tasks only."""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

DEFAULT_MANIFEST = Path("library/benchmarks/tau-knowledge/cohort.manifest.json")
DEFAULT_OUTPUT = Path("derived/harbor-tasks/tau")
PYTHON_BASE_IMAGE = (
    "python:3.12-slim@sha256:"
    "09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217"
)
AGENT_DOCKERFILE = f"""FROM {PYTHON_BASE_IMAGE}

WORKDIR /app
RUN apt-get update \\
    && apt-get install -y --no-install-recommends ca-certificates git \\
    && rm -rf /var/lib/apt/lists/*
"""

try:
    from preflight import sha256, validate_source
except ImportError as exc:
    _spec = importlib.util.spec_from_file_location(
        "tau_knowledge_preflight", Path(__file__).with_name("preflight.py")
    )
    if _spec is None or _spec.loader is None:
        raise RuntimeError("cannot load Tau preflight") from exc
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)
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
        raise RuntimeError(
            f"blocked:adapter_commit_mismatch:expected={expected}:actual={actual}"
        )
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


def harden_agent_environment(task_dir: Path) -> None:
    """Remove simulator secrets and benchmark source data from the agent service."""
    task_config = task_dir / "task.toml"
    lines = task_config.read_text(encoding="utf-8").splitlines()
    section = ""
    rewritten: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped
        if section == "[environment]" and stripped.startswith("env ="):
            rewritten.append("env = {}")
        else:
            rewritten.append(line)
    task_config.write_text("\n".join(rewritten) + "\n", encoding="utf-8")

    agent_dockerfile = task_dir / "environment/Dockerfile"
    agent_dockerfile.write_text(AGENT_DOCKERFILE, encoding="utf-8")


def harden_verifier_environment(task_dir: Path, manifest: Mapping[str, Any]) -> None:
    """Move Tau evaluator dependencies into a verifier-only container."""
    task_config = task_dir / "task.toml"
    text = task_config.read_text(encoding="utf-8")
    if "artifacts =" not in text:
        text = text.replace(
            'schema_version = "1.1"\n',
            'schema_version = "1.1"\n'
            'artifacts = ["/app/tau3_runtime_state.json"]\n',
            1,
        )
    text = text.replace(
        "[verifier]\n",
        '[verifier]\nenvironment_mode = "separate"\n',
        1,
    )
    text = text.replace(
        "\n[agent]\n",
        '\n[[verifier.collect]]\n'
        'command = "if [ ! -f /app/tau3_runtime_state.json ] '
        "&& [ -f /logs/agent/tau3_runtime_state.json ]; then "
        "cp /logs/agent/tau3_runtime_state.json "
        '/app/tau3_runtime_state.json; fi"\n'
        'service = "main"\n\n'
        '[verifier.environment]\nnetwork_mode = "no-network"\n\n[agent]\n',
        1,
    )
    task_config.write_text(text, encoding="utf-8")

    commit = manifest["required_upstream"]["commit"]
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
    (task_dir / "tests/Dockerfile").write_text(dockerfile, encoding="utf-8")

    evaluate_path = task_dir / "tests/evaluate.py"
    evaluate_text = evaluate_path.read_text(encoding="utf-8")
    runtime_log_declaration = (
        'DEFAULT_RUNTIME_LOG_PATH = Path("/logs/agent/tau3_runtime_state.json")'
    )
    runtime_log_resolution = (
        'DEFAULT_RUNTIME_LOG_PATH = Path("/app/tau3_runtime_state.json")'
    )
    if runtime_log_declaration not in evaluate_text:
        raise RuntimeError("blocked:tau_verifier_runtime_log_contract_drift")
    evaluate_path.write_text(
        evaluate_text.replace(
            runtime_log_declaration,
            runtime_log_resolution,
            1,
        ),
        encoding="utf-8",
    )

    test_path = task_dir / "tests/test.sh"
    test_text = test_path.read_text(encoding="utf-8")
    runtime_log_argument = "--runtime-log /logs/agent/tau3_runtime_state.json"
    if runtime_log_argument not in test_text:
        raise RuntimeError("blocked:tau_verifier_test_entrypoint_contract_drift")
    test_path.write_text(
        test_text.replace(
            runtime_log_argument,
            "--runtime-log /app/tau3_runtime_state.json",
            1,
        ),
        encoding="utf-8",
    )


def harden_oracle_solution(task_dir: Path) -> None:
    """Build the oracle runtime log without installing Tau data in the agent image."""
    config = json.loads((task_dir / "tests/config.json").read_text(encoding="utf-8"))
    task = config["task"]
    tool_results = {
        "apply_for_credit_card": (
            "Credit card application submitted:\n"
            "Your application has been successfully submitted. "
            "You will receive a decision within 5-7 business days via email."
        )
    }
    unsupported_actions = sorted(
        {
            str(action["name"])
            for action in config.get("expected_actions") or []
            if action["name"] not in tool_results
        }
    )
    if unsupported_actions:
        raise RuntimeError(
            "blocked:tau_oracle_result_contract_missing:"
            + ",".join(unsupported_actions)
        )
    simulation = {
        "actions": config.get("expected_actions") or [],
        "communicate_info": config.get("expected_communicate_info") or [],
        "initialization_data": task.get("initial_state"),
        "initialization_actions": task.get("initialization_actions") or [],
        "tool_results": tool_results,
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

state_path = Path("/app/tau3_runtime_state.json")
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
                raise RuntimeError(
                    f"blocked:oracle_boundary_leak:{path.relative_to(task_dir)}"
                )


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
    adapter_type(
        destination, overwrite=False, task_ids=[task_id], tau2_root=source
    ).run()
    if not (task_dir / "task.toml").is_file() or not (
        task_dir / "tests/config.json"
    ).is_file():
        raise RuntimeError(f"adapter did not produce complete task: {task_dir}")
    harden_agent_environment(task_dir)
    harden_oracle_solution(task_dir)
    harden_verifier_environment(task_dir, manifest)
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
        Path(os.environ["TAU2_BENCH_ROOT"])
        if os.environ.get("TAU2_BENCH_ROOT")
        else None
    )
    adapter = args.adapter or (
        Path(os.environ["TAU3_BENCH_ADAPTER_ROOT"])
        if os.environ.get("TAU3_BENCH_ADAPTER_ROOT")
        else None
    )
    if source is None or adapter is None:
        raise SystemExit(
            "blocked: TAU2_BENCH_ROOT and TAU3_BENCH_ADAPTER_ROOT are required"
        )
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
