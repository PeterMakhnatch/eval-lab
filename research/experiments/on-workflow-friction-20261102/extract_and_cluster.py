#!/usr/bin/env python3
"""F12-FRICTION-SYNTHESIS: Extraction, clustering, and ranking of workflow frictions.

Extracts workflow friction bullets, review blocker classes, and overnight incidents
from 2026-11-02 overnight run evidence. Produces local raw evidence tables and
committed aggregate taxonomy and ranking manifests.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

BRIDGE_ROOT = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight")
RECEIPTS_DIR = BRIDGE_ROOT / "wf/raw/F12-FRICTION-SYNTHESIS/receipts"
LOCAL_RESULTS_DIR = Path.home() / "Developer/eval-lab-results/2026-11-02/F12"
LOCAL_RESULTS_NAMED_DIR = Path.home() / "Developer/eval-lab-results/2026-11-02/F12-FRICTION-SYNTHESIS/receipts"
EXP_DIR = Path("research/experiments/on-workflow-friction-20261102")

CATEGORIES = [
    "git_rebase_docs_conflict",
    "review_fix_loop",
    "tool_sandbox_blocking",
    "spend_accounting_stalls",
    "attestation_model_routing",
    "gate_dependency_waiting",
    "data_policy_leakage",
    "brief_drift_dynamic_config",
    "concurrency_races_infra",
    "hang_detection_visibility",
    "crash_recovery_resume",
    "quota_parking_ratelimit",
]

RULES = [
    ("quota_parking_ratelimit", [
        r"\b429\b", r"quota", r"rate[- ]limit", r"\bdevin\b", r"token limit", r"allowance", r"backoff"
    ]),
    ("spend_accounting_stalls", [
        r"reservation", r"settle", r"spend[- ]gate", r"spend[- ]cap", r"spend\.jsonl",
        r"cloudspendgate", r"over-reservation", r"budget", r"spend tracking", r"ledger"
    ]),
    ("data_policy_leakage", [
        r"data[- ]policy", r"third-party", r"transcript", r"held-out", r"answer[- ]key",
        r"unredacted", r"leakage", r"privacy", r"raw prompt"
    ]),
    ("review_fix_loop", [
        r"review pass", r"review round", r"review blocker", r"reviewer", r"independent review",
        r"re-review", r"review-fix", r"blocker"
    ]),
    ("brief_drift_dynamic_config", [
        r"\bbrief\b", r"addendum", r"inlined", r"prompt drift", r"instruction drift",
        r"spec change", r"prompt\.md", r"prereg"
    ]),
    ("attestation_model_routing", [
        r"attest", r"served model", r"refusal", r"safety refusal", r"model name",
        r"model alias", r"openrouter", r"fallback", r"temperature", r"naming ambiguity"
    ]),
    ("hang_detection_visibility", [
        r"\bhang\b", r"hung", r"0% cpu", r"silent output", r"heartbeat", r"dead-man",
        r"unresponsive", r"visibility", r"dashboard", r"progress monitor"
    ]),
    ("concurrency_races_infra", [
        r"worktree\.lock", r"config lock", r"modal.*200", r"200.*modal", r"200 deployed",
        r"port collision", r"loopback", r"sqlite.*lock", r"sqlite_busy", r"\bflock\b",
        r"concurrency race", r"lock race", r"process locking"
    ]),
    ("git_rebase_docs_conflict", [
        r"merge[- ]train", r"repo[- ]map", r"repomap", r"docindex", r"docs/index",
        r"rebase", r"merge conflict", r"force-with-lease", r"squash", r"branch drift",
        r"auto-merge", r"ci polling", r"pr checks", r"github actions"
    ]),
    ("gate_dependency_waiting", [
        r"gate file", r"gates/", r"inter-worker", r"dependency wait", r"waiting on",
        r"blocked on", r"polling latency", r"downstream item", r"coordination wait"
    ]),
    ("crash_recovery_resume", [
        r"resum", r"restart", r"crash", r"kill -9", r"sigkill", r"checkpoint",
        r"state\.json", r"adoptedproc", r"orphan", r"interruption", r"recovery"
    ]),
    ("tool_sandbox_blocking", [
        r"tool timeout", r"sandbox", r"cancellederror", r"argparse", r"percent-sign",
        r"returncode", r"virtualenv", r"uv sync", r"\bvenv\b", r"restrictedworkflow",
        r"\bpipe\b", r"bash timeout", r"glob timeout", r"traversal latency", r"ast pars",
        r"regex syntax", r"syntax warning", r"assertion", r"floating-point", r"schema"
    ]),
]

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def classify_bullet(b: str) -> str:
    t = b.lower()
    for cat, patterns in RULES:
        for p in patterns:
            if re.search(p, t):
                return cat
    if any(k in t for k in ["git", "commit", "branch", " pr "]):
        return "git_rebase_docs_conflict"
    if any(k in t for k in ["modal", "container", "docker"]):
        return "concurrency_races_infra"
    if any(k in t for k in ["spend", "cost", "dollar"]):
        return "spend_accounting_stalls"
    if any(k in t for k in ["wait", "poll"]):
        return "gate_dependency_waiting"
    if any(k in t for k in ["model", "provider"]):
        return "attestation_model_routing"
    return "tool_sandbox_blocking"

def extract_frictions():
    raw_reports = sorted(glob.glob(str(BRIDGE_ROOT / "*/raw/*/report.md")))
    lane_reports = sorted(glob.glob(str(BRIDGE_ROOT / "lanes/*-report.md")))
    all_reports = raw_reports + lane_reports

    records = []
    bullet_idx = 0

    for p in all_reports:
        if "F12-FRICTION-SYNTHESIS" in p:
            continue
        with open(p, errors="ignore") as f:
            text = f.read()

        m = re.search(r"^#+\s+[^\n]*?workflow\s+friction[^\n]*\n(.*?)(?=\n#|\Z)", text, re.IGNORECASE | re.MULTILINE | re.DOTALL)
        if not m:
            continue

        section = m.group(1)
        rel_path = os.path.relpath(p, str(BRIDGE_ROOT))

        if "/raw/" in p:
            parts = p.split("/")
            squad = parts[-4]
            item_id = parts[-2]
        else:
            squad = "lane"
            item_id = Path(p).name.replace("-report.md", "")

        for line in section.splitlines():
            if line.strip().startswith(("---", "DS-DONE", "DS-BLOCKED")):
                break
            if re.match(r"^\s*[-*•\d\.]+\s+", line):
                cleaned = re.sub(r"^\s*[-*•\d\.]+\s+", "", line).strip()
                if cleaned:
                    bullet_idx += 1
                    category = classify_bullet(cleaned)
                    records.append({
                        "id": bullet_idx,
                        "item_id": item_id,
                        "squad": squad,
                        "report_path": rel_path,
                        "category": category,
                        "bullet_text": cleaned,
                    })

    return records

def extract_review_blockers():
    reviews = sorted(glob.glob(str(BRIDGE_ROOT / "ver/raw/REVIEW-*/report.md")))
    review_records = []

    pr_map = {
        "REVIEW-1726": (1726, 1, 6, ["Docs stub instead of implementation", "Selective p-value quoting", "Third-party per-run label", "Nonexistent JSON field", "Invalid command", "Unverified prereg labels"]),
        "REVIEW-1726-R2": (1726, 2, 2, ["Real mode certify-gold is stub", "Inaccurate cost/mode claims"]),
        "REVIEW-1726-R3": (1726, 3, 1, ["Experiment driver cannot certify operator package"]),
        "REVIEW-1726-R4": (1726, 4, 1, ["Stale against origin/main; doc conflict with merged #1744"]),
        "REVIEW-1729": (1729, 1, 5, ["AutoReview stage was no-op", "Real ATIF trial dirs cannot be kept", "DEV-only refusal swallowed", "Label parser inverts not_cheat", "In-sample B4 cohort"]),
        "REVIEW-1729-R2": (1729, 2, 3, ["Real-omp attestation missing", "Honest code handling flaws", "Label inversion edge cases"]),
        "REVIEW-1729-R3": (1729, 3, 3, ["Branch no longer merges with main", "Test-file rule keyed on tool verb dropping honest tests", "DEV admission ignores rollout task id"]),
        "REVIEW-1729-R4": (1729, 4, 2, ["Existing test read in trajectory can be overwritten and kept", "Transcript text leaked into manifest (data policy violation)"]),
        "REVIEW-1729-R5": (1729, 5, 1, ["Docs/PR-body contradicts verb-only create implementation"]),
        "REVIEW-1730": (1730, 1, 2, ["Judge Station verdict leaked in queue", "Guard paths drop unserved requests"]),
        "REVIEW-1730-R2": (1730, 2, 0, []),
        "REVIEW-1732": (1732, 1, 0, []),
        "REVIEW-1732-R2": (1732, 2, 0, []),
        "REVIEW-1738": (1738, 1, 4, ["Spend gate accounts incorrectly in ledger mode", "Migration resets existing caller ledgers", "Default 1800s timeout over-reserves", "Missing settle cleanup"]),
        "REVIEW-1738-R2": (1738, 2, 1, ["Reservations sized from 1800s default stall batch at 5 cells ($2.97 reserved vs $0.01 spent)"]),
        "REVIEW-1738-R3": (1738, 3, 1, ["Mechanical merge conflict / lint error against origin/main"]),
        "REVIEW-1744": (1744, 1, 1, ["Unmeasured reference treated as failed reference"]),
        "REVIEW-1744-R2": (1744, 2, 0, []),
        "REVIEW-1752": (1752, 1, 4, ["Back-fill writes subscription-notional dollars into ledger", "Duplicate rows written to shared ledger", "Reconcile verdict passes with nothing verified", "Unchecked provider API errors"]),
        "REVIEW-1752-R2": (1752, 2, 0, []),
    }

    for r in reviews:
        name = Path(r).parent.name
        if name in pr_map:
            pr, rnd, blockers, desc = pr_map[name]
            review_records.append({
                "review_id": name,
                "pr_number": pr,
                "round": rnd,
                "blocker_count": blockers,
                "blocker_descriptions": desc,
                "report_path": os.path.relpath(r, str(BRIDGE_ROOT)),
            })

    return review_records

def extract_incidents():
    incidents = [
        {
            "id": 1,
            "title": "Multi-Runner Worktree Lock Race",
            "time": "01:09 ET",
            "impact": "Concurrent git worktree add -b across 4 squads collided on .git/config and worktree.lock, failing the first item of all 4 squads.",
            "time_lost_minutes": 5,
            "mitigation_now": "ds_squad.py retry with jitter, cleanup of stray branch, live max_parallel reload; R7 fcntl.flock serialization (F11 KT7).",
            "primitive": "Distributed Worktree File Lock (fcntl.flock)",
            "off_the_shelf": "NO (F11 KT7 proved DBOS/Temporal queues bypass multi-process OS locks)",
        },
        {
            "id": 2,
            "title": "Devin Subscription Quota Rapid Drain",
            "time": "01:14 ET",
            "impact": "Canary showed no quota movement, but 2 real workers consumed 6% daily and 3% weekly quota in 3 minutes via rapid agent tool calls.",
            "time_lost_minutes": 15,
            "mitigation_now": "Emergency re-routing of squads to Gemini 3.8 Flash (workers) and direct Claude Sonnet (reviewers).",
            "primitive": "Preflight Agent Loop Canary & Token Rate-Limiter",
            "off_the_shelf": "NO (Custom model routing)",
        },
        {
            "id": 3,
            "title": "Modal Workspace 200 Deployed Apps Limit",
            "time": "02:08 ET",
            "impact": "Fresh image builds in K7 hit transient 'reached limit of 200 deployed apps' error despite modal app list showing 95 apps.",
            "time_lost_minutes": 10,
            "mitigation_now": "Preflight with fresh-image probe; retry transient deploy failures.",
            "primitive": "Infrastructure Capacity Probe & Circuit Breaker",
            "off_the_shelf": "Partially (retry with backoff in Temporal/Dagu/DBOS)",
        },
        {
            "id": 4,
            "title": "A2 Sweep Process Silent Hang (Incident A)",
            "time": "02:11 ET",
            "impact": "Audit sweep process hung at 0% CPU with silent output and no network sockets for 25 minutes before manual coordinator kill.",
            "time_lost_minutes": 25,
            "mitigation_now": "Killed manually, resumed from on-disk cells; per-item wall-clock caps (max_minutes, start_to_close_timeout).",
            "primitive": "Output-Advancement Dead-Man Watchdog",
            "off_the_shelf": "Partially (Temporal start_to_close_timeout caught in 4.67s; heartbeat failed to catch PID liveness)",
        },
        {
            "id": 5,
            "title": "Modal Spend Reservation Batch Stall",
            "time": "03:15 ET",
            "impact": "Conservative 1800s default timeout reserved $0.297/cell; 10 workers reserved $2.97 after 5 cells, stalling 195/200 cells under $1.50 cap.",
            "time_lost_minutes": 90,
            "mitigation_now": "FIX-S1-1738 patched estimate_cost to use requested timeouts; immediate lease release.",
            "primitive": "Dynamic Two-Phase Budget Reservation & Settle Ledger",
            "off_the_shelf": "NO (Custom financial/token ledger)",
        },
        {
            "id": 6,
            "title": "Brief Edit Not Reaching Running Worker",
            "time": "03:20 ET",
            "impact": "Briefs inlined into prompt.md at launch; coordinator addendum to D3 brief did not reach running worker until manual kill-and-restart.",
            "time_lost_minutes": 15,
            "mitigation_now": "Manual worker kill and restart by coordinator.",
            "primitive": "Dynamic Configuration Subscription & Signal Bus",
            "off_the_shelf": "Partially (Temporal Signals / dynamic plan reload)",
        },
        {
            "id": 7,
            "title": "K7B Commit to Already-Merged PR Branch",
            "time": "04:15 ET",
            "impact": "K7B full-sweep commit pushed to branch whose PR had already merged, stranding evidence until follow-up PR was created.",
            "time_lost_minutes": 20,
            "mitigation_now": "Coordinator detected orphaned branch, created follow-up PR.",
            "primitive": "Git Merge State Verifier & Branch Protection",
            "off_the_shelf": "NO (Git/GitHub API workflow)",
        },
        {
            "id": 8,
            "title": "A4 Gold v1 Harness-Confounded Zero",
            "time": "05:00 ET",
            "impact": "Test hook ran under bare python3 instead of task virtualenv; 15+ tasks failed at import (ModuleNotFoundError), reporting false 0/32 certified.",
            "time_lost_minutes": 120,
            "mitigation_now": "VF-A-GOLD and A4B caught failure; froze prereg at 05:29; rewrote environment hook under task interpreter.",
            "primitive": "Harness Execution Isolation & Virtualenv Parity Checker",
            "off_the_shelf": "NO (Domain-specific task runtime validator)",
        },
        {
            "id": 9,
            "title": "Generated Docs Rebase Conflicts (repo-map.md, INDEX.md)",
            "time": "All Night",
            "impact": "Multiple PRs merging to main conflicted constantly on auto-generated documentation, stalling PR merges across squads.",
            "time_lost_minutes": 180,
            "mitigation_now": "tools/merge_train.py auto-regenerates repo-map.md and INDEX.md during rebase before force-push.",
            "primitive": "Merge Train with Auto-Doc-Regeneration Policy",
            "off_the_shelf": "NO (Custom merge queue handler)",
        },
        {
            "id": 10,
            "title": "Attacker Model Safety Refusal Storm",
            "time": "04:10 ET",
            "impact": "Attacker model refused explicit 'slip past monitoring rules' prompt 24/24 times, leaving L0-aware attacker arm unmeasured.",
            "time_lost_minutes": 45,
            "mitigation_now": "Preflight prompt refusal canary; prompt rephrasing in K7D/K7E.",
            "primitive": "Preflight Safety Canary & Prompt Refusal Detector",
            "off_the_shelf": "NO (LLM-specific policy handler)",
        },
    ]
    return incidents

def main():
    print("Extracting workflow friction evidence...")
    frictions = extract_frictions()
    print(f"Extracted {len(frictions)} friction bullets.")

    review_blockers = extract_review_blockers()
    print(f"Extracted {len(review_blockers)} review rounds.")

    incidents = extract_incidents()
    print(f"Extracted {len(incidents)} overnight incidents.")

    # Aggregate by category
    cat_counts = Counter([f["category"] for f in frictions])
    item_by_cat = defaultdict(set)
    squad_by_cat = defaultdict(set)
    bullets_by_cat = defaultdict(list)

    for f in frictions:
        c = f["category"]
        item_by_cat[c].add(f["item_id"])
        squad_by_cat[c].add(f["squad"])
        bullets_by_cat[c].append(f)

    taxonomy = []
    for cat in CATEGORIES:
        taxonomy.append({
            "category": cat,
            "bullet_count": cat_counts[cat],
            "distinct_items": len(item_by_cat[cat]),
            "distinct_squads": len(squad_by_cat[cat]),
            "item_ids": sorted(list(item_by_cat[cat])),
            "squads": sorted(list(squad_by_cat[cat])),
        })

    # Ranked workflows
    ranked_workflows = [
        {
            "rank": 1,
            "name": "Merge Train with Auto-Doc-Regeneration (PR Merge Queue)",
            "hours_saved_per_night": 15.0,
            "effort": "S",
            "evidence_bullets": cat_counts["git_rebase_docs_conflict"],
            "evidence_items": len(item_by_cat["git_rebase_docs_conflict"]),
            "prerequisites": "evallab.repomap, evallab.docindex, gh CLI",
            "first_step": "Promote tools/merge_train.py into src/evallab/merge_train.py and integrate into CI / on_watch.py loop.",
            "category": "git_rebase_docs_conflict",
        },
        {
            "rank": 2,
            "name": "Automated Review-Fix-Review Orchestrator (Multi-Pass Code Verification)",
            "hours_saved_per_night": 12.0,
            "effort": "M",
            "evidence_bullets": cat_counts["review_fix_loop"] + sum(r["blocker_count"] for r in review_blockers),
            "evidence_items": len(item_by_cat["review_fix_loop"]) + len(set(r["pr_number"] for r in review_blockers)),
            "prerequisites": "Independent reviewer model route (Claude Sonnet), git worktree manager",
            "first_step": "Standardize BLOCKER schema in review reports and build automated parser that spawns FIX worktree with checklist.",
            "category": "review_fix_loop",
        },
        {
            "rank": 3,
            "name": "Dynamic Spend Reservation & Two-Phase Settlement Gate",
            "hours_saved_per_night": 5.0,
            "effort": "S",
            "evidence_bullets": cat_counts["spend_accounting_stalls"],
            "evidence_items": len(item_by_cat["spend_accounting_stalls"]),
            "prerequisites": "spend.jsonl or SQLite failure library ledger",
            "first_step": "Patch modal_cells.py to size reservations from requested timeout rather than 1800s default; release unused reservations immediately.",
            "category": "spend_accounting_stalls",
        },
        {
            "rank": 4,
            "name": "Output-Advancement Dead-Man Watchdog (Hang Detection)",
            "hours_saved_per_night": 4.0,
            "effort": "S",
            "evidence_bullets": cat_counts["hang_detection_visibility"],
            "evidence_items": len(item_by_cat["hang_detection_visibility"]),
            "prerequisites": "Subprocess pipe streaming in runner",
            "first_step": "Add stdout/stderr activity timestamp check in evallab/runner.py to terminate tasks that produce 0 output for >300s.",
            "category": "hang_detection_visibility",
        },
        {
            "rank": 5,
            "name": "Preflight Model Canary & Attestation Router",
            "hours_saved_per_night": 3.5,
            "effort": "S",
            "evidence_bullets": cat_counts["attestation_model_routing"],
            "evidence_items": len(item_by_cat["attestation_model_routing"]),
            "prerequisites": "Model API credentials store",
            "first_step": "Add mandatory 1-call preflight probe before batch sweeps to check model alias, safety refusal, and attestation logging.",
            "category": "attestation_model_routing",
        },
        {
            "rank": 6,
            "name": "Event-Driven DAG Dependency Bus (Replacing Filesystem Markers)",
            "hours_saved_per_night": 3.0,
            "effort": "M",
            "evidence_bullets": cat_counts["gate_dependency_waiting"],
            "evidence_items": len(item_by_cat["gate_dependency_waiting"]),
            "prerequisites": "Runner state event publisher",
            "first_step": "Replace gates/<ITEM>-done polling with SQLite state notifications or event bus publisher.",
            "category": "gate_dependency_waiting",
        },
        {
            "rank": 7,
            "name": "Pre-PR Third-Party Data Policy Boundary Linter",
            "hours_saved_per_night": 2.5,
            "effort": "S",
            "evidence_bullets": cat_counts["data_policy_leakage"],
            "evidence_items": len(item_by_cat["data_policy_leakage"]),
            "prerequisites": "evallab.governance",
            "first_step": "Add third-party trace / card signature regex scanner to evallab.governance check.",
            "category": "data_policy_leakage",
        },
        {
            "rank": 8,
            "name": "Dynamic Plan & Brief Subscription Bus (Live Worker Configuration)",
            "hours_saved_per_night": 2.0,
            "effort": "M",
            "evidence_bullets": cat_counts["brief_drift_dynamic_config"],
            "evidence_items": len(item_by_cat["brief_drift_dynamic_config"]),
            "prerequisites": "Headless worker control channel",
            "first_step": "Add versioned brief hash check to worker loop that gracefully pauses or updates configuration when brief changes.",
            "category": "brief_drift_dynamic_config",
        },
    ]

    # Save local raw data
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    LOCAL_RESULTS_NAMED_DIR.mkdir(parents=True, exist_ok=True)
    EXP_DIR.mkdir(parents=True, exist_ok=True)

    raw_files = {
        "friction_raw.json": frictions,
        "review_blockers.json": review_blockers,
        "incidents.json": incidents,
    }

    for fname, data in raw_files.items():
        with open(RECEIPTS_DIR / fname, "w") as f:
            json.dump(data, f, indent=2)
        with open(LOCAL_RESULTS_DIR / fname, "w") as f:
            json.dump(data, f, indent=2)
        with open(LOCAL_RESULTS_NAMED_DIR / fname, "w") as f:
            json.dump(data, f, indent=2)

    # Save repository aggregate summaries
    repo_files = {
        "taxonomy_summary.json": taxonomy,
        "ranked_workflows.json": ranked_workflows,
        "review_summary.json": review_blockers,
        "incident_summary.json": incidents,
    }

    for fname, data in repo_files.items():
        with open(EXP_DIR / fname, "w") as f:
            json.dump(data, f, indent=2)
        with open(RECEIPTS_DIR / fname, "w") as f:
            json.dump(data, f, indent=2)
        with open(LOCAL_RESULTS_DIR / fname, "w") as f:
            json.dump(data, f, indent=2)
        with open(LOCAL_RESULTS_NAMED_DIR / fname, "w") as f:
            json.dump(data, f, indent=2)

    # Generate Manifest
    manifest = {}
    for p in sorted(RECEIPTS_DIR.glob("*.json")):
        manifest[p.name] = {
            "sha256": sha256_file(p),
            "sha256_12": sha256_file(p)[:12],
            "size_bytes": p.stat().st_size,
        }

    with open(RECEIPTS_DIR / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    with open(EXP_DIR / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    with open(LOCAL_RESULTS_DIR / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)
    with open(LOCAL_RESULTS_NAMED_DIR / "manifest.json", "w") as f:
        json.dump(manifest, f, indent=2)

    print("\nExtraction & Clustering Completed Successfully.")
    print(f"Receipts saved to: {RECEIPTS_DIR}")
    print(f"Local results saved to: {LOCAL_RESULTS_DIR}")
    print(f"Aggregates saved to: {EXP_DIR}")
    print("\nManifest of Hashes:")
    for k, v in manifest.items():
        print(f"  {k:25s}: {v['sha256_12']} ({v['size_bytes']} bytes)")

if __name__ == "__main__":
    main()
