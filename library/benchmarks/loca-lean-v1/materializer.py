"""Materialize one LOCA canary under ignored derived Harbor output."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from source import Pin, PinError, fetch_pinned, load_pins, source_digest, verify_cache
from state import CANARY, INSTRUCTION, SIZES, SEEDS, build_state

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
DERIVED = REPO / "derived" / "harbor-tasks" / "loca"
PACKAGE_NAME = "loca-abtesting-8k-seed42"
SOURCE_CACHE = REPO / "derived" / "harbor-source-cache" / "loca"


def _acquire(cache_dir: Path) -> dict[str, Path]:
    record, pins = load_pins()
    license_pin = Pin("LICENSE", record["license"]["url"], record["license"]["sha256"], "MIT")
    entries = (license_pin, *pins)
    return {pin.name: fetch_pinned(pin, cache_dir, offline=False) for pin in entries}


def output_path() -> Path:
    record, pins = load_pins()
    digest = source_digest(record, pins)
    return DERIVED / digest / PACKAGE_NAME

def _safe_output(path: Path, digest: str) -> Path:
    resolved = path.resolve()
    expected_parent = (DERIVED / digest).resolve()
    if resolved.parent != expected_parent or resolved.name != PACKAGE_NAME:
        raise PinError(f"generated output must be exactly {expected_parent / PACKAGE_NAME}")
    return resolved
def _write_harbor_package(target: Path, digest: str, manifest: dict[str, object]) -> None:
    environment = target / "environment"
    solution = target / "solution"
    tests = target / "tests"
    environment.mkdir()
    solution.mkdir()
    tests.mkdir()
    source_root = Path(__file__).resolve().parent
    task_state = environment / "task_state"
    verifier_state = tests / "task_state"
    for name in ("files", "agent_workspace", "local_db"):
        shutil.copytree(target / name, task_state / name)
        shutil.copytree(target / name, verifier_state / name)
    shutil.copy2(target / "state_manifest.json", task_state / "state_manifest.json")
    shutil.copy2(target / "state_manifest.json", verifier_state / "state_manifest.json")
    shutil.copy2(source_root / "runtime.py", environment / "runtime.py")
    shutil.copy2(source_root / "oracle.py", environment / "oracle.py")
    shutil.copy2(source_root / "templates.py", environment / "templates.py")
    shutil.copy2(source_root / "verifier.py", tests / "verifier.py")
    shutil.copy2(source_root / "templates.py", tests / "templates.py")
    (environment / "Dockerfile").write_text(
        "FROM python:3.12-slim\nWORKDIR /app\nENV NETWORK_MODE=no-network\nCOPY . /app\nENTRYPOINT [\"/app/entrypoint.sh\"]\n", encoding="utf-8"
    )
    (environment / "entrypoint.sh").write_text(
        "#!/bin/sh\nset -eu\npython3 /app/runtime.py --task-dir /app/task_state\nif [ \"$#\" -gt 0 ]; then exec \"$@\"; fi\nexec sleep infinity\n", encoding="utf-8"
    )
    (environment / "entrypoint.sh").chmod(0o755)
    (environment / "service-config.json").write_text(
        json.dumps({"type": "google_cloud", "data_dir": "/app/task_state/local_db/google_cloud", "network_mode": "no-network"}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (solution / "solve.sh").write_text(
    (tests / "Dockerfile").write_text("FROM python:3.12-slim\nWORKDIR /tests\nCOPY . /tests\nCMD [\"sleep\", \"infinity\"]\n", encoding="utf-8")
    (tests / "test.sh").write_text(
        "#!/bin/sh\nset -eu\nmkdir -p /logs/verifier\nexec python3 /tests/verifier.py --task-dir /tests/task_state --workspace /app/task_state/agent_workspace --reward-dir /logs/verifier\n", encoding="utf-8"
    )
    (tests / "test.sh").chmod(0o755)
    (target / "task.toml").write_text(
        f'''schema_version = "1.4"\nartifacts = ["/app/task_state/agent_workspace/record.csv", "/app/task_state/agent_workspace/promo-assets-for-b.marker"]\n\n[task]\nversion = "1.0.0"\nname = "loca-bench/ab-testing-seed-42-8k"\ndescription = "Pinned LOCA ABTestingS2LEnv canary."\n[metadata]\nauthor_name = "LOCA-bench Contributors"\ncategory = "long-context-agents"\ntags = ["loca", "mcp", "context-growth"]\nsource_ref = "8b6fac49d9edd92922593e703b74ea255357c3ec"\nlicense = "MIT"\n[verifier]\ntimeout_sec = 900.0\nenvironment_mode = "separate"\n[agent]\ntimeout_sec = 7200.0\n[environment]\nnetwork_mode = "none"\nmcp_servers = [{{ name = "google_cloud", transport = "stdio", command = "python3", args = ["-m", "mcps.google_cloud.server"] }}]\n''', encoding="utf-8"
    )
    task_toml = target / "task.toml"
    task_toml.write_text(task_toml.read_text(encoding="utf-8").replace('network_mode = "none"', 'network_mode = "no-network"'), encoding="utf-8")
    (target / "harbor-task.json").write_text(
        json.dumps({"benchmark": "LOCA-bench", "source_digest": digest, "task": "loca-abtesting-8k-seed42", "state": manifest["state_strategy"], "network_mode": "none", "runtime": "shared benchmark runtime.py", "verifier": "shared benchmark verifier.py"}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    harbor_json = target / "harbor-task.json"
    harbor_json.write_text(harbor_json.read_text(encoding="utf-8").replace('"network_mode": "none"', '"network_mode": "no-network"'), encoding="utf-8")
    (target / "instruction.md").write_text(INSTRUCTION, encoding="utf-8")
def materialize(
    output_dir: Path | None = None,
    *,
    size: str = CANARY[0],
    seed: int = CANARY[1],
    cache_dir: Path | None = None,
    verify_sources: bool = True,
) -> dict:
    """Rebuild one canary from verified pinned inputs."""
    if (size, seed) != CANARY:
        raise ValueError("lean LOCA materializer exposes exactly the 8k seed-42 canary")
    record, pins = load_pins()
    digest = source_digest(record, pins)
    target = _safe_output(output_dir or output_path(), digest)
    source_paths: dict[str, Path] = {}
    if verify_sources:
        source_paths = _acquire(cache_dir or SOURCE_CACHE)
        verify_cache(cache_dir or SOURCE_CACHE, offline=True)
    config_path = source_paths.get("final_8k_set_config.json")
    if config_path is not None:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        canary_configs = [
            item for item in config.get("configurations", [])
            if item.get("name") == "ABTestingS2LEnv" and item.get("env_params", {}).get("seed") == seed
        ]
        if len(canary_configs) != 1 or canary_configs[0]["env_params"].get("num_scenarios") != 10 or canary_configs[0]["env_params"].get("num_days") != 6:
            raise PinError("pinned 8k config does not contain the expected ABTestingS2LEnv canary")
    if target.exists():
        shutil.rmtree(target)
    files = target / "files"
    workspace = target / "agent_workspace"
    local_db = target / "local_db" / "google_cloud"
    files.mkdir(parents=True)
    workspace.mkdir()
    local_db.mkdir(parents=True)
    generator_path = source_paths.get("generate_ab_data.py")
    schema_path = source_paths.get("mcp_tool_schemas.json")
    clickstream, environment, manifest = build_state(
        size=size, seed=seed, source_digest=digest, generator_path=generator_path, tool_schemas_path=schema_path
    )
    if verify_sources and manifest["environment_description_bytes"] != 32674:
        raise PinError(f"pinned canary context bytes drifted: {manifest['environment_description_bytes']} != 32674")
    if verify_sources and manifest["state_digest"] != "sha256:829bba54bad9ca179d3fc3c03f2d6737dfc4e1fc91f22c94cef73d7f4f4b2d9d":
        raise PinError("pinned canary state digest drifted from preserved PR #168 evidence")
    (files / "clickstream.csv").write_bytes(clickstream)
    (files / "environment_description.json").write_bytes(environment)
    (workspace / "record.csv").write_text("scenario,A_conversion %,B_conversion %\n", encoding="utf-8")
    (local_db / "database.json").write_text(
        json.dumps({"project": "local-project", "dataset": "ab_testing", "table": "clickstream", "row_count": manifest["row_count"]}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    manifest.update({
        "materialized_at": "source-digest-addressed",
        "output_relpath": f"derived/harbor-tasks/loca/{digest}/{PACKAGE_NAME}",
        "source_pins_verified": bool(verify_sources),
        "legacy_8k_seed42_state_digest": "sha256:829bba54bad9ca179d3fc3c03f2d6737dfc4e1fc91f22c94cef73d7f4f4b2d9d",
        "legacy_8k_seed42_database_digest": "sha256:38060fd9580ad0fc08069d01e442807f984405fa0ee7f8a2b363da5b5a5aeb02",
    })
    (target / "state_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_harbor_package(target, digest, manifest)
    return manifest


def reject_committed_corpora(root: Path = REPO) -> None:
    """Fail CI if any superseded/generated LOCA corpus is reintroduced."""
    forbidden_roots = (
        root / "library/benchmarks/loca-bench/tasks",
        root / "library/benchmarks/loca-bench/vendor",
        root / "library/benchmarks/loca-bench/source_configs",
        root / "library/benchmarks/loca-lean-v1/tasks",
        root / "library/benchmarks/loca-lean-v1/vendor",
        root / "library/benchmarks/loca-lean-v1/source_configs",
    )
    forbidden = [str(path) for base in forbidden_roots if base.exists() for path in base.iterdir()]
    if forbidden:
        raise AssertionError("LOCA generated/vendor corpus is forbidden: " + ", ".join(forbidden))
