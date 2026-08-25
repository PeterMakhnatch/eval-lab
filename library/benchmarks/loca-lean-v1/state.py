"""Deterministic LOCA state generation from the pinned upstream generator."""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import io
import json
import random
from pathlib import Path
from typing import Any

SIZES = {
    "8k": {"num_scenarios": 10, "num_days": 6, "configured_tokens": 8192},
    "64k": {"num_scenarios": 120, "num_days": 64, "configured_tokens": 65536},
    "128k": {"num_scenarios": 2000, "num_days": 365, "configured_tokens": 131072},
}
SEEDS = (42, 123, 456)
CANARY = ("8k", 42)
FIELDS = ("scenario", "time_window", "A_clicks", "A_store_views", "B_clicks", "B_store_views")
INSTRUCTION = (
    "The A/B test for our new homepage has concluded, and the raw clickstream data has been stored in the `ab_testing` dataset in BigQuery. Analyze this data to calculate the conversion rate for each scenario as well as the overall conversion rate, which should be labeled `overall (total_store_views/total_clicks)`. Record these results in `record.csv`, following the same format used in that file — do not change column names. After completing the analysis, determine which version ('A' or 'B') has the highest overall conversion rate, i.e., the overall conversion rate is defined as the arithmetic mean of the per-scenario conversion rates. If version B outperforms, immediately create a new Cloud Storage bucket named `promo-assets-for-b` for the full promotion, and you do not need to write any log entry in this process. If version A wins or the results are a tie, no bucket creation is required, but a log entry with the message `{'status': 'AB_Test_Concluded', 'winner': 'A', 'action': 'No_Change'}` must be written to the `abtesting_logging` bucket. \n\nThis external Harbor package uses the pinned LOCA local MCP service. Do not use network.\n\n\n**Required context initialization (external LOCA adapter).** Before analysis, perform a first tool read of `/app/task_state/files/environment_description.json` (the serialized upstream state/tool description). Preserve that tool result in your trajectory; it is the configured context-size condition, not padding. Then use the configured `google_cloud` MCP server for all data access.\n"
)


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _csv(rows: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _upstream_rows(generator_path: Path, size: str, seed: int) -> list[dict[str, Any]]:
    spec = importlib.util.spec_from_file_location("loca_pinned_generator", generator_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load pinned generator: {generator_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    params = SIZES[size]
    scenarios = module.ABTestingDataGenerator(seed=seed).generate_scenarios(
        num_scenarios=params["num_scenarios"], num_days=params["num_days"], difficulty="medium"
    )["scenarios"]
    return [{"scenario": scenario["name"], **row} for scenario in scenarios for row in scenario["data_rows"]]


def _state_digest(clickstream: bytes, environment: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(b"clickstream.csv")
    digest.update(clickstream)
    digest.update(b"environment_description.json")
    try:
        value = json.loads(environment)
        digest.update(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        digest.update(environment)
    return "sha256:" + digest.hexdigest()


def _test_rows(size: str, seed: int) -> list[dict[str, Any]]:
    """Small offline-only fallback used by unit tests without a source cache."""
    rng = random.Random(f"loca-test:{size}:{seed}")
    params = SIZES[size]
    rows: list[dict[str, Any]] = []
    for scenario in range(params["num_scenarios"]):
        for day in range(params["num_days"]):
            for hour in range(24):
                clicks_a, clicks_b = 20 + rng.randrange(81), 20 + rng.randrange(81)
                rows.append({"scenario": f"scenario-{scenario:04d}", "time_window": f"day-{day:03d}T{hour:02d}:00Z", "A_clicks": clicks_a, "A_store_views": rng.randrange(clicks_a + 1), "B_clicks": clicks_b, "B_store_views": rng.randrange(clicks_b + 1)})
    return rows


def generate_rows(size: str, seed: int, generator_path: Path | None = None) -> list[dict[str, Any]]:
    if size not in SIZES or seed not in SEEDS:
        raise ValueError(f"supported sizes={tuple(SIZES)} seeds={SEEDS}")
    return _upstream_rows(generator_path, size, seed) if generator_path is not None else _test_rows(size, seed)


def environment_description(rows: list[dict[str, Any]], *, size: str, seed: int, source_digest: str, tool_schemas_path: Path | None = None) -> bytes:
    params = SIZES[size]
    tool_schemas: dict[str, Any] = {}
    if tool_schemas_path is not None:
        tool_schemas = json.loads(tool_schemas_path.read_text(encoding="utf-8"))["servers"]["google_cloud"]
    env = {"instruction": INSTRUCTION, "mcp_server": "google_cloud", "tool_definitions": tool_schemas, "state_seed": {"difficulty": "medium", "num_days": params["num_days"], "num_scenarios": params["num_scenarios"], "seed": seed}, "state_rows": []}
    budget = params["configured_tokens"] * 4
    for row in rows:
        candidate = {**env, "state_rows": [*env["state_rows"], row]}
        encoded = json.dumps(candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(encoded) > budget:
            break
        env = candidate
    return json.dumps(env, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def build_state(*, size: str, seed: int, source_digest: str, generator_path: Path | None = None, tool_schemas_path: Path | None = None) -> tuple[bytes, bytes, dict[str, Any]]:
    rows = generate_rows(size, seed, generator_path)
    clickstream = _csv(rows)
    environment = environment_description(rows, size=size, seed=seed, source_digest=source_digest, tool_schemas_path=tool_schemas_path)
    params = SIZES[size]
    manifest = {
        "benchmark": "LOCA-bench",
        "benchmark_version": "upstream-main@8b6fac49d9edd92922593e703b74ea255357c3ec",
        "configured_size_tokens": params["configured_tokens"],
        "environment_description_bytes": len(environment),
        "official_seed": seed,
        "params": {"difficulty": "medium", "num_days": params["num_days"], "num_scenarios": params["num_scenarios"], "seed": seed},
        "row_count": len(rows),
        "sandbox_commit": "2b4a1c77bd65d83750372ee079a2e5c5d13cb27c",
        "scenario_count": params["num_scenarios"],
        "size": size,
        "source_digest": source_digest,
        "state_digest": _state_digest(clickstream, environment),
        "state_strategy": "pinned_upstream_generator_parameters" if generator_path else "offline_test_fallback",
        "padding_only": False,
        "realized_context_tokens": max(1, len(environment) // 4),
        "context_measurement": "serialized environment_description.json UTF-8 bytes / 4",
    }
    return clickstream, environment, manifest
