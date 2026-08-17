"""LANCE: LanceDB vector store for tasks and trials beside DuckDB.

Default embedder is deterministic lexical (hashing) only; no semantics.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import lancedb
import pyarrow.parquet as pq

from evallab.craft import (
    TASK_INSTRUCTION,
    TASK_MANIFEST,
    discover_tasks,
    library_source,
    repository_root,
)
from evallab.paths import derived_root_from_environment


class Embedder(Protocol):
    """Protocol for text embedders. Implementations must be deterministic."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        ...


@dataclass(frozen=True)
class HashingEmbedder:
    """Pure deterministic lexical embedder.

    Tokenises on \w+, hashes tokens stably with MD5 into dim buckets,
    accumulates counts, L2-normalises. Same text always yields identical
    vector in-process and across processes. Captures only lexical overlap,
    not semantics. Use a real model later via the protocol without changing
    callers.
    """

    dim: int = 256

    def _stable_hash(self, token: str) -> int:
        h = hashlib.md5(token.encode("utf-8")).digest()
        return int.from_bytes(h[:4], "little") % self.dim

    def _tokenize(self, text: str) -> list[str]:
        return re.findall(r"\w+", text.lower())

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            v = [0.0] * self.dim
            for tok in self._tokenize(text):
                idx = self._stable_hash(tok)
                v[idx] += 1.0
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vectors.append([x / norm for x in v])
        return vectors


def _lance_root() -> Path:
    repo = repository_root()
    derived = derived_root_from_environment(repo)
    root = derived / "lance"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _build_tasks(embedder: Embedder, root: Path) -> tuple[int, str | None, str | None]:
    repo = repository_root()
    source = library_source(repo)
    task_dirs = discover_tasks(source.root)
    if not task_dirs:
        return 0, "no library tasks discovered", None
    task_refs: list[str] = []
    instructions: list[str] = []
    for td in task_dirs:
        try:
            manifest_bytes = (td / TASK_MANIFEST).read_bytes()
            manifest = tomllib.loads(manifest_bytes.decode("utf-8"))
            declared = manifest.get("task", {}).get("name")
            task_ref = (
                declared
                if isinstance(declared, str) and declared
                else td.relative_to(source.root).as_posix()
            )
            instr_path = td / TASK_INSTRUCTION
            if not instr_path.is_file():
                continue
            instr = instr_path.read_text(encoding="utf-8")
            full_ref = f"{source.source_repo}:{task_ref}"
            task_refs.append(full_ref)
            instructions.append(instr)
        except Exception:
            continue
    if not instructions:
        return 0, "no tasks with instruction.md", None
    vectors = embedder.embed(instructions)
    data = [
        {"task_ref": tr, "instruction": instr, "vector": vec}
        for tr, instr, vec in zip(task_refs, instructions, vectors, strict=True)
    ]
    db = lancedb.connect(str(root))
    if "tasks" in db.table_names():
        db.drop_table("tasks")
    tbl = db.create_table("tasks", data=data, mode="create")
    index_reason: str | None = None
    try:
        tbl.create_index(vector_column_name="vector", metric="cosine")
        # cosine because the embedder L2-normalises all vectors to unit length;
        # cosine distance on unit vectors is the appropriate metric for angular similarity
    except RuntimeError as e:
        if "Not enough rows to train" in str(e):
            index_reason = "too few rows for ANN index (exact brute-force search)"
        else:
            raise
    except Exception:
        raise
    return len(data), None, index_reason


def _build_trials(embedder: Embedder, root: Path) -> tuple[int, str | None, str | None]:
    derived = derived_root_from_environment(repository_root())
    parquet_root = derived / "parquet"
    if not parquet_root.is_dir():
        return 0, "no derived/parquet directory", None
    parquets = list(parquet_root.rglob("trial_facts.parquet"))
    if not parquets:
        return 0, "no trial_facts.parquet files", None
    rows: list[dict] = []
    texts: list[str] = []
    for p in parquets:
        try:
            table = pq.read_table(p)
            for row in table.to_pylist():
                job_id = str(row.get("job_id", ""))
                trial_id = str(row.get("trial_id", ""))
                task_name = str(row.get("task_name") or "")
                agent_name = str(row.get("agent_name") or "")
                reward = row.get("primary_reward")
                exc = str(row.get("exception_class") or "")
                text = f"{task_name} {agent_name} {exc}".strip() or "empty"
                rows.append(
                    {
                        "job_id": job_id,
                        "trial_id": trial_id,
                        "task_name": task_name,
                        "agent_name": agent_name,
                        "primary_reward": reward,
                        "exception_class": exc,
                        "text": text,
                        "vector": None,
                    }
                )
                texts.append(text)
        except Exception:
            continue
    if not texts:
        return 0, "no parsable trial rows", None
    vectors = embedder.embed(texts)
    for i, v in enumerate(vectors):
        rows[i]["vector"] = v
    db = lancedb.connect(str(root))
    if "trials" in db.table_names():
        db.drop_table("trials")
    tbl = db.create_table("trials", data=rows, mode="create")
    index_reason: str | None = None
    try:
        tbl.create_index(vector_column_name="vector", metric="cosine")
        # cosine because the embedder L2-normalises all vectors to unit length;
        # cosine distance on unit vectors is the appropriate metric for angular similarity
    except RuntimeError as e:
        if "Not enough rows to train" in str(e):
            index_reason = "too few rows for ANN index (exact brute-force search)"
        else:
            raise
    except Exception:
        raise
    return len(rows), None, index_reason


def build(table: str = "all") -> None:
    embedder: Embedder = HashingEmbedder()
    root = _lance_root()
    if table in ("tasks", "all"):
        n, reason, idx_reason = _build_tasks(embedder, root)
        if reason:
            print(f"tasks: skipped ({reason})")
        else:
            print(f"tasks: {n} rows")
            if idx_reason:
                print(f"tasks index: skipped ({idx_reason})")
            else:
                print("tasks index: created")
    if table in ("trials", "all"):
        n, reason, idx_reason = _build_trials(embedder, root)
        if reason:
            print(f"trials: skipped ({reason})")
        else:
            print(f"trials: {n} rows")
            if idx_reason:
                print(f"trials index: skipped ({idx_reason})")
            else:
                print("trials index: created")


def search(query: str, table: str = "tasks", k: int = 5) -> None:
    embedder: Embedder = HashingEmbedder()
    vec = embedder.embed([query])[0]
    root = _lance_root()
    db = lancedb.connect(str(root))
    if table not in db.table_names():
        print(f"table {table} not found")
        return
    tbl = db.open_table(table)
    res = tbl.search(vec).limit(k).to_list()
    for r in res:
        dist = r.get("_distance", float("nan"))
        cols = {k: v for k, v in r.items() if k != "vector"}
        print(f"dist={dist:.4f} {cols}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evallab.lance")
    sub = parser.add_subparsers(dest="cmd", required=True)
    bp = sub.add_parser("build", help="build or refresh tables")
    bp.add_argument("--table", choices=["tasks", "trials", "all"], default="all")
    sp = sub.add_parser("search", help="nearest neighbour search")
    sp.add_argument("query")
    sp.add_argument("--table", default="tasks")
    sp.add_argument("--k", type=int, default=5)
    args = parser.parse_args(argv)
    if args.cmd == "build":
        build(args.table)
    elif args.cmd == "search":
        search(args.query, args.table, args.k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
