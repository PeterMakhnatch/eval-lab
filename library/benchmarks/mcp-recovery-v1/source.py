"""Source metadata, licensing, and rejection guard for mcp-recovery-v1."""
from __future__ import annotations

import hashlib
import json
import subprocess
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


def reject_committed_corpora() -> None:
    tracked = subprocess.check_output(
        [
            "git",
            "ls-files",
            "library/benchmarks/mcp-recovery-v1/tasks",
            "derived/harbor-tasks/mcp-recovery",
        ],
        text=True,
    ).splitlines()
    if tracked:
        raise AssertionError(f"Generated task corpus is tracked in git: {tracked[:5]}")
