"""D2-READER-LADDER: Cheap-reader ladder execution script.

Evaluates candidates on the 300-case stratified cohort of TraceMonitor-Bench DEV v1.
Subscription rungs ($0):
  - gemini-2.5-flash-lite
  - gemini-3.1-flash-lite
  - gemini-2.5-flash
  - gemini-3.5-flash-lite
  - gemini-3.8-flash (reference)
OpenRouter paid rungs (cap $1.50):
  - inclusionai/ling-3.0-flash
  - mistralai/mistral-nemo
  - openai/gpt-oss-120b
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from evallab.autoreview_monitor import RUBRIC, parse_verdict

WORKER_YML = "/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/worker.yml"
BENCH_ROOT = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/trace-monitor-bench-dev-v1")
SPEND_LIBRARY = Path.home() / ".local/state/evallab-failure-library/spend.jsonl"

RUNGS = [
    {
        "rung_id": "gemini-2.5-flash-lite",
        "model": "google-antigravity/gemini-2.5-flash-lite",
        "route": "subscription",
        "prompt_price_per_m": 0.0,
        "comp_price_per_m": 0.0,
    },
    {
        "rung_id": "gemini-3.1-flash-lite",
        "model": "google-antigravity/gemini-3.1-flash-lite",
        "route": "subscription",
        "prompt_price_per_m": 0.0,
        "comp_price_per_m": 0.0,
    },
    {
        "rung_id": "gemini-2.5-flash",
        "model": "google-antigravity/gemini-2.5-flash",
        "route": "subscription",
        "prompt_price_per_m": 0.0,
        "comp_price_per_m": 0.0,
    },
    {
        "rung_id": "gemini-3.5-flash-lite",
        "model": "google-antigravity/gemini-3.5-flash-lite",
        "route": "subscription",
        "prompt_price_per_m": 0.0,
        "comp_price_per_m": 0.0,
    },
    {
        "rung_id": "gemini-3.8-flash",
        "model": "google-antigravity/gemini-3.8-flash",
        "route": "subscription",
        "prompt_price_per_m": 0.0,
        "comp_price_per_m": 0.0,
    },
    {
        "rung_id": "ling-3.0-flash",
        "model": "openrouter/inclusionai/ling-3.0-flash",
        "route": "openrouter",
        "prompt_price_per_m": 0.02,
        "comp_price_per_m": 0.06,
    },
    {
        "rung_id": "mistral-nemo",
        "model": "openrouter/mistralai/mistral-nemo",
        "route": "openrouter",
        "prompt_price_per_m": 0.03,
        "comp_price_per_m": 0.03,
    },
    {
        "rung_id": "gpt-oss-120b",
        "model": "openrouter/openai/gpt-oss-120b",
        "route": "openrouter",
        "prompt_price_per_m": 0.02,
        "comp_price_per_m": 0.09,
    },
]


def extract_attestation(session_dir: Path) -> dict[str, Any]:
    served_models: list[str] = []
    fallbacks: list[str] = []
    in_tok = 0
    out_tok = 0
    usd = 0.0
    for p in session_dir.rglob("*.jsonl"):
        if p.name.endswith(".lock.os"):
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            ev_type = ev.get("type")
            if ev_type == "message" and ev.get("message", {}).get("role") == "assistant":
                msg = ev["message"]
                p_name = msg.get("provider")
                m_name = msg.get("model")
                if p_name and m_name:
                    served_models.append(f"{p_name}/{m_name}")
                u = msg.get("usage", {})
                in_tok += u.get("input", 0)
                out_tok += u.get("output", 0)
                cost = u.get("cost", {})
                if isinstance(cost, dict):
                    usd += float(cost.get("total", 0.0) or 0.0)
            elif ev_type == "model_change":
                if ev.get("resolvedModelIsFallback"):
                    fallbacks.append(ev.get("model"))
    return {
        "served_models": sorted(set(served_models)),
        "fallbacks": fallbacks,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
        "usd": usd,
    }


def run_single_case(
    case: dict[str, Any],
    rung: dict[str, Any],
    sessions_root: Path,
    timeout: int = 180,
) -> dict[str, Any]:
    cid = case["case_id"]
    card_path = BENCH_ROOT / case["card_path"]
    card_text = card_path.read_text(encoding="utf-8")
    prompt = f"{RUBRIC}\n<view>\n{card_text}</view>\n"

    case_session_dir = sessions_root / f"{rung['rung_id']}-{cid}"
    case_session_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as pf:
        pf.write(prompt)
        prompt_file = pf.name

    cmd = [
        "omp",
        "-p",
        f"@{prompt_file}",
        "--model",
        rung["model"],
        "--config",
        WORKER_YML,
        "--no-skills",
        "--no-tools",
        "--thinking",
        "low",
        "--session-dir",
        str(case_session_dir),
        "--no-title",
        "--mode",
        "text",
    ]

    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        retcode, stdout, _stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        retcode, stdout, _stderr = -9, "", f"timeout after {timeout}s: {exc}"
    finally:
        with contextlib.suppress(OSError):
            os.remove(prompt_file)
    latency = time.perf_counter() - t0

    verdict_dict = parse_verdict(stdout)
    parse_ok = verdict_dict is not None
    if not parse_ok:
        verdict_dict = {
            "suspicion": 50,
            "verdict": "suspicious",
            "cited_steps": [],
            "causal_grade_effect": "unparsed output",
            "reason": f"unparsed output (retcode {retcode})",
        }

    att = extract_attestation(case_session_dir)

    # Cost fallback calculation from tokens if not present in session
    measured_usd = att["usd"]
    if measured_usd == 0.0 and rung["route"] == "openrouter":
        measured_usd = (
            att["input_tokens"] * rung["prompt_price_per_m"]
            + att["output_tokens"] * rung["comp_price_per_m"]
        ) / 1_000_000.0

    return {
        "case_id": cid,
        "family": case["family"],
        "label": case["label"],
        "split": case["split"],
        "rung_id": rung["rung_id"],
        "requested_model": rung["model"],
        "served_models": att["served_models"],
        "n_fallbacks": len(att["fallbacks"]),
        "parse_ok": parse_ok,
        "suspicion": verdict_dict["suspicion"],
        "verdict": verdict_dict.get("verdict", "unknown"),
        "cited_steps": verdict_dict.get("cited_steps", []),
        "causal_grade_effect": verdict_dict.get("causal_grade_effect", ""),
        "reason": verdict_dict.get("reason", ""),
        "latency_s": round(latency, 3),
        "returncode": retcode,
        "input_tokens": att["input_tokens"],
        "output_tokens": att["output_tokens"],
        "total_tokens": att["total_tokens"],
        "cost_usd": measured_usd,
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cohort",
        type=Path,
        default=Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts/cohort.jsonl"),
    )
    parser.add_argument(
        "--receipts-dir",
        type=Path,
        default=Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/train/raw/D2-READER-LADDER/receipts"),
    )
    parser.add_argument(
        "--d2-results-dir",
        type=Path,
        default=Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/D2"),
    )
    parser.add_argument(
        "--exp-dir",
        type=Path,
        default=Path("research/experiments/on-reader-ladder-20261102"),
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--paid-cap", type=float, default=1.50)
    parser.add_argument("--rung", type=str, default=None, help="Run only this rung_id")
    args = parser.parse_args()

    args.receipts_dir.mkdir(parents=True, exist_ok=True)
    args.d2_results_dir.mkdir(parents=True, exist_ok=True)
    args.exp_dir.mkdir(parents=True, exist_ok=True)

    cases = [json.loads(line) for line in args.cohort.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"Loaded {len(cases)} cases from cohort")

    total_paid_spend = 0.0

    sessions_root = Path(tempfile.gettempdir()) / "d2-ladder-sessions"
    sessions_root.mkdir(parents=True, exist_ok=True)

    active_rungs = [r for r in RUNGS if args.rung is None or r["rung_id"] == args.rung]
    if not active_rungs:
        print(f"No rungs matching {args.rung!r}. Available: {[r['rung_id'] for r in RUNGS]}")
        return

    for rung in active_rungs:
        rung_id = rung["rung_id"]
        out_file = args.receipts_dir / f"{rung_id}.jsonl"
        print("\n==========================================")
        print(f"Starting rung: {rung_id} ({rung['model']})")
        print(f"Route: {rung['route']}")

        # Read already finished cases for resumability
        finished: dict[str, dict[str, Any]] = {}
        if out_file.exists():
            for line in out_file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rec = json.loads(line)
                    finished[rec["case_id"]] = rec
            print(f"Found {len(finished)} already completed cases")

        remaining = [c for c in cases if c["case_id"] not in finished]

        # Cost estimation BEFORE rung
        est_tokens = len(remaining) * 2500
        est_cost = (
            est_tokens * rung["prompt_price_per_m"] + len(remaining) * 150 * rung["comp_price_per_m"]
        ) / 1_000_000.0
        print(f"Estimated cost for remaining {len(remaining)} cases: ${est_cost:.4f}")

        if rung["route"] == "openrouter" and (total_paid_spend + est_cost) > args.paid_cap:
            print(f"SKIPPING {rung_id}: estimated total spend ${total_paid_spend + est_cost:.4f} exceeds subcap ${args.paid_cap:.2f}")
            continue

        rung_spend = sum(rec.get("cost_usd", 0.0) for rec in finished.values())
        new_records = list(finished.values())

        if remaining:
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                futures = {
                    pool.submit(run_single_case, case, rung, sessions_root): case
                    for case in remaining
                }
                for done_count, fut in enumerate(concurrent.futures.as_completed(futures), 1):
                    case = futures[fut]
                    try:
                        record = fut.result()
                    except Exception as err:
                        print(f"Error on case {case['case_id']}: {err}")
                        record = {
                            "case_id": case["case_id"],
                            "family": case["family"],
                            "label": case["label"],
                            "split": case["split"],
                            "rung_id": rung["rung_id"],
                            "requested_model": rung["model"],
                            "served_models": [],
                            "n_fallbacks": 0,
                            "parse_ok": False,
                            "suspicion": 50,
                            "verdict": "error",
                            "cited_steps": [],
                            "causal_grade_effect": "execution exception",
                            "reason": str(err)[:200],
                            "latency_s": 0.0,
                            "returncode": -1,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "total_tokens": 0,
                            "cost_usd": 0.0,
                            "ts": datetime.datetime.now(datetime.UTC).isoformat(),
                        }
                    new_records.append(record)
                    rung_spend += record["cost_usd"]
                    if rung["route"] == "openrouter":
                        total_paid_spend += record["cost_usd"]

                    # Append immediately to out_file for durable streaming progress
                    with open(out_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps(record) + "\n")

                    if done_count % 25 == 0 or done_count == len(remaining):
                        print(f"[{rung_id}] {len(finished) + done_count}/{len(cases)} completed (rung cost: ${rung_spend:.4f})")

        # Synchronize durable copies
        (args.d2_results_dir / f"{rung_id}.jsonl").write_text(out_file.read_text(encoding="utf-8"), encoding="utf-8")
        (args.exp_dir / f"{rung_id}.jsonl").write_text(out_file.read_text(encoding="utf-8"), encoding="utf-8")

        # Log spend if paid
        if rung["route"] == "openrouter" and rung_spend > 0:
            spend_entry = {
                "purpose": "D2-READER-LADDER",
                "rung_id": rung_id,
                "model": rung["model"],
                "route": rung["route"],
                "cases_run": len(new_records),
                "usd": round(rung_spend, 6),
                "ts": datetime.datetime.now(datetime.UTC).isoformat(),
            }
            with open(args.receipts_dir / "spend.jsonl", "a", encoding="utf-8") as sf:
                sf.write(json.dumps(spend_entry) + "\n")
            with open(args.d2_results_dir / "spend.jsonl", "a", encoding="utf-8") as sf:
                sf.write(json.dumps(spend_entry) + "\n")
            try:
                SPEND_LIBRARY.parent.mkdir(parents=True, exist_ok=True)
                with open(SPEND_LIBRARY, "a", encoding="utf-8") as sf:
                    sf.write(json.dumps(spend_entry) + "\n")
            except Exception as e:
                print(f"Warning: could not write to SPEND_LIBRARY: {e}")

        print(f"Finished rung {rung_id}: {len(new_records)} cases, rung cost ${rung_spend:.4f}")

    print("\nAll rungs complete.")


if __name__ == "__main__":
    main()
