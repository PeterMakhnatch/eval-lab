"""Immutable raw custody and bounded native-row iteration."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from evallab.failure_library.runs.cached import (
    CHEATBENCH_REVISION,
    GOODFIRE_REVISION,
    WRENCH_INDEX_REVISION,
)
from evallab.failure_library.runs.published import SOURCES

MALT_REVISION = "6100006c2fd23d81e5afe2b1cc3bf3f3fff48df1"
MALT_REDISTRIBUTION_REVISION = "1045a7336432c40182924bbd3698af292ea24acb"
REPOSITORIES = {
    **SOURCES,
    "terminal-wrench": ("few-sh/terminal-wrench-trajectories", WRENCH_INDEX_REVISION),
    "goodfire-reward-hack-data": ("goodfire/reward-hack-data", GOODFIRE_REVISION),
    "cheatbench": ("steinad/CheatBench", CHEATBENCH_REVISION),
    "metr-malt": ("metr-evals/malt-public", MALT_REVISION),
}

# Retained custody pins, captured November 11, 2026. The digest binds every
# relative filename and file SHA-256, including source metadata and labels.
# Per-file receipts live outside Git; this is not a claim of label correctness.
RETAINED_BUNDLE_SHA256 = {
    "cheatbench": "5aa5bc755b0904ee731d61533feb062f67196cfede444fde91ad94c7f3437d3f",
    "goodfire-reward-hack-data": "b45d88b40c5a30787b98823147e094b6335ac67803ed4c31884aad4840cda539",
    "hacktrace": "3c4e006743b58afe0599e3908de478138714f875425b2c6f243e6a7cb3539f39",
    "metr-malt": "a2393ffe036011af71b4387720d54df62c64c6031d87e3833ec181c7e6921d3c",
    "olmo-reward-hacking": "c941e3b2e42f22b3c7623339cd27aa014b24ea109507e47bae8f0f1c8e1159e7",
    "reward-hacking-ablation": "645362ca3aea04965cf33f9281f7412673ea1c257beb1103a13be6b813fa8c41",
    "termgrade": "fe27927de3849edb858f7ee7c5dcf32920269015d1f623334d9ca8e81e3424b6",
    "terminal-wrench": "a5110c2bd50c587c5d04e70fcf0a01d635c5c469a896b318b9c29d3bb3b90a15",
}


def download_bytes(url: str, *, user_agent: str, timeout: int) -> bytes:
    """Read anonymous source bytes; callers retain their pins and digest checks."""
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return response.read()


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def retained_bundle_digest(directory: Path) -> tuple[str, int]:
    """Hash file identities and bytes, never parse or expose retained row bodies."""
    if not directory.is_dir():
        raise FileNotFoundError(directory)
    tree = hashlib.sha256()
    count = 0
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("retained bundle contains a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        tree.update(json.dumps([relative, digest(path)], separators=(",", ":")).encode() + b"\n")
        count += 1
    if not count:
        raise ValueError("retained bundle is empty")
    return tree.hexdigest(), count


def verify_retained_bundle(directory: Path, expected: str) -> int:
    actual, count = retained_bundle_digest(directory)
    if actual != expected:
        raise ValueError(f"retained bundle hash differs: {directory}")
    return count


def retained_source_health(
    repo_root: Path, *, home: Path, collections: tuple[str, ...] = tuple(REPOSITORIES),
) -> tuple[bool, str]:
    """Verify pinned native bundles and normalized bundle hashes without downloads."""
    state = Path(os.environ.get(
        "EVALLAB_FAILURE_LIBRARY_STATE", home / ".local/state/evallab-failure-library"
    ))
    problems: list[str] = []
    files = 0
    pins = []
    for collection in sorted(collections):
        _repo, revision = REPOSITORIES[collection]
        pins.append(f"{collection}@{revision}")
        raw = raw_root(state, collection)
        try:
            files += verify_retained_bundle(raw, RETAINED_BUNDLE_SHA256[collection])
            if not any(
                _source_files(collection, str(path.relative_to(raw)))
                for path in raw.rglob("*") if path.is_file()
            ):
                problems.append(f"{collection}:native-data-MISSING(access-exclusion)")
        except (OSError, ValueError) as exc:
            problems.append(f"{collection}@{revision}:{type(exc).__name__}")
        try:
            manifest_path = repo_root / "research/failure-library/runs" / f"{collection}.manifest.json"
            manifest = json.loads(manifest_path.read_bytes())
            admitted_revisions = {revision}
            if collection == "metr-malt":
                admitted_revisions.add(f"ktolnos/rh-bench@{MALT_REDISTRIBUTION_REVISION}")
            bundle = state / "runs" / f"{collection}.jsonl"
            if manifest.get("source_revision") not in admitted_revisions:
                problems.append(f"{collection}:revision-drift")
            if not bundle.is_file() or digest(bundle) != manifest["sha256"]:
                problems.append(f"{collection}:normalized-bundle-missing-or-corrupt")
        except (OSError, ValueError, KeyError):
            problems.append(f"{collection}:bundle-manifest-missing-or-invalid")
    return not problems, (
        f"offline=True repositories={len(collections)} raw_files={files} "
        f"pins={','.join(pins)}; SHA256=RETAINED_BUNDLE_SHA256,committed-run-manifests "
        f"problems={','.join(problems) or 'none'}"
    )


def reference(path: Path, index: int, *, jsonl: bool = False, field: str | None = None) -> str:
    locator = f"line={index + 1}" if jsonl else f"row={index}"
    if field:
        locator += "&field=" + urllib.parse.quote(field, safe="[].,_")
    return path.resolve().as_uri() + "#" + locator


def parquet_rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    index = 0
    # Long Terminus episodes can be several MB each; never materialize a full shard.
    for batch in pq.ParquetFile(path).iter_batches(batch_size=32):
        for row in batch.to_pylist():
            yield index, row
            index += 1


def _source_files(collection: str, filename: str) -> bool:
    if collection == "terminal-wrench":
        return filename in {"baseline.parquet", "hack.parquet", "non_hack.parquet", "tasks.parquet"}
    if collection == "goodfire-reward-hack-data":
        return filename.endswith(("traces.jsonl", "judge_rollouts.jsonl", "monitor_rollouts.jsonl"))
    return filename.endswith((".parquet", ".jsonl", ".jsonl.gz"))


def raw_root(state_root: Path, collection: str) -> Path:
    return state_root / "raw" / collection / REPOSITORIES[collection][1]
