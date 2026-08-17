"""LANCE: LanceDB vector store for tasks and trials beside DuckDB.

Default embedder is deterministic lexical (hashing) only; no semantics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import lancedb
import pyarrow as pa
import pyarrow.parquet as pq
from lancedb.index import IvfPq

from evallab.craft import (
    TASK_INSTRUCTION,
    TASK_MANIFEST,
    discover_tasks,
    library_source,
    repository_root,
)
from evallab.paths import derived_root_from_environment, shared_checkout_root


MIN_ROWS_FOR_ANN = 1000
"""Minimum rows to attempt ANN index.
Below this, exact brute-force search is used (correct and fast at current corpus sizes;
avoids LanceDB "dataset too small" and empty-cluster warnings). Applied uniformly to
tasks, trials, steps so policy is consistent.
"""


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

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            v = [0.0] * self.dim
            for tok in re.findall(r"\w+", text.lower()):
                h = int(hashlib.md5(tok.encode()).hexdigest(), 16) % self.dim
                v[h] += 1
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vectors.append([x / norm for x in v])
        return vectors


def _lance_root() -> Path:
    repo = repository_root()
    derived = derived_root_from_environment(repo)
    root = derived / "lance"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _load_trajectory_steps(path: Path) -> list[dict] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not str(
        payload.get("schema_version", "")
    ).startswith("ATIF-"):
        return None
    steps = payload.get("steps")
    return [item for item in steps if isinstance(item, dict)] if isinstance(steps, list) else []


def _resolve_runs_roots(runs_root: Path | None = None) -> list[Path]:
    """Return ordered list of candidate runs roots to search for trajectories.
    Prefers explicit override (CLI or EVALLAB_RUNS_ROOT), else shared primary
    checkout's runs/ + research/evidence/runs/ (for promoted bundles).
    Per-root counts reported; exact missing path kept in skip reasons.
    """
    if runs_root is None:
        env = os.environ.get("EVALLAB_RUNS_ROOT")
        if env:
            runs_root = Path(env)
    if runs_root is not None:
        return [Path(runs_root).resolve()]
    repo = repository_root()
    primary = shared_checkout_root(repo)
    cands = [
        primary / "runs",
        primary / "research/evidence/runs",
        primary / "evidence/runs",
    ]
    seen: set[Path] = set()
    roots: list[Path] = []
    for c in cands:
        rc = c.resolve()
        if rc not in seen:
            seen.add(rc)
            roots.append(c)
    return roots


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
    tbl = db.create_table("tasks", data=data, mode="overwrite")
    index_reason: str | None = None
    n_rows = len(data)
    if n_rows < MIN_ROWS_FOR_ANN:
        index_reason = "too few rows for ANN index (exact brute-force search)"
    else:
        try:
            tbl.create_index("vector", config=IvfPq(distance_type="cosine"))
        except RuntimeError as e:
            if "Not enough rows to train" in str(e):
                index_reason = "too few rows for ANN index (exact brute-force search)"
            else:
                raise
        except Exception:
            raise
    return n_rows, None, index_reason


def _build_trials(embedder: Embedder, root: Path, runs_root: Path | None = None) -> tuple[int, str | None, str | None]:
    derived = derived_root_from_environment(repository_root())
    parquet_root = derived
    if not parquet_root.is_dir():
        return 0, f"no derived/parquet directory ({parquet_root})", None
    parquets = list(parquet_root.rglob("trial_facts.parquet"))
    if not parquets:
        return 0, f"no trial_facts.parquet files ({parquet_root})", None
    runs_roots = _resolve_runs_roots(runs_root)
    rows: list[dict] = []
    texts: list[str] = []
    skipped: list[str] = []
    root_counts: dict[str, int] = {}
    arrow_tables: list[pa.Table] = []
    for p in parquets:
        try:
            table = pq.read_table(p)
            arrow_tables.append(table)
        except Exception:
            continue
    if not arrow_tables:
        return 0, "no parsable trial rows", None
    combined = pa.concat_tables(arrow_tables, promote_options="permissive")
    for row in combined.to_pylist():
        job_id = str(row.get("job_id", ""))
        trial_id = str(row.get("trial_id", ""))
        job_name = str(row.get("job_name", ""))
        trial_name = str(row.get("trial_name", ""))
        task_name = str(row.get("task_name") or "")
        agent_version = str(row.get("agent_version") or row.get("agent_name") or "")
        reward = row.get("primary_reward")
        exc = str(row.get("exception_class") or "")
        exc_phase = str(row.get("exception_phase") or "")
        steps = []
        for rroot in runs_roots:
            traj_path = rroot / job_name / trial_name / "agent" / "trajectory.json"
            s = _load_trajectory_steps(traj_path) or []
            if s:
                root_key = str(rroot)
                root_counts[root_key] = root_counts.get(root_key, 0) + 1
                steps = s
                break
        if not steps:
            primary_traj = (runs_roots[0] / job_name / trial_name / "agent" / "trajectory.json") if runs_roots else Path("unknown")
            skipped.append(str(primary_traj))
            text = f"{task_name} {agent_version} {exc} {exc_phase}".strip() or "empty"
        else:
            MAX_CHARS = 2048
            HEAD = 8
            TAIL = 4
            msgs: list[str] = []
            for s in steps:
                m = s.get("message")
                if isinstance(m, dict):
                    m = str(m)
                if isinstance(m, str):
                    msgs.append(m)
            if msgs:
                head = msgs[:HEAD]
                tail = msgs[-TAIL:] if len(msgs) > HEAD + TAIL else []
                sep = ["..."] if len(msgs) > HEAD + TAIL else []
                doc = "\n".join(head + sep + tail)
                text = doc[:MAX_CHARS]
            else:
                text = f"{task_name} {agent_version} {exc} {exc_phase}".strip() or "empty"
        rows.append(
            {
                "job_id": job_id,
                "trial_id": trial_id,
                "task_name": task_name,
                "agent_version": agent_version,
                "primary_reward": reward,
                "exception_class": exc,
                "exception_phase": exc_phase,
                "text": text,
                "vector": None,
            }
        )
        texts.append(text)
    if not texts:
        reason = "no parsable trial rows"
        if skipped:
            reason = f"missing {len(skipped)} trajectories (e.g. {skipped[0]})"
        return 0, reason, None
    if root_counts:
        counts_str = "; ".join(f"{k}: {v}" for k, v in sorted(root_counts.items()))
        print(f"trials per-root trajectories: {counts_str}")
    if skipped:
        print(f"missing {len(skipped)} trajectories (e.g. {skipped[0]})")
    vectors = embedder.embed(texts)
    for i, v in enumerate(vectors):
        rows[i]["vector"] = v
    db = lancedb.connect(str(root))
    tbl = db.create_table("trials", data=rows, mode="overwrite")
    index_reason: str | None = None
    n_rows = len(rows)
    if n_rows < MIN_ROWS_FOR_ANN:
        index_reason = "too few rows for ANN index (exact brute-force search)"
    else:
        try:
            tbl.create_index("vector", config=IvfPq(distance_type="cosine"))
        except RuntimeError as e:
            if "Not enough rows to train" in str(e):
                index_reason = "too few rows for ANN index (exact brute-force search)"
            else:
                raise
        except Exception:
            raise
    return n_rows, None, index_reason


def _build_steps(embedder: Embedder, root: Path, runs_root: Path | None = None) -> tuple[int, str | None, str | None]:
    derived = derived_root_from_environment(repository_root())
    parquet_root = derived
    if not parquet_root.is_dir():
        return 0, f"no derived/parquet directory ({parquet_root})", None
    parquets = list(parquet_root.rglob("trial_facts.parquet"))
    if not parquets:
        return 0, f"no trial_facts.parquet files ({parquet_root})", None
    runs_roots = _resolve_runs_roots(runs_root)
    rows: list[dict] = []
    texts: list[str] = []
    skipped: list[str] = []
    root_counts: dict[str, int] = {}
    arrow_tables: list[pa.Table] = []
    for p in parquets:
        try:
            table = pq.read_table(p)
            arrow_tables.append(table)
        except Exception:
            continue
    if not arrow_tables:
        return 0, "no parsable trial rows", None
    combined = pa.concat_tables(arrow_tables, promote_options="permissive")
    for row in combined.to_pylist():
        job_id = str(row.get("job_id", ""))
        trial_id = str(row.get("trial_id", ""))
        job_name = str(row.get("job_name", ""))
        trial_name = str(row.get("trial_name", ""))
        task_name = str(row.get("task_name") or "")
        reward = row.get("primary_reward")
        steps = []
        for rroot in runs_roots:
            traj_path = rroot / job_name / trial_name / "agent" / "trajectory.json"
            s = _load_trajectory_steps(traj_path) or []
            if s:
                root_key = str(rroot)
                root_counts[root_key] = root_counts.get(root_key, 0) + len(s)
                steps = s
                break
        if not steps:
            primary_traj = (runs_roots[0] / job_name / trial_name / "agent" / "trajectory.json") if runs_roots else Path("unknown")
            skipped.append(str(primary_traj))
            continue
        for step in steps:
            step_id = step.get("step_id")
            source = str(step.get("source") or "")
            m = step.get("message")
            if isinstance(m, dict):
                m = str(m)
            msg_text = str(m) if isinstance(m, str) else ""
            rows.append(
                {
                    "job_id": job_id,
                    "trial_id": trial_id,
                    "task_name": task_name,
                    "step_id": step_id,
                    "source": source,
                    "primary_reward": reward,
                    "message": msg_text,
                    "vector": None,
                }
            )
            texts.append(msg_text)
    if not texts:
        reason = "no trajectory steps found"
        if skipped:
            reason = f"missing {len(skipped)} trajectories (e.g. {skipped[0]})"
        return 0, reason, None
    if root_counts:
        counts_str = "; ".join(f"{k}: {v}" for k, v in sorted(root_counts.items()))
        print(f"steps per-root trajectories: {counts_str}")
    if skipped:
        print(f"missing {len(skipped)} trajectories (e.g. {skipped[0]})")
    vectors = embedder.embed(texts)
    for i, v in enumerate(vectors):
        rows[i]["vector"] = v
    db = lancedb.connect(str(root))
    tbl = db.create_table("steps", data=rows, mode="overwrite")
    index_reason: str | None = None
    n_rows = len(rows)
    if n_rows < MIN_ROWS_FOR_ANN:
        index_reason = "too few rows for ANN index (exact brute-force search)"
    else:
        try:
            tbl.create_index("vector", config=IvfPq(distance_type="cosine"))
        except RuntimeError as e:
            if "Not enough rows to train" in str(e):
                index_reason = "too few rows for ANN index (exact brute-force search)"
            else:
                raise
        except Exception:
            raise
    return n_rows, None, index_reason


def build(table: str = "all", runs_root: Path | None = None) -> None:
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
        n, reason, idx_reason = _build_trials(embedder, root, runs_root)
        if reason:
            print(f"trials: skipped ({reason})")
        else:
            print(f"trials: {n} rows")
            if idx_reason:
                print(f"trials index: skipped ({idx_reason})")
            else:
                print("trials index: created")
    if table in ("steps", "all"):
        n, reason, idx_reason = _build_steps(embedder, root, runs_root)
        if reason:
            print(f"steps: skipped ({reason})")
        else:
            print(f"steps: {n} rows")
            if idx_reason:
                print(f"steps index: skipped ({idx_reason})")
            else:
                print("steps index: created")


def search(query: str, table: str = "tasks", k: int = 5) -> None:
    embedder: Embedder = HashingEmbedder()
    vec = embedder.embed([query])[0]
    root = _lance_root()
    db = lancedb.connect(str(root))
    if table not in db.list_tables():
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
    bp.add_argument("--table", choices=["tasks", "trials", "steps", "all"], default="all")
    bp.add_argument("--runs-root", type=Path, default=None, help="explicit override for runs root (also EVALLAB_RUNS_ROOT env); enables worktree support and custom locations")
    sp = sub.add_parser("search", help="nearest neighbour search")
    sp.add_argument("query")
    sp.add_argument("--table", default="tasks")
    sp.add_argument("--k", type=int, default=5)
    args = parser.parse_args(argv)
    if args.cmd == "build":
        build(args.table, getattr(args, "runs_root", None))
    elif args.cmd == "search":
        search(args.query, args.table, args.k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
