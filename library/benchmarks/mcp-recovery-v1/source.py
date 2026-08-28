"""Source metadata, licensing, and deterministic digest calculation for mcp-recovery-v1."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def load_manifest() -> dict[str, Any]:
    with open(HERE / "sources.json", "r", encoding="utf-8") as f:
        return json.load(f)


def source_digest(extra_content: str = "") -> str:
    manifest = load_manifest()
    hasher = hashlib.sha256()
    hasher.update(json.dumps(manifest, sort_keys=True).encode("utf-8"))
    if extra_content:
        hasher.update(extra_content.encode("utf-8"))
    return hasher.hexdigest()
