#!/usr/bin/env python3
"""GATED, ONCE-ONLY historical reliability audit of the six RC-1 tasks that are TEST in tb4-audit-v1.

The DEV census never runs this. It is the frozen protocol run ONCE by the delivery owner AFTER the
rules/prompts/thresholds in FREEZE.json are final (no outcome-driven tuning afterwards). What it does:

* reads TEST-AUTHORIZATION.json (must bind this exact FREEZE.json hash) and atomically creates a
  once-only lock BEFORE touching any TEST byte; a second run refuses (``--resume`` continues the same
  frozen run after a crash; ``--rehearse`` runs the identical code on the two DEV tasks first);
* replays the RC-1 pass-side evidence reading (RC-1 screens, source-pinned deterministic proofs and
  question set) with the NEW frozen cheap judge on the canonical-stratum PASSES of
  atrx-vep-crispr, batched-eval-parity, biped-contact-dynamics, cumulative-layout-shift,
  data-anonymization and wal-recovery-ordering, and replays RC-1's deterministic fail rules on their
  FAILS (no model call);
* only then opens RC-1's per-run labels for these six tasks and compares. Per-run details stay in the
  external results dir; the repository receives aggregates only (legacy8-validation.json);
* writes legacy8/exposure.json (what was read, calls, spend) for the exposure record.

It is a trace-label agreement check on a cheap judge, NOT a task-auditor score of tb4-audit-v1 TEST.
RC-1's own labels were adaptive (hash-pinned proofs chosen after reading), so agreement is a
reproducibility/recall check of the new judge against historical labels, not independent ground truth.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cheap_judge
import legacy
from census_core import HERE, JUDGE_DIR, RESULTS, SYSTEM, fmt, rate, sha256_file, write_jsonl
from evidence import Trial
from index import INDEX, normalize, stratum
from pass_rules import legacy_pass

TEST_TASKS = ("atrx-vep-crispr", "batched-eval-parity", "biped-contact-dynamics",
              "cumulative-layout-shift", "data-anonymization", "wal-recovery-ordering")
AUTH = RESULTS / "TEST-AUTHORIZATION.json"
LOCK = RESULTS / "legacy8-test.once"
OUT = RESULTS / "legacy8"
FLAGGED = {"known_exploit", "exploit_candidate"}


def authorize(resume: bool) -> dict[str, Any]:
    if not AUTH.exists():
        raise SystemExit(f"no authorization at {AUTH}: the delivery owner writes it after the final freeze")
    auth = json.loads(AUTH.read_text())
    freeze = sha256_file(HERE / "FREEZE.json")
    if auth.get("freeze_sha256") != freeze or auth.get("scope") != "legacy8-historical-test-once":
        raise SystemExit("authorization does not bind the current FREEZE.json / scope")
    RESULTS.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        # --resume only continues the SAME frozen run after a crash (judge calls are cached, so it spends
        # nothing twice); it can never run under a different freeze.
        if resume and json.loads(LOCK.read_text()).get("freeze_sha256") == freeze:
            return auth
        raise SystemExit("once-only lock already consumed; no re-run, no outcome-driven tuning") from None
    os.write(fd, json.dumps({"freeze_sha256": freeze, "auth": auth}).encode())
    os.close(fd)
    return auth


def load_memberships(tasks: tuple[str, ...]) -> list[dict[str, Any]]:
    needles = tuple(f'"task":"terminal-bench/{t}"' for t in tasks) + tuple(
        f'"task": "terminal-bench/{t}"' for t in tasks)
    out = []
    with INDEX.open(encoding="utf-8") as handle:
        for line in handle:
            if any(n in line for n in needles):
                m = normalize(json.loads(line))
                if m["task"] in tasks and stratum(m) == "canonical":
                    out.append(m)
    return out


def evaluate(tasks: tuple[str, ...], judge: cheap_judge.Judge, workers: int, out: Path) -> dict[str, Any]:
    """Predictions for every canonical membership of ``tasks`` are written BEFORE RC-1's labels are read."""
    mems = load_memberships(tasks)
    out.mkdir(parents=True, exist_ok=True)

    def work(m: dict[str, Any]) -> dict[str, Any]:
        rec = {"task": m["task"], "trial_id": m["trial_id"], "reward": m["reward"]}
        if not m.get("archive_path"):
            return rec | {"side": "pass" if (m["reward"] or 0) >= 1 else "fail", "label": "unmeasured",
                          "error": "no archive"}
        trial = Trial(Path(m["archive_path"]))
        try:
            if m["reward"] is None:
                return rec | {"side": "unscored", "label": "unscored"}
            if m["reward"] >= 1:
                d, _ = legacy_pass(m["task"], trial, judge, m["trial_id"], [], {})
                return rec | {"side": "pass", "label": d.label, "mechanisms": d.mechanisms}
            r = legacy.fail_classify(m["task"], trial)
            return rec | {"side": "fail", "label": r["label"], "mechanism": r["mechanism"]}
        except Exception as exc:
            return rec | {"side": "pass" if (m["reward"] or 0) >= 1 else "fail", "label": "unmeasured",
                          "error": f"{type(exc).__name__}: {exc}"[:200]}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        preds = list(pool.map(work, mems))
    write_jsonl(out / "predictions.jsonl", preds)

    gold = legacy.legacy_labels(set(tasks))
    summary: dict[str, Any] = {"system": SYSTEM, "tasks": {}}
    for task in tasks:
        rows = [p for p in preds if p["task"] == task and p["trial_id"] in gold]
        pas = [p for p in rows if p["side"] == "pass"]
        fai = [p for p in rows if p["side"] == "fail"]
        pos = [p for p in pas if gold[p["trial_id"]]["label"] == "known_exploit"]
        neg = [p for p in pas if gold[p["trial_id"]]["label"] == "neither"]
        agree_f = [p for p in fai if gold[p["trial_id"]]["label"] == p["label"]]
        summary["tasks"][task] = {
            "memberships": len(rows), "canonical_memberships_in_index": sum(p["task"] == task for p in preds),
            "passes": len(pas), "legacy_known_exploit": len(pos),
            "known_exploit_recall": rate(sum(p["label"] == "known_exploit" for p in pos), len(pos)),
            "flagged_recall": rate(sum(p["label"] in FLAGGED for p in pos), len(pos)),
            "flag_rate_on_legacy_neither": rate(sum(p["label"] in FLAGGED for p in neg), len(neg)),
            "pass_abstention": rate(sum(p["label"] == "unmeasured" for p in pas), len(pas)),
            "fails": len(fai), "fail_rule_replay_agreement": rate(len(agree_f), len(fai)),
            "confusion_pass": dict(Counter(f"{gold[p['trial_id']]['label']}->{p['label']}" for p in pas)),
            "confusion_fail": dict(Counter(f"{gold[p['trial_id']]['label']}->{p['label']}" for p in fai)),
        }
    return summary


def run(args: argparse.Namespace) -> None:
    judge = cheap_judge.Judge(JUDGE_DIR, route=args.route, allow_calls=True)
    if args.rehearse:
        # Same code path on the two DEV tasks (no authorization, no lock, nothing committed): proves the
        # gated run cannot crash on a code error after it has consumed its once-only slot.
        summary = evaluate(tuple(sorted(legacy.LEGACY_DEV_TASKS)), judge, args.workers, OUT / "rehearsal")
        (OUT / "rehearsal" / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
        scope = "rehearsal (DEV2 only)"
    else:
        auth = authorize(args.resume)
        legacy.allow_historical_test(set(TEST_TASKS))
        summary = evaluate(TEST_TASKS, judge, args.workers, OUT)
        spend = cheap_judge.spend(JUDGE_DIR / "judge-ledger.jsonl")
        summary |= {"scope": auth["scope"], "freeze_sha256": sha256_file(HERE / "FREEZE.json"), "spend": spend}
        (HERE / "legacy8-validation.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
        (OUT / "exposure.json").write_text(json.dumps({
            "opened": ["RC-1 per-run labels of the six TEST tasks (once, after predictions were written)",
                       "canonical-stratum archives of the six tasks (evidence reading)"],
            "model_calls": spend["settled"], "spend": spend, "freeze_sha256": summary["freeze_sha256"],
        }, indent=1) + "\n")
        scope = "legacy8 TEST (once)"
    print(scope)
    for task, t in summary["tasks"].items():
        print(task, "flagged recall", fmt(t["flagged_recall"]), "| flag on neither",
              fmt(t["flag_rate_on_legacy_neither"]), "| abstain", fmt(t["pass_abstention"]),
              "| fail replay", fmt(t["fail_rule_replay_agreement"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--route", default=cheap_judge.DEFAULT_ROUTE)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--rehearse", action="store_true", help="dry run of the same code on DEV2 only")
    parser.add_argument("--resume", action="store_true", help="continue the same frozen run after a crash")
    run(parser.parse_args())


if __name__ == "__main__":
    main()
