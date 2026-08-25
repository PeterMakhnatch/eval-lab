"""Small deterministic state generator for the LOCA AB-testing canary."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import random
from dataclasses import dataclass
from typing import Any

SIZES = {
    "8k": {"num_scenarios": 10, "num_days": 6, "configured_tokens": 8192},
    "64k": {"num_scenarios": 120, "num_days": 64, "configured_tokens": 65536},
    "128k": {"num_scenarios": 2000, "num_days": 365, "configured_tokens": 131072},
}
SEEDS = (42, 123, 456)
CANARY = ("8k", 42)
FIELDS = ("scenario", "time_window", "A_clicks", "A_store_views", "B_clicks", "B_store_views")


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _csv(rows: list[dict[str, Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def generate_rows(size: str, seed: int) -> list[dict[str, Any]]:
    """Generate stable rows without relying on process hash randomization."""
    if size not in SIZES or seed not in SEEDS:
        raise ValueError(f"supported sizes={tuple(SIZES)} seeds={SEEDS}")
    params = SIZES[size]
    rng = random.Random(f"loca-bench:{size}:{seed}")
    rows: list[dict[str, Any]] = []
    for scenario_id in range(params["num_scenarios"]):
        scenario = f"scenario-{scenario_id:04d}"
        for day in range(params["num_days"]):
            for hour in range(24):
                # The generator preserves the upstream shape: each row has
                # clicks and store views for both arms, with views <= clicks.
                a_clicks = 20 + rng.randrange(81)
                b_clicks = 20 + rng.randrange(81)
                a_views = rng.randrange(a_clicks + 1)
                b_views = rng.randrange(b_clicks + 1)
                rows.append({
                    "scenario": scenario,
                    "time_window": f"day-{day:03d}T{hour:02d}:00Z",
                    "A_clicks": a_clicks,
                    "A_store_views": a_views,
                    "B_clicks": b_clicks,
                    "B_store_views": b_views,
                })
    return rows


def environment_description(rows: list[dict[str, Any]], *, size: str, seed: int, source_digest: str) -> bytes:
    params = SIZES[size]
    # Keep the context contract explicit: this is the serialized state, not
    # padding and not a model-generated trace.
    payload = {
        "benchmark": "LOCA-bench",
        "source_digest": source_digest,
        "task_family": "ABTestingS2LEnv",
        "instruction": "Compute conversion rates for A and B from clickstream.csv.",
        "mcp_server": "google_cloud",
        "state_seed": {"difficulty": "medium", "num_days": params["num_days"], "num_scenarios": params["num_scenarios"], "seed": seed},
        "state_rows": rows,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_state(*, size: str, seed: int, source_digest: str) -> tuple[bytes, bytes, dict[str, Any]]:
    rows = generate_rows(size, seed)
    clickstream = _csv(rows)
    environment = environment_description(rows, size=size, seed=seed, source_digest=source_digest)
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
        "state_digest": _sha(clickstream + environment),
        "state_strategy": "deterministic_seeded_rows",
        "padding_only": False,
        "realized_context_tokens": max(1, len(environment) // 4),
        "context_measurement": "serialized environment_description.json UTF-8 bytes / 4",
    }
    return clickstream, environment, manifest
