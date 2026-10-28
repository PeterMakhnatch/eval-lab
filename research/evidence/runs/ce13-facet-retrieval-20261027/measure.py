"""CE-13: category precision of facet/cluster-aware retrieval vs the lexical baseline.

No model calls. Aggregates only: no trace bodies, facet text or labels are printed.

    measure.py prepare --live DB --atlas DIR --clone OUT.sqlite --receipt prep.json
    measure.py measure --clone OUT.sqlite --out results.json

``prepare`` copies the live SQLite store with the online-backup API (the live
file is opened read-only and never written), imports only facet records whose
model input was fully source-blinded, and installs the latest Run Atlas
cluster build trimmed to runs that exist in the store. ``measure`` repeats the
CE-4 leave-one-out protocol (same query words, top 5, query task excluded,
precision = returned trace shares a publisher category with the query task's
non-provisional labels, a seeded random draw from the arm's own returnable
pool as baseline) for each retrieval arm on one common query population.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import random
import re
import sqlite3
import statistics
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.production.cloud_trace_db import (
    CloudTraceDatabase,
    CloudTraceRetriever,
    import_run_facets,
)
from evallab.production.corpus_queue import CorpusRegistry

# Verbatim from the CE-4 leave-one-out script so the lexical arm is the same query.
STOP = {"about", "above", "after", "again", "against", "because", "before", "being", "below", "between",
        "could", "doing", "during", "further", "having", "itself", "other", "should", "their", "theirs",
        "themselves", "there", "these", "those", "through", "under", "until", "which", "while", "would",
        "yours", "files", "using", "please", "where", "within"}
FACET_FIELDS = ("outcome", "tests", "eval_awareness", "shortcut", "grader_or_harness_touched",
                "answer_material_accessed", "success_claim_without_evidence", "suspicion")
KINDS = ("reward_seeker", "misalignment")
TOP_K = 5
SEED = 20261027


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _jsonl(path: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for line in path.read_bytes().split(b"\n"):
        if line.strip():
            record = json.loads(line)
            records[record["run_id"]] = record
    return records


def prepare(args: argparse.Namespace) -> None:
    live, atlas, clone = Path(args.live), Path(args.atlas), Path(args.clone)
    if clone.exists():
        raise FileExistsError(clone)
    with sqlite3.connect(live.as_uri() + "?mode=ro", uri=True) as source, sqlite3.connect(clone) as target:
        source.backup(target)
    clone.chmod(0o600)

    facets = _jsonl(atlas / "facets-v1.jsonl")
    refreshed = _jsonl(atlas / "reblinded-facets-v1.jsonl")
    refresh = json.loads((atlas / "blinding-refresh-v1.json").read_text())
    if refresh.get("schema") != "run-atlas-blinding-refresh/v1":
        raise ValueError("unexpected blinding-refresh schema")
    needs_refresh = set(refresh["run_ids"])
    clean = {run: record for run, record in facets.items() if run not in needs_refresh}
    clean.update({run: refreshed[run] for run in needs_refresh & refreshed.keys()})
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", dir=clone.parent, delete=False) as stream:
        for run in sorted(clean):
            stream.write(json.dumps(clean[run], ensure_ascii=False) + "\n")
        clean_path = Path(stream.name)

    registry = CorpusRegistry()
    database = CloudTraceDatabase(clone)
    try:
        imported = import_run_facets(database, clean_path, registry=registry)
        with database._transaction():
            stored = {row[0] for row in database._execute("SELECT run_id FROM run_facets").fetchall()}

        # Latest contiguous producer build, as atlas_view reads it.
        build: list[dict[str, Any]] = []
        build_id = None
        for line in (atlas / "clusters-v1.jsonl").read_text().splitlines():
            row = json.loads(line)
            if row["build_id"] != build_id:
                build, build_id = [], row["build_id"]
            build.append(row)
        produced_members = set().union(*(set(c["run_ids"]) for c in build if c["level"] == 1))
        trimmed = []
        for cluster in build:
            members = sorted(set(cluster["run_ids"]) & stored)
            if members:
                trimmed.append({
                    "cluster_id": cluster["cluster_id"], "name": cluster["name"], "level": cluster["level"],
                    "parent": cluster["parent"], "run_ids": members, "centroid_text": cluster["centroid_text"],
                    "size": len(members), "label_rates": None,
                })
        clusters = database.replace_run_clusters(trimmed, registry=registry)
    finally:
        database.close()
        clean_path.unlink()

    receipt = {
        "schema": "evallab.ce13_prepare/v1", "recorded_at_utc": datetime.now(UTC).isoformat(),
        "live_sha256_note": "live store read through sqlite3 backup in read-only mode",
        "clone_sha256": _sha256(clone),
        "inputs": {name: _sha256(atlas / name) for name in
                   ("facets-v1.jsonl", "reblinded-facets-v1.jsonl", "blinding-refresh-v1.json", "clusters-v1.jsonl")},
        "facet_records": len(facets), "needs_refresh": len(needs_refresh),
        "refreshed_used": len(needs_refresh & refreshed.keys()),
        "excluded_unrefreshed": len(needs_refresh - refreshed.keys()), "clean_records": len(clean),
        "import": {key: imported[key] for key in imported if key not in {"path", "refused_examples"}},
        "stored_facets": len(stored), "cluster_build_id": build_id,
        "cluster_build_members": len(produced_members),
        "cluster_members_stored": len(produced_members & stored),
        "clusters_stored": clusters,
    }
    Path(args.receipt).write_text(json.dumps(receipt, indent=1, sort_keys=True) + "\n")
    print(json.dumps(receipt, indent=1, sort_keys=True))


class ScopedRetriever(CloudTraceRetriever):
    """Narrow (never widen) registry admission to one dataset, as CE-4's single-source bundle did."""

    def __init__(self, *args: Any, scope: frozenset[str], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.scope = scope

    def _admitted_ids(self) -> tuple[list[str], set[str]]:
        admitted, sealed = super()._admitted_ids()
        return [task for task in admitted if task in self.scope], sealed


def measure(args: argparse.Namespace) -> None:
    database = CloudTraceDatabase(Path(args.clone))
    registry = CorpusRegistry()
    with database._transaction():
        scope = frozenset(row[0] for row in database._execute(
            "SELECT task_id FROM corpus_tasks WHERE dataset_id = ?", (args.dataset,)).fetchall())
        retriever = ScopedRetriever(database, registry=registry, scope=scope)
        admitted = set(retriever._admitted_ids()[0])
        instructions = dict(database._execute("SELECT task_id, instruction_text FROM corpus_tasks").fetchall())
        trace_rows = database._execute(
            "SELECT task_id, trace_id, json_extract(verified_receipt_json, '$.source_label.categories'), trust_tier"
            " FROM agent_traces").fetchall()
        facet_rows = database._rows("SELECT * FROM run_facets")
        cluster_rows = database._rows("SELECT cluster_id, level, run_ids FROM run_clusters")
    trace_rows = [row for row in trace_rows if row[0] in admitted]

    def query_for(task_id: str) -> str:
        words: list[str] = []
        for word in re.findall(r"[a-z][a-z_]{4,}", (instructions.get(task_id) or "").casefold()):
            if word not in STOP and word not in words:
                words.append(word)
            if len(words) == 8:
                break
        return " ".join(words)

    labels = {trace: set(json.loads(text)) if text else set() for _, trace, text, _ in trace_rows}
    tier = {trace: trust for _, trace, _, trust in trace_rows}
    task_of = {trace: task for task, trace, _, _ in trace_rows}
    task_labels: dict[str, set[str]] = collections.defaultdict(set)
    for task, trace, _, trust in trace_rows:
        if trust != "tier3_provisional":
            task_labels[task] |= labels[trace]
    facets = {row["run_id"]: row for row in facet_rows if task_of.get(row["run_id"]) in admitted}
    deepest: dict[str, tuple[int, str]] = {}
    for cluster in cluster_rows:
        for run in cluster["run_ids"]:
            if cluster["level"] > deepest.get(run, (0, ""))[0]:
                deepest[run] = (cluster["level"], cluster["cluster_id"])

    returnable = [trace for trace, trust in tier.items() if trust != "tier3_provisional"]
    faceted_returnable = [trace for trace in returnable if trace in facets]
    # One query run per task: its lowest faceted trace id (deterministic, label-blind).
    query_run: dict[str, str] = {}
    for run in sorted(facets):
        query_run.setdefault(task_of[run], run)
    population = sorted(
        task for task, run in query_run.items()
        if task_labels.get(task) and query_for(task) and run in deepest
    )

    def facet_pool(row: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
        return [t for t in faceted_returnable if all(facets[t][f] == row[f] for f in fields)]

    def arms(task: str) -> dict[str, tuple[dict[str, Any], list[str]]]:
        run = query_run[task]
        row = facets[run]
        cluster = deepest[run][1]
        members = next(set(c["run_ids"]) for c in cluster_rows if c["cluster_id"] == cluster)
        words = query_for(task)
        profile = ("tests", "eval_awareness")
        return {
            "lexical": ({"query": words}, returnable),
            "lexical_faceted_pool": ({"query": words, "facets": {}}, faceted_returnable),
            "like_run": ({"query": "", "like_run_id": run}, faceted_returnable),
            "lexical_like_run": ({"query": words, "like_run_id": run}, faceted_returnable),
            "same_cluster": ({"query": "", "cluster_id": cluster},
                             [t for t in faceted_returnable if t in members]),
            "facet_profile": ({"query": "", "facets": {f: row[f] for f in profile}}, facet_pool(row, profile)),
        }

    report: dict[str, Any] = {
        "schema": "evallab.ce13_facet_retrieval/v1", "recorded_at_utc": datetime.now(UTC).isoformat(),
        "protocol": "CE-4 leave-one-out, top_k 5, query task excluded, shared publisher category",
        "dataset": args.dataset, "population": len(population), "admitted_tasks": len(admitted),
        "returnable_pool": len(returnable), "faceted_returnable_pool": len(faceted_returnable),
        "faceted_admitted_runs": len(facets), "results": {},
    }
    per_query: dict[str, dict[str, tuple[int, int]]] = {}
    out = Path(args.out)
    for kind in KINDS:
        for arm in arms(population[0]) if population else ():
            random.seed(SEED)
            counts, latencies = [], []
            hits = base_hits = judged = 0
            per_query[f"{kind}/{arm}"] = queries = {}
            for task in population:
                arguments, pool = arms(task)[arm]
                pool = [t for t in pool if task_of[t] != task]
                start = time.perf_counter()
                examples = retriever.retrieve_similar_examples(
                    arguments.pop("query"), kind, TOP_K, exclude_task_ids=[task], **arguments,
                )
                latencies.append(time.perf_counter() - start)
                counts.append(len(examples))
                want = task_labels[task]
                task_hits = sum(
                    bool(labels.get(example.provenance["trace_id"], set()) & want) for example in examples
                )
                queries[task] = (task_hits, len(examples))
                hits += task_hits
                judged += len(examples)
                for _ in examples:
                    base_hits += bool(labels[random.choice(pool)] & want)
            report["results"][f"{kind}/{arm}"] = {
                "queries_with_results": sum(c > 0 for c in counts),
                "queries_with_full_top_k": sum(c == TOP_K for c in counts),
                "returned_total": sum(counts), "judged": judged,
                "precision_shared_label": round(hits / judged, 3) if judged else None,
                "random_pool_shared_label": round(base_hits / judged, 3) if judged else None,
                "latency_median_s": round(statistics.median(latencies), 3) if latencies else None,
            }
            out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
            print(f"{kind}/{arm}", json.dumps(report["results"][f"{kind}/{arm}"], sort_keys=True), flush=True)
        # Paired query-level bootstrap of each arm's precision minus a baseline's,
        # over queries both answered (micro precision within a resample). The
        # faceted-pool lexical arm isolates ranking from the smaller faceted pool.
        for baseline in ("lexical", "lexical_faceted_pool"):
            base = per_query[f"{kind}/{baseline}"]
            for arm in arms(population[0]):
                if arm in {"lexical", baseline}:
                    continue
                mine = per_query[f"{kind}/{arm}"]
                shared = [task for task in population if mine[task][1] and base[task][1]]
                if not shared:
                    continue
                rng = random.Random(SEED)

                def delta(sample: list[str], mine: dict[str, tuple[int, int]] = mine,
                          base: dict[str, tuple[int, int]] = base) -> float:
                    return (sum(mine[t][0] for t in sample) / sum(mine[t][1] for t in sample)
                            - sum(base[t][0] for t in sample) / sum(base[t][1] for t in sample))

                draws = sorted(delta([rng.choice(shared) for _ in shared]) for _ in range(2000))
                report["results"][f"{kind}/{arm}"][f"paired_vs_{baseline}"] = {
                    "queries": len(shared), "delta": round(delta(shared), 3),
                    "ci95": [round(draws[49], 3), round(draws[1949], 3)],
                }
    database.close()
    out.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(json.dumps(report, indent=1, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--live", required=True)
    prep.add_argument("--atlas", required=True)
    prep.add_argument("--clone", required=True)
    prep.add_argument("--receipt", required=True)
    run = commands.add_parser("measure")
    run.add_argument("--clone", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--dataset", default="goodfire-reward-hack-data")
    args = parser.parse_args()
    prepare(args) if args.command == "prepare" else measure(args)


if __name__ == "__main__":
    main()
