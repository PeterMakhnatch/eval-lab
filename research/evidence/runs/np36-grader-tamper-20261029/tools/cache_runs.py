"""Freeze the run inputs of NP36-TW-1 (steps only, label-free) into a private cache.

Writes ``<cache>/<population>.pkl`` = ``{run_id: (steps...)}`` for every population, and ``<cache>/strict_gate.json`` with the
strict-gate facts about the frozen NP31 pair list. Reads no class label: for the strict gate it only asks whether a run is
in the admitted cohort and carries a verdict (``positive is not None``), never which class.
"""

from __future__ import annotations

import hashlib
import json
import pickle
import sys
from pathlib import Path

import data

from evallab.production.trace_harness import DEFAULT_DATABASE
from evallab.production.trace_harness_corpora import load_corpus_labels

CACHE = Path.home() / ".local/state/evallab-worker-evidence/np36-tw1-20261029"
POPULATIONS = ("pairs", "g150", "cheatbench", "goodfire_dev", "cm18", "np21", "np33")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def strict_gate(repo: Path) -> dict:
    labels = load_corpus_labels("goodfire-dev", database=DEFAULT_DATABASE, repo_root=repo)
    admitted = {r for r in labels.run_ids if labels.positive.get(r) is not None}
    pairs = data.pair_rows()
    inside = [p for p in pairs if p["enacted"] in admitted and p["clean"] in admitted]
    facts = {
        "pair_file_sha256": sha256_file(data.PAIRS),
        "pairs": len(pairs),
        "pairs_both_members_in_strict_goodfire_dev_with_verdict": len(inside),
        "distinct_runs": len({r for p in pairs for r in (p["enacted"], p["clean"])}),
        "distinct_tasks": len({p["task"] for p in pairs}),
        "strict_goodfire_dev_cohort_with_verdict": len(admitted),
        "strict_goodfire_dev_cohort_all": len(labels.run_ids),
    }
    if len(inside) != len(pairs):
        raise SystemExit(f"a pair member is outside the strict goodfire-dev gate: {facts}")
    return facts


def main() -> int:
    repo = Path.cwd()
    CACHE.mkdir(parents=True, exist_ok=True)
    if not (CACHE / "strict_gate.json").exists():
        facts = strict_gate(repo)
        (CACHE / "strict_gate.json").write_text(json.dumps(facts, indent=2, sort_keys=True))
        print(facts, flush=True)
    for name in sys.argv[1:] or list(POPULATIONS):
        runs = data.load_runs(name, repo)
        (CACHE / f"{name}.pkl").write_bytes(pickle.dumps({r.run_id: tuple(r.steps) for r in runs}))
        print(name, len(runs), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
