"""HAR-85 sealed-split view: terminal rows of HAR-81's sealed split, read through.

There is exactly one split: ``../har81-mimo-sft/split.json`` (sealed,
manifest ``sha256:c3df70a5...``). This module projects its terminal-domain
rows into the row shape the HAR-85 pool scripts consume (``task_id``,
``task_package_digest``, ``assignment``, ``split_group``) plus the id lists.
It is a read-through view, not a second split: no bytes are copied, and every
consumer re-asserts the sealed manifest digest on load.

Stdlib only, so the DSPy launcher (which runs outside the Lab venv) can
import it too.
"""

from __future__ import annotations

import json
from pathlib import Path

EXPERIMENT_DIR = Path(__file__).resolve().parent
SEALED_SPLIT_PATH = EXPERIMENT_DIR.parent / "har81-mimo-sft" / "split.json"

SEALED_CONTRACT = "evallab.sft_split/2"
SEALED_MANIFEST_DIGEST = (
    "sha256:c3df70a5551c47d6c695abb3b8e51ca009b48d606289de4f637a7c1329e49f58"
)
SEALED_SALT = "har81-sealed-20260928"

#: HAR-85 searches terminal tasks only (as under the provisional split).
DOMAIN = "terminal"

TRAIN_EXCLUSIONS_PATH = EXPERIMENT_DIR / "train-exclusions.json"
HELDOUT_EXCLUSIONS_PATH = EXPERIMENT_DIR / "heldout-exclusions.json"


def load_sealed_manifest(path: Path = SEALED_SPLIT_PATH) -> dict:
    """Load HAR-81's sealed split, refusing on contract or digest drift."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"sealed split {path} is unreadable: {exc}") from exc
    if payload.get("contract") != SEALED_CONTRACT:
        raise ValueError(f"sealed split {path} has unexpected contract")
    if payload.get("manifest_digest") != SEALED_MANIFEST_DIGEST:
        raise ValueError(
            f"sealed split digest {payload.get('manifest_digest')} != pinned "
            f"{SEALED_MANIFEST_DIGEST} (HAR-81 may have sealed a new split)"
        )
    return payload


def terminal_rows(manifest: dict) -> list[dict]:
    """Terminal rows in pool shape (package digest = sealed version digest)."""
    rows = []
    for task in manifest["tasks"]:
        if task.get("domain") != DOMAIN:
            continue
        assignment = {"train": "train", "heldout": "heldout"}.get(task.get("split", ""))
        if assignment is None:
            raise ValueError(f"sealed split row has unknown split: {task}")
        rows.append(
            {
                "task_id": task["task_id"],
                "task_package_digest": task["task_version_digest"],
                "assignment": assignment,
                "split_group": task["split_group"],
            }
        )
    return sorted(rows, key=lambda row: row["task_id"])


def train_task_ids(rows: list[dict]) -> list[str]:
    return [row["task_id"] for row in rows if row["assignment"] == "train"]


def heldout_task_ids(rows: list[dict]) -> list[str]:
    return [row["task_id"] for row in rows if row["assignment"] == "heldout"]


def projected_split() -> dict:
    """Sealed manifest in the shape train_pool.py consumes (no frozen copy)."""
    manifest = load_sealed_manifest()
    rows = terminal_rows(manifest)
    return {
        "manifest_digest": manifest["manifest_digest"],
        "tasks": rows,
        "train_task_ids": train_task_ids(rows),
        "heldout_task_ids": heldout_task_ids(rows),
    }


def _excluded_ids(path: Path, manifest_digest: str, scope: str) -> set[str]:
    try:
        exclusions = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path.name} is unreadable: {exc}") from exc
    if exclusions.get("split_manifest_digest") != manifest_digest:
        raise ValueError(
            f"{path.name} was recorded against another split; re-review it"
        )
    rows = terminal_rows(load_sealed_manifest())
    by_id = {row["task_id"]: row for row in rows}
    ids: set[str] = set()
    for entry in exclusions.get("excluded", []):
        task_id = entry["task_id"]
        row = by_id.get(task_id)
        if row is None or row["assignment"] != scope:
            raise ValueError(f"excluded task {task_id} is not a {scope} task of the sealed split")
        if entry["task_package_digest"] != row["task_package_digest"]:
            raise ValueError(f"excluded task {task_id} names different bytes than the sealed split")
        if not entry.get("reason") or not entry.get("evidence"):
            raise ValueError(f"excluded task {task_id} lacks a recorded reason and evidence")
        ids.add(task_id)
    return ids


def excluded_train_ids(manifest_digest: str) -> set[str]:
    """Train ids excluded from the pool; fails closed on a foreign record."""
    return _excluded_ids(TRAIN_EXCLUSIONS_PATH, manifest_digest, "train")


def excluded_heldout_ids(manifest_digest: str) -> set[str]:
    """Held-out ids excluded from the final eval; fails closed on drift."""
    return _excluded_ids(HELDOUT_EXCLUSIONS_PATH, manifest_digest, "heldout")


def train_pool_rows() -> tuple[dict, list[dict]]:
    """(projected split, pool rows): sealed train minus train exclusions."""
    split = projected_split()
    excluded = excluded_train_ids(split["manifest_digest"])
    rows = [
        {"task_id": row["task_id"], "task_package_digest": row["task_package_digest"]}
        for row in split["tasks"]
        if row["assignment"] == "train" and row["task_id"] not in excluded
    ]
    return split, rows


def heldout_ids() -> tuple[dict, list[str]]:
    """(projected split, held-out ids): sealed held-out minus held-out exclusions."""
    split = projected_split()
    excluded = excluded_heldout_ids(split["manifest_digest"])
    return split, [tid for tid in split["heldout_task_ids"] if tid not in excluded]
