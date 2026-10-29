"""NP29-CM-1 data access: strict cross-key first, then steps; facet and Flake loaders. Never prints a cell."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Collection, Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
RESULTS = HERE.parent / "results"
REPO = Path(
    os.environ.get("EVALLAB_REPO")
    or subprocess.run(
        ["git", "-C", str(HERE), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
)
sys.path.insert(0, str(REPO / "src"))
os.environ["EVALLAB_DISABLE_LOCAL_DOCKER"] = "1"
sys.dont_write_bytecode = True

STATE = Path.home() / ".local/state"
SAMPLE = (
    Path.home()
    / "Developer/research-context/bridge/2026-10-25-systems-and-swarms"
    / "raw/cm2-monitor-20261026/goodfire-sample.json"
)
FLAKE_STATE = STATE / "evallab-np21-cm1"
HARNESS = STATE / "evallab-prod-live/trace-harness"
CM11B = HARNESS / "sources/cm11b"
HAIKU = "anthropic/claude-haiku-5-5"
V2 = "run-atlas-prompt/v2"
RUN_ID = re.compile(r'"run_id"\s*:\s*"([^"]+)"')
RECORD_FIELDS = (
    "answer_material_accessed", "approach", "eval_awareness", "evidence_steps", "facet_model",
    "facet_version", "goal", "grader_or_harness_touched", "input_tokens", "outcome", "output_tokens",
    "run_id", "shortcut", "source", "split", "success_claim_without_evidence", "suspicion", "tests",
)  # fmt: skip


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def first_prompt(doc: Mapping[str, Any] | None) -> str | None:
    for step in (doc or {}).get("steps") or []:
        if (
            step.get("source") == "user"
            and isinstance(step.get("message"), str)
            and step["message"].strip()
        ):
            return step["message"]
    return None


def task_key(run_id: str, doc: Mapping[str, Any] | None, corpus: str) -> tuple[str, bool]:
    """``(key, has_prompt)``. Goodfire: one run per task (the run id). CheatBench: the registry holds only the suite id, so the
    key is the sha256 of the whitespace-normalised first user message (label-free); a run with no user step has no task prompt
    and is its own cluster (an upper bound on distinct tasks, pessimistic for clustering)."""
    if corpus == "goodfire":
        return run_id, True
    prompt = first_prompt(doc)
    if prompt is None:
        return run_id, False
    return hashlib.sha256(" ".join(prompt.split()).encode()).hexdigest()[:16], True


def load_cohorts() -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """``{run_id: {corpus, class, doc, task, has_prompt}}`` after the strict cross-key (``assignments.csv`` guard) runs FIRST."""
    from evallab.production import run_atlas as ra
    from evallab.production import run_atlas_score as sc
    from evallab.production.corpus_queue import CorpusRegistry
    from evallab.results_home import results_root

    registry = CorpusRegistry(REPO)
    gf_ids = sorted({row["trace_id"] for row in json.loads(SAMPLE.read_text())})
    cb_items = [
        i
        for i in registry.items(dataset_id=ra.CHEATBENCH_DATASET, split="dev")
        if not registry.assigned_test_anywhere(item_id=i.item_id, task_id=i.task_id)
        and (i.metadata.get("trial_ref") or {}).get("path")
    ]
    cb_ids = sorted(i.item_id for i in cb_items)
    required = set(gf_ids) | set(cb_ids)
    with closing(ra.connect_readonly(ra.DEFAULT_DATABASE)) as c:
        labels, checked = sc._read_labels(
            c, registry, required=required, wanted=required
        )  # cross-key FIRST
        admitted = set(labels)
        docs = {r: ra.cloud_doc(c, r) for r in gf_ids if r in admitted}
    for run in ra.cheatbench_runs(
        registry, results_root(), item_ids=[r for r in cb_ids if r in admitted]
    ):
        docs[run.run_id] = run.doc
    cb = set(cb_ids)
    out: dict[str, dict[str, Any]] = {}
    for r in admitted:
        corpus = "cheatbench" if r in cb else "goodfire"
        key, has_prompt = task_key(r, docs.get(r), corpus)
        out[r] = {
            "corpus": corpus,
            "class": labels[r].stratum,
            "doc": docs.get(r),
            "task": key,
            "has_prompt": has_prompt,
        }
    meta = {
        "identities_required": len(required),
        "admitted_by_strict_cross_key": len(checked),
        "facet_store": ra.DEFAULT_FACETS,
    }
    return out, meta


def facet_lines(store: Path, ids: Collection[str], keep: Collection[str]) -> list[dict[str, Any]]:
    """Stream the facet store; parse only admitted ids. Each line is reduced to ``keep`` plus ``_valid`` (all fields present).

    The full record is parsed in memory (``suspicion`` included) before the reduction; phase 1 never inspects it.
    """
    out: list[dict[str, Any]] = []
    with store.open(encoding="utf-8") as handle:
        for line in handle:
            m = RUN_ID.search(line)
            if m and m.group(1) in ids:
                rec = json.loads(line)
                reduced = {k: rec.get(k) for k in keep}
                reduced["_valid"] = all(f in rec for f in RECORD_FIELDS)
                out.append(reduced)
    return out


def attested_runs(calls_path: Path, ids: Collection[str]) -> set[str]:
    """Runs with a successful Haiku ``form`` call at prompt v2 (the NP22 attestation)."""
    out: set[str] = set()
    with calls_path.open(encoding="utf-8") as handle:
        for line in handle:
            m = RUN_ID.search(line)
            if m and m.group(1) in ids:
                c = json.loads(line)
                if (
                    c.get("status") == "ok"
                    and c.get("purpose") == "form"
                    and c.get("prompt_version") == V2
                    and str(c.get("served_model")) == HAIKU
                ):
                    out.add(c["run_id"])
    return out


def flake_runs() -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Flake sample runs (steps, ``stratum``, ``task``) and the publisher outcome per run key. No cascade or facet file."""
    import pickle

    runs = [r for r in pickle.loads((FLAKE_STATE / "runs.pkl").read_bytes()) if r["set"] == "flake"]
    outcome = json.loads((FLAKE_STATE / "reasons.json").read_text())
    return runs, outcome
