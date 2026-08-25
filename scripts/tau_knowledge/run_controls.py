#!/usr/bin/env python3
"""Fail-closed tau-Knowledge control/Luna sequencer.

The existing Harbor tau3 adapter is intentionally imported from its pinned
checkout; this script never copies or reimplements that adapter. It validates
an externally pinned tau2 source and the immutable cohort before invoking any
trial. Luna is unreachable until every control for the task has a passing
status record.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def _load_preflight() -> Any:
    """Load the sibling preflight module without a package."""
    module_path = Path(__file__).with_name("preflight.py")
    spec = importlib.util.spec_from_file_location("tau_knowledge_preflight_rc", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load tau-knowledge preflight module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


preflight = _load_preflight()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _load(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"expected mapping: {path}")
    return value


def _manifest(config_path: Path, config: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = (config_path.parent / str(config["cohort_manifest"])).resolve()
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("immutable") is not True:
        raise RuntimeError("cohort manifest is not immutable")
    if value.get("benchmark") != "tau-Knowledge":
        raise RuntimeError("unexpected benchmark in cohort manifest")
    tasks = value.get("tasks")
    selected = value.get("selection", {}).get("task_ids")
    if not isinstance(tasks, list) or not isinstance(selected, list) or len(tasks) != len(selected):
        raise RuntimeError("cohort selection is not deterministic")
    if [row.get("task_id") for row in tasks] != selected:
        raise RuntimeError("cohort task order does not match manifest selection")
    return path, value


def _validate_pin(config_path: Path, config: dict[str, Any], manifest: dict[str, Any]) -> Path:
    root_value = os.environ.get(str(config["upstream_root_env"]))
    if not root_value:
        raise RuntimeError(f"{config['upstream_root_env']} must point at the pinned tau2 checkout")
    root = Path(root_value).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError(f"missing upstream checkout: {root}")
    expected = config["upstream_required"]
    commit = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    if commit != expected["commit"]:
        raise RuntimeError(f"upstream commit mismatch: expected {expected['commit']}, got {commit}")
    tag = subprocess.run(
        ["git", "-C", str(root), "describe", "--tags", "--exact-match", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if tag != expected["tag"]:
        raise RuntimeError(f"upstream tag mismatch: expected {expected['tag']}, got {tag or '<none>'}")
    task_file = root / "data/tau2/domains/banking_knowledge/tasks"
    for row in manifest["tasks"]:
        path = task_file / f"{row['task_id']}.json"
        if not path.is_file() or _sha256(path) != row["task_sha256"]:
            raise RuntimeError(f"source task digest mismatch: {path}")
    required_file = root / manifest["required_upstream"]["tasks_file"]["path"]
    if _sha256(required_file) != manifest["required_upstream"]["tasks_file"]["sha256"]:
        raise RuntimeError("source tasks.json digest mismatch")
    return root


def _validate_generated(config_path: Path, config: dict[str, Any], manifest: dict[str, Any]) -> Path:
    generated = (config_path.parent / str(config["generated_tasks"])).resolve()
    for row in manifest["tasks"]:
        path = generated / f"tau3-banking_knowledge-{row['task_id'].replace('_', '-')}"
        if not (path / "task.toml").is_file() or not (path / "tests/config.json").is_file():
            raise RuntimeError(f"generated task is incomplete: {path}")
        dockerfile = path / "environment/runtime-server/Dockerfile"
        content = dockerfile.read_text(encoding="utf-8")
        expected = manifest["required_upstream"]["commit"]
        if f'--branch "{manifest["required_upstream"]["release_tag"]}"' not in content or expected not in content:
            raise RuntimeError(f"runtime source is not pinned: {dockerfile}")
    evidence = generated.parent / "runtime-pin-evidence.json"
    if not evidence.is_file():
        raise RuntimeError("runtime pin evidence is missing")
    return generated


def _status_path(config_path: Path, config: dict[str, Any]) -> Path:
    root = (config_path.parent / str(config["outputs"]["root"])).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / str(config["outputs"]["controls_status"])


def _preflight_path(config_path: Path, config: dict[str, Any]) -> Path:
    root = (config_path.parent / str(config["outputs"]["root"])).resolve()
    root.mkdir(parents=True, exist_ok=True)
    filename = config.get("outputs", {}).get("credential_preflight", "credential-preflight.json")
    return root / filename


def _load_status(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _run(command: list[str], *, task_path: Path, timeout: int, env: dict[str, str]) -> None:
    rendered = preflight.render_command(command, task_path=task_path, env=env)
    subprocess.run(rendered, check=True, timeout=timeout, env=env)


def _resolve_luna_agent(env: dict[str, str], step: dict[str, Any]) -> str:
    """Return the Luna agent selector, defaulting to PinnedCodex."""
    agent = env.get("LUNA_AGENT")
    if agent:
        return agent
    agent = step.get("agent")
    if agent and not agent.startswith("$"):
        return agent
    return preflight.DEFAULT_LUNA_AGENT


def _render_plan(
    phase: str,
    tasks: list[Path],
    decision: Any,
    execute: bool,
) -> str:
    return json.dumps(
        {
            "phase": phase,
            "tasks": [str(path) for path in tasks],
            "execute": execute,
            "preflight": decision.to_dict(),
        },
        indent=2,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("library/benchmarks/tau-knowledge/config/run.yaml"))
    parser.add_argument("--phase", choices=["reference", "oracle", "clean_reset_repetition", "luna"], default="reference")
    parser.add_argument("--execute", action="store_true", help="invoke Harbor trials; otherwise validate and print plan")
    args = parser.parse_args()
    config_path = args.config.resolve()
    config = _load(config_path)
    _, manifest = _manifest(config_path, config)
    status_path = _status_path(config_path, config)
    status = _load_status(status_path)
    step = next(item for item in config["sequence"] if item["id"] == args.phase)
    if args.phase == "luna":
        required = set(step.get("requires", []))
        for row in manifest["tasks"]:
            task_state = status.get(row["task_id"], {})
            missing = sorted(required - {name for name, value in task_state.items() if value == "passed"})
            if missing:
                raise RuntimeError(f"Luna gate closed for {row['task_id']}; missing controls: {', '.join(missing)}")
    generated = _validate_generated(config_path, config, manifest)
    tasks = [generated / f"tau3-banking_knowledge-{row['task_id'].replace('_', '-')}" for row in manifest["tasks"]]

    home = Path.home()
    luna_agent = _resolve_luna_agent(os.environ, step) if args.phase == "luna" else None
    decision = preflight.preflight_tau_phase(
        args.phase,
        env=os.environ,
        home=home,
        config=config,
        agent=luna_agent,
    )

    if not decision.proceed:
        _preflight_path(config_path, config).write_text(
            json.dumps(decision.to_dict(), indent=2) + "\n",
            encoding="utf-8",
        )
        raise RuntimeError(f"{decision.detail} ({decision.reason_code})")

    if not args.execute:
        print(_render_plan(args.phase, tasks, decision, execute=False))
        return 0

    _validate_pin(config_path, config, manifest)
    repo_root = Path(__file__).resolve().parents[2]
    child_env = preflight.build_child_env(
        args.phase,
        env=os.environ,
        home=home,
        repo_root=repo_root,
        adapter_pythonpath=config.get("adapter_pythonpath"),
        luna_agent=luna_agent,
    )

    timeout = int(config["limits"]["timeout_seconds"])
    for task_path in tasks:
        _run(step["command"], task_path=task_path, timeout=timeout, env=child_env)
        task_id = "task_" + task_path.name.rsplit("-", 1)[-1]
        status.setdefault(task_id, {})[args.phase] = "passed"
        status_path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(f"tau-Knowledge run refused: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
