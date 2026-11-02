#!/usr/bin/env python3
"""Synthesize friction reports, census incidents, and engine comparisons for F6-ROADMAP.

Produces structured receipts and summary statistics.
"""

import json
from pathlib import Path

# 1. Tonight's friction items collected across reports
FRICTION_ITEMS = [
    # Lanes
    {"source": "D1-report.md", "category": "Harness / Execution", "text": "Foreground build moved to background after 60s despite foreground-only rule; lost inline streaming.", "candidate_wf": "WF-01"},
    {"source": "D4-report.md", "category": "Harness / Execution", "text": "Foreground read moved to background after 60s; had to poll completion notice.", "candidate_wf": "WF-01"},
    {"source": "F1-report.md", "category": "Orchestration / Wait", "text": "Waiting on 4 headless workers required foreground kill -0 loop with 600s timeout; no resume on session loss.", "candidate_wf": "WF-01"},
    {"source": "F1-report.md", "category": "Tooling / External", "text": "link-archive get timeouts/skips on arXiv, docs.prefect.io, union.ai; required manual notes.", "candidate_wf": "Tooling"},
    {"source": "F1-report.md", "category": "Data / Custody", "text": "Worker raw files live outside git in eval-lab-results, separating report citations from git commit.", "candidate_wf": "Tooling"},
    {"source": "K7-report.md", "category": "Cloud / Sandbox", "text": "Modal 200 deployed-app limit silently truncated sweeps ($0.0004 cost, 0 reward); needed preflight check.", "candidate_wf": "WF-03"},
    {"source": "K7-report.md", "category": "State / Resume", "text": "run_cell cached result.json on unrun trials; resume required manual directory deletion.", "candidate_wf": "WF-03"},
    {"source": "E1-raw/report.md", "category": "Tooling / External", "text": "Managed headless browser hung on screenshots; 25 minutes lost debugging about:blank args.", "candidate_wf": "Tooling"},
    # Verifiers
    {"source": "REVIEW-1726", "category": "Environment / Policy", "text": "Shell policy blocked heredoc/cat/redirection; required 6 separate write tool calls for 1 fixture.", "candidate_wf": "Tooling"},
    {"source": "REVIEW-1726", "category": "Tooling / Search", "text": "Directory prefix grep returned unrelated hits; missing file in semicolon path silently skipped.", "candidate_wf": "Tooling"},
    {"source": "REVIEW-1726", "category": "Data / Receipts", "text": "Receipts scattered across research/experiments and bridge; cross-checking required opening 10 JSONs by hand.", "candidate_wf": "WF-08"},
    {"source": "REVIEW-1729", "category": "Test Infrastructure", "text": "Reviewing required hand-building ATIF fixtures; lack of shared synthetic fixture library.", "candidate_wf": "Tooling"},
    {"source": "REVIEW-1729", "category": "Tooling / Git", "text": "gh pr diff output truncated in tool; required manual checkout of review worktree to see changes.", "candidate_wf": "Tooling"},
    {"source": "REVIEW-1729", "category": "Environment / Setup", "text": "Review worktree required 20s uv sync --extra laminar only to run CLI.", "candidate_wf": "Tooling"},
    # Train / Loop
    {"source": "C1-HACKDROP", "category": "CI / Polling", "text": "GitHub Actions CI check polling loops triggered tool call loop detection interrupts.", "candidate_wf": "WF-02"},
    {"source": "C1-HACKDROP", "category": "Data / Collision", "text": "Target surface patterns matched runner receipts (grade.json, run.json) causing false alarms.", "candidate_wf": "WF-10"},
    {"source": "C1-HACKDROP", "category": "Schema / Ingestion", "text": "Variant vs behavior label ingestion: null variant evaluated as False before checking behavior.", "candidate_wf": "WF-10"},
    {"source": "C1-HACKDROP", "category": "Governance / Docs", "text": "Adding tracked symbols requires mandatory evallab.repomap generate or governance check fails.", "candidate_wf": "WF-02"},
    {"source": "C1-HACKDROP", "category": "Typechecking", "text": "Dictionary comprehension created int | dict union types in ty requiring manual separation.", "candidate_wf": "Tooling"},
    {"source": "A0-SUBMISSIONS", "category": "Orchestration / Wait", "text": "Inter-worker coordination wait: A0 had to poll file system waiting for A1's gold-universe.json.", "candidate_wf": "WF-01"},
    {"source": "A0-SUBMISSIONS", "category": "Data / Schema", "text": "Heterogeneous trial output schemas across historical runs requiring multi-tier fallback parsing.", "candidate_wf": "WF-14"},
    {"source": "A0-SUBMISSIONS", "category": "Data / Identifiers", "text": "Inconsistent task identifiers across runs requiring canonical regex extraction and zero-padding.", "candidate_wf": "WF-14"},
    {"source": "A0-SUBMISSIONS", "category": "Filesystem / I/O", "text": "APFS directory traversal latency over tens of thousands of run directories without index cache.", "candidate_wf": "WF-14"},
    {"source": "A1-INVENTORY", "category": "Filesystem / I/O", "text": "Broad glob timeouts over deep derived stores exceeding 300s tool deadline.", "candidate_wf": "WF-14"},
    {"source": "A1-INVENTORY", "category": "Code / Traps", "text": "Worktree relative path depth traps (parents[2] vs parents[3]) breaking answer key resolution.", "candidate_wf": "Tooling"},
    {"source": "A1-INVENTORY", "category": "Code / Parsing", "text": "Raw patch diff hunks fail Python AST parsing without surrounding file context.", "candidate_wf": "WF-03"},
    {"source": "A1-INVENTORY", "category": "Task Anatomy", "text": "Wrapper scripts (mimo_test_command.sh) mask underlying test runner as custom_script.", "candidate_wf": "WF-13"},
    {"source": "A1-INVENTORY", "category": "Spend / Accounting", "text": "Conservative reservation cap ($0.01) consumes budget headroom faster than actual billed spend ($0.003-$0.006).", "candidate_wf": "WF-05"},
    {"source": "S1-MODAL-CELLS", "category": "Concurrency / Locks", "text": "Deadlock on non-reentrant locks in CloudSpendGate (self._lock acquired twice in to_json/summary).", "candidate_wf": "WF-05"},
    {"source": "S1-MODAL-CELLS", "category": "Schema / Collisions", "text": "Upstream grader ledger schema assertions rejected cell runner ledgers on trial finalization.", "candidate_wf": "WF-05"},
    {"source": "S1-MODAL-CELLS", "category": "Spend / Accounting", "text": "Double reservation on ModalBackend admission: helper charged spend gate 3x up front.", "candidate_wf": "WF-05"},
    {"source": "S1-MODAL-CELLS", "category": "CI / Polling", "text": "Tool repetition detection triggered when polling CI checks via bash in a loop.", "candidate_wf": "WF-02"},
    {"source": "S1-MODAL-CELLS", "category": "Environment / Setup", "text": "Isolated worktrees require manual uv sync --extra laminar before ruff and pytest work.", "candidate_wf": "Tooling"},
    {"source": "B1-HOOK", "category": "Schema / Ingestion", "text": "OMP session JSONL schema evolution: model calls under model_change/message rather than model_call.", "candidate_wf": "WF-04"},
    {"source": "B1-HOOK", "category": "CI / Queues", "text": "Superseded CI runs from previous commits continued running and consuming runner headroom.", "candidate_wf": "WF-02"},
    {"source": "B1-HOOK", "category": "Architecture / IPC", "text": "ContinuousWorkQueue abstracts connection without exposing public .database attribute.", "candidate_wf": "WF-04"},
    {"source": "FIX-HACKDROP-1729", "category": "CI / Polling", "text": "GitHub Actions CI queue latency and tool repetition interrupts during 30s status polling.", "candidate_wf": "WF-02"},
    {"source": "FIX-HACKDROP-1729", "category": "Governance / Docs", "text": "Modifying tracked symbols required 60s evallab.repomap generate execution.", "candidate_wf": "WF-02"},
    # Workflow Prototype Trials
    {"source": "F3-TEMPORAL", "category": "Engine / Sandbox", "text": "Temporal determinism tax strictly forbids time.time(), direct I/O, asyncio.wait() in workflows.", "candidate_wf": "WF-01"},
    {"source": "F3-TEMPORAL", "category": "Engine / Lifecycle", "text": "Cancellation propagation cooperative via heartbeat; needed WAIT_CANCELLATION_COMPLETED to avoid orphans.", "candidate_wf": "WF-01"},
    {"source": "F3-TEMPORAL", "category": "Engine / Bugs", "text": "Temporal 2s deadlock detector triggered if wait_condition evaluates True without yielding.", "candidate_wf": "WF-01"},
    {"source": "F3-TEMPORAL", "category": "OS / Darwin", "text": "Terminated child processes remain as zombies in OS table; os.kill(pid, 0) returned false alive status.", "candidate_wf": "WF-01"},
    {"source": "F3-TEMPORAL", "category": "Engine / History", "text": "Continue-as-new counter reset trap: threshold counters must be generation-relative or loop recurses.", "candidate_wf": "WF-01"},
    {"source": "F4-MERGETRAIN", "category": "Git / State", "text": "Leftover worktree references in .git/worktrees/ on failed rebase require git worktree remove --force.", "candidate_wf": "WF-02"},
    {"source": "F4-MERGETRAIN", "category": "Engine / Subprocess", "text": "Temporal activity threads do not inherit worker env vars without explicit subprocess mapping.", "candidate_wf": "WF-02"},
    {"source": "F4-MERGETRAIN", "category": "Engine / Traps", "text": "Deadlock detector on wait_condition inside loops requires binding closure variables (Ruff B023).", "candidate_wf": "WF-02"},
    {"source": "F4-MERGETRAIN", "category": "Engine / Testing", "text": "start_time_skipping fast-forwards workflow timers, but not blocking time.sleep() inside activities.", "candidate_wf": "WF-02"},
    {"source": "F4-MERGETRAIN", "category": "Git / Bare Repos", "text": "Squash-merging in bare git repos requires detached worktree checkout of refs/heads/main.", "candidate_wf": "WF-02"},
    {"source": "F7-DBOS", "category": "Engine / Registration", "text": "DBOS step name determinism: functions must be module-scoped or have explicit name attribute.", "candidate_wf": "WF-01"},
    {"source": "F7-DBOS", "category": "Datetime / Parsing", "text": "datetime.fromisoformat() defaults to local time if offset omitted, turning 90s wait into multi-hour wait.", "candidate_wf": "WF-07"},
    {"source": "F7-DBOS", "category": "Engine / Lifecycle", "text": "DBOS.register_queue cannot be called prior to DBOS.launch().", "candidate_wf": "WF-01"},
    {"source": "F8-DAGU", "category": "Shell / Environment", "text": "Dagu shell executor shadows ${SECONDS} with bash internal counter, executing tasks in 0.0003s.", "candidate_wf": "Tooling"},
    {"source": "F8-DAGU", "category": "Engine / Config", "text": "Server parameters in base.yaml cause silent DAG loading errors across all definitions.", "candidate_wf": "Tooling"},
    {"source": "F8-DAGU", "category": "Engine / Latency", "text": "Default zombie detector latency takes up to 225s to detect dead process until tuned.", "candidate_wf": "WF-01"},
    {"source": "F8-DAGU", "category": "Engine / Retries", "text": "Native retry policy lacks dynamic quota backoff; issues repeated blind attempts during 429 backoff.", "candidate_wf": "WF-07"},
    {"source": "F8-DAGU", "category": "Engine / Syntax", "text": "Defining name: at top of DAG entrypoint file rejected by dagu validate; name derived from filename.", "candidate_wf": "Tooling"}
]

# 2. Documented historical incidents from F2 Census (16 workflows)
CENSUS_INCIDENTS = [
    {"wf": "WF-01", "incident": "Git config lock collision (_ds-ao.log:2-10) killed 2 tasks simultaneously at launch"},
    {"wf": "WF-01", "incident": "W3B-EXHAUSTIVE-GOLD-CERTIFIER killed-time at 60m with 0 report (.watch-state.json:39)"},
    {"wf": "WF-01", "incident": "ds_squad runner meter undercounting 9x compared to guard balance (OVERNIGHT.md:18-20)"},
    {"wf": "WF-01", "incident": "Fragile routing constraints across 5 model pools (CHARTER.md:20-30)"},
    {"wf": "WF-02", "incident": "W2-MERGE-TRAIN failed after 1.3 min, leaving retry loop broken (_ds-fb2.log:10-12)"},
    {"wf": "WF-02", "incident": "T2A-REBASE required manual rebase of PR #1691 after #1693 merged (ds-squad-fb2.md:1-25)"},
    {"wf": "WF-02", "incident": "22 PRs required continuous manual coordinator babysitting across 7 hours (MORNING-REPORT-2026-11-01.md:5)"},
    {"wf": "WF-03", "incident": "Gold certifier timed out, leaving 8 trials without report/PR (VF-OV2/report.md:115)"},
    {"wf": "WF-03", "incident": "CloudSpendGate reservation exhausted $2.50 cap on $0.0089 real spend (T3-SAFEGUARD-BENCH/report.md:73)"},
    {"wf": "WF-03", "incident": "26 cells refused before execution by spend-gate estimate flaw (ds-squad-ao.md:51)"},
    {"wf": "WF-03", "incident": "Aborted runs lose container sandbox charges, leaving tasks half-certified (K6 left 000232 unproven in NARRATIVE.md:42)"},
    {"wf": "WF-04", "incident": "Documented 33 items re-queued 12x and quadratic start-up recovery (factory.py:22-31)"},
    {"wf": "WF-04", "incident": "Hand-rolled at-least-once transactional outbox relay with JetStream pub-ack (outbox_relay.py:1-8)"},
    {"wf": "WF-04", "incident": "Leases requiring heartbeat renewal and unguessable fencing tokens (corpus_queue.py:20-22)"},
    {"wf": "WF-05", "incident": "CloudSpendGate flat reservation exhausts caps at 1/4 of real spend (NARRATIVE.md:41)"},
    {"wf": "WF-05", "incident": "Reservations are estimates kept even on failed/short executions (regrade.py:241-298)"},
    {"wf": "WF-05", "incident": "49 cumulative reservations stalled runs on $0.26 shortfall (classifiers-v7-canonical-incomplete-receipt.json)"},
    {"wf": "WF-06", "incident": "Terminal Wrench clean/hack inversion due to prefix rules (tripwires.py, monitor_worker.py)"},
    {"wf": "WF-07", "incident": "Direct Claude 7-day quota hit 100% at 12:57 AM ET without auto-parking (MORNING-REPORT-2026-11-01.md:84)"},
    {"wf": "WF-07", "incident": "Quota exhaustion code 29 retry storms burning tokens before reset timestamp (F1 survey #64328)"},
    {"wf": "WF-08", "incident": "Recomputation discrepancy between prose and receipts caught by blind verifiers VF-OV1/VF-OV2"},
    {"wf": "WF-08", "incident": "Receipts left in /tmp lost across machine reboots (VF-OV1/report.md:42)"},
    {"wf": "WF-09", "incident": "Judge station priority inversions and unblinded prompt leaks during live reviews"},
    {"wf": "WF-10", "incident": "Filter misses on semantic exploits (C5 glyph2-decode vendored tables) bypassing static L0"},
    {"wf": "WF-11", "incident": "Swarm coordinator thread starvation during multi-judge consensus adjudication"},
    {"wf": "WF-12", "incident": "Live supervisor watchdog false alarms and zombie process table leaks on Darwin"},
    {"wf": "WF-13", "incident": "Task hardening package synthesis missing container probe dumps for AST mutator"},
    {"wf": "WF-14", "incident": "Corpus queue lease expiration causing duplicate downloads of 500MB CAS blobs"}
]

def main():
    print(f"Total tonight's friction items: {len(FRICTION_ITEMS)}")
    print(f"Total census incidents: {len(CENSUS_INCIDENTS)}")

    # Friction counts by candidate workflow
    wf_friction_counts = {}
    for item in FRICTION_ITEMS:
        target = item["candidate_wf"]
        wf_friction_counts[target] = wf_friction_counts.get(target, 0) + 1

    # Census incident counts by workflow
    census_counts = {}
    for item in CENSUS_INCIDENTS:
        wf = item["wf"]
        census_counts[wf] = census_counts.get(wf, 0) + 1

    summary = {
        "friction_item_count": len(FRICTION_ITEMS),
        "census_incident_count": len(CENSUS_INCIDENTS),
        "total_evidence_points": len(FRICTION_ITEMS) + len(CENSUS_INCIDENTS),
        "friction_by_candidate": wf_friction_counts,
        "census_incidents_by_wf": census_counts,
        "top_addressed_workflows": [
            {
                "id": "WF-01",
                "name": "Multi-Agent Squad Dispatch & Worker Lifecycle",
                "friction_count": wf_friction_counts.get("WF-01", 0),
                "census_incident_count": census_counts.get("WF-01", 0),
                "total_pain_points": wf_friction_counts.get("WF-01", 0) + census_counts.get("WF-01", 0)
            },
            {
                "id": "WF-02",
                "name": "Automated PR Merge Train & Spec Harmonization",
                "friction_count": wf_friction_counts.get("WF-02", 0),
                "census_incident_count": census_counts.get("WF-02", 0),
                "total_pain_points": wf_friction_counts.get("WF-02", 0) + census_counts.get("WF-02", 0)
            },
            {
                "id": "WF-03",
                "name": "Corpus-Scale Certify-Gold & STING Mutation Pipeline",
                "friction_count": wf_friction_counts.get("WF-03", 0),
                "census_incident_count": census_counts.get("WF-03", 0),
                "total_pain_points": wf_friction_counts.get("WF-03", 0) + census_counts.get("WF-03", 0)
            },
            {
                "id": "WF-05",
                "name": "Cloud Spend Gate & Dynamic Budget Ledger",
                "friction_count": wf_friction_counts.get("WF-05", 0),
                "census_incident_count": census_counts.get("WF-05", 0),
                "total_pain_points": wf_friction_counts.get("WF-05", 0) + census_counts.get("WF-05", 0)
            },
            {
                "id": "WF-07",
                "name": "Provider Quota-Aware Fleet Routing & Rate Throttling",
                "friction_count": wf_friction_counts.get("WF-07", 0),
                "census_incident_count": census_counts.get("WF-07", 0),
                "total_pain_points": wf_friction_counts.get("WF-07", 0) + census_counts.get("WF-07", 0)
            }
        ],
        "tooling_environment_friction_count": wf_friction_counts.get("Tooling", 0)
    }

    print("\nSummary:")
    print(json.dumps(summary, indent=2))

    # Write receipts to all locations
    paths = [
        Path("research/experiments/on-workflow-roadmap-20261102/receipts/evidence_summary.json"),
        Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F6-ROADMAP/receipts/evidence_summary.json"),
        Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F6-ROADMAP/receipts/evidence_summary.json"),
    ]
    for p in paths:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(summary, f, indent=2)
        print(f"Wrote receipt to {p}")

    # Write complete evidence items
    detailed_evidence = {
        "friction_items": FRICTION_ITEMS,
        "census_incidents": CENSUS_INCIDENTS,
    }
    detail_paths = [
        Path("research/experiments/on-workflow-roadmap-20261102/receipts/detailed_evidence.json"),
        Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F6-ROADMAP/receipts/detailed_evidence.json"),
        Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F6-ROADMAP/receipts/detailed_evidence.json"),
    ]
    for p in detail_paths:
        with open(p, "w") as f:
            json.dump(detailed_evidence, f, indent=2)
        print(f"Wrote detailed evidence to {p}")

if __name__ == "__main__":
    main()
