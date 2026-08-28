"""Benchmark contract definition for mcp-recovery-v1."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from source import load_manifest, source_digest

HERE = Path(__file__).resolve().parent


def get_benchmark_contract() -> dict[str, Any]:
    manifest = load_manifest()
    digest = source_digest()
    return {
        "family": "mcp-recovery-v1",
        "version": manifest.get("version", "v1.0.0"),
        "construct": manifest.get("benchmark", {}).get("construct", ""),
        "cell_factors": {
            "fault_classes": manifest.get("benchmark", {}).get("fault_classes", []),
            "persistence_levels": manifest.get("benchmark", {}).get("persistence_levels", []),
            "seeds": manifest.get("benchmark", {}).get("calibration_seeds", []),
        },
        "verifier_truth_digest": digest,
        "evidence_contract": {
            "events_path": "/app/evidence/benchmark-events.jsonl",
            "final_state_path": "/app/evidence/final-state.json",
        },
    }
