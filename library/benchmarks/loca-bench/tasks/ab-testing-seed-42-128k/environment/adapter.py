"""External LOCA-bench adapter using pinned upstream local MCP implementation."""
from __future__ import annotations
import csv, hashlib, importlib.util, json, os, shutil, sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor" / "loca_upstream"
GENERATOR = VENDOR / "gem" / "envs" / "ab_testing_s2l" / "generate_ab_data.py"
SIZE_PARAMS: dict[str, dict[str, int]] = {
    "8k": {"num_scenarios": 10, "num_days": 6},
    "64k": {"num_scenarios": 120, "num_days": 64},
    "128k": {"num_scenarios": 2000, "num_days": 365},
}
SEEDS = (42, 123, 456)


def _generator() -> Any:
    spec = importlib.util.spec_from_file_location("loca_ab_generator", GENERATOR)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load pinned generator: {GENERATOR}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ABTestingDataGenerator


def _db(data_dir: Path) -> Any:
    sys.path.insert(0, str(VENDOR / "mcp_convert"))
    try:
        from mcps.google_cloud.database_utils import GoogleCloudDatabase
    finally:
        sys.path.pop(0)
    return GoogleCloudDatabase(data_dir=str(data_dir))


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if not child.is_file():
            continue
        digest.update(str(child.relative_to(path)).encode())
        if child.suffix == ".json":
            try:
                value = json.loads(child.read_text(encoding="utf-8"))

                def strip_created(item):
                    if isinstance(item, dict):
                        return {k: strip_created(v) for k, v in item.items() if k != "created"}
                    if isinstance(item, list):
                        return [strip_created(v) for v in item]
                    return item

                digest.update(
                    json.dumps(strip_created(value), sort_keys=True, separators=(",", ":")).encode()
                )
                continue
            except (json.JSONDecodeError, UnicodeDecodeError):
                pass
        digest.update(child.read_bytes())
    return digest.hexdigest()


def _load_instruction() -> str:
    instruction_path = next((ROOT / "tasks").glob("*/instruction.md"), ROOT / "instruction.md")
    return instruction_path.read_text(encoding="utf-8")


def materialize(
    task_dir: Path, size: str, seed: int, *, golden_dir: Path | None = None
) -> dict[str, Any]:
    """Build real upstream-generated state and the local MCP database.

    Agent-visible state is written under ``task_dir``.  Expected/golden
    state is never placed there; if ``golden_dir`` is supplied it is written
    to that hidden directory (the verifier image) and is not part of the
    agent image.
    """
    if size not in SIZE_PARAMS:
        raise ValueError(f"size must be one of {sorted(SIZE_PARAMS)}")
    if seed not in SEEDS:
        raise ValueError(f"seed must be one of {SEEDS}")
    task_dir = task_dir.resolve()
    if task_dir.exists():
        shutil.rmtree(task_dir)
    files, db_dir = task_dir / "files", task_dir / "local_db" / "google_cloud"
    files.mkdir(parents=True)
    db_dir.mkdir(parents=True)
    (task_dir / "agent_workspace").mkdir()

    params = {**SIZE_PARAMS[size], "seed": seed, "difficulty": "medium"}
    Generator = _generator()
    generated = Generator(seed=seed).generate_scenarios(
        num_scenarios=params["num_scenarios"],
        num_days=params["num_days"],
        difficulty=params["difficulty"],
    )
    scenarios = generated["scenarios"]

    source = files / "clickstream.csv"
    with source.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "scenario",
                "time_window",
                "A_clicks",
                "A_store_views",
                "B_clicks",
                "B_store_views",
            ],
        )
        writer.writeheader()
        for scenario in scenarios:
            for row in scenario["data_rows"]:
                writer.writerow({"scenario": scenario["name"], **row})

    (task_dir / "agent_workspace" / "record.csv").write_text(
        "scenario,A_conversion %,B_conversion %\n", encoding="utf-8"
    )

    # Golden/expected state is written only to the caller-supplied hidden
    # directory.  It must never appear under the agent-visible task_dir.
    if golden_dir is not None:
        golden_dir = golden_dir.resolve()
        golden_dir.mkdir(parents=True, exist_ok=True)
        Generator(seed=seed).save_expected_ratio(scenarios, golden_dir / "expected_record.csv")

    db, project, dataset = _db(db_dir), "local-project", "ab_testing"
    db.create_bigquery_dataset(
        project, dataset, {"location": "US", "description": "LOCA upstream AB test", "labels": {}}
    )
    rows = [{"scenario": s["name"], **row} for s in scenarios for row in s["data_rows"]]
    instruction = _load_instruction()
    tool_schemas = json.loads(
        (VENDOR / "scripts" / "mcp_tool_schemas.json").read_text(encoding="utf-8")
    )["servers"]["google_cloud"]
    budget = int(size[:-1]) * 1024 * 4
    env_description = {
        "instruction": instruction,
        "mcp_server": "google_cloud",
        "tool_definitions": tool_schemas,
        "state_seed": params,
        "state_rows": [],
    }
    for row in rows:
        candidate = {
            **env_description,
            "state_rows": [*env_description["state_rows"], row],
        }
        encoded = json.dumps(
            candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        if len(encoded) > budget:
            break
        env_description = candidate
    env_bytes = json.dumps(
        env_description, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    (files / "environment_description.json").write_bytes(env_bytes)
    schema = [
        {"name": "scenario", "type": "STRING", "mode": "NULLABLE"},
        {"name": "time_window", "type": "STRING", "mode": "NULLABLE"},
        {"name": "A_clicks", "type": "INTEGER", "mode": "NULLABLE"},
        {"name": "A_store_views", "type": "INTEGER", "mode": "NULLABLE"},
        {"name": "B_clicks", "type": "INTEGER", "mode": "NULLABLE"},
        {"name": "B_store_views", "type": "INTEGER", "mode": "NULLABLE"},
    ]
    db.create_bigquery_table(
        project, dataset, "clickstream", {"schema": schema, "description": "Generated by pinned LOCA generator"}
    )
    if not db.insert_table_rows(project, dataset, "clickstream", rows):
        raise RuntimeError("upstream GoogleCloudDatabase refused clickstream rows")
    db.create_storage_bucket(
        "promo-assets-for-b", {"location": "US", "description": "LOCA upstream AB test"}
    )
    bytes_total = sum(
        p.stat().st_size for p in (task_dir / "local_db").rglob("*") if p.is_file()
    )
    state_digest = _sha(files)
    database_digest = _sha(task_dir / "local_db")
    manifest = {
        "benchmark": "LOCA-bench",
        "benchmark_version": "upstream-main@8b6fac49d9edd92922593e703b74ea255357c3ec",
        "sandbox_commit": "2b4a1c77bd65d83750372ee079a2e5c5d13cb27c",
        "task_family": "ABTestingS2LEnv",
        "official_seed": seed,
        "size": size,
        "configured_size_tokens": int(size[:-1]) * 1024,
        "state_bytes": bytes_total,
        "realized_state_bytes": bytes_total,
        "state_tokens_estimate": max(1, bytes_total // 4),
        "environment_description_bytes": len(env_bytes),
        "realized_context_tokens": max(1, len(env_bytes) // 4),
        "prompt_tokens": max(1, len(instruction.encode("utf-8")) // 4),
        "scenario_count": len(scenarios),
        "row_count": len(rows),
        "state_digest": state_digest,
        "database_digest": database_digest,
        "state_strategy": "upstream_generator_parameters",
        "padding_only": False,
        "mcp_service": "vendored upstream mcp_convert.mcps.google_cloud",
        "params": params,
    }
    (task_dir / "state_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    if golden_dir is not None:
        golden_manifest = {
            "benchmark": "LOCA-bench",
            "task_family": "ABTestingS2LEnv",
            "official_seed": seed,
            "size": size,
            "row_count": len(rows),
            "padding_only": False,
            "state_digest": state_digest,
        }
        (golden_dir / "manifest.json").write_text(
            json.dumps(golden_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    return manifest


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--size", choices=sorted(SIZE_PARAMS), required=True)
    parser.add_argument("--seed", type=int, choices=SEEDS, required=True)
    parser.add_argument("--golden-dir", type=Path, default=None)
    args = parser.parse_args()
    print(
        json.dumps(
            materialize(args.task_dir, args.size, args.seed, golden_dir=args.golden_dir),
            sort_keys=True,
        )
    )
