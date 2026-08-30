"""LANCE: LanceDB vector store for tasks, trials, steps, and analyses beside DuckDB.

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
from typing import Any, Protocol

import lancedb
import pyarrow.parquet as pq
from lancedb.index import IvfPq

from evallab.craft import (
    TASK_INSTRUCTION,
    TASK_MANIFEST,
    discover_tasks,
    library_source,
    repository_root,
)
from evallab.storage.attach import attach
from evallab.storage.paths import derived_root_from_environment, shared_checkout_root

MIN_ROWS_FOR_ANN = 1000
"""Minimum rows to attempt ANN index.
Below this, exact brute-force search is used (correct and fast at current corpus sizes;
avoids LanceDB "dataset too small" and empty-cluster warnings). Applied uniformly to
tasks, trials, steps, analyses so policy is consistent.
"""

DEFAULT_REDACTION_POLICY: str = "default_redaction_v1"
DEFAULT_REDACTION_POLICY_DIGEST: str = hashlib.sha256(
    DEFAULT_REDACTION_POLICY.encode("utf-8")
).hexdigest()


@dataclass(frozen=True)
class LanceIndexManifest:
    """Manifest capturing provenance, schema, and invalidation metadata for a LanceDB index table."""

    table_name: str
    snapshot_digest: str
    candidate_pool_digest: str
    embedder_id: str
    embedder_version: str
    embedder_digest: str
    redaction_policy_digest: str
    row_count: int
    index_digest: str
    decision_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "table_name": self.table_name,
            "snapshot_digest": self.snapshot_digest,
            "candidate_pool_digest": self.candidate_pool_digest,
            "embedder_id": self.embedder_id,
            "embedder_version": self.embedder_version,
            "embedder_digest": self.embedder_digest,
            "redaction_policy_digest": self.redaction_policy_digest,
            "row_count": self.row_count,
            "index_digest": self.index_digest,
            "decision_eligible": self.decision_eligible,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LanceIndexManifest:
        return cls(
            table_name=str(d["table_name"]),
            snapshot_digest=str(d["snapshot_digest"]),
            candidate_pool_digest=str(d["candidate_pool_digest"]),
            embedder_id=str(d["embedder_id"]),
            embedder_version=str(d["embedder_version"]),
            embedder_digest=str(d["embedder_digest"]),
            redaction_policy_digest=str(d["redaction_policy_digest"]),
            row_count=int(d["row_count"]),
            index_digest=str(d["index_digest"]),
            decision_eligible=bool(d.get("decision_eligible", False)),
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> LanceIndexManifest:
        return cls.from_dict(json.loads(s))

    @staticmethod
    def compute_index_digest(
        table_name: str,
        snapshot_digest: str,
        candidate_pool_digest: str,
        embedder_digest: str,
        redaction_policy_digest: str,
        row_count: int,
    ) -> str:
        payload = {
            "table_name": table_name,
            "snapshot_digest": snapshot_digest,
            "candidate_pool_digest": candidate_pool_digest,
            "embedder_digest": embedder_digest,
            "redaction_policy_digest": redaction_policy_digest,
            "row_count": row_count,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Embedder(Protocol):
    """Protocol for text embedders. Implementations must be deterministic."""

    @property
    def identity(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def digest(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class HashingEmbedder:
    r"""Pure deterministic lexical embedder.

    Tokenises on \w+, hashes tokens stably with MD5 into dim buckets,
    accumulates counts, L2-normalises. Same text always yields identical
    vector in-process and across processes. Captures only lexical overlap,
    not semantics. Use a real model later via the protocol without changing
    callers.
    """

    dim: int = 256
    identity: str = "hashing"
    version: str = "v1"

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            f"{self.identity}:{self.version}:dim={self.dim}".encode("utf-8")
        ).hexdigest()

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


@dataclass(frozen=True)
class LanceSearchHit:
    """Typed search hit from LanceDB retrieval.

    Carries source identity and citation fields, but MUST NOT carry
    reward, primary_reward, exception_class, verdict, or decision fields.
    """

    record_id: str
    table: str
    job_id: str | None = None
    trial_id: str | None = None
    task_name: str | None = None
    step_id: int | str | None = None
    analysis_id: str | None = None
    source: str | None = None
    model: str | None = None
    category: str | None = None
    created_at: str | None = None
    text: str = ""
    distance: float = 0.0
    score: float = 0.0
    decision_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "table": self.table,
            "job_id": self.job_id,
            "trial_id": self.trial_id,
            "task_name": self.task_name,
            "step_id": self.step_id,
            "analysis_id": self.analysis_id,
            "source": self.source,
            "model": self.model,
            "category": self.category,
            "created_at": self.created_at,
            "text": self.text,
            "distance": self.distance,
            "score": self.score,
            "decision_eligible": self.decision_eligible,
        }


def _get_embedder_metadata(embedder: Embedder) -> tuple[str, str, str]:
    emb_id = getattr(embedder, "identity", "hashing")
    emb_ver = getattr(embedder, "version", "v1")
    digest_attr = getattr(embedder, "digest", None)
    if callable(digest_attr):
        emb_digest = str(digest_attr())
    elif isinstance(digest_attr, str):
        emb_digest = digest_attr
    else:
        dim_val = getattr(embedder, "dim", 256)
        emb_digest = hashlib.sha256(
            f"{emb_id}:{emb_ver}:dim={dim_val}".encode("utf-8")
        ).hexdigest()
    return emb_id, emb_ver, emb_digest


def _save_manifest(
    root: Path,
    table_name: str,
    snapshot_digest: str,
    candidate_pool_digest: str,
    embedder: Embedder,
    row_count: int,
    redaction_policy_digest: str = DEFAULT_REDACTION_POLICY_DIGEST,
) -> LanceIndexManifest:
    emb_id, emb_ver, emb_digest = _get_embedder_metadata(embedder)
    index_digest = LanceIndexManifest.compute_index_digest(
        table_name=table_name,
        snapshot_digest=snapshot_digest,
        candidate_pool_digest=candidate_pool_digest,
        embedder_digest=emb_digest,
        redaction_policy_digest=redaction_policy_digest,
        row_count=row_count,
    )
    manifest = LanceIndexManifest(
        table_name=table_name,
        snapshot_digest=snapshot_digest,
        candidate_pool_digest=candidate_pool_digest,
        embedder_id=emb_id,
        embedder_version=emb_ver,
        embedder_digest=emb_digest,
        redaction_policy_digest=redaction_policy_digest,
        row_count=row_count,
        index_digest=index_digest,
        decision_eligible=False,
    )
    manifest_path = root / f"{table_name}.manifest.json"
    manifest_path.write_text(manifest.to_json(), encoding="utf-8")
    return manifest


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
    if not isinstance(payload, dict) or not str(payload.get("schema_version", "")).startswith(
        "ATIF-"
    ):
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
    candidate_pool_digest = hashlib.sha256(
        "\n".join(sorted(task_refs)).encode("utf-8")
    ).hexdigest()
    snapshot_content = "\n".join(
        f"{tr}:{hashlib.sha256(instr.encode('utf-8')).hexdigest()}"
        for tr, instr in sorted(zip(task_refs, instructions, strict=True))
    )
    snapshot_digest = hashlib.sha256(snapshot_content.encode("utf-8")).hexdigest()
    _save_manifest(root, "tasks", snapshot_digest, candidate_pool_digest, embedder, n_rows)
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
    return n_rows, None, index_reason


def _build_trials(
    embedder: Embedder,
    root: Path,
    runs_root: Path | None = None,
) -> tuple[int, str | None, str | None]:
    repo = repository_root()
    att = attach(repo_root=repo)
    try:
        z3 = next((z for z in att.zones if z.name == "z3"), None)
        if z3 is None or not z3.attached:
            reason = z3.reason if z3 else "z3 not attached"
            detail = f" ({z3.detail})" if z3 and z3.detail else ""
            return 0, f"{reason}{detail}", None

        try:
            cur = att.connection.execute("SELECT * FROM trial_facts")
            cols = [c[0] for c in cur.description]
            raw_rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
        except Exception as e:
            return 0, f"error querying trial_facts: {e}", None
    finally:
        att.connection.close()

    if not raw_rows:
        return 0, f"no trial_facts rows ({z3.detail})", None

    runs_roots = _resolve_runs_roots(runs_root)
    rows: list[dict] = []
    texts: list[str] = []
    skipped: list[str] = []
    root_counts: dict[str, int] = {}

    for row in raw_rows:
        job_id = str(row.get("job_id") or "")
        trial_id = str(row.get("trial_id") or "")
        job_name = str(row.get("job_name") or "")
        trial_name = str(row.get("trial_name") or "")
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
            primary_traj = (
                (runs_roots[0] / job_name / trial_name / "agent" / "trajectory.json")
                if runs_roots
                else Path("unknown")
            )
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
    candidate_pool_digest = hashlib.sha256(
        "\n".join(sorted(r["trial_id"] for r in rows)).encode("utf-8")
    ).hexdigest()
    snapshot_content = "\n".join(
        f"{r['job_id']}:{r['trial_id']}:{hashlib.sha256(r['text'].encode('utf-8')).hexdigest()}"
        for r in sorted(rows, key=lambda x: (x["job_id"], x["trial_id"]))
    )
    snapshot_digest = hashlib.sha256(snapshot_content.encode("utf-8")).hexdigest()
    _save_manifest(root, "trials", snapshot_digest, candidate_pool_digest, embedder, n_rows)
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
    return n_rows, None, index_reason


def _build_steps(
    embedder: Embedder,
    root: Path,
    runs_root: Path | None = None,
) -> tuple[int, str | None, str | None]:
    repo = repository_root()
    att = attach(repo_root=repo)
    try:
        z3 = next((z for z in att.zones if z.name == "z3"), None)
        if z3 is None or not z3.attached:
            reason = z3.reason if z3 else "z3 not attached"
            detail = f" ({z3.detail})" if z3 and z3.detail else ""
            return 0, f"{reason}{detail}", None

        try:
            cur = att.connection.execute("SELECT * FROM trial_facts")
            cols = [c[0] for c in cur.description]
            raw_rows = [dict(zip(cols, r, strict=False)) for r in cur.fetchall()]
        except Exception as e:
            return 0, f"error querying trial_facts: {e}", None
    finally:
        att.connection.close()

    if not raw_rows:
        return 0, f"no trial_facts rows ({z3.detail})", None

    runs_roots = _resolve_runs_roots(runs_root)
    rows: list[dict] = []
    texts: list[str] = []
    skipped: list[str] = []
    root_counts: dict[str, int] = {}

    for row in raw_rows:
        job_id = str(row.get("job_id") or "")
        trial_id = str(row.get("trial_id") or "")
        job_name = str(row.get("job_name") or "")
        trial_name = str(row.get("trial_name") or "")
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
            primary_traj = (
                (runs_roots[0] / job_name / trial_name / "agent" / "trajectory.json")
                if runs_roots
                else Path("unknown")
            )
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
    candidate_pool_digest = hashlib.sha256(
        "\n".join(
            sorted(f"{r['job_id']}:{r['trial_id']}:{r['step_id']}" for r in rows)
        ).encode("utf-8")
    ).hexdigest()
    snapshot_content = "\n".join(
        f"{r['job_id']}:{r['trial_id']}:{r['step_id']}:{hashlib.sha256(r['message'].encode('utf-8')).hexdigest()}"
        for r in sorted(
            rows, key=lambda x: (x["job_id"], x["trial_id"], str(x["step_id"]))
        )
    )
    snapshot_digest = hashlib.sha256(snapshot_content.encode("utf-8")).hexdigest()
    _save_manifest(root, "steps", snapshot_digest, candidate_pool_digest, embedder, n_rows)
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
    return n_rows, None, index_reason


def _build_analyses(
    embedder: Embedder,
    root: Path,
) -> tuple[int, str | None, str | None]:
    repo = repository_root()
    derived = derived_root_from_environment(repo)
    analyses_parquet = derived / "analyses" / "analyses.parquet"
    if (
        not analyses_parquet.is_file()
        and (derived / "parquet" / "analyses" / "analyses.parquet").is_file()
    ):
        analyses_parquet = derived / "parquet" / "analyses" / "analyses.parquet"

    if not analyses_parquet.is_file():
        return 0, f"analyses.parquet not found ({analyses_parquet})", None

    try:
        tbl_pq = pq.read_table(analyses_parquet)
        raw_rows = tbl_pq.to_pylist()
    except Exception as e:
        return 0, f"error reading analyses.parquet: {e} ({analyses_parquet})", None

    if not raw_rows:
        return 0, f"no analyses rows ({analyses_parquet})", None

    trial_to_job: dict[str, str] = {}
    catalog_note: str | None = None
    att = attach(repo_root=repo)
    try:
        z3 = next((z for z in att.zones if z.name == "z3"), None)
        if z3 and z3.attached:
            try:
                cur = att.connection.execute("SELECT trial_id, job_id FROM trial_facts")
                for r in cur.fetchall():
                    if r[0] and r[1]:
                        trial_to_job[str(r[0])] = str(r[1])
            except Exception as e:
                # Never silent: without this map every row lacking an explicit job_id
                # is skipped for "missing identity", which reads as a data problem
                # rather than the catalog read failure it actually is.
                catalog_note = f"trial->job map unavailable ({type(e).__name__}: {e})"
        else:
            catalog_note = "trial->job map unavailable (z3 not attached)"
    finally:
        att.connection.close()
    if catalog_note:
        print(f"analyses: {catalog_note}")

    analysis_dirs = [
        repo / "research/analysis",
        repo / "research/evidence/analyses",
        derived / "analyses",
    ]
    rows: list[dict] = []
    texts: list[str] = []
    skipped: list[str] = []

    for row in raw_rows:
        analysis_id = str(row.get("analysis_id") or "").strip()
        trial_id = str(row.get("trial_id") or "").strip()
        job_id = str(row.get("job_id") or trial_to_job.get(trial_id) or "").strip()
        model = str(row.get("model") or "").strip()
        category = str(row.get("category") or "").strip()
        created_at = str(row.get("created_at") or "").strip()
        conclusion = str(
            row.get("conclusion") or row.get("summary") or row.get("text") or ""
        ).strip()

        corrupt_error = None
        if not conclusion or not job_id:
            for adir in analysis_dirs:
                json_path = adir / f"{analysis_id}.json"
                if not json_path.is_file():
                    json_path = adir / analysis_id / "analysis.json"
                if json_path.is_file():
                    try:
                        j_data = json.loads(json_path.read_text(encoding="utf-8"))
                        if isinstance(j_data, dict):
                            if not conclusion:
                                conclusion = str(
                                    j_data.get("summary")
                                    or j_data.get("conclusion")
                                    or j_data.get("text")
                                    or (
                                        j_data.get("output", {}).get("summary")
                                        if isinstance(j_data.get("output"), dict)
                                        else ""
                                    )
                                    or ""
                                ).strip()
                            if not job_id:
                                job_id = str(
                                    j_data.get("job_id") or trial_to_job.get(trial_id) or ""
                                ).strip()
                            if not model and j_data.get("model"):
                                model = str(j_data.get("model")).strip()
                            if not created_at and j_data.get("created_at"):
                                created_at = str(j_data.get("created_at")).strip()
                        else:
                            corrupt_error = f"invalid JSON in {json_path.name}"
                            break
                    except Exception as exc:
                        corrupt_error = f"corrupt {json_path.name}: {exc}"
                        break
                if conclusion and job_id:
                    break

        if corrupt_error:
            skipped.append(f"{analysis_id or 'unknown'} ({corrupt_error})")
            continue

        if (
            not analysis_id
            or not trial_id
            or not job_id
            or not model
            or not created_at
            or not conclusion
        ):
            skipped.append(analysis_id or "unknown")
            continue

        rows.append(
            {
                "analysis_id": analysis_id,
                "trial_id": trial_id,
                "job_id": job_id,
                "model": model,
                "category": category,
                "created_at": created_at,
                "conclusion": conclusion,
                "vector": None,
            }
        )
        texts.append(conclusion)

    skip_info: str | None = None
    if skipped:
        skip_examples = ", ".join(skipped[:3]) + (", ..." if len(skipped) > 3 else "")
        skip_info = f"{len(skipped)} skipped: {skip_examples}"

    if not texts:
        reason = (
            f"no valid analyses rows ({skip_info})"
            if skip_info
            else f"no valid analyses rows ({analyses_parquet})"
        )
        return 0, reason, None

    vectors = embedder.embed(texts)
    for i, v in enumerate(vectors):
        rows[i]["vector"] = v
    db = lancedb.connect(str(root))
    tbl = db.create_table("analyses", data=rows, mode="overwrite")
    index_reason: str | None = None
    n_rows = len(rows)
    candidate_pool_digest = hashlib.sha256(
        "\n".join(sorted(r["analysis_id"] for r in rows)).encode("utf-8")
    ).hexdigest()
    snapshot_content = "\n".join(
        f"{r['analysis_id']}:{hashlib.sha256(r['conclusion'].encode('utf-8')).hexdigest()}"
        for r in sorted(rows, key=lambda x: x["analysis_id"])
    )
    snapshot_digest = hashlib.sha256(snapshot_content.encode("utf-8")).hexdigest()
    _save_manifest(root, "analyses", snapshot_digest, candidate_pool_digest, embedder, n_rows)
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
    return n_rows, skip_info, index_reason


def search_records(
    query: str,
    table: str = "analyses",
    k: int = 5,
    *,
    snapshot_digest: str | None = None,
    candidate_pool_digest: str | None = None,
    embedder: Embedder | None = None,
    redaction_policy_digest: str | None = None,
    analysis_ready: bool = True,
    manifest: LanceIndexManifest | None = None,
) -> list[LanceSearchHit]:
    """Retrieve nearest records from a LanceDB table with validation and manifest checks.

    Strictly refuses non-analysis-ready pools and stale/mismatched manifests.
    Returns typed LanceSearchHit items with decision_eligible=False.
    """
    if not analysis_ready:
        raise ValueError(
            "Candidate pool is not analysis-ready: retrieval refused. "
            "AnalysisRecord and review artifacts require explicit analysis-ready pool."
        )
    if candidate_pool_digest is not None and not candidate_pool_digest.strip():
        raise ValueError("Candidate pool is invalid or candidate identity is missing")

    root = _lance_root()
    manifest_path = root / f"{table}.manifest.json"
    if manifest is None:
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"Manifest for table '{table}' not found at {manifest_path}"
            )
        try:
            manifest = LanceIndexManifest.from_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except Exception as e:
            raise ValueError(f"Corrupt manifest for table '{table}': {e}") from e

    if snapshot_digest is not None and snapshot_digest != manifest.snapshot_digest:
        raise ValueError("Snapshot digest mismatch: index is stale or from different vintage")
    if (
        candidate_pool_digest is not None
        and candidate_pool_digest != manifest.candidate_pool_digest
    ):
        raise ValueError("Candidate pool digest mismatch")
    if embedder is not None:
        _, _, emb_digest = _get_embedder_metadata(embedder)
        if emb_digest != manifest.embedder_digest:
            raise ValueError("Embedder digest mismatch: embedder configuration changed")
    if (
        redaction_policy_digest is not None
        and redaction_policy_digest != manifest.redaction_policy_digest
    ):
        raise ValueError("Redaction policy digest mismatch: index invalidated")

    db = lancedb.connect(str(root))
    tables_obj = db.list_tables()
    names = tables_obj.tables if hasattr(tables_obj, "tables") else list(tables_obj)
    if table not in names:
        raise FileNotFoundError(f"Table '{table}' not found in LanceDB at {root}")

    active_embedder: Embedder = embedder if embedder is not None else HashingEmbedder()
    vec = active_embedder.embed([query])[0]
    tbl = db.open_table(table)
    qb = tbl.search(vec)
    fn = getattr(qb, "distance_type", None)
    if callable(fn):
        qb = fn("cosine")
    res = qb.limit(k).to_list()

    hits: list[LanceSearchHit] = []
    for r in res:
        raw_dist = float(r.get("_distance", 0.0))
        text_val = str(
            r.get("text")
            or r.get("conclusion")
            or r.get("message")
            or r.get("instruction")
            or ""
        )

        # Self-distance is strictly 0.0 for exact matches or float precision rounding
        if text_val.strip() == query.strip() or abs(raw_dist) < 1e-6:
            dist = 0.0
        else:
            dist = raw_dist

        score = max(0.0, 1.0 - dist)

        record_id = str(
            r.get("analysis_id")
            or r.get("task_ref")
            or (
                f"{r.get('trial_id')}:{r.get('step_id')}"
                if r.get("step_id") is not None and r.get("trial_id")
                else None
            )
            or r.get("trial_id")
            or r.get("record_id")
            or ""
        )

        hit = LanceSearchHit(
            record_id=record_id,
            table=table,
            job_id=str(r["job_id"]) if r.get("job_id") is not None else None,
            trial_id=str(r["trial_id"]) if r.get("trial_id") is not None else None,
            task_name=str(r.get("task_name") or r.get("task_ref") or "")
            if (r.get("task_name") or r.get("task_ref")) is not None
            else None,
            step_id=r.get("step_id"),
            analysis_id=str(r["analysis_id"]) if r.get("analysis_id") is not None else None,
            source=str(r["source"]) if r.get("source") is not None else None,
            model=str(r.get("model") or r.get("agent_version") or "")
            if (r.get("model") or r.get("agent_version")) is not None
            else None,
            category=str(r["category"]) if r.get("category") is not None else None,
            created_at=str(r["created_at"]) if r.get("created_at") is not None else None,
            text=text_val,
            distance=dist,
            score=score,
            decision_eligible=False,
        )
        hits.append(hit)

    return hits


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
    if table in ("analyses", "all"):
        n, reason, idx_reason = _build_analyses(embedder, root)
        if n == 0 and reason:
            print(f"analyses: skipped ({reason})")
        else:
            skip_msg = f" ({reason})" if reason else ""
            print(f"analyses: {n} rows{skip_msg}")
            if idx_reason:
                print(f"analyses index: skipped ({idx_reason})")
            else:
                print("analyses index: created")


def search(query: str, table: str = "tasks", k: int = 5) -> None:
    embedder: Embedder = HashingEmbedder()
    vec = embedder.embed([query])[0]
    root = _lance_root()
    db = lancedb.connect(str(root))
    tables_obj = db.list_tables()
    names = tables_obj.tables if hasattr(tables_obj, "tables") else list(tables_obj)
    if table not in names:
        print(f"table {table} not found")
        return
    tbl = db.open_table(table)
    qb = tbl.search(vec)
    fn = getattr(qb, "distance_type", None)
    if callable(fn):
        qb = fn("cosine")
    res = qb.limit(k).to_list()
    for r in res:
        dist = r.get("_distance", float("nan"))
        cols = {k: v for k, v in r.items() if k != "vector"}
        print(f"dist={dist:.4f} {cols}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evallab.lance")
    sub = parser.add_subparsers(dest="cmd", required=True)
    bp = sub.add_parser("build", help="build or refresh tables")
    bp.add_argument(
        "--table",
        choices=["tasks", "trials", "steps", "analyses", "all"],
        default="all",
    )
    bp.add_argument(
        "--runs-root",
        type=Path,
        default=None,
        help=(
            "explicit override for runs root (also EVALLAB_RUNS_ROOT env); "
            "enables worktree support and custom locations"
        ),
    )
    sp = sub.add_parser("search", help="nearest neighbour search")
    sp.add_argument("query")
    sp.add_argument(
        "--table",
        choices=["tasks", "trials", "steps", "analyses"],
        default="tasks",
    )
    sp.add_argument("--k", type=int, default=5)
    args = parser.parse_args(argv)
    if args.cmd == "build":
        build(args.table, getattr(args, "runs_root", None))
    elif args.cmd == "search":
        search(args.query, args.table, args.k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
