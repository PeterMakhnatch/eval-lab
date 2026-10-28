"""Pinned downloads, immutable raw custody and bounded native-row iteration."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import urllib.parse
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from evallab.failure_library.runs.cached import (
    CHEATBENCH_REVISION,
    GOODFIRE_REVISION,
    WRENCH_INDEX_REVISION,
)
from evallab.failure_library.runs.published import SOURCES

COLLECTIONS = (
    "terminal-wrench",
    "goodfire-reward-hack-data",
    "cheatbench",
    *SOURCES,
    "metr-malt",
)
MALT_REVISION = "6100006c2fd23d81e5afe2b1cc3bf3f3fff48df1"
MALT_REDISTRIBUTION_REVISION = "1045a7336432c40182924bbd3698af292ea24acb"
REPOSITORIES = {
    **SOURCES,
    "terminal-wrench": ("few-sh/terminal-wrench-trajectories", WRENCH_INDEX_REVISION),
    "goodfire-reward-hack-data": ("goodfire/reward-hack-data", GOODFIRE_REVISION),
    "cheatbench": ("steinad/CheatBench", CHEATBENCH_REVISION),
    "metr-malt": ("metr-evals/malt-public", MALT_REVISION),
}


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


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


def jsonl_rows(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # Ablation's early smoke exports concatenate pretty-printed objects
                # despite their .jsonl suffix. Retain each object's actual start line.
                decoder = json.JSONDecoder()
                buffer = line
                base = index
                while True:
                    start = len(buffer) - len(buffer.lstrip())
                    if start == len(buffer):
                        base += buffer.count("\n")
                        buffer = handle.read(65536)
                        if not buffer:
                            return
                        continue
                    try:
                        row, end = decoder.raw_decode(buffer, start)
                    except json.JSONDecodeError:
                        chunk = handle.read(65536)
                        if not chunk:
                            raise
                        buffer += chunk
                        continue
                    yield base + buffer.count("\n", 0, start), row
                    base += buffer.count("\n", 0, end)
                    buffer = buffer[end:]
            yield index, row


def _source_files(collection: str, filename: str) -> bool:
    if collection == "terminal-wrench":
        return filename in {"baseline.parquet", "hack.parquet", "non_hack.parquet", "tasks.parquet"}
    if collection == "goodfire-reward-hack-data":
        return filename.endswith(("traces.jsonl", "judge_rollouts.jsonl", "monitor_rollouts.jsonl"))
    return filename.endswith((".parquet", ".jsonl", ".jsonl.gz"))


def stage_input(collection: str, source: Path, destination: Path) -> None:
    """Archive existing raw bytes once; refuse to overwrite different pinned bytes."""
    if source.resolve() == destination.resolve():
        return
    if source.is_file():
        name = "redistributed.parquet" if collection == "metr-malt" else source.name
        files = [(source, Path(name))]
    else:
        files = [
            (path, path.relative_to(source))
            for path in sorted(source.rglob("*"))
            if path.is_file() and _source_files(collection, str(path.relative_to(source)))
        ]
    for original, relative in files:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if digest(target) != digest(original):
                raise ValueError(f"different bytes already occupy pinned raw path {target}")
            continue
        shutil.copyfile(original, target)
        target.chmod(0o444)


def _selected_download(collection: str, path: str) -> bool:
    if path in {"README.md", "MANIFEST.json"}:
        return True
    if collection == "reward-hacking-ablation":
        return path.endswith(
            (
                "/episodes.jsonl",
                "/trajectories.jsonl",
                "/trajectories.jsonl.gz",
                "/records.jsonl.gz",
            )
        )
    if collection == "termgrade":
        return path in {"data/trials.parquet", "data/tests.parquet"} or path.startswith(
            "data/terminus/"
        )
    if collection == "hacktrace":
        return path.startswith("trajectories/") and path.endswith(".parquet")
    if collection == "olmo-reward-hacking":
        return path.startswith("data/") and path.endswith(".parquet")
    if collection == "metr-malt":
        return path.startswith("data/public-") and path.endswith(".parquet")
    if collection == "cheatbench":
        return path == "data/processed/parquet/full.parquet"
    if collection == "terminal-wrench":
        return path in {
            "data/baseline/train-00000-of-00001.parquet",
            "data/hack/train-00000-of-00001.parquet",
            "data/non_hack/train-00000-of-00001.parquet",
            "data/tasks/train-00000-of-00001.parquet",
        }
    if collection == "goodfire-reward-hack-data":
        return path.endswith(
            (
                "/traces.jsonl",
                "/labels/judge_rollouts.jsonl",
                "/generic_monitor/monitor_rollouts.jsonl",
            )
        )
    return False


def download_source(collection: str, destination: Path) -> list[Path]:
    repo, revision = REPOSITORIES[collection]
    headers = {"User-Agent": "eval-lab-failure-library/1"}
    if os.environ.get("HF_TOKEN"):
        headers["Authorization"] = "Bearer " + os.environ["HF_TOKEN"]
    request = urllib.request.Request(
        f"https://huggingface.co/api/datasets/{repo}/revision/{revision}", headers=headers
    )
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
        metadata = json.load(response)
    files = [
        item["rfilename"]
        for item in metadata["siblings"]
        if _selected_download(collection, item["rfilename"])
    ]

    def fetch(filename: str) -> Path:
        target = destination / filename
        if target.is_file():
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        encoded = urllib.parse.quote(filename, safe="/")
        request = urllib.request.Request(
            f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{encoded}", headers=headers
        )
        partial = target.with_name(target.name + ".partial")
        with urllib.request.urlopen(request, timeout=300) as response, partial.open("wb") as out:  # noqa: S310
            shutil.copyfileobj(response, out)
        partial.replace(target)
        target.chmod(0o444)
        return target

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(fetch, files))


def raw_root(state_root: Path, collection: str) -> Path:
    return state_root / "raw" / collection / REPOSITORIES[collection][1]
