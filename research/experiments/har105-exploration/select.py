#!/usr/bin/env python3
"""HAR-105 exploration task set: about 10 train-side MiMo tasks over all six domains.

Rule, applied per domain to the sealed split's ``train`` tasks
(``research/experiments/har81-mimo-sft/split.json``):

1. Drop tasks with an ``error`` finding in ``task_findings.parquet``, tasks in
   the pinned export-broken list, and the HAR-105 part-2 suspects (they come
   back as fixed variants, listed separately).
2. general only: drop tasks whose instruction asks for a "final response".
   The grader reads that from an OpenCode event log, which Terminus-2 does
   not write, so under Terminus-2 those tasks are unpassable (see README).
3. Rank by ``sha256("har105:" + task_id)``; walk the ranking taking at most one
   task per ``split_group`` and per ``metadata.category``.
4. code, cyber, terminal: a task must already have an ``ok`` Daytona nop row
   for its ``task_version_digest`` in ``task_qualification.parquet``, and its
   nop verifier output must show no setup/import error (the HAR-97 rule).
   general, music, webdev have (almost) no nop rows, so the top
   ``CANDIDATES`` per domain are listed for a nop check; the first ``PICKS``
   that come back clean are the set.

Writes ``selection.json`` next to this file and one Daytona nop spec per
music candidate in ``specs/`` (general and webdev cannot be nop-checked
through the queue; see README).
"""

from __future__ import annotations

import hashlib
import json
import tomllib
from pathlib import Path

import pyarrow.parquet as pq
from nopspec import (
    OUT,
    PRIMARY,
    ROOT,
    SETUP_ERROR,
    SNAPSHOTS,
    SUSPECTS,
    nop_spec,
    nop_verifier_text,
)

SEED = "har105:"
PICKS = {"code": 2, "cyber": 1, "terminal": 1, "general": 2, "music": 2, "webdev": 2}
CANDIDATES = 3
JUDGE_BLOCK = (
    "the grader needs a model-judge key (task.toml [verifier.env] = ${HF_TOKEN}); Harbor refuses "
    "the job at start when HF_TOKEN is unset, and Eval Lab's executor passes no such key"
)


def rank(task_id: str) -> str:
    return hashlib.sha256((SEED + task_id).encode()).hexdigest()


def task_dir(domain: str, task_id: str) -> Path:
    return PRIMARY / "derived/task-store/hf" / SNAPSHOTS[domain] / "tasks" / task_id


def main() -> None:
    split = json.loads((ROOT / "research/experiments/har81-mimo-sft/split.json").read_text())
    cohort = json.loads((ROOT / "research/experiments/har81-mimo-sft/cohort.json").read_text())
    broken = set(cohort["excluded_task_version_digests"])
    catalog = PRIMARY / "derived/parquet/external/task_catalog"
    errors = {
        row["task_version_digest"]
        for row in pq.read_table(catalog / "task_findings.parquet").to_pylist()
        if row["severity"] == "error"
    }
    nops: dict[str, dict] = {}
    for row in pq.read_table(catalog / "task_qualification.parquet").to_pylist():
        if row["backend"] == "daytona" and row["status"] == "ok":
            nops[row["task_version_digest"]] = row

    selection: dict[str, list[dict]] = {}
    for domain, picks in PICKS.items():
        pool = sorted(
            (t for t in split["tasks"] if t["domain"] == domain and t["split"] == "train"),
            key=lambda t: rank(t["task_id"]),
        )
        groups: set[str] = set()
        categories: set[str] = set()
        want = picks if domain in ("code", "cyber", "terminal") else CANDIDATES
        chosen: list[dict] = []
        for task in pool:
            digest = task["task_version_digest"]
            if digest in errors or digest in broken or task["task_id"] in SUSPECTS:
                continue
            path = task_dir(domain, task["task_id"])
            config = tomllib.loads((path / "task.toml").read_text())
            category = config.get("metadata", {}).get("category") or "Unknown"
            if task["split_group"] in groups or category in categories:
                continue
            keywords = config.get("metadata", {}).get("keywords") or []
            if "Chinese" in keywords:
                continue
            instruction = (path / "instruction.md").read_text()
            if domain == "general" and "final response" in instruction.lower():
                continue
            entry = {
                "task_id": task["task_id"],
                "domain": domain,
                "category": category,
                "split_group": task["split_group"],
                "task_version_digest": digest,
                "rank": rank(task["task_id"])[:12],
                "task": str(path.relative_to(PRIMARY)),
                "agent_timeout_sec": config.get("agent", {}).get("timeout_sec"),
                "verifier_env": sorted(config.get("verifier", {}).get("env", {})),
                "mcp_servers": len(config.get("environment", {}).get("mcp_servers", [])),
            }
            nop = nops.get(digest)
            text = nop_verifier_text(nop["job_name"]) if nop else None
            if nop is not None and text is not None and not SETUP_ERROR.search(text):
                entry["nop"] = {
                    "status": "ok",
                    "backend": "daytona",
                    "job_name": nop["job_name"],
                    "reward": nop["reward"],
                    "verifier_setup_errors": 0,
                    "est_cost_usd": nop["est_cost_usd"],
                }
            elif domain in ("code", "cyber", "terminal"):
                continue
            elif entry["verifier_env"]:
                entry["nop"] = {"status": "blocked", "reason": JUDGE_BLOCK}
            else:
                entry["nop"] = {"status": "pending"}
            groups.add(task["split_group"])
            categories.add(category)
            chosen.append(entry)
            if len(chosen) == want:
                break
        ready = [e for e in chosen if e["nop"]["status"] == "ok"][:picks]
        for entry in chosen:
            entry["in_set"] = entry in ready or (
                not ready and entry["nop"]["status"] == "blocked" and chosen.index(entry) < picks
            )
        selection[domain] = chosen

    specs = OUT / "specs"
    specs.mkdir(exist_ok=True)
    for old in specs.glob("har105-qual-m-*.json"):
        old.unlink()
    for entry in selection["music"]:
        name = f"har105-qual-m-{entry['task_id']}"
        spec = nop_spec(
            entry["task"],
            name,
            "The MiMo task starts, passes its healthcheck and grades on Daytona: "
            "a nop control completes the verifier with reward 0.",
        )
        (specs / f"{name}.json").write_text(json.dumps(spec, indent=2) + "\n")
        entry["spec"] = f"specs/{name}.json"

    (OUT / "selection.json").write_text(
        json.dumps(
            {
                "card": "HAR-105",
                "seed": SEED,
                "split_manifest_digest": split["manifest_digest"],
                "picks": PICKS,
                "candidates_per_new_domain": CANDIDATES,
                "excluded_suspects": sorted(SUSPECTS),
                "selection": selection,
            },
            indent=2,
        )
        + "\n"
    )
    for domain, rows in selection.items():
        print(domain, [(r["task_id"], r["category"]) for r in rows])


if __name__ == "__main__":
    main()
