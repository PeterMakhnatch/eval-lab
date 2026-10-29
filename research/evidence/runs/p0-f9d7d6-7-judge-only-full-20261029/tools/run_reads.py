#!/usr/bin/env python3
"""Label-free p0f7 judge-only runner. Invoke from the p0f7 worktree."""
from __future__ import annotations

import json
import os
import pickle
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("EVALLAB_DISABLE_LOCAL_DOCKER", "1")

from evallab.production.trace_harness import (
    DEFAULT_DATABASE,
    FindingStore,
    HarnessOptions,
    HarnessRun,
    actor_inputs_available,
    resolve_runs,
    run_detectors,
)
from evallab.production.trace_harness_stream import (
    CallGuard,
    CascadeJudgeOnlyStreamDetector,
    QuotaGate,
    QuotaStop,
    VerdictCache,
    cache_key,
    live_call,
    quota_used,
)

STATE = Path.home() / ".local/state"
ROOT = STATE / "evallab-prod-live/trace-harness/p0f7"
MANIFEST = ROOT / "gate-audit-runs.jsonl"
SPEND = STATE / "evallab-failure-library/spend.jsonl"
READER = "anthropic/claude-haiku-5-5"
DEADLINE = datetime.fromisoformat("2026-10-29T23:28:00+00:00").timestamp()
ORDER = ("555", "pairs", "cheatbench", "cm18", "np21", "g150")
OLD = [STATE / "evallab-prod-live/trace-harness/p0f5/judge-cache-anthropic_claude-haiku-5-5.jsonl",
       STATE / "evallab-prod-live/trace-harness/p0-f9d7d6-3/judge-cache-anthropic_claude-haiku-5-5.jsonl",
       STATE / "evallab-prod-live/trace-harness/p0f6/judge-cache.jsonl"]
STAGED = {"cm18": STATE / "evallab-cm18/runs.pkl", "np21": STATE / "evallab-np21-cm1/runs.pkl"}


def jsonl(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_manifest():
    rows = jsonl(MANIFEST)
    if not rows:
        raise ValueError("empty or missing manifest")
    by_id = {}
    for row in rows:
        rid, populations = row["run_id"], row["populations"]
        if not isinstance(rid, str) or not rid or not isinstance(populations, list) or not populations:
            raise ValueError("invalid manifest row")
        if rid in by_id or any(p not in ORDER for p in populations):
            raise ValueError("duplicate id or unknown population")
        by_id[rid] = row
    return by_id


def load_runs(manifest, repo):
    # The manifest contains only admitted IDs; no labels or sample strata are read.
    ordinary = [rid for rid, row in manifest.items() if not any(p in STAGED for p in row["populations"])]
    found = {}
    for start in range(0, len(ordinary), 100):
        for run in resolve_runs(",".join(ordinary[start:start + 100]), database=DEFAULT_DATABASE, repo_root=repo):
            if run.run_id in found:
                raise ValueError("duplicate resolved run")
            found[run.run_id] = run
    for tag, path in STAGED.items():
        wanted = {rid for rid, row in manifest.items() if tag in row["populations"]}
        if not wanted:
            continue
        with path.open("rb") as f:
            rows = pickle.load(f)
        for row in rows:
            rid = f"{tag}:{row['set']}:{row['key']}"
            if rid in wanted:
                if rid in found:
                    raise ValueError("duplicate staged run")
                steps = tuple(row["steps"])
                found[rid] = HarnessRun(rid, f"{tag}:{row['set']}", "CROSS-TEST", "unknown",
                                        steps, actor_inputs_available(steps))
    missing = set(manifest) - set(found)
    if missing:
        raise ValueError(f"manifest resolution failed for {len(missing)} ids; refusing partial load")
    return found

def stop(reason, **details):
    status = {"reason": reason, "ts": datetime.now(UTC).isoformat(), **details}
    (ROOT / "runner-status.json").write_text(json.dumps(status, sort_keys=True) + "\n")
    print(json.dumps(status, sort_keys=True), flush=True)
    return 2


def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    if time.time() >= DEADLINE:
        return stop("mission deadline")
    manifest = load_manifest()
    audit_path = ROOT / "card-audit-runs.jsonl"
    audit_summary = ROOT / "card-audit-summary.json"
    j_path = ROOT / "historical-j-audit.json"
    if not all(p.is_file() for p in (audit_path, audit_summary, j_path)):
        return stop("required label-free audits incomplete")
    audited = jsonl(audit_path)
    if len(audited) != len(manifest) or {r["run_id"] for r in audited} != set(manifest):
        return stop("card audit does not match manifest")
    j_audit = json.loads(j_path.read_text())
    if j_audit["runs"] != len(manifest) or j_audit["missing_historical"] or j_audit["mismatches"]:
        return stop("historical J audit incomplete or mismatched")
    audited_gate_misses = {w["key"] for r in audited for w in r["windows"]
                           if w["rule"] == "grader_tamper" and not w["cached"]}
    sources = []
    for path in OLD:
        if not path.is_file():
            raise ValueError(f"missing prior cache: {path}")
        sources.extend(jsonl(path))
    cache = VerdictCache(ROOT / "judge-cache.jsonl", model=READER, rows=sources)
    ledger = ROOT / "attempts.jsonl"
    earlier = jsonl(ledger)
    if len(earlier) >= 1500:
        return stop("attempt budget already exhausted")
    consecutive = 0
    for row in reversed(earlier):
        if row.get("refusal"):
            consecutive += 1
        else:
            break
    if consecutive >= 3:
        return stop("prior three consecutive refusals")
    guard = CallGuard(max_attempts=1500 - len(earlier), max_consecutive_refusals=3)
    guard.consecutive_refusals = consecutive
    gate = QuotaGate(provider="anthropic", stop=.85, refresh_seconds=30)
    transport = live_call(READER, gate, ledger, guard)
    blocked = []

    def gate_only(model, system, user):
        # The candidate is the structured card, not text searched in its window.
        try:
            candidate = json.loads(user)["candidate"]
            allowed = (isinstance(candidate, dict) and candidate.get("rule") == "grader_tamper"
                       and cache_key(model, system, user) in audited_gate_misses)
        except (ValueError, KeyError, TypeError):
            allowed = False
        if model != READER or not allowed:
            blocked.append({"reason": "non-gate or unaudited cache miss, or wrong route"})
            return {"ok": False, "attempts": 0, "answer": None,
                    "errors": ["GuardStop: non-gate or unaudited cache miss, or wrong route"],
                    "requested_model": model, "input_tokens": 0, "output_tokens": 0}
        return transport(model, system, user)

    options = HarnessOptions(database=DEFAULT_DATABASE, repo_root=Path.cwd(), allow_model=True, reader=READER)
    # Serial execution prevents any in-flight calls after the third refusal.
    detector = CascadeJudgeOnlyStreamDetector(options, cache=cache, call=gate_only, workers=1)
    store = FindingStore(ROOT / "judge-only.jsonl")
    existing = store.records()
    gate_run_ids = {r["run_id"] for r in audited if any(
        w["rule"] == "grader_tamper" and not w["cached"] for w in r["windows"]
    )}
    ordered = sorted(gate_run_ids, key=lambda rid: (min(ORDER.index(p) for p in manifest[rid]["populations"]), rid))
    todo = [rid for rid in ordered if (rid, detector.name, detector.version, READER) not in existing]
    runs = load_runs({rid: manifest[rid] for rid in todo}, Path.cwd())
    print(json.dumps({"manifest_runs": len(ordered), "remaining": len(todo), "cached_judgments": len(cache),
                      "previous_attempts": len(earlier), "deadline_utc": datetime.fromtimestamp(DEADLINE, UTC).isoformat()}), flush=True)
    for batch, offset in enumerate(range(0, len(todo), 16)):
        if time.time() >= DEADLINE:
            return stop("mission deadline", guard=guard.as_dict())
        try:
            quota = quota_used("anthropic")
        except QuotaStop as exc:
            return stop("quota unavailable", error=str(exc), guard=guard.as_dict())
        if quota >= .85:
            return stop("quota", quota=quota, guard=guard.as_dict())
        if time.time() >= DEADLINE:
            return stop("mission deadline after quota check", guard=guard.as_dict())
        chunk = todo[offset:offset + 16]
        pos = ledger.stat().st_size if ledger.exists() else 0
        started = 0
        try:
            for rid in chunk:
                if blocked or guard.stop_reason or detector.stats.quota_stopped:
                    break
                started += 1
                run_detectors([runs[rid]], [detector], store)
                if rid in detector.runs:
                    with (ROOT / "judge-only-details.jsonl").open("a", encoding="utf-8") as f:
                        f.write(json.dumps({"run_id": rid, **detector.runs.pop(rid)}, sort_keys=True) + "\n")
        finally:
            rows = []
            if ledger.exists():
                with ledger.open("rb") as f:
                    f.seek(pos)
                    rows = [json.loads(line) for line in f if line.strip()]
            receipt = {"ts": datetime.now(UTC).isoformat(), "route": READER, "usd": 0,
                       "purpose": "p0-SELF-f9d7d6-7", "batch": batch, "runs_started": started,
                       "quota": quota, "attempts": len(rows),
                       "served": dict(Counter(str(r.get("served")) for r in rows)),
                       "rejected": sum(not r.get("ok") for r in rows),
                       "refusals": sum(bool(r.get("refusal")) for r in rows),
                       "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
                       "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows)}
            SPEND.parent.mkdir(parents=True, exist_ok=True)
            with SPEND.open("a", encoding="utf-8") as f:
                f.write(json.dumps(receipt, sort_keys=True) + "\n")
            print(json.dumps({"batch_receipt": receipt, "guard": guard.as_dict(), "blocked": len(blocked)}), flush=True)
        if blocked or guard.stop_reason or detector.stats.quota_stopped:
            return stop("guard, quota, or non-gate cache miss", guard=guard.as_dict(), blocked=blocked)
    status = {"reason": "complete", "guard": guard.as_dict(), "stats": detector.stats.as_dict()}
    (ROOT / "runner-status.json").write_text(json.dumps(status, sort_keys=True) + "\n")
    print(json.dumps(status), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
