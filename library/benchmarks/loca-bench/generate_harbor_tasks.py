"""Single source of truth for the nine LOCA Harbor task packages.

This generator:

1. Copies agent-side files into ``environment/`` and verifier-side files
   into ``tests/`` for each of the nine ``ab-testing-seed-{seed}-{size}``
   packages.
2. Uses ``adapter.materialize()`` (the same adapter the runtime uses) to
   create the verifier-only golden state under ``tests/golden/``.  Expected
   conversion ratios are baked into the verifier image and never appear in
   the agent-visible tree.
3. Refreshes ``harbor-loca-config.json``, ``REALIZED_SIZE_EVIDENCE.json``,
   and ``SOURCE_MANIFEST.json`` so the repository inventory stays coherent.

Security:

- Agent/model artifacts become visible to the separate verifier because
  ``task.toml`` declares only the workspace outputs (``record.csv`` and the
  optional ``promo-assets-for-b.marker``) and Harbor rematerializes declared
  artifacts at their original source paths.
- Golden expected state stays hidden from the agent because it lives only in
  ``tests/``, which is the verifier-image build context and is not copied
  into the agent image built from ``environment/``.
- Separate-container isolation stays on (``environment_mode = "separate"``).
- The agent still has to compute the answer from the clickstream/MCP data.
  The vendored ``save_expected_ratio`` method remains in the agent image
  (inside ``vendor/loca_upstream``) because it is part of the pinned LOCA
  generator that produces the task; that is an intentional residual risk
  that the task accepts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Never write pyc files while generating, so committed hashes stay stable.
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor" / "loca_upstream"
TASKS_DIR = ROOT / "tasks"
SIZES = ("8k", "64k", "128k")
SEEDS = (42, 123, 456)


def _sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_file(path: Path, content: str, *, executable: bool = False) -> None:
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def _copy_text_file(src: Path, dst: Path) -> None:
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def _copy_tree_no_pyc(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


# Static templates and file content.
_ENV_DOCKERFILE = """FROM python:3.12-slim
RUN pip install --no-cache-dir fastmcp==2.12.4 pyyaml==6.0.2
WORKDIR /adapter
COPY . /adapter
ENV PYTHONPATH=/adapter/vendor/loca_upstream/mcp_convert
ENTRYPOINT ["/adapter/entrypoint.sh"]
"""

_TESTS_DOCKERFILE = """FROM python:3.12-slim
WORKDIR /tests
COPY . /tests
"""

_TEST_SH = """#!/bin/sh
set -eu
mkdir -p /logs/verifier
out=/logs/verifier/verify.json
reward=0
if python3 /tests/verify.py --workspace /app/task_state/agent_workspace --golden-dir /tests/golden > "$out" 2>&1; then
  reward=1
fi
cat "$out"
printf '%s\\n' "$reward" > /logs/verifier/reward.txt
"""

_SERVICE_CONFIG = """{
  "type": "google_cloud",
  "command": "python3 -m mcps.google_cloud.server",
  "data_dir": "/app/task_state/local_db/google_cloud",
  "network_mode": "no-network"
}
"""

_SOLUTION_SOLVE_SH = """#!/bin/sh
set -eu
python3 /adapter/oracle.py --task-dir "${LOCA_TASK_DIR:-/app/task_state}" --workspace "${LOCA_AGENT_WORKSPACE:-/app/task_state/agent_workspace}"
"""


def _entrypoint_sh(size: str, seed: int) -> str:
    return f"""#!/bin/sh
set -eu
python3 /adapter/adapter.py --task-dir /app/task_state --size {size} --seed {seed}
export LOCA_TASK_ROOT=/app/task_state
exec "$@"
"""


def _task_toml(size: str, seed: int, configured_tokens: int) -> str:
    return f"""schema_version = "1.4"

artifacts = [
  "/app/task_state/agent_workspace/record.csv",
  "/app/task_state/agent_workspace/promo-assets-for-b.marker",
]

[task]
version = "1.0.0"
name = "loca-bench/ab-testing-seed-{seed}-{size}"
description = "Official LOCA-bench ABTestingS2LEnv seed {seed}, generated at {size}."
[metadata]
author_name = "LOCA-bench Contributors"
category = "long-context-agents"
tags = ["loca", "mcp", "bigquery", "context-growth"]
source_ref = "8b6fac49d9edd92922593e703b74ea255357c3ec"
license = "MIT"
[verifier]
timeout_sec = 900.0
environment_mode = "separate"
[agent]
timeout_sec = 7200.0
[environment]
network_mode = "public"
mcp_servers = [{{ name = "google_cloud", transport = "stdio", command = "python3", args = ["-m", "mcps.google_cloud.server"] }}]
env = {{ GOOGLE_CLOUD_DATA_DIR = "/app/task_state/local_db/google_cloud", LOCA_TASK_ROOT = "/app/task_state" }}
cpus = 2
memory_mb = 8192
storage_mb = 20480
"""


def _harbor_task_json(size: str, seed: int, configured_tokens: int) -> str:
    return json.dumps(
        {
            "benchmark": "LOCA-bench",
            "benchmark_version": "8b6fac49d9edd92922593e703b74ea255357c3ec",
            "sandbox_commit": "2b4a1c77bd65d83750372ee079a2e5c5d13cb27c",
            "official_task": "ABTestingS2LEnv",
            "official_seed": seed,
            "context_size": size,
            "mcp_servers": {
                "google_cloud": {
                    "type": "vendored-upstream-mcp_convert",
                    "command": "python3 -m mcps.google_cloud.server",
                    "data_dir": "{task_workspace}/local_db/google_cloud",
                }
            },
            "backend": {"kind": "native-m4", "proof": "../../native_backend.json"},
            "verifier": {"command": "python3 /tests/verify.py", "deterministic": True},
            "state": {
                "strategy": "upstream_generator_parameters",
                "configured_tokens": configured_tokens,
                "padding_only": False,
            },
        },
        indent=2,
        sort_keys=True,
    )


def _tests_test_outputs_py() -> str:
    return r"""from pathlib import Path
import importlib.util

def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pkg():
    p = Path(__file__).resolve().parents[1]
    parts = p.name.split("-")
    return p, int(parts[3]), parts[4]


def test_golden_present_and_task_toml_declares_artifacts():
    pkg, _seed, _size = _pkg()
    import tomllib

    data = tomllib.loads((pkg / "task.toml").read_text())
    assert data["verifier"]["environment_mode"] == "separate"
    artifacts = data["artifacts"]
    assert "/app/task_state/agent_workspace/record.csv" in artifacts
    assert "/app/task_state/agent_workspace/promo-assets-for-b.marker" in artifacts
    for a in artifacts:
        assert a.startswith("/app/task_state/agent_workspace/")
        assert "expected" not in a

    assert (pkg / "tests" / "golden" / "expected_record.csv").exists()
    assert (pkg / "tests" / "golden" / "manifest.json").exists()


def test_test_sh_writes_reward():
    pkg, _, _ = _pkg()
    text = (pkg / "tests" / "test.sh").read_text()
    assert "mkdir -p /logs/verifier" in text
    assert "/logs/verifier/reward.txt" in text


def test_no_verify_or_expected_in_environment():
    pkg, _, _ = _pkg()
    for p in (pkg / "environment").rglob("*"):
        if p.is_file():
            assert "expected_record" not in p.name
    assert not (pkg / "environment" / "verify.py").exists()


def test_materialize_verify_and_oracle():
    pkg, seed, size = _pkg()
    if size != "8k":
        return

    import tempfile

    env = _load("adapter", pkg / "environment" / "adapter.py")
    orc = _load("oracle", pkg / "environment" / "oracle.py")
    ver = _load("verify", pkg / "tests" / "verify.py")
    golden = pkg / "tests" / "golden"

    with tempfile.TemporaryDirectory() as tmp:
        task = Path(tmp) / "task"
        env.materialize(task, size, seed, golden_dir=golden)

        # No expected state in the agent-visible tree.
        for f in (task / "files").rglob("*"):
            if f.is_file():
                assert "expected_record" not in f.name
        assert not (task / "files" / "expected_record.csv").exists()
        assert not (task / "agent_workspace" / "expected_record.csv").exists()

        # Fresh header-only workspace verifies to 0.0 without raising.
        result = ver.verify(task_dir=task, golden_dir=golden)
        assert result["reward"] == 0.0
        assert result["assertions"]["record_exists"]
        assert not result["assertions"]["record_matches_upstream_oracle"]
        assert result["assertions"]["state_is_nonempty"]

        # Oracle computes the answer from clickstream and verifies to 1.0.
        orc.solve(task, task / "agent_workspace")
        result = ver.verify(task_dir=task, golden_dir=golden)
        assert result["reward"] == 1.0
        assert result["assertions"]["record_matches_upstream_oracle"]
        assert result["assertions"]["all_assertions"]
"""


# Package and environment instruction are identical.  Load it before any
# packages are deleted so regeneration remains idempotent even when a fresh
# package is the only copy of the text.
_INSTRUCTION_PATH = ROOT / "tasks" / "ab-testing-seed-42-8k" / "instruction.md"
if _INSTRUCTION_PATH.exists():
    INSTRUCTION_MD = _read_file(_INSTRUCTION_PATH)
else:
    INSTRUCTION_MD = ""


def _generate_size_evidence_row(manifest: dict) -> dict:
    return {
        "benchmark": manifest["benchmark"],
        "benchmark_version": manifest["benchmark_version"],
        "configured_size_tokens": manifest["configured_size_tokens"],
        "context_measurement": "serialized environment_description.json UTF-8 bytes / 4",
        "database_digest": manifest["database_digest"],
        "environment_description_bytes": manifest["environment_description_bytes"],
        "mcp_service": manifest["mcp_service"],
        "official_seed": manifest["official_seed"],
        "padding_only": manifest["padding_only"],
        "params": manifest["params"],
        "prompt_tokens": manifest["prompt_tokens"],
        "realized_context_tokens": manifest["realized_context_tokens"],
        "realized_state_bytes": manifest["realized_state_bytes"],
        "row_count": manifest["row_count"],
        "sandbox_commit": manifest["sandbox_commit"],
        "scenario_count": manifest["scenario_count"],
        "size": manifest["size"],
        "state_bytes": manifest["state_bytes"],
        "state_digest": manifest["state_digest"],
        "state_strategy": manifest["state_strategy"],
        "task_family": manifest["task_family"],
    }


def _generate_source_manifest() -> dict:
    # adapter_files: top-level .py source modules.
    adapter_files = {}
    for path in sorted(ROOT.glob("*.py")):
        if path.name == "__init__.py":
            continue
        adapter_files[path.name] = _sha_bytes(path.read_bytes())

    # official_config_files: the LOCA bench source configs.
    official_config_files = {}
    for path in sorted((ROOT / "source_configs").glob("final_*.json")):
        official_config_files[path.name] = _sha_bytes(path.read_bytes())

    # vendored_files: the pinned LOCA upstream source, excluding pyc caches.
    vendored_files = {}
    for path in sorted((ROOT / "vendor" / "loca_upstream").rglob("*")):
        if not path.is_file():
            continue
        if path.name.endswith(".pyc") or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT / "vendor" / "loca_upstream")
        vendored_files[str(Path("vendor/loca_upstream") / rel)] = _sha_bytes(path.read_bytes())

    return {
        "adapter_files": adapter_files,
        "benchmark": "LOCA-bench",
        "official_config_files": official_config_files,
        "policy": {
            "backend": "native-m4",
            "harbor_core_edits": False,
            "network": "none",
            "network_enforcement": "unavailable_on_docker_darwin",
            "network_mode": "public",
            "padding_only": False,
        },
        "selection": {
            "official_seeds": list(SEEDS),
            "official_task": "ABTestingS2LEnv",
            "selection_source": "task-configs/final_{8,64,128}k_set_config.json",
            "sizes": list(SIZES),
            "task_count": len(SEEDS) * len(SIZES),
        },
        "task_package_count": len(SEEDS) * len(SIZES),
        "upstream": {
            "config_blobs": {
                "final_128k_set_config.json": "298a3901be6ed75e242c26a6560b36790a3eceeb",
                "final_64k_set_config.json": "079a7ece036e367954355fac33b4130b4244f9b9",
                "final_8k_set_config.json": "402d407fccc844d76de0b5c3bea36ba27d9868b8",
            },
            "license": "MIT",
            "license_digest": "sha256:2bb75401bf1c737039d17c6defda15c2f37eb2b5eb62facbf6dea50bf72811c2",
            "main_commit": "8b6fac49d9edd92922593e703b74ea255357c3ec",
            "repository": "https://github.com/hkust-nlp/LOCA-bench",
            "sandbox_commit": "2b4a1c77bd65d83750372ee079a2e5c5d13cb27c",
        },
        "vendored_files": vendored_files,
    }


def _generate_harbor_loca_config(rows: list[dict]) -> dict:
    tasks = []
    for row in rows:
        seed = row["official_seed"]
        size = row["size"]
        configured = row["configured_size_tokens"]
        tasks.append(
            {
                "atif": "python3 emit_atif.py --task-dir $LOCA_TASK_DIR --trial-dir $LOCA_TRIAL_DIR",
                "backend": "native-m4",
                "configured_size_tokens": configured,
                "mcp_servers": {
                    "google_cloud": {
                        "command": "python3 -m mcps.google_cloud.server",
                        "data_dir": "$LOCA_TASK_DIR/local_db/google_cloud",
                        "type": "google_cloud",
                    }
                },
                "network_mode": "public",
                "official_seed": seed,
                "official_task": "ABTestingS2LEnv",
                "prepare": f"python3 adapter.py --task-dir $LOCA_TASK_DIR --size {size} --seed {seed}",
                "runtime_evidence": "python3 runtime_evidence.py --task-dir $LOCA_TASK_DIR --output $LOCA_TRIAL_DIR/runtime_evidence.json",
                "state_strategy": "upstream_generator_parameters",
                "task": f"tasks/ab-testing-seed-{seed}-{size}",
                "verifier": f"python3 verify.py --workspace $LOCA_TASK_DIR/agent_workspace --golden-dir tasks/ab-testing-seed-{seed}-{size}/tests/golden",
            }
        )
    return {
        "benchmark": "LOCA-bench",
        "network_enforcement": "unavailable_on_docker_darwin",
        "tasks": tasks,
        "upstream_main": "8b6fac49d9edd92922593e703b74ea255357c3ec",
        "upstream_sandbox": "2b4a1c77bd65d83750372ee079a2e5c5d13cb27c",
    }


def _emit_package(size: str, seed: int, manifest: dict, golden_dir: Path) -> Path:
    pkg = TASKS_DIR / f"ab-testing-seed-{seed}-{size}"
    if pkg.exists():
        shutil.rmtree(pkg)
    (pkg / "environment").mkdir(parents=True)
    (pkg / "tests" / "golden").mkdir(parents=True)
    (pkg / "solution").mkdir(parents=True)

    # Copy the agent-side source into environment/.
    for name in ("adapter.py", "oracle.py", "emit_atif.py", "runtime_evidence.py"):
        _copy_text_file(ROOT / name, pkg / "environment" / name)
    _copy_tree_no_pyc(VENDOR, pkg / "environment" / "vendor" / "loca_upstream")

    _write_file(pkg / "environment" / "Dockerfile", _ENV_DOCKERFILE)
    _write_file(pkg / "environment" / "entrypoint.sh", _entrypoint_sh(size, seed), executable=True)
    _write_file(pkg / "environment" / "instruction.md", INSTRUCTION_MD)
    _write_file(pkg / "environment" / "service-config.json", _SERVICE_CONFIG)

    # Copy the verifier-side source into tests/.
    _copy_text_file(ROOT / "verify.py", pkg / "tests" / "verify.py")
    _write_file(pkg / "tests" / "Dockerfile", _TESTS_DOCKERFILE)
    _write_file(pkg / "tests" / "test.sh", _TEST_SH, executable=True)
    _write_file(pkg / "tests" / "test_outputs.py", _tests_test_outputs_py())

    # Bake the golden expected state (verifier-only).
    _copy_text_file(golden_dir / "expected_record.csv", pkg / "tests" / "golden" / "expected_record.csv")
    _copy_text_file(golden_dir / "manifest.json", pkg / "tests" / "golden" / "manifest.json")

    # Package-level files.
    _write_file(pkg / "instruction.md", INSTRUCTION_MD)
    _write_file(pkg / "task.toml", _task_toml(size, seed, manifest["configured_size_tokens"]))
    _write_file(pkg / "harbor-task.json", _harbor_task_json(size, seed, manifest["configured_size_tokens"]))
    _write_file(pkg / "solution" / "solve.sh", _SOLUTION_SOLVE_SH, executable=True)

    return pkg


def generate(*, quiet: bool = False) -> list[dict]:
    """Regenerate all packages and return the realized-size evidence rows."""
    # Import the local adapter so the generator uses the exact same source
    # that is copied into the packages.
    from adapter import materialize

    rows: list[dict] = []
    for seed in SEEDS:
        for size in SIZES:
            with tempfile.TemporaryDirectory() as tmp:
                task_dir = Path(tmp) / "task"
                golden_dir = Path(tmp) / "golden"
                if not quiet:
                    print(f"generating ab-testing-seed-{seed}-{size} ...", flush=True)
                manifest = materialize(task_dir, size, seed, golden_dir=golden_dir)
                _emit_package(size, seed, manifest, golden_dir)
                rows.append(_generate_size_evidence_row(manifest))

    # Write the repository-level derived inventories.
    rows.sort(key=lambda r: (r["size"], r["official_seed"]))
    (ROOT / "REALIZED_SIZE_EVIDENCE.json").write_text(
        json.dumps({"rows": rows, "source": "materialize() generated state; exact serialized environment_description.json measured UTF-8 bytes/4"}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (ROOT / "SOURCE_MANIFEST.json").write_text(
        json.dumps(_generate_source_manifest(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (ROOT / "harbor-loca-config.json").write_text(
        json.dumps(_generate_harbor_loca_config(rows), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    # Clean up pyc caches created while importing the adapter inside the
    # generator process; they should not be part of the committed source.
    for pycache in ROOT.rglob("__pycache__"):
        if pycache.is_dir():
            shutil.rmtree(pycache)

    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Regenerate LOCA Harbor task packages.")
    parser.add_argument("--quiet", action="store_true", help="reduce progress output")
    args = parser.parse_args()
    rows = generate(quiet=args.quiet)
    print(f"generated {len(rows)} task packages")


if __name__ == "__main__":
    main()
