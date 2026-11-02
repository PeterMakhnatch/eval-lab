#!/usr/bin/env python3
"""Blind judging runner for A2B-EQUIV across 74 mutants.
Evaluates two independent judge routes:
- Judge 1: google-antigravity/gemini-3.8-flash (medium thinking)
- Judge 2: google-antigravity/gemini-3.1-flash-lite (medium thinking)

Runs at most 4 concurrent processes.
Checks session attestation for served model and 0 fallbacks.
Preserves receipts in both cert/raw/A2B-EQUIV/receipts and Developer/eval-lab-results/2026-11-02/A2B-EQUIV/receipts.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

CERT_RECEIPTS = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/raw/A2B-EQUIV/receipts")
DURABLE_RECEIPTS = Path.home() / "Developer/eval-lab-results/2026-11-02/A2B-EQUIV/receipts"
WORKER_CONFIG = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/worker.yml")

JUDGES = {
    "gemini-3.8-flash": {
        "slug": "gemini-3.8-flash",
        "model_arg": "google-antigravity/gemini-3.8-flash:medium",
        "expected_model": "google-antigravity/gemini-3.8-flash",
    },
    "gemini-3.1-flash-lite": {
        "slug": "gemini-3.1-flash-lite",
        "model_arg": "google-antigravity/gemini-3.1-flash-lite:medium",
        "expected_model": "google-antigravity/gemini-3.1-flash-lite",
    },
}


def make_prompt(item: dict[str, Any]) -> str:
    inst = item["instruction"]
    if len(inst) > 3500:
        inst = inst[:3500] + "\n... [instruction truncated] ..."

    context_code = item["context_code"]
    diff_text = item["diff_text"]
    rel_path = item["context_file"]

    return f"""You are a senior compiler and software verification engineer evaluating whether a proposed code mutation changes program behavior or is functionally equivalent.

### Problem Context / Task Specification:
{inst}

### Reference Implementation (Post-Patch Context):
File: {rel_path}
```python
{context_code}
```

### Proposed Mutation (Unified Diff):
```diff
{diff_text}
```

### Evaluation Rubric:
Analyze whether applying this diff to the reference implementation produces any observable semantic difference in program execution.
Observable behavior includes: return values, raised exceptions (types or messages), global or object state modifications, I/O, or termination.

Choose exactly one verdict:
- "behavior_changing": If there exists at least one legal, reachable, or plausible input or invocation sequence (consistent with the task description and code context) for which the mutant produces different observable behavior than the reference code.
- "equivalent": If the mutant produces identical observable behavior on ALL inputs and execution paths (e.g. unreachable/dead code, redundant defensive check already guaranteed by caller/type invariants, identical computation expressed differently, unused variable/side-effect-free statement removal).
- "uncertain": Only if the provided context is fundamentally insufficient to determine equivalence (e.g. unknown external library semantics).

### Required Output Format:
Respond ONLY with a JSON object in the following format (no extra markdown outside the json block):
{{
  "verdict": "behavior_changing" | "equivalent" | "uncertain",
  "reason": "<One clear sentence naming the specific input/condition distinguishing behavior, explaining why it is strictly equivalent, or stating why it cannot be determined.>"
}}
"""


def extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    # Try direct parse
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "verdict" in data:
            return data
    except Exception:
        pass

    # Try regex match for ```json ... ``` or { ... }
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict) and "verdict" in data:
                return data
        except Exception:
            pass

    m2 = re.search(r"\{[^{}]*\"verdict\"[^{}]*\}", text, re.DOTALL)
    if m2:
        try:
            data = json.loads(m2.group(0))
            if isinstance(data, dict) and "verdict" in data:
                return data
        except Exception:
            pass

    return None


def inspect_session(session_path: Path, expected_model: str) -> dict[str, Any]:
    attestation = {
        "session_file": str(session_path),
        "requested_model": expected_model,
        "served_model": None,
        "is_fallback": None,
        "attestation_ok": False,
    }
    if not session_path.exists():
        return attestation

    for line in session_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") == "model_change":
            attestation["served_model"] = entry.get("model")
            attestation["is_fallback"] = entry.get("resolvedModelIsFallback")
            if (
                attestation["served_model"] == expected_model
                and attestation["is_fallback"] is False
            ):
                attestation["attestation_ok"] = True

    return attestation


def evaluate_item(
    item: dict[str, Any],
    judge_info: dict[str, Any],
    worker_idx: int,
    receipt_dirs: list[Path],
) -> dict[str, Any]:
    opaque_id = item["opaque_id"]
    slug = judge_info["slug"]
    expected_model = judge_info["expected_model"]
    model_arg = judge_info["model_arg"]

    # Check if receipt already exists in first receipt dir
    cached_file = receipt_dirs[0] / slug / f"{opaque_id}.json"
    if cached_file.exists():
        try:
            data = json.loads(cached_file.read_text())
            if data.get("verdict") in ("behavior_changing", "equivalent", "uncertain") and data.get("attestation", {}).get("attestation_ok"):
                return data
        except Exception:
            pass

    worker_dir = Path(f"/tmp/omp-worker-{worker_idx}")
    worker_dir.mkdir(parents=True, exist_ok=True)
    agent_db_source = Path.home() / ".omp/agent/agent.db"
    shutil.copy2(agent_db_source, worker_dir / "agent.db")

    sessions_root = worker_dir / "sessions"
    before_sessions = set(sessions_root.glob("**/*.jsonl")) if sessions_root.exists() else set()

    prompt = make_prompt(item)
    cmd = [
        "omp", "-p",
        "--model", model_arg,
        "--config", str(WORKER_CONFIG),
        "--no-skills", "--no-tools",
        prompt,
    ]

    env = dict(os.environ)
    env["PI_CODING_AGENT_DIR"] = str(worker_dir)
    env["PATH"] = f"/usr/local/bin:/usr/bin:/bin:{Path.home() / '.local/bin'}"

    last_error = None
    for _attempt in range(1, 4):
        try:
            res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
            stdout = res.stdout.strip()

            # Find new session file
            after_sessions = set(sessions_root.glob("**/*.jsonl")) if sessions_root.exists() else set()
            new_sessions = after_sessions - before_sessions
            latest_session = None
            if new_sessions:
                latest_session = max(new_sessions, key=lambda p: p.stat().st_mtime)

            attestation = (
                inspect_session(latest_session, expected_model)
                if latest_session
                else {
                    "session_file": None,
                    "requested_model": expected_model,
                    "served_model": None,
                    "is_fallback": None,
                    "attestation_ok": False,
                }
            )

            parsed = extract_json(stdout)
            if not parsed or parsed.get("verdict") not in ("behavior_changing", "equivalent", "uncertain"):
                last_error = f"Invalid JSON or verdict in stdout: {stdout[:200]}"
                continue

            record = {
                "opaque_id": opaque_id,
                "judge": slug,
                "verdict": parsed["verdict"],
                "reason": parsed.get("reason", "").strip(),
                "raw_output": stdout,
                "attestation": attestation,
            }

            for r_dir in receipt_dirs:
                out_path = r_dir / slug / f"{opaque_id}.json"
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(json.dumps(record, indent=2))

            return record
        except subprocess.TimeoutExpired:
            last_error = "Execution timed out (120s)"
        except Exception as exc:
            last_error = str(exc)

    fallback_record = {
        "opaque_id": opaque_id,
        "judge": slug,
        "verdict": "uncertain",
        "reason": f"Judge execution failed after 3 attempts: {last_error}",
        "raw_output": "",
        "attestation": {
            "session_file": None,
            "requested_model": expected_model,
            "served_model": None,
            "is_fallback": None,
            "attestation_ok": False,
        },
    }
    for r_dir in receipt_dirs:
        out_path = r_dir / slug / f"{opaque_id}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(fallback_record, indent=2))
    return fallback_record


def run_judge_fleet(
    judge_name: str,
    blind_items: list[dict[str, Any]],
    receipt_dirs: list[Path],
    concurrency: int = 4,
) -> list[dict[str, Any]]:
    judge_info = JUDGES[judge_name]
    slug = judge_info["slug"]
    print(f"\n=== Starting Judge: {slug} ({len(blind_items)} items, concurrency {concurrency}) ===", flush=True)

    results: list[dict[str, Any]] = [None] * len(blind_items)  # type: ignore

    def worker_fn(idx_and_item: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        idx, item = idx_and_item
        worker_slot = idx % concurrency
        res = evaluate_item(item, judge_info, worker_slot, receipt_dirs)
        print(f"  [{idx + 1}/{len(blind_items)}] {slug} on {item['opaque_id']}: {res['verdict']}", flush=True)
        return idx, res

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        for idx, res in executor.map(worker_fn, enumerate(blind_items)):
            results[idx] = res

    # Summary attestation check
    served_ok = sum(1 for r in results if r["attestation"]["attestation_ok"])
    fallbacks = sum(1 for r in results if r["attestation"]["is_fallback"] is True)
    print(f"Finished {slug}: {served_ok}/{len(results)} attested OK, {fallbacks} fallbacks.", flush=True)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run blind LLM judges")
    parser.add_argument("--judge", choices=list(JUDGES.keys()) + ["all"], default="all")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    blind_items_path = CERT_RECEIPTS / "blind_items.json"
    if not blind_items_path.exists():
        print(f"Error: {blind_items_path} not found. Run prepare_cohort.py first.", file=sys.stderr)
        sys.exit(1)

    blind_items = json.loads(blind_items_path.read_text())
    receipt_dirs = [CERT_RECEIPTS, DURABLE_RECEIPTS]

    judges_to_run = list(JUDGES.keys()) if args.judge == "all" else [args.judge]

    all_judgments: dict[str, list[dict[str, Any]]] = {}
    for j_name in judges_to_run:
        results = run_judge_fleet(j_name, blind_items, receipt_dirs, concurrency=args.concurrency)
        all_judgments[j_name] = results

        # Save summary per judge
        for r_dir in receipt_dirs:
            summary_file = r_dir / f"judgments_{j_name}.json"
            summary_file.write_text(json.dumps(results, indent=2))

    print("\nAll requested judging runs completed successfully.")


if __name__ == "__main__":
    main()
