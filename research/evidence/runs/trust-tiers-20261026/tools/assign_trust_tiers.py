#!/usr/bin/env python3
"""CE-3: derive evidence trust tiers for a CloudTraceDatabase and optionally apply them.

Policy (strongest first; every row not matched below is ``tier3_provisional``):

``tier1_container_verified`` issues are confirmed wrong-pass differentials with original
reward 1.0 and repaired reward 0.0 whose retained issue record proves a container replay:

* ``tb4-audit-v1``: a differentially replayed witness (``status_scope`` is
  ``retained_native_differential`` and a witness digest is stored);
* ``tb2-to-tb21-v1``: a maintainer-fix differential (``status_scope`` is
  ``retained_native_differential`` and the finding targets ``tb2_vs_tb21_differential``);
* ``mimo-defect-map-v1``: the repaired package is one of the nine historically qualified
  repairs (``train_repairs_v7`` in ``research/experiments/mimo-clean-v7/manifest.json``),
  bound by exact task and repaired package digest.

``tier1_container_verified`` traces are confirmed/verified witness traces whose receipt links
to a tier-1 issue. ``tier2_consensus_labeled`` traces are Goodfire rollouts with three-vote
``verdict_enacted == "yes"`` and at least one passage the three judges unanimously verified
positive; exactly those passage steps become ``flagged_passage_steps``. CheatBench
human-verified runs would also be tier 2, but the store holds none, so no rule reads them.

Reads are identity/label metadata only (no task, trace or step bodies are printed). Without
``--apply`` the database is opened read-only and nothing is written. ``--apply`` first takes a
consistent SQLite backup, then opens the store through ``CloudTraceDatabase`` (adding the
trust columns if absent) and writes the complete assignment in one transaction.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")

from evallab.production.cloud_trace_db import (  # noqa: E402
    PROVISIONAL_TIER,
    TRUST_TIERS,
    CloudTraceDatabase,
)

REPO = Path(__file__).resolve().parents[5]
MIMO_MANIFEST = REPO / "research/experiments/mimo-clean-v7/manifest.json"
TIER1, TIER2 = TRUST_TIERS[0], TRUST_TIERS[1]
GOODFIRE = "goodfire-reward-hack-data"


def qualified_mimo_repairs() -> set[tuple[str, str]]:
    manifest = json.loads(MIMO_MANIFEST.read_text(encoding="utf-8"))
    cohort = manifest["cohorts"]["train_repairs_v7"]
    members = set(cohort["members"])
    if len(members) != cohort["historical_qualified"]:
        raise SystemExit("train_repairs_v7 membership no longer equals its qualified count")
    return {
        (task["task_id"], task["package_digest"])
        for task in manifest["tasks"] if task["record_id"] in members
    }


def cas_record(cas: Path, uri: str | None) -> dict[str, Any] | None:
    if not uri:
        return None
    path = cas / "sha256" / uri.rsplit("/", 1)[1]
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != path.name:
        raise SystemExit(f"CAS integrity mismatch for {uri}")
    return json.loads(data)


def unanimous_positive(passage: dict[str, Any]) -> bool:
    return (
        passage.get("consensus") == "positive"
        and passage.get("votes_positive_verified") == 3
        and passage.get("votes_negative") == 0
        and passage.get("in_segments_unanimous") is True
    )


def derive(connection: sqlite3.Connection, cas: Path) -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, Any]]:
    qualified = qualified_mimo_repairs()
    issues: dict[str, str] = {}
    reasons: collections.Counter[str] = collections.Counter()
    for issue_id, dataset, status, original, repaired, witness, uri in connection.execute(
        "SELECT i.issue_id, t.dataset_id, i.status, i.original_reward, i.repaired_reward,"
        " i.witness_digest, i.cas_uri FROM task_issues i JOIN corpus_tasks t USING(task_id)"
        " ORDER BY i.issue_id"
    ):
        tier = PROVISIONAL_TIER
        if status == "confirmed" and (original, repaired) == (1.0, 0.0):
            record = cas_record(cas, uri) or {}
            scope = record.get("status_scope")
            variant = record.get("variant") or {}
            finding = record.get("finding") or {}
            if dataset == "tb4-audit-v1" and scope == "retained_native_differential" and witness:
                tier, reason = TIER1, "tb4 differentially replayed witness"
            elif dataset == "tb2-to-tb21-v1" and scope == "retained_native_differential" and (
                finding.get("target_version") == "tb2_vs_tb21_differential"
            ):
                tier, reason = TIER1, "tb2.1 maintainer-fix differential"
            elif dataset == "mimo-defect-map-v1" and (
                record.get("task_id"), variant.get("repaired_package_digest"),
            ) in qualified:
                tier, reason = TIER1, "mimo qualified repair"
            else:
                reason = f"{dataset} confirmed 1->0 without tier-1 proof"
        else:
            reason = f"{dataset} {status} not a strict 1->0 differential"
        reasons[reason] += 1
        issues[issue_id] = tier
    tier1_issues = {issue_id for issue_id, tier in issues.items() if tier == TIER1}

    traces: dict[str, dict[str, Any]] = {}
    goodfire_yes: list[str] = []
    for trace_id, dataset, receipt_text in connection.execute(
        "SELECT a.trace_id, t.dataset_id, a.verified_receipt_json FROM agent_traces a"
        " JOIN corpus_tasks t USING(task_id) ORDER BY a.trace_id"
    ):
        receipt = json.loads(receipt_text) if receipt_text else {}
        receipt = receipt if isinstance(receipt, dict) else {}
        traces[trace_id] = {"trust_tier": PROVISIONAL_TIER, "flagged_passage_steps": []}
        if receipt.get("status") in {"confirmed", "verified"} and receipt.get("linked_issue_id") in tier1_issues:
            traces[trace_id]["trust_tier"] = TIER1
            reasons["trace linked to tier-1 issue"] += 1
        elif dataset == GOODFIRE and (receipt.get("source_label") or {}).get("verdict_enacted") == "yes":
            goodfire_yes.append(trace_id)
    passages: dict[str, list[int]] = collections.defaultdict(list)
    for start in range(0, len(goodfire_yes), 500):
        page = goodfire_yes[start:start + 500]
        for trace_id, step_index, flags_text in connection.execute(
            "SELECT trace_id, step_index, flags_json FROM trace_steps"
            f" WHERE trace_id IN ({', '.join('?' for _ in page)})"
            " AND flags_json LIKE '%\"consensus\":\"positive\"%' ORDER BY trace_id, step_index",
            page,
        ):
            flags = json.loads(flags_text)
            if any(unanimous_positive(passage) for passage in flags.get("source_passages") or ()):
                passages[trace_id].append(step_index)
    for trace_id in goodfire_yes:
        if passages.get(trace_id):
            traces[trace_id] = {"trust_tier": TIER2, "flagged_passage_steps": passages[trace_id]}
            reasons["goodfire 3-vote enacted with unanimous passage"] += 1
        else:
            reasons["goodfire enacted without unanimous passage"] += 1
    summary = {
        "issues_by_tier": dict(collections.Counter(issues.values())),
        "traces_by_tier": dict(collections.Counter(value["trust_tier"] for value in traces.values())),
        "flagged_passage_steps": sum(len(value["flagged_passage_steps"]) for value in traces.values()),
        "mimo_qualified_repairs_in_manifest": len(qualified),
        "reasons": dict(sorted(reasons.items())),
    }
    return issues, traces, summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--cas", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup", type=Path, help="required with --apply; must not exist")
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    with sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True) as connection:
        issues, traces, summary = derive(connection, args.cas)
    assignment = json.dumps({"issues": issues, "traces": traces}, sort_keys=True, separators=(",", ":"))
    receipt: dict[str, Any] = {
        "schema": "evallab.trust_tier_assignment/v1",
        "recorded_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "database": str(args.database), "applied": False,
        "assignment_sha256": hashlib.sha256(assignment.encode()).hexdigest(),
        **summary,
    }
    if args.apply:
        if args.backup is None or args.backup.exists():
            raise SystemExit("--apply needs a --backup path that does not exist yet")
        with (
            closing(sqlite3.connect(args.database.resolve().as_uri() + "?mode=ro", uri=True)) as source,
            closing(sqlite3.connect(args.backup)) as backup,
        ):
            source.backup(backup)
        receipt["backup"] = str(args.backup)
        with CloudTraceDatabase(args.database) as database:
            receipt["changed"] = database.assign_trust_tiers(issues=issues, traces=traces)
            with database._transaction():
                receipt["stored_issue_tiers"] = dict(database._execute(
                    "SELECT trust_tier, count(*) FROM task_issues GROUP BY 1").fetchall())
                receipt["stored_trace_tiers"] = dict(database._execute(
                    "SELECT trust_tier, count(*) FROM agent_traces GROUP BY 1").fetchall())
        receipt["applied"] = True
    text = json.dumps(receipt, indent=1, sort_keys=True) + "\n"
    if args.receipt:
        args.receipt.write_text(text, encoding="utf-8")
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
