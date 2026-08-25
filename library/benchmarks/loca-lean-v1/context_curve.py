"""Explicit context-curve contract for generated LOCA state."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

CONTEXT_CONTRACT = {
    "measurement": "UTF-8 bytes of files/environment_description.json divided by 4",
    "configured_size": "state_manifest.configured_size_tokens",
    "realized_size": "state_manifest.realized_context_tokens",
    "unknowns": "model/scaffold tokens are not inferred",
    "provenance": "mechanical",
}


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def emit(task_dir: Path, trial_id: str, output: Path) -> list[dict]:
    manifest = json.loads((task_dir / "state_manifest.json").read_text(encoding="utf-8"))
    content = (task_dir / "files" / "environment_description.json").read_bytes()
    base = {
        "benchmark": "LOCA-bench",
        "contract": CONTEXT_CONTRACT,
        "provenance_kind": "mechanical",
        "trial_id": trial_id,
        "source_ref": str(task_dir / "state_manifest.json"),
        "source_digest": _sha((task_dir / "state_manifest.json").read_bytes()),
        "configured_size": manifest["configured_size_tokens"],
        "realized_size": manifest["realized_context_tokens"],
        "prompt_tokens": None,
        "content_digest": _sha(content),
    }
    rows = [
        {**base, "operation_id": "initial_context", "operation": "memory_read", "before_token_count": 0, "after_token_count": manifest["realized_context_tokens"]},
        {**base, "operation_id": "mcp_query_result", "operation": "mcp_query", "before_token_count": manifest["realized_context_tokens"], "after_token_count": manifest["realized_context_tokens"]},
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    return rows


def project(rows_path: Path) -> dict:
    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if any(row.get("contract") != CONTEXT_CONTRACT for row in rows):
        raise ValueError("context rows do not satisfy LOCA context-curve contract")
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(row["configured_size"], []).append(row)
    return {
        "benchmark": "LOCA-bench",
        "contract": CONTEXT_CONTRACT,
        "curve": [
            {"configured_size": configured, "realized_sizes": sorted({row["realized_size"] for row in facts}), "before_tokens": sorted({row["before_token_count"] for row in facts}), "after_tokens": sorted({row["after_token_count"] for row in facts})}
            for configured, facts in sorted(grouped.items())
        ],
    }
