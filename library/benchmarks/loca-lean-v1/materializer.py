"""Materialize one LOCA canary under ignored derived Harbor output."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from source import PinError, load_pins, source_digest, verify_cache
from state import CANARY, SIZES, SEEDS, build_state

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
DERIVED = REPO / "derived" / "harbor-tasks" / "loca"


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
    verify_sources: bool = False,
) -> dict:
    """Rebuild a canary from pins and deterministic state parameters.

    ``verify_sources`` is enabled by CI when a pinned cache is available.  It
    is intentionally opt-in so a clean checkout can regenerate from the
    immutable pin record without committing upstream source payloads.
    """
    if (size, seed) != CANARY:
        raise ValueError("lean LOCA materializer exposes exactly the 8k seed-42 canary")
    record, pins = load_pins()
    digest = source_digest(record, pins)
    target = _safe_output(output_dir or output_path(), digest)
    if verify_sources:
        if cache_dir is None:
            raise PinError("cache_dir is required when verify_sources=True")
        verify_cache(cache_dir, offline=True)
    if target.exists():
        shutil.rmtree(target)
    files = target / "files"
    workspace = target / "agent_workspace"
    local_db = target / "local_db" / "google_cloud"
    files.mkdir(parents=True)
    workspace.mkdir()
    local_db.mkdir(parents=True)
    clickstream, environment, manifest = build_state(size=size, seed=seed, source_digest=digest)
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
    })
    (target / "state_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest


def reject_committed_corpora(root: Path = REPO) -> None:
    """Fail CI if generated nine-tree task packages are tracked or present."""
    forbidden = []
    for path in root.glob("library/benchmarks/loca-bench/tasks/*"):
        if path.is_dir():
            forbidden.append(str(path))
    if forbidden:
        raise AssertionError("committed LOCA task corpus is forbidden: " + ", ".join(forbidden))
    if any(root.glob("library/benchmarks/loca-lean-v1/tasks/*")):
        raise AssertionError("lean LOCA must not contain generated task trees")
