"""Safety checks for immutable bundles from the retired promotion script."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROMOTED_RUNS = ROOT / "research/evidence/runs"


def test_all_repository_promotion_manifests_are_v2() -> None:
    manifests = sorted(PROMOTED_RUNS.glob("*/PROMOTION.json"))
    assert manifests
    for path in manifests:
        assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 2


def test_no_raw_prompt_stream_paths_survive_in_repository_bundles() -> None:
    banned_names = {
        "job.log",
        "trial.log",
        "codex.txt",
        "opencode.txt",
        "opencode.db",
        "opencode.db-wal",
        "opencode.db-shm",
        "auth.json",
    }
    leaked: list[str] = []
    for manifest in sorted(PROMOTED_RUNS.glob("*/PROMOTION.json")):
        bundle = manifest.parent
        for path in bundle.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(bundle)
            if (
                path.name in banned_names
                or "sessions" in relative.parts
                or "opencode" in relative.parts
            ):
                leaked.append(f"{bundle.name}/{relative}")
    assert leaked == []
