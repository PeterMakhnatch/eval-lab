"""Materialize one LOCA canary under ignored derived Harbor output."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from source import Pin, PinError, fetch_pinned, load_pins, source_digest, verify_cache
from state import CANARY, SIZES, SEEDS, build_state

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
DERIVED = REPO / "derived" / "harbor-tasks" / "loca"
SOURCE_CACHE = REPO / "derived" / "harbor-source-cache" / "loca"


def _acquire(cache_dir: Path) -> dict[str, Path]:
    record, pins = load_pins()
    license_pin = Pin("LICENSE", record["license"]["url"], record["license"]["sha256"], "MIT")
    entries = (license_pin, *pins)
    return {pin.name: fetch_pinned(pin, cache_dir, offline=False) for pin in entries}


def output_path() -> Path:
    record, pins = load_pins()
    return DERIVED / source_digest(record, pins)


def _safe_output(path: Path, digest: str) -> Path:
    resolved = path.resolve()
    expected_parent = DERIVED.resolve()
    if resolved.parent != expected_parent or resolved.name != digest:
        raise PinError(f"generated output must be exactly {expected_parent / digest}")
    return resolved
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
        "output_relpath": f"derived/harbor-tasks/loca/{digest}",
        "source_pins_verified": bool(verify_sources),
        "legacy_8k_seed42_state_digest": "sha256:829bba54bad9ca179d3fc3c03f2d6737dfc4e1fc91f22c94cef73d7f4f4b2d9d",
        "legacy_8k_seed42_database_digest": "sha256:38060fd9580ad0fc08069d01e442807f984405fa0ee7f8a2b363da5b5a5aeb02",
    })
    (target / "state_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
