#!/usr/bin/env python3
"""DEV-only per-mechanism census of public TB4 trajectories (system tb4-dev-run-census/1).

Run with the worktree's ``.venv/bin/python``; paid judge calls only through ``keys run --``::

    run_census.py freeze                      # hashes of registry/rules/prompts/settings -> FREEZE.json
    run_census.py smoke --judge               # ONE dev-only judge request; route/provider/price check
    run_census.py classify --judge            # label every DEV membership (labels-dev.jsonl)
    run_census.py calibrate                   # cheap-judge + rule calibration (calibration.json)
    run_census.py tables                      # CENSUS.md / census.json
    run_census.py export                      # DEV-only dataset + manifest (fail-closed on non-DEV)
    run_census.py check                       # invariants: DEV-only, conservation, budget

No downloaded content is executed. Missing evidence, judge errors and budget refusals are
``unmeasured`` -- never clean. No TEST task is read, labelled, judged or exported.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cheap_judge
import index as idx
from census_core import (
    DEV_TASKS,
    HERE,
    JUDGE_DIR,
    LEGACY_DEV_TASKS,
    SYSTEM,
    UNMEASURED,
    UNSCORED,
    membership_id,
    out_dir,
    read_jsonl,
    sha256_file,
    sha256_text,
    write_jsonl,
)
from evidence import Trial
from fail_rules import Decision, fail_decision
from mechanisms import MECHANISMS, check_registry, for_task, registry_rows
from pass_rules import SCREENS, pass_decision

PM_URL = "https://preferencemodel.com/blog/terminal-bench-4-is-saturated/issues/"
FILES = ("census_core.py", "mechanisms.py", "evidence.py", "fail_rules.py", "pass_rules.py", "legacy.py",
         "cheap_judge.py", "run_census.py", "tables.py", "calibrate.py", "index.py", "validate_legacy8.py")


def side_of(reward: float | None) -> str:
    return "unscored" if reward is None else ("pass" if reward >= 1 else "fail")


def label_membership(m: dict[str, Any], judge: cheap_judge.Judge) -> tuple[dict[str, Any], dict[str, Any]]:
    task = m["task"]
    side = side_of(m["reward"])
    audit: dict[str, Any] = {}
    reg = for_task(task, side) if side != "unscored" else []
    checked = [x.id for x in reg if x.op != "none"]
    unchecked = [{"mechanism": x.id, "reason": x.reason} for x in reg if x.op == "none"]
    if side == "unscored":
        d = Decision(UNSCORED, [], f"reward is null (stratum {m['stratum']})", "low")
    elif m["stratum"] == "quarantine":
        # Different-source / drifted locks: mechanism signatures are not qualified for these tasks'
        # sources, so they inherit none. The public archive is retained; the label stays unmeasured.
        d = Decision(UNMEASURED, [], f"quarantined source ({m['provenance_admission']}): task-source "
                     "signatures not qualified; archive retained, not classified", "low")
    elif not m.get("archive_path") or m["stratum"] in {"unavailable", "incomplete"}:
        d = Decision(UNMEASURED, [], f"no classifiable archive: acquisition_state={m['acquisition_state']}",
                     "low")
    else:
        trial = Trial(Path(m["archive_path"]))
        if trial.reward is not None and float(trial.reward) != float(m["reward"]):
            d = Decision(UNMEASURED, [], f"archive reward {trial.reward} != index reward {m['reward']}", "low")
        elif side == "fail":
            d = fail_decision(task, trial)
        else:
            d, audit = pass_decision(task, trial, judge, membership_id(m))
    rec = {
        "system": SYSTEM, "membership_id": membership_id(m), "task": task, "split": "dev",
        "row_id": m["row_id"], "board_rank": m["board_rank"], "model": m["model"], "effort": m["effort"],
        "hub_trial_id": m["trial_id"], "archive_trial_id": m["archive_trial_id"], "job_id": m["job_id"],
        "attempt": m["attempt"], "reward": m["reward"], "side": side, "label": d.label,
        "mechanisms": d.mechanisms, "mechanisms_checked": checked, "mechanisms_unchecked": unchecked,
        "method": d.method, "judge_route": judge.route.key if d.method == "judge" else None,
        "prompt_version": cheap_judge.PROMPT_VERSION if d.method == "judge" else None,
        "evidence": d.evidence, "confidence": d.confidence, "facts": d.facts,
        "catalog_source": f"{PM_URL}#{task}" if reg else None,
        "provenance": {"stratum": m["stratum"], "admission": m["provenance_admission"],
                       "archive_class": m["archive_class"], "acquisition_state": m["acquisition_state"],
                       "evidence_class": m["evidence_class"], "native_lock_stratum_1120": m["provenance_1120"],
                       "archive_tree_sha256": m["tree_sha256"]},
    }
    return rec, audit


def load_memberships(bootstrap: bool) -> list[dict[str, Any]]:
    mems = idx.bootstrap_index() if bootstrap else idx.load_index()
    return idx_only_dev(mems)


def idx_only_dev(mems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bad = [m for m in mems if m["task"] not in DEV_TASKS]
    if bad:
        raise SystemExit(f"{len(bad)} non-DEV memberships in the working set")
    return mems



def cmd_freeze(args: argparse.Namespace) -> None:
    receipt = check_registry()
    screened = {(t, s.slug) for t, scs in SCREENS.items() for s in scs}
    legacy_ok = {m.id for m in MECHANISMS if m.task in LEGACY_DEV_TASKS and m.side == "pass"}
    missing = [m.id for m in MECHANISMS if m.side == "pass" and m.op != "none" and m.id not in legacy_ok
               and (m.task, m.slug) not in screened]
    if missing:
        raise SystemExit(f"pass-side mechanisms without a detector: {missing}")
    freeze = {
        "system": SYSTEM, "split_key": "tb4-audit-v1", "dev_tasks": sorted(DEV_TASKS),
        "registry": {"mechanisms": len(MECHANISMS), "sha256": sha256_text(json.dumps(registry_rows(), sort_keys=True)),
                     **receipt},
        "files_sha256": {f: sha256_file(HERE / f) for f in FILES},
        "judge": {"route": cheap_judge.DEFAULT_ROUTE, "prompt_version": cheap_judge.PROMPT_VERSION,
                  "system_prompt_sha256": sha256_text(cheap_judge.SYSTEM), "cap_usd": cheap_judge.CAP_USD,
                  "max_request_usd": cheap_judge.MAX_REQUEST_USD,
                  "routes": {k: {"model": r.model, "provider": r.provider,
                                 "price_in_per_mtok": round(r.price_in * 1e6, 6),
                                 "price_out_per_mtok": round(r.price_out * 1e6, 6), "max_tokens": r.max_tokens,
                                 "verified": r.verified} for k, r in cheap_judge.ROUTES.items()}},
        "positive_rule": "known_exploit/task_defect need deterministic proof; judge yes needs a verified quote "
                         "and yields exploit_candidate only; unsure/error/refusal is unmeasured",
        "index": idx.index_receipt(),
    }
    path = HERE / "FREEZE.json"
    path.write_text(json.dumps(freeze, indent=1, sort_keys=True) + "\n")
    (HERE / "registry.json").write_text(json.dumps(registry_rows(), indent=1, sort_keys=True) + "\n")
    print(json.dumps({k: freeze[k] for k in ("registry", "judge")}, indent=1)[:1500])


def cmd_smoke(args: argparse.Namespace) -> None:
    """One dev-only judge request on a DEV2 pass trial; proves route/provider/price/ledger."""
    mems = [m for m in load_memberships(args.bootstrap) if m["task"] == "vf2-speedup-networkx"
            and m["reward"] == 1 and m.get("archive_path")]
    if not mems:
        raise SystemExit("no vf2 pass archive on disk yet")
    judge = cheap_judge.Judge(JUDGE_DIR, allow_calls=args.judge)
    rec = audit = None
    for m in mems:  # first DEV2 pass that is not settled by a deterministic proof, so a request happens
        rec, audit = label_membership(m, judge)
        if audit.get("judge"):
            break
    if rec is None or audit is None:
        raise SystemExit("no vf2 pass reached the judge")
    print(json.dumps({"membership": rec["membership_id"], "label": rec["label"], "evidence": rec["evidence"],
                      "judge": {k: v for k, v in (audit.get("judge") or {}).items() if k != "answers"},
                      "answers": (audit.get("judge") or {}).get("answers"),
                      "spend": cheap_judge.spend(judge.ledger)}, indent=1, default=str)[:4000])


def cmd_classify(args: argparse.Namespace) -> None:
    mems = load_memberships(args.bootstrap)
    if args.task:
        mems = [m for m in mems if m["task"] in set(args.task)]
    if args.side:
        mems = [m for m in mems if side_of(m["reward"]) == args.side]
    root = out_dir(args.bootstrap)
    root.mkdir(parents=True, exist_ok=True)
    judge = cheap_judge.Judge(JUDGE_DIR, route=args.route, allow_calls=args.judge)

    def work(m: dict[str, Any]):
        try:
            return label_membership(m, judge)
        except Exception as exc:  # an unreadable archive is unknown, never clean
            rec, _ = label_membership({**m, "archive_path": None}, judge)
            rec["label"], rec["confidence"] = UNMEASURED, "low"
            rec["evidence"] = f"classifier error: {type(exc).__name__}: {exc}"[:280]
            return rec, {"error": repr(exc)}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(work, mems))
    labels = [r for r, _ in results]
    suffix = f"-{args.tag}" if args.tag else ""
    existing = {r["membership_id"]: r for r in read_jsonl(root / f"labels{suffix or '-all'}.jsonl")}
    existing.update({r["membership_id"]: r for r in labels})
    write_jsonl(root / f"labels{suffix or '-all'}.jsonl", sorted(existing.values(), key=lambda r: r["membership_id"]))
    audits = {r["membership_id"]: a for r, a in results}
    old = {r["membership_id"]: r for r in read_jsonl(root / f"audit{suffix}.jsonl")}
    old.update({k: {"membership_id": k, **v} for k, v in audits.items()})
    write_jsonl(root / f"audit{suffix}.jsonl", list(old.values()))
    print(json.dumps({"classified": len(labels), "labels": dict(Counter(r["label"] for r in labels)),
                      "spend": cheap_judge.spend(judge.ledger)}, indent=1))


def cmd_check(args: argparse.Namespace) -> None:
    root = out_dir(args.bootstrap)
    labels = read_jsonl(root / "labels-all.jsonl")
    bad = [r["task"] for r in labels if r["task"] not in DEV_TASKS or r.get("split") != "dev"]
    if bad:
        raise SystemExit(f"non-DEV label rows: {sorted(set(bad))}")
    ledger = cheap_judge.spend(JUDGE_DIR / "judge-ledger.jsonl")
    if ledger["charged_or_reserved_usd"] > cheap_judge.CAP_USD:
        raise SystemExit(f"budget exceeded: {ledger}")
    print(json.dumps({"labels": len(labels), "dev_only": True, "spend": ledger}, indent=1))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bootstrap", action="store_true", help="development only: index from archives on disk")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("freeze", "smoke", "classify", "check", "tables", "export", "calibrate"):
        p = sub.add_parser(name)
        p.add_argument("--judge", action="store_true", help="allow new metered judge requests")
        p.add_argument("--task", action="append")
        p.add_argument("--side", choices=("pass", "fail"))
        p.add_argument("--route", default=cheap_judge.DEFAULT_ROUTE, choices=sorted(cheap_judge.ROUTES))
        p.add_argument("--workers", type=int, default=6)
        p.add_argument("--tag", help="write labels-<tag>.jsonl instead of labels-all.jsonl (route comparisons)")
    args = parser.parse_args(argv)
    if args.cmd in {"tables", "export", "calibrate"}:
        import tables  # deferred: only needs finished labels

        getattr(tables, f"cmd_{args.cmd}")(args)
        return
    {"freeze": cmd_freeze, "smoke": cmd_smoke, "classify": cmd_classify, "check": cmd_check}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
