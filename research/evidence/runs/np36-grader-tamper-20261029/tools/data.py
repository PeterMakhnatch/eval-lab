"""Run loading for NP36-TW-1. Steps only; labels are joined by score.py and nowhere else.

Populations (same run sets and loaders as p0-f9d7d6-3 where it used them):
  pairs       the 246 runs of the 123 NP31 primary pairs (frozen list, private: np31-length-matched/goodfire-dev-pairs.jsonl)
  g150        the 150 labelled runs of the Goodfire CM-2 sample (strict ``goodfire`` cohort)
  cheatbench  the 150 CheatBench DEV runs
  goodfire_dev  every strict ``goodfire-dev`` run (2,388 with a verdict)
  cm18 / np21 staged cross-test sets (positives and established negatives only; read-only)
  np33        staged cross-test sets agentcheck / caught_not_taught / PostTrainBench (read-only)
"""

from __future__ import annotations

import json
import os
import pickle
from pathlib import Path

os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")

from evallab.production.trace_harness import (  # noqa: E402
    DEFAULT_DATABASE,
    HarnessRun,
    actor_inputs_available,
    resolve_runs,
)
from evallab.production.trace_harness_corpora import load_corpus_labels  # noqa: E402

STATE = Path.home() / ".local/state"
PAIRS = STATE / "evallab-prod-live/trace-harness/np31-length-matched/goodfire-dev-pairs.jsonl"
STAGED = {"cm18": STATE / "evallab-cm18", "np21": STATE / "evallab-np21-cm1"}
STAGED_STRATA = ("positive", "negative_established")
STAGED_NP33 = STATE / "evallab-np33-cm1"


def pair_rows() -> list[dict]:
    return [json.loads(line) for line in PAIRS.read_text().splitlines() if line.strip()]


def staged_runs(tag: str) -> list[HarnessRun]:
    root = STAGED[tag]
    wanted = {
        (row["set"], row["key"])
        for row in json.loads((root / "sample.json").read_text())["runs"]
        if row["stratum"] in STAGED_STRATA and (tag == "cm18" or row["set"] == "flake")
    }
    with (root / "runs.pkl").open("rb") as handle:
        rows = pickle.load(handle)
    runs = []
    for row in rows:
        if (row["set"], row["key"]) in wanted:
            steps = tuple(row["steps"])
            runs.append(
                HarnessRun(
                    f"{tag}:{row['set']}:{row['key']}",
                    f"{tag}:{row['set']}",
                    "CROSS-TEST",
                    "unknown",
                    steps,
                    actor_inputs_available(steps),
                )
            )
    return runs


def np33_runs() -> list[HarnessRun]:
    sample = json.loads((STAGED_NP33 / "sample.json").read_text())
    runs = []
    for row in sample["runs"]:
        with (STAGED_NP33 / "runs" / f"{row['key']}.pkl").open("rb") as handle:
            steps = tuple(pickle.load(handle)["steps"])
        runs.append(
            HarnessRun(
                f"np33:{row['set']}:{row['key']}",
                f"np33:{row['set']}",
                "CROSS-TEST",
                "unknown",
                steps,
                actor_inputs_available(steps),
            )
        )
    return runs


def load_runs(population: str, repo: Path) -> list[HarnessRun]:
    if population == "g150":
        labels = load_corpus_labels("goodfire", database=DEFAULT_DATABASE, repo_root=repo)
        ids = sorted(r for r in labels.run_ids if labels.positive.get(r) is not None)
        return resolve_runs(",".join(ids), repo_root=repo)
    if population == "pairs":
        ids = []
        for pair in pair_rows():
            ids += [pair["enacted"], pair["clean"]]
        return resolve_runs(",".join(ids), repo_root=repo)
    if population == "cheatbench":
        return resolve_runs("cheatbench", repo_root=repo)
    if population == "goodfire_dev":
        return resolve_runs("goodfire-dev", repo_root=repo)
    if population == "np33":
        return np33_runs()
    return staged_runs(population)
