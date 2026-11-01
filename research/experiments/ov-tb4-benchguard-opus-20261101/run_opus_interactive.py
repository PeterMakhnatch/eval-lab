"""W4-BENCHGUARD-OPUS-INTERACTIVE: interactive tool-using Claude Opus 5.5 as BenchGuard Family 1.

Extends W3-BENCHGUARD-3FAM-TOOLS (#1711) on the 14 TB4 DEV tasks (45 keys):
- Runs 14 interactive audits on google-antigravity/claude-opus-5-5 (read,grep,glob; cwd = task package),
  reusing the W3 prompt template, citation verifier, attestation and parser unchanged.
- Audits every session transcript for tool calls resolving outside the task package.
- Pools five source arms per task (oq6_sonnet_B, sonnet_k8, deepseek_k8, interactive_gemini38_k8,
  interactive_opus55_k8), shuffles with random.Random(case_id), and scores them with OQ-6 scorer.py under two
  blinded judges: gemini-3.8-flash (primary, continuity with #1709/#1711) and claude-sonnet-5-5 (cross-check).
- Computes NOFLOOR / CONF0.5 recall, Wilson intervals, Eval³ dedup precision, not_in_view recovery and exact
  paired McNemar tests.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import importlib.util
import json
import random
import subprocess
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
W3_SCRIPT = HERE.parent / "ov-tb4-benchguard-3fam-20261031/run_3fam_tools.py"

BRIDGE = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight")
WORKER_YML = BRIDGE / "ag-opus/worker.yml"
STATS_PY = BRIDGE / "ag-opus/stats.py"
W3_AUDITS = BRIDGE / "g38-flash/raw/W3-BENCHGUARD-3FAM-TOOLS/receipts/work/audits/interactive_gemini38"
FLAT_AUDITS = BRIDGE / "g38-flash/raw/W3-TB4-FINISH-SCORE/receipts/work/audits"
RAW_RECEIPTS = BRIDGE / "ag-opus/raw/W4-BENCHGUARD-OPUS-INTERACTIVE/receipts"
WORK = RAW_RECEIPTS / "work"

OPUS_MODEL = "google-antigravity/claude-opus-5-5"
JUDGES = {
    "gemini38": "google-antigravity/gemini-3.8-flash",
    "sonnet55": "google-antigravity/claude-sonnet-5-5",
}
AUDIT_WORKERS = 3
OMP_SLOTS = threading.BoundedSemaphore(4)
FINDING_CAP = 8
KEYS_TOTAL = 45

SOURCE_ARMS = ["oq6_sonnet_B", "sonnet", "deepseek", "interactive_gemini38", "interactive_opus55"]
# name -> (member source arms, top-8 cap)
EVAL_ARMS = {
    "oq6_sonnet_B": (["oq6_sonnet_B"], False),
    "sonnet_k8": (["sonnet"], False),
    "deepseek_k8": (["deepseek"], False),
    "interactive_gemini38_k8": (["interactive_gemini38"], False),
    "interactive_opus55_k8": (["interactive_opus55"], False),
    "union_3fam_top8": (["sonnet", "deepseek", "interactive_gemini38"], True),
    "union_interactive_3fam_top8": (["interactive_opus55", "deepseek", "interactive_gemini38"], True),
    "union_interactive_3fam_all": (["interactive_opus55", "deepseek", "interactive_gemini38"], False),
    "union_4fam_top8": (["sonnet", "interactive_opus55", "deepseek", "interactive_gemini38"], True),
    "union_4fam_all": (["sonnet", "interactive_opus55", "deepseek", "interactive_gemini38"], False),
}
FAMILY_ORDER = {"interactive_opus55": 0, "sonnet": 1, "deepseek": 2, "interactive_gemini38": 3}


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


w3 = load_module("w3_run_3fam_tools", W3_SCRIPT)


def _resolve_targets(raw: str, pkg: Path) -> list[Path]:
    targets = []
    for part in str(raw).split(";"):
        part = part.strip().lstrip("!")
        if not part:
            continue
        clean = part.split(":")[0].strip() or "."
        p = Path(clean)
        targets.append(p.resolve() if p.is_absolute() else (pkg / p).resolve())
    return targets


def verify_tool_boundaries(session_dir: Path, package_path: Path) -> dict:
    """Stricter than W3: handles ';'-joined paths, '!' excludes, and absolute/'..' glob patterns."""
    pkg = package_path.resolve()
    calls, violations = [], []
    for path in sorted(session_dir.rglob("*.jsonl")):
        for line in path.read_text(errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            msg = entry.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict) or item.get("type") not in ("toolCall", "tool_call", "tool_use"):
                    continue
                tool = item.get("name")
                args = item.get("arguments") or item.get("input") or {}
                calls.append({"tool": tool, "args": args})
                if tool not in ("read", "grep", "glob"):
                    violations.append({"tool": tool, "args": args, "reason": "tool_not_allowed"})
                for target in _resolve_targets(args.get("path") or ".", pkg):
                    reasons = []
                    if not target.is_relative_to(pkg):
                        reasons.append("outside_package")
                    elif "solution" in target.relative_to(pkg).parts:
                        reasons.append("solution_directory")
                    if "answer-keys" in target.parts:
                        reasons.append("answer_keys")
                    for reason in reasons:
                        violations.append({"tool": tool, "path": args.get("path"), "resolved": str(target),
                                           "reason": reason})
                pattern = args.get("pattern")
                if tool == "glob" and isinstance(pattern, str) and (pattern.startswith("/") or ".." in pattern):
                    violations.append({"tool": tool, "pattern": pattern, "reason": "glob_pattern_escape"})
    return {"n_tool_calls": len(calls), "tool_calls": calls, "n_violations": len(violations),
            "violations": violations, "clean": not violations}


def run_opus_audit(task: dict) -> dict:
    case = task["case_id"]
    pkg = Path(task["package_path"])
    out_dir = WORK / "audits/interactive_opus55" / case
    done_file = out_dir / "audit.json"
    if done_file.exists():
        cached = json.loads(done_file.read_text())
        if cached.get("status") == "answered" and cached["attestation"]["attested"] and cached["error"] is None:
            print(f"[{case}] cached opus audit ({cached['parsed_findings']} findings)", flush=True)
            return cached

    session_dir = out_dir / "session"
    if session_dir.exists():
        for old in session_dir.rglob("*"):
            if old.is_file():
                old.unlink()
    session_dir.mkdir(parents=True, exist_ok=True)
    prompt, card = w3.render_interactive_prompt(task, out_dir)
    package_files = w3.build_package_files_dict(pkg, card)
    cmd = [
        "omp", "-p",
        "--config", str(WORKER_YML),
        "--model", OPUS_MODEL,
        "--thinking", "high",
        "--no-skills",
        "--tools", "read,grep,glob",
        "--auto-approve",
        "--no-rules", "--no-extensions", "--no-lsp", "--no-title", "--no-prewalk", "--no-pty",
        "--mode", "text",
        "--session-dir", str(session_dir),
        "--max-time", "600s",
        "--system-prompt", w3.AUDITOR_SYSTEM,
        f"@{out_dir / 'prompt.txt'}",
    ]
    started = time.time()
    with OMP_SLOTS:
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=700, stdin=subprocess.DEVNULL,
                                  cwd=str(pkg))
            rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
        except subprocess.TimeoutExpired:
            rc, stdout, stderr = -9, "", "timeout"
    sec = round(time.time() - started, 1)
    (session_dir / "stdout.txt").write_text(stdout)
    (session_dir / "stderr.txt").write_text(stderr[-5000:])

    att = w3.attestation(session_dir, OPUS_MODEL)
    usg = w3.usage(session_dir)
    boundary = verify_tool_boundaries(session_dir, pkg)
    findings, parse_err = w3.parse_interactive_findings(stdout, package_files, OPUS_MODEL)
    status = "answered" if findings or (parse_err is None and rc == 0) else "error"
    if parse_err:
        err = parse_err
    elif not att["attested"]:
        err = f"attestation_failed: {att}"
    elif not boundary["clean"]:
        err = f"boundary_violation: {boundary['violations']}"
    else:
        err = None
    record = {
        "case_id": case, "task_id": task["task_id"], "family": "interactive_opus55", "route": OPUS_MODEL,
        "status": status, "error": err, "returncode": rc, "seconds": sec, "attestation": att, "usage": usg,
        "tool_boundary_check": boundary, "prompt_sha256": w3.sha(prompt), "response_sha256": w3.sha(stdout),
        "parsed_findings": len(findings), "findings": findings[:FINDING_CAP],
        "memberships": w3.gates(findings[:FINDING_CAP]),
    }
    done_file.write_text(json.dumps(record, indent=1, default=str))
    print(f"[{case}] opus audit: status={status} err={err and err[:120]} findings={len(findings)} "
          f"tools={boundary['n_tool_calls']} viol={boundary['n_violations']} attested={att['attested']} "
          f"sec={sec} usd={usg['usd']}", flush=True)
    return record


def load_audit(path: Path, label: str) -> list[dict]:
    audit = json.loads(path.read_text())
    assert audit.get("status") == "answered", f"{label} not answered: {path}"
    return audit.get("findings", [])


def pooled_items(case: str) -> list[tuple[str, dict]]:
    items = [("oq6_sonnet_B", p) for p in w3.baseline_payloads(case)]
    for fam in ("sonnet", "deepseek"):
        items += [(fam, p) for p in load_audit(FLAT_AUDITS / fam / case / "audit.json", fam)]
    items += [("interactive_gemini38", p) for p in load_audit(W3_AUDITS / case / "audit.json", "gemini")]
    opus = WORK / "audits/interactive_opus55" / case / "audit.json"
    items += [("interactive_opus55", p) for p in load_audit(opus, "opus")]
    return items


def judge_transport_slot(*args, **kwargs):
    with OMP_SLOTS:
        return w3.judge_transport(*args, **kwargs)


def run_judge(task: dict, judge: str) -> dict:
    case = task["case_id"]
    route = JUDGES[judge]
    out_dir = WORK / f"pooled_{judge}" / case
    done_path = out_dir / "pool.json"
    if done_path.exists():
        cached = json.loads(done_path.read_text())
        if cached.get("status") == "complete":
            print(f"[{case}] cached {judge} judge", flush=True)
            return cached

    items = pooled_items(case)
    order = list(range(len(items)))
    random.Random(case).shuffle(order)
    payloads = [items[i][1] for i in order]
    origin = []
    for n, idx in enumerate(order, 1):
        arm, p = items[idx]
        origin.append({
            "cid": f"C{n}", "arm": arm, "rank": p.get("rank"), "confidence": float(p.get("confidence", 0.0)),
            "citation_verified": bool(p.get("citation_verified", False)), "direction": p.get("direction"),
            "mechanism": p.get("mechanism"), "citations": p.get("citations", []), "pool_idx": idx,
        })
    ids = [o["cid"] for o in origin]
    mem = {"old_0_8": [], "new_verified_0_5": ids, "union": ids}
    bundle = w3.BUNDLE_DIR / case / "source.txt"
    sc = {
        "case_id": case, "task_id": task["task_id"], "source_path": str(bundle),
        "source_sha256": w3.sha(bundle.read_bytes()), "package": task["package_path"], "files": [],
        "keys_path": task["keys_path"], "key_count": task["key_count"],
    }
    assert w3.sha(Path(task["keys_path"]).read_bytes()) == task["keys_sha256"], f"keys drift {case}"
    scorer = load_module(f"scorer_{judge}_{case}", w3.SCORER_PY)
    scorer.SCORER_ROUTE = route
    print(f"[{case}] {judge} judge start: {len(ids)} candidates", flush=True)
    result = {}
    for attempt in (1, 2):
        result = scorer.score_union(
            sc, payloads, mem, output_directory=out_dir / f"attempt{attempt}",
            transport_call=judge_transport_slot, config=WORKER_YML, config_sha256=w3.sha(WORKER_YML.read_bytes()),
        )
        result["attempt"] = attempt
        attested = ((result.get("transport_receipt") or {}).get("attestation") or {}).get("attested")
        if result.get("status") == "complete" and attested:
            break
        print(f"[{case}] {judge} attempt {attempt}: status={result.get('status')} attested={attested} "
              f"err={result.get('error')}", flush=True)
    result["origin"] = origin
    result["n_candidates"] = len(ids)
    result["judge"] = judge
    done_path.write_text(json.dumps(result, indent=1, default=str))
    tr = result.get("transport_receipt") or {}
    print(f"[{case}] {judge} judge done: status={result.get('status')} "
          f"attested={(tr.get('attestation') or {}).get('attested')} sec={tr.get('seconds')}", flush=True)
    return result


def select(origin: list[dict], members: list[str], top8: bool, floor: float | None) -> list[dict]:
    cands = [o for o in origin if o["arm"] in members and o["citation_verified"]
             and (floor is None or o["confidence"] >= floor)]
    if top8:
        cands.sort(key=lambda o: (-o["confidence"], FAMILY_ORDER.get(o["arm"], 99), o["rank"] or 0))
        cands = cands[:FINDING_CAP]
    return cands


def evaluate(tasks: list[dict], pools: dict[str, dict], stats, floor: float | None) -> dict:
    niv = json.loads(w3.NOT_IN_VIEW.read_text())["not_in_view_subclass"]
    arms = {}
    for name, (members, top8) in EVAL_ARMS.items():
        matched, task_hits, mech = set(), set(), []
        counts = {"match": 0, "dup": 0, "plausible_unkeyed": 0, "false_alarm": 0, "unclear": 0}
        cells = []
        for t in tasks:
            case = t["case_id"]
            pool = pools[case]
            judgments = {j["id"]: j for j in pool["judgments"]}
            chosen = select(pool["origin"], members, top8, floor)
            seen = set()
            for o in chosen:
                j = judgments[o["cid"]]
                mech.append(len(o["mechanism"] or ""))
                if j["verdict"] == "match":
                    keys = set(j["matched_key_ids"])
                    counts["match" if keys - seen else "dup"] += 1
                    seen |= keys
                    matched |= {(case, k) for k in keys}
                    if keys:
                        task_hits.add(case)
                elif j["verdict"] in ("plausible_unkeyed", "false_alarm"):
                    counts[j["verdict"]] += 1
                else:
                    counts["unclear"] += 1
            cells.append({"case_id": case, "findings": len(chosen), "matched_key_ids": sorted(seen)})
        judged = sum(counts.values())
        k = len(matched)
        arms[name] = {
            "matched_keys": k, "matched": sorted(matched), "recall": stats.fmt(k, KEYS_TOTAL),
            "tasks_with_hit": len(task_hits), "findings_judged": judged, **counts,
            "precision_strict": stats.fmt(counts["match"] + counts["dup"], judged),
            "precision_dedup_eval3": stats.fmt(counts["match"], judged - counts["dup"]),
            "findings_per_task": round(judged / len(tasks), 2),
            "mechanism_chars_median": sorted(mech)[len(mech) // 2] if mech else 0,
            "not_in_view_recovered": sorted(f"{c}:{key.split(':')[-1]}" for c, key in matched
                                            if f"{c}:{key.split(':')[-1]}" in niv),
            "cells": cells,
        }
    comparisons = {}
    for ref in ("oq6_sonnet_B", "union_3fam_top8"):
        ref_set = set(map(tuple, arms[ref]["matched"]))
        for name in EVAL_ARMS:
            if name == ref:
                continue
            cur = set(map(tuple, arms[name]["matched"]))
            b, c = len(ref_set - cur), len(cur - ref_set)
            comparisons.setdefault(name, {})[f"vs_{ref}"] = {
                "b": b, "c": c, "both": len(ref_set & cur), "p": stats.mcnemar_exact(b, c)}
    return {"arms": arms, "comparisons": comparisons}


def judge_agreement(tasks: list[dict], a: dict[str, dict], b: dict[str, dict]) -> dict:
    total = agree = match_both = match_a = match_b = 0
    for t in tasks:
        ja = {j["id"]: j for j in a[t["case_id"]]["judgments"]}
        jb = {j["id"]: j for j in b[t["case_id"]]["judgments"]}
        for cid in ja:
            total += 1
            va, vb = ja[cid]["verdict"], jb[cid]["verdict"]
            agree += va == vb
            match_a += va == "match"
            match_b += vb == "match"
            match_both += va == "match" and vb == "match"
    return {"candidates": total, "verdict_agreement": agree, "match_gemini": match_a, "match_sonnet": match_b,
            "match_both": match_both}


def boundary_summary(tasks: list[dict]) -> dict:
    rows, calls, viol = [], 0, 0
    for t in tasks:
        rec = json.loads((WORK / "audits/interactive_opus55" / t["case_id"] / "audit.json").read_text())
        check = verify_tool_boundaries(WORK / "audits/interactive_opus55" / t["case_id"] / "session",
                                       Path(t["package_path"]))
        calls += check["n_tool_calls"]
        viol += check["n_violations"]
        rows.append({"case_id": t["case_id"], "tool_calls": check["n_tool_calls"],
                     "violations": check["violations"], "attested": rec["attestation"]["attested"],
                     "assistant_provider_model": rec["attestation"]["assistant_provider_model"],
                     "n_fallbacks": rec["attestation"]["n_fallbacks"], "findings": rec["parsed_findings"],
                     "verified_findings": sum(f["citation_verified"] for f in rec["findings"]),
                     "seconds": rec["seconds"], "usd": rec["usage"]["usd"]})
    return {"tasks": rows, "total_tool_calls": calls, "total_violations": viol,
            "attested": sum(r["attested"] for r in rows), "usd": round(sum(r["usd"] for r in rows), 4)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=["run", "eval"], default="run")
    parser.add_argument("--case", default=None)
    args = parser.parse_args()
    tasks = w3.cohort()
    if args.case:
        tasks = [t for t in tasks if t["case_id"] == args.case]

    if args.action == "run":
        judge_pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)
        judge_futures = []

        def audit_then_queue(task: dict) -> dict:
            rec = run_opus_audit(task)
            if rec["status"] == "answered" and rec["error"] is None:
                judge_futures.extend(judge_pool.submit(run_judge, task, j) for j in JUDGES)
            return rec

        with concurrent.futures.ThreadPoolExecutor(max_workers=AUDIT_WORKERS) as pool:
            audits = list(pool.map(audit_then_queue, tasks))
        results = [f.result() for f in judge_futures]
        judge_pool.shutdown()
        bad_audits = [a["case_id"] for a in audits if a["status"] != "answered" or a["error"]]
        bad_judges = [(r["case_id"], r["judge"]) for r in results if r.get("status") != "complete"]
        print(f"bad audits: {bad_audits}\nbad judges: {bad_judges}", flush=True)
        return

    stats = load_module("stats", STATS_PY)
    out = {"cohort": {"tasks": len(tasks), "keys": KEYS_TOTAL}, "audits": boundary_summary(tasks), "judges": {}}
    pools_by_judge = {}
    for judge in JUDGES:
        pools = {t["case_id"]: json.loads((WORK / f"pooled_{judge}" / t["case_id"] / "pool.json").read_text())
                 for t in tasks}
        assert all(p["status"] == "complete" for p in pools.values()), judge
        pools_by_judge[judge] = pools
        out["judges"][judge] = {
            "route": JUDGES[judge],
            "attested": sum(bool(((p.get("transport_receipt") or {}).get("attestation") or {}).get("attested"))
                            for p in pools.values()),
            "candidates": sum(p["n_candidates"] for p in pools.values()),
            "nofloor": evaluate(tasks, pools, stats, None),
            "conf05": evaluate(tasks, pools, stats, 0.5),
        }
    out["judge_agreement"] = judge_agreement(tasks, pools_by_judge["gemini38"], pools_by_judge["sonnet55"])
    text = json.dumps(out, indent=1, default=str)
    (RAW_RECEIPTS / "results.json").write_text(text)
    (HERE / "results.json").write_text(text)
    for judge, res in out["judges"].items():
        for variant in ("nofloor", "conf05"):
            print(f"\n=== judge={judge} variant={variant} ===")
            for name, r in res[variant]["arms"].items():
                comp = res[variant]["comparisons"].get(name, {})
                vb = comp.get("vs_oq6_sonnet_B", {})
                vu = comp.get("vs_union_3fam_top8", {})
                print(f"{name:<28} {r['recall']:<26} tasks={r['tasks_with_hit']:>2} judged={r['findings_judged']:>3} "
                      f"strict={r['precision_strict']:<24} eval3={r['precision_dedup_eval3']:<24} "
                      f"niv={len(r['not_in_view_recovered'])}/10 "
                      f"vsB b={vb.get('b')} c={vb.get('c')} p={vb.get('p')} | vsU3 b={vu.get('b')} c={vu.get('c')} "
                      f"p={vu.get('p')}")
    print(json.dumps(out["judge_agreement"]), json.dumps({k: v for k, v in out["audits"].items() if k != "tasks"}))


if __name__ == "__main__":
    main()
