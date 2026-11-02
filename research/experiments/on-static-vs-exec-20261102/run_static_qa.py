"""A6-STATIC-VS-EXEC: Static task QA vs executable grader weakness.

Evaluates whether PackageAnatomy structural metrics and a zero-tool LLM audit (Gemini 3.8 Flash)
predict executable mutant survival (A2 ground truth) on 28 DEV tasks.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

# Ensure evallab is importable
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

BRIDGE = Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight")
CENSUS_PATH = BRIDGE / "gates/census.json"
WEAK_TASKS_PATH = BRIDGE / "gates/weak-tasks.json"
WORKER_YML = BRIDGE / "cert/worker.yml"
STATS_PY_PATH = BRIDGE / "cert/stats.py"
RAW_DIR = BRIDGE / "cert/raw/A6-STATIC-VS-EXEC"
RECEIPTS_DIR = RAW_DIR / "receipts"
DURABLE_RESULTS_DIR = Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A6")

MODEL_ROUTE = "google-antigravity/gemini-3.8-flash"
MAX_CONCURRENT_OMP = 4

SYSTEM_PROMPT = (
    "You are an expert software testing and evaluation auditor assessing test suite rigor.\n"
    "Evaluate whether the provided tests are weak, meaning that an incorrect, incomplete, "
    "or wrong-but-plausible implementation would pass them.\n"
    "You must respond with a single valid JSON object and nothing else."
)

VARIANT_S_TEMPLATE = """# Task Instruction
{instruction}

# Task Evaluation Tests
{tests}

# Instructions
Assess the rigor and completeness of the test suite against the task specification.
Specifically, determine whether the test suite has gaps or weak checks such that a wrong-but-plausible implementation (e.g. an incomplete implementation, wrong edge cases, returning hardcoded or trivial values, or missing key requirements) would still pass the tests.

Rate the weakness of the test suite on a scale from 0 to 100:
- 0 = Extremely rigorous test suite. Every requirement, edge case, and boundary is thoroughly verified with strict assertions. A wrong or incomplete implementation cannot pass.
- 100 = Extremely weak test suite. Trivial or completely incorrect implementations easily pass (e.g., tests only check types, existence, or don't assert key behaviors).

Also identify the 3 weakest assertions in the test suite that are most vulnerable to being bypassed or that leave critical behavior unverified.

Respond strictly in this JSON format:
{{
  "weakness_score": <int between 0 and 100>,
  "rationale": "<2-3 sentence explanation of test suite weaknesses or rigor>",
  "weakest_assertions": [
    {{"assertion": "<code snippet or exact description of assertion 1>", "reason": "<why this check is weak or bypassable>"}},
    {{"assertion": "<code snippet or exact description of assertion 2>", "reason": "<why this check is weak or bypassable>"}},
    {{"assertion": "<code snippet or exact description of assertion 3>", "reason": "<why this check is weak or bypassable>"}}
  ]
}}
"""

VARIANT_R_TEMPLATE = """# Task Instruction
{instruction}

# Reference Patch (Gold Solution)
{reference_patch}

# Task Evaluation Tests
{tests}

# Instructions
Assess the rigor and completeness of the test suite against the task specification and the reference solution.
Inspect which branches, functions, and logic modified in the reference solution are actually exercised and asserted by the tests.
Determine whether the test suite has gaps or weak checks such that a wrong-but-plausible implementation (e.g., an implementation that mutates, omits, or inverts parts of the logic) would still pass the tests.

Rate the weakness of the test suite on a scale from 0 to 100:
- 0 = Extremely rigorous test suite. Every changed branch and requirement is strictly asserted against incorrect behaviors.
- 100 = Extremely weak test suite. Mutated or partially wrong implementations easily pass because tests fail to assert changed behaviors or only perform shallow checks.

Also identify the 3 weakest assertions in the test suite that are most vulnerable to being bypassed or that leave critical behavior unverified.

Respond strictly in this JSON format:
{{
  "weakness_score": <int between 0 and 100>,
  "rationale": "<2-3 sentence explanation of test suite weaknesses or rigor>",
  "weakest_assertions": [
    {{"assertion": "<code snippet or exact description of assertion 1>", "reason": "<why this check is weak or bypassable>"}},
    {{"assertion": "<code snippet or exact description of assertion 2>", "reason": "<why this check is weak or bypassable>"}},
    {{"assertion": "<code snippet or exact description of assertion 3>", "reason": "<why this check is weak or bypassable>"}}
  ]
}}
"""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_cohort_metadata() -> list[dict[str, Any]]:
    """Loads metadata for tasks audited by A2 without reading labels."""
    with open(CENSUS_PATH, encoding="utf-8") as f:
        census = json.load(f)
    cohort = []
    for t in census["tasks"]:
        if t.get("control_status") == "passed":
            cohort.append({
                "task_id": t["task_id"],
                "package_path": t["package_path"],
                "reference_patch_path": t.get("reference_patch_path"),
            })
    return cohort


def extract_package_texts(pkg_path: str, ref_patch_path: str | None) -> dict[str, str]:
    pkg = Path(pkg_path)
    inst_file = pkg / "instruction.md"
    instruction = inst_file.read_text(encoding="utf-8", errors="replace") if inst_file.is_file() else ""

    test_parts = []
    tests_dir = pkg / "tests"
    if tests_dir.is_dir():
        patch_file = tests_dir / "test.patch"
        if patch_file.is_file():
            test_parts.append(f"--- tests/test.patch ---\n{patch_file.read_text(encoding='utf-8', errors='replace')}")
        for child in sorted(tests_dir.iterdir()):
            if child.is_file() and child.name not in ("test.patch", "test.sh", "test_command.sh"):
                test_parts.append(f"--- {child.name} ---\n{child.read_text(encoding='utf-8', errors='replace')}")
        # Include test_command.sh if short
        cmd_file = tests_dir / "test_command.sh"
        if cmd_file.is_file():
            test_parts.append(f"--- tests/test_command.sh ---\n{cmd_file.read_text(encoding='utf-8', errors='replace')}")

    tests = "\n\n".join(test_parts)

    ref_patch = ""
    if ref_patch_path and os.path.exists(ref_patch_path):
        ref_patch = Path(ref_patch_path).read_text(encoding="utf-8", errors="replace")

    return {
        "instruction": instruction,
        "tests": tests,
        "reference_patch": ref_patch,
    }


def compute_anatomy_features(pkg_path: str, ref_patch_path: str | None) -> dict[str, Any]:
    from evallab.package_anatomy import analyze_package

    card = analyze_package(pkg_path, reference_patch=ref_patch_path)
    hyps = card.get("vulnerability_hypotheses", [])
    weak_hyps = [h for h in hyps if h.get("mechanism_class") == "weak_check"]

    counts = card.get("test_suite_rigor", {}).get("counts", {})
    ratios = card.get("test_suite_rigor", {}).get("ratios", {})

    n_tests = counts.get("tests", 0)
    assertions = counts.get("assertions", 0)
    surface_assertions = counts.get("surface_assertions", 0)
    literal_eq_assertions = counts.get("literal_eq_assertions", 0)

    assertions_per_test = ratios.get("assertions_per_test")
    if assertions_per_test is None:
        assertions_per_test = round(assertions / max(1, n_tests), 2)

    surface_share = ratios.get("surface_share")
    if surface_share is None:
        surface_share = round(surface_assertions / max(1, assertions), 3)

    literal_eq_share = ratios.get("literal_eq_share")
    if literal_eq_share is None:
        literal_eq_share = round(literal_eq_assertions / max(1, assertions), 3)

    return {
        "anatomy_weak_hypotheses_count": len(weak_hyps),
        "anatomy_max_weak_score": max((h.get("score", 0) for h in weak_hyps), default=0),
        "anatomy_sum_weak_score": sum(h.get("score", 0) for h in weak_hyps),
        "n_tests": n_tests,
        "inv_test_count": round(1.0 / (1 + n_tests), 4),
        "assertions": assertions,
        "assertions_per_test": assertions_per_test,
        "inv_assertions_per_test": round(1.0 / (1 + assertions_per_test), 4),
        "surface_assertions": surface_assertions,
        "surface_assertion_share": surface_share,
        "literal_eq_assertions": literal_eq_assertions,
        "value_assertion_share": literal_eq_share,
        "inv_value_assertion_share": round(1.0 - literal_eq_share, 4),
        "weak_hypotheses_details": [
            {"id": h.get("id"), "score": h.get("score"), "title": h.get("title"), "confidence": h.get("confidence")}
            for h in weak_hyps
        ],
    }


def parse_llm_json(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError(f"No JSON object found in response: {raw_text[:200]}")
    parsed = json.loads(text[start:end + 1])
    score = int(parsed.get("weakness_score", 50))
    score = max(0, min(100, score))
    return {
        "weakness_score": score,
        "rationale": parsed.get("rationale", ""),
        "weakest_assertions": parsed.get("weakest_assertions", []),
    }


def check_attestation(session_dir: Path, expected_model: str) -> dict[str, Any]:
    served = []
    fallbacks = []
    providers = []
    for p in session_dir.rglob("*.jsonl"):
        for line in p.read_text(errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except Exception:
                continue
            kind = entry.get("type")
            if kind == "model_change":
                served.append(str(entry.get("model")))
                if entry.get("resolvedModelIsFallback"):
                    fallbacks.append(entry)
            elif kind in ("model_fallback", "fallback"):
                fallbacks.append(entry)
            msg = entry.get("message")
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                providers.append(f"{msg.get('provider')}/{msg.get('model')}")

    served_set = sorted(set(served))
    provider_set = sorted(set(providers))
    expected_pure = expected_model.split(":")[0]

    attested = (
        bool(provider_set)
        and all(p == expected_pure for p in provider_set)
        and len(fallbacks) == 0
        and all(s.split(":")[0] == expected_pure for s in served_set)
    )
    return {
        "expected": expected_model,
        "served_models": served_set,
        "assistant_provider_models": provider_set,
        "fallback_count": len(fallbacks),
        "attested": attested,
    }


def extract_usage(session_dir: Path) -> dict[str, Any]:
    totals = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "cost_usd": 0.0}
    for p in session_dir.rglob("*.jsonl"):
        for line in p.read_text(errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except Exception:
                continue
            msg = entry.get("message")
            u = msg.get("usage") if isinstance(msg, dict) else None
            if not isinstance(u, dict):
                continue
            totals["input"] += int(u.get("input") or 0)
            totals["output"] += int(u.get("output") or 0)
            totals["cache_read"] += int(u.get("cacheRead") or 0)
            totals["cache_write"] += int(u.get("cacheWrite") or 0)
            cost = u.get("cost")
            if isinstance(cost, dict):
                cost = cost.get("total")
            if isinstance(cost, (int, float)) and not isinstance(cost, bool):
                totals["cost_usd"] += float(cost)
    totals["cost_usd"] = round(totals["cost_usd"], 4)
    return totals


def run_omp_audit(
    task_id: str,
    variant: str,
    prompt_text: str,
    session_dir: Path,
    timeout: int = 180,
) -> dict[str, Any]:
    session_dir.mkdir(parents=True, exist_ok=True)
    prompt_file = session_dir / "prompt.txt"
    prompt_file.write_text(prompt_text, encoding="utf-8")

    argv = [
        "omp", "-p",
        "--config", str(WORKER_YML),
        "--model", MODEL_ROUTE,
        "--thinking", "medium",
        "--no-tools",
        "--no-skills",
        "--no-rules",
        "--no-extensions",
        "--no-lsp",
        "--no-title",
        "--no-prewalk",
        "--no-pty",
        "--mode", "text",
        "--session-dir", str(session_dir),
        "--system-prompt", SYSTEM_PROMPT,
        f"@{prompt_file}",
    ]

    t0 = time.time()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            cwd=str(session_dir),
        )
        wall_s = round(time.time() - t0, 2)
        raw_stdout = proc.stdout
        stderr_sample = proc.stderr[-2000:]
        rc = proc.returncode
    except subprocess.TimeoutExpired as exc:
        wall_s = round(time.time() - t0, 2)
        raw_stdout = ""
        stderr_sample = f"timeout expired: {exc}"
        rc = -9

    (session_dir / "stdout.txt").write_text(raw_stdout, encoding="utf-8")
    (session_dir / "stderr.txt").write_text(stderr_sample, encoding="utf-8")

    att = check_attestation(session_dir, MODEL_ROUTE)
    usg = extract_usage(session_dir)

    parsed = {}
    parse_error = None
    if rc == 0:
        try:
            parsed = parse_llm_json(raw_stdout)
        except Exception as exc:
            parse_error = str(exc)
    else:
        parse_error = f"Non-zero return code {rc}"

    return {
        "task_id": task_id,
        "variant": variant,
        "returncode": rc,
        "wall_s": wall_s,
        "prompt_sha256": sha256_text(prompt_text),
        "prompt_chars": len(prompt_text),
        "parsed": parsed,
        "parse_error": parse_error,
        "attestation": att,
        "usage": usg,
    }


def collect_all_predictions(cohort: list[dict[str, Any]], work_dir: Path) -> list[dict[str, Any]]:
    print(f"Step 1: Computing PackageAnatomy features for {len(cohort)} tasks...")
    anatomy_results = {}
    task_texts = {}
    for item in cohort:
        tid = item["task_id"]
        pkg = item["package_path"]
        ref = item["reference_patch_path"]
        anatomy_results[tid] = compute_anatomy_features(pkg, ref)
        task_texts[tid] = extract_package_texts(pkg, ref)

    print(f"Step 2: Preparing prompts and running LLM audits (concurrency <= {MAX_CONCURRENT_OMP})...")
    llm_jobs = []
    for item in cohort:
        tid = item["task_id"]
        texts = task_texts[tid]

        # Variant S
        prompt_s = VARIANT_S_TEMPLATE.format(
            instruction=texts["instruction"],
            tests=texts["tests"],
        )
        s_dir = work_dir / "sessions" / tid / "variant_s"
        llm_jobs.append((tid, "S", prompt_s, s_dir))

        # Variant R
        prompt_r = VARIANT_R_TEMPLATE.format(
            instruction=texts["instruction"],
            reference_patch=texts["reference_patch"],
            tests=texts["tests"],
        )
        r_dir = work_dir / "sessions" / tid / "variant_r"
        llm_jobs.append((tid, "R", prompt_r, r_dir))

    llm_results: dict[str, dict[str, Any]] = {item["task_id"]: {} for item in cohort}

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_OMP) as executor:
        future_map = {
            executor.submit(run_omp_audit, tid, var, prompt, sdir): (tid, var)
            for tid, var, prompt, sdir in llm_jobs
        }
        for future in as_completed(future_map):
            tid, var = future_map[future]
            try:
                res = future.result()
                llm_results[tid][var] = res
                status = "OK" if res["returncode"] == 0 and not res["parse_error"] else f"ERR: {res['parse_error']}"
                score = res["parsed"].get("weakness_score", "N/A")
                print(f"[{var}] {tid:<35} score={score:>3} wall={res['wall_s']:>4}s status={status}")
            except Exception as exc:
                print(f"[{var}] {tid:<35} EXCEPTION: {exc}")
                llm_results[tid][var] = {"returncode": -1, "parse_error": str(exc), "parsed": {}}

    print("Step 3: Compiling static prediction rows (label-free)...")
    predictions = []
    for item in cohort:
        tid = item["task_id"]
        anat = anatomy_results[tid]
        res_s = llm_results[tid].get("S", {})
        res_r = llm_results[tid].get("R", {})

        score_s = res_s.get("parsed", {}).get("weakness_score", 50.0)
        score_r = res_r.get("parsed", {}).get("weakness_score", 50.0)
        anat_max_weak = anat["anatomy_max_weak_score"]

        # Pre-registered fixed composites
        composite_r = round(0.5 * score_r + 0.5 * anat_max_weak, 2)
        composite_s = round(0.5 * score_s + 0.5 * anat_max_weak, 2)

        predictions.append({
            "task_id": tid,
            "package_path": item["package_path"],
            "reference_patch_path": item["reference_patch_path"],
            "instruction_length": len(task_texts[tid]["instruction"]),
            "tests_length": len(task_texts[tid]["tests"]),
            "reference_patch_length": len(task_texts[tid]["reference_patch"]),
            "anatomy": anat,
            "llm_variant_s": {
                "weakness_score": score_s,
                "rationale": res_s.get("parsed", {}).get("rationale", ""),
                "weakest_assertions": res_s.get("parsed", {}).get("weakest_assertions", []),
                "wall_s": res_s.get("wall_s"),
                "usage": res_s.get("usage", {}),
                "attestation": res_s.get("attestation", {}),
            },
            "llm_variant_r": {
                "weakness_score": score_r,
                "rationale": res_r.get("parsed", {}).get("rationale", ""),
                "weakest_assertions": res_r.get("parsed", {}).get("weakest_assertions", []),
                "wall_s": res_r.get("wall_s"),
                "usage": res_r.get("usage", {}),
                "attestation": res_r.get("attestation", {}),
            },
            "scores": {
                "anatomy_weak_hypotheses_count": anat["anatomy_weak_hypotheses_count"],
                "anatomy_max_weak_score": anat_max_weak,
                "anatomy_sum_weak_score": anat["anatomy_sum_weak_score"],
                "inv_test_count": anat["inv_test_count"],
                "inv_assertions_per_test": anat["inv_assertions_per_test"],
                "surface_assertion_share": anat["surface_assertion_share"],
                "inv_value_assertion_share": anat["inv_value_assertion_share"],
                "llm_variant_s_score": score_s,
                "llm_variant_r_score": score_r,
                "composite_static_r": composite_r,
                "composite_static_s": composite_s,
            },
        })

    return predictions


def bootstrap_auroc(pos: list[float], neg: list[float], n_boot: int = 10000, seed: int = 42) -> tuple[float, float]:
    rng = random.Random(seed)
    n_p, n_n = len(pos), len(neg)
    if n_p == 0 or n_n == 0:
        return (float("nan"), float("nan"))
    boot_vals = []
    for _ in range(n_boot):
        b_pos = [pos[rng.randrange(n_p)] for _ in range(n_p)]
        b_neg = [neg[rng.randrange(n_n)] for _ in range(n_n)]
        wins = sum(1.0 if p > n else (0.5 if p == n else 0.0) for p in b_pos for n in b_neg)
        boot_vals.append(wins / (n_p * n_n))
    boot_vals.sort()
    lo = boot_vals[int(0.025 * n_boot)]
    hi = boot_vals[int(0.975 * n_boot)]
    return (round(lo, 4), round(hi, 4))


def precision_at_k(items: list[dict[str, Any]], score_key: str, k: int) -> float:
    sorted_items = sorted(items, key=lambda x: x["scores"][score_key], reverse=True)
    top_k = sorted_items[:k]
    if not top_k:
        return 0.0
    true_pos = sum(1 for x in top_k if x["is_weak"])
    return round(true_pos / len(top_k), 4)


def simulate_loo_triage(
    items: list[dict[str, Any]],
    score_key: str,
    target_skip_frac: float = 0.30,
    max_train_misses: int = 1,
) -> dict[str, Any]:
    """Leave-one-out cross-validation of threshold triage selection.
    
    Rule:
    On train set (N-1), choose lowest threshold T that skips >= target_skip_frac
    while missing <= max_train_misses weak tasks.
    """
    n = len(items)
    fold_decisions = []

    for i in range(n):
        train = [items[j] for j in range(n) if j != i]
        held_out = items[i]

        train_scores = sorted({x["scores"][score_key] for x in train})
        # Threshold options: strictly less than T is skipped
        # Consider possible cutoffs
        candidates = []
        for t in train_scores:
            skipped = [x for x in train if x["scores"][score_key] < t]
            skip_frac = len(skipped) / len(train)
            misses = sum(1 for x in skipped if x["is_weak"])
            candidates.append({"threshold": t, "skip_frac": skip_frac, "misses": misses})

        # Valid candidates: skip_frac >= target_skip_frac and misses <= max_train_misses
        valid = [c for c in candidates if c["skip_frac"] >= target_skip_frac and c["misses"] <= max_train_misses]

        if valid:
            # Pick lowest threshold among valid (most conservative for skipping, or lowest score cut)
            # Actually, lower threshold means fewer skipped. The candidate with skip_frac >= target_skip_frac
            # that minimizes threshold will be the lowest score threshold that meets the target.
            chosen = min(valid, key=lambda c: c["threshold"])
            chosen_threshold = chosen["threshold"]
            policy = "valid_criteria"
        else:
            # Fallback: minimize misses subject to highest skip frac, or min misses
            # Fallback 1: minimal misses among those with skip_frac >= target_skip_frac
            skip_eligible = [c for c in candidates if c["skip_frac"] >= target_skip_frac]
            if skip_eligible:
                chosen = min(skip_eligible, key=lambda c: (c["misses"], c["threshold"]))
                chosen_threshold = chosen["threshold"]
                policy = "fallback_min_misses_at_target_skip"
            else:
                # Fallback 2: zero threshold (no skip)
                chosen_threshold = min(train_scores)
                policy = "fallback_no_skip"

        # Apply to held_out
        held_out_score = held_out["scores"][score_key]
        is_skipped = bool(held_out_score < chosen_threshold)
        is_miss = bool(is_skipped and held_out["is_weak"])

        fold_decisions.append({
            "task_id": held_out["task_id"],
            "score": held_out_score,
            "threshold": chosen_threshold,
            "policy": policy,
            "skipped": is_skipped,
            "is_weak": held_out["is_weak"],
            "miss": is_miss,
            "modal_cost_usd": held_out.get("total_cost_usd", 0.0),
        })

    n_skipped = sum(1 for d in fold_decisions if d["skipped"])
    n_missed = sum(1 for d in fold_decisions if d["miss"])
    total_cost = sum(d["modal_cost_usd"] for d in fold_decisions)
    saved_cost = sum(d["modal_cost_usd"] for d in fold_decisions if d["skipped"])

    return {
        "n_total": n,
        "n_skipped": n_skipped,
        "skip_rate": round(n_skipped / n, 4),
        "n_missed": n_missed,
        "miss_rate": round(n_missed / max(1, sum(1 for d in fold_decisions if d["is_weak"])), 4),
        "total_modal_cost_usd": round(total_cost, 2),
        "saved_modal_cost_usd": round(saved_cost, 2),
        "saved_cost_pct": round(100.0 * saved_cost / max(1e-6, total_cost), 1),
        "fold_decisions": fold_decisions,
    }


def evaluate_joined_data(predictions: list[dict[str, Any]]) -> dict[str, Any]:
    """Loads A2 labels and joins with predictions to compute evaluation metrics."""
    with open(CENSUS_PATH, encoding="utf-8") as f:
        census = json.load(f)

    # Map task_id to ground truth labels
    census_map = {t["task_id"]: t for t in census["tasks"]}

    joined = []
    for p in predictions:
        tid = p["task_id"]
        ct = census_map.get(tid, {})
        survivors = ct.get("survivors_count", 0)
        is_weak = ct.get("weak_original", False) or (survivors > 0)
        joined.append({
            **p,
            "is_weak": bool(is_weak),
            "survivors_count": survivors,
            "surviving_operators": ct.get("surviving_operators", []),
            "mutants": ct.get("mutants", []),
            "total_cost_usd": ct.get("total_cost_usd", 0.0),
            "total_wall_s": ct.get("total_wall_s", 0.0),
        })

    n_pos = sum(1 for j in joined if j["is_weak"])
    n_neg = len(joined) - n_pos

    print(f"\nEvaluating Joined Data: Total={len(joined)}, Weak (Pos)={n_pos}, Robust (Neg)={n_neg}")

    # Import stats module
    import importlib.util
    spec = importlib.util.spec_from_file_location("stats", STATS_PY_PATH)
    stats_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stats_mod)

    score_keys = [
        "anatomy_weak_hypotheses_count",
        "anatomy_max_weak_score",
        "anatomy_sum_weak_score",
        "inv_test_count",
        "inv_assertions_per_test",
        "surface_assertion_share",
        "inv_value_assertion_share",
        "llm_variant_s_score",
        "llm_variant_r_score",
        "composite_static_r",
        "composite_static_s",
    ]

    predictor_metrics = {}
    for sk in score_keys:
        pos_scores = [j["scores"][sk] for j in joined if j["is_weak"]]
        neg_scores = [j["scores"][sk] for j in joined if not j["is_weak"]]

        auroc_val = round(stats_mod.auroc(pos_scores, neg_scores), 4)
        auroc_ci = bootstrap_auroc(pos_scores, neg_scores)

        p_at_3 = precision_at_k(joined, sk, 3)
        p_at_5 = precision_at_k(joined, sk, 5)
        p_at_10 = precision_at_k(joined, sk, 10)

        triage_sim = simulate_loo_triage(joined, sk, target_skip_frac=0.30, max_train_misses=1)

        predictor_metrics[sk] = {
            "auroc": auroc_val,
            "auroc_95ci": auroc_ci,
            "beats_chance": bool(auroc_ci[0] > 0.50),
            "precision_at_3": p_at_3,
            "precision_at_5": p_at_5,
            "precision_at_10": p_at_10,
            "triage": triage_sim,
        }

    # Paired McNemar comparison between Variant S and Variant R triage skips
    triage_s_skips = [d["skipped"] for d in predictor_metrics["llm_variant_s_score"]["triage"]["fold_decisions"]]
    triage_r_skips = [d["skipped"] for d in predictor_metrics["llm_variant_r_score"]["triage"]["fold_decisions"]]
    paired_s_vs_r = stats_mod.paired(triage_s_skips, triage_r_skips)

    # Mechanism exploration
    # Check if named weak assertions match surviving mutant files or functions
    mechanism_findings = []
    for j in joined:
        if not j["is_weak"]:
            continue
        named_assertions_r = j["llm_variant_r"].get("weakest_assertions", [])
        surviving_mutants = [m for m in j["mutants"] if (m.get("reward") or 0.0) > 0.0]
        mutant_targets = [f"{m.get('path', '')}:{m.get('line', '')} ({m.get('operator', '')})" for m in surviving_mutants]
        mutant_files = {m.get("path", "") for m in surviving_mutants if m.get("path")}

        # Check if any named assertion mentions the mutant file stem or symbols
        matches = []
        for wa in named_assertions_r:
            txt = (wa.get("assertion", "") + " " + wa.get("reason", "")).lower()
            for mf in mutant_files:
                stem = Path(mf).stem.lower()
                if stem and len(stem) >= 4 and stem in txt:
                    matches.append({"assertion": wa.get("assertion"), "target_file": mf})

        mechanism_findings.append({
            "task_id": j["task_id"],
            "survivors_count": len(surviving_mutants),
            "mutant_targets": mutant_targets[:5],
            "named_assertions": named_assertions_r,
            "matched_assertions": matches,
            "match_found": bool(matches),
        })
    # Length confounding check
    import math
    def rank_list(vals: list[float]) -> list[float]:
        sorted_vals = sorted(enumerate(vals), key=lambda x: x[1])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(vals):
            j = i
            while j < len(vals) - 1 and sorted_vals[j + 1][1] == sorted_vals[j][1]:
                j += 1
            avg_rank = (i + j + 2) / 2.0
            for k in range(i, j + 1):
                ranks[sorted_vals[k][0]] = avg_rank
            i = j + 1
        return ranks

    def spearman_corr(x: list[float], y: list[float]) -> float:
        rx = rank_list(x)
        ry = rank_list(y)
        mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
        num = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
        den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
        return round(num / den, 4) if den > 0 else 0.0

    inst_lens = [j["instruction_length"] for j in joined]
    test_lens = [j["tests_length"] for j in joined]
    ref_lens = [j["reference_patch_length"] for j in joined]

    length_confounding = {}
    for sk in ("llm_variant_s_score", "llm_variant_r_score", "anatomy_max_weak_score", "composite_static_r"):
        s_vals = [j["scores"][sk] for j in joined]
        length_confounding[sk] = {
            "spearman_inst_len": spearman_corr(s_vals, inst_lens),
            "spearman_test_len": spearman_corr(s_vals, test_lens),
            "spearman_ref_len": spearman_corr(s_vals, ref_lens),
        }

    return {
        "cohort_size": len(joined),
        "weak_count": n_pos,
        "robust_count": n_neg,
        "weak_rate_fmt": stats_mod.fmt(n_pos, len(joined)),
        "predictor_metrics": predictor_metrics,
        "paired_triage_skips_s_vs_r": paired_s_vs_r,
        "mechanism_findings": mechanism_findings,
        "length_confounding": length_confounding,
        "joined_tasks": joined,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-llm", action="store_true", help="Use existing LLM outputs if available")
    args = parser.parse_args()

    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    DURABLE_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    cohort = load_cohort_metadata()
    print(f"Cohort loaded: {len(cohort)} tasks.")

    static_pred_file = RECEIPTS_DIR / "static_predictions.json"
    durable_pred_file = DURABLE_RESULTS_DIR / "static_predictions.json"

    if args.skip_llm and static_pred_file.is_file():
        print(f"Loading cached predictions from {static_pred_file}...")
        predictions = json.loads(static_pred_file.read_text(encoding="utf-8"))
    else:
        predictions = collect_all_predictions(cohort, DURABLE_RESULTS_DIR)
        pred_json = json.dumps(predictions, indent=2)
        static_pred_file.write_text(pred_json, encoding="utf-8")
        durable_pred_file.write_text(pred_json, encoding="utf-8")
        print(f"Predictions written to {static_pred_file} and {durable_pred_file}")
        print(f"Predictions sha256: {sha256_text(pred_json)[:12]}")

    # Now run evaluation
    eval_results = evaluate_joined_data(predictions)
    eval_json = json.dumps(eval_results, indent=2)

    eval_out_file = RECEIPTS_DIR / "evaluation_results.json"
    durable_eval_file = DURABLE_RESULTS_DIR / "evaluation_results.json"
    eval_out_file.write_text(eval_json, encoding="utf-8")
    durable_eval_file.write_text(eval_json, encoding="utf-8")

    # Also save clean summary results to experiment directory
    exp_results_file = Path(__file__).resolve().parent / "results.json"
    summary_for_repo = {
        "cohort_size": eval_results["cohort_size"],
        "weak_count": eval_results["weak_count"],
        "robust_count": eval_results["robust_count"],
        "weak_rate_fmt": eval_results["weak_rate_fmt"],
        "predictor_metrics": {
            k: {
                "auroc": v["auroc"],
                "auroc_95ci": v["auroc_95ci"],
                "beats_chance": v["beats_chance"],
                "precision_at_3": v["precision_at_3"],
                "precision_at_5": v["precision_at_5"],
                "precision_at_10": v["precision_at_10"],
                "triage": {
                    "skip_rate": v["triage"]["skip_rate"],
                    "n_skipped": v["triage"]["n_skipped"],
                    "n_missed": v["triage"]["n_missed"],
                    "saved_modal_cost_usd": v["triage"]["saved_modal_cost_usd"],
                    "saved_cost_pct": v["triage"]["saved_cost_pct"],
                },
            }
            for k, v in eval_results["predictor_metrics"].items()
        },
        "paired_triage_skips_s_vs_r": eval_results["paired_triage_skips_s_vs_r"],
        "length_confounding": eval_results["length_confounding"],
    }
    exp_results_file.write_text(json.dumps(summary_for_repo, indent=2), encoding="utf-8")

    print("\n================ SUMMARY TABLE ================")
    print(f"{'Predictor':<32} {'AUROC (95% CI)':<22} {'P@3':<6} {'P@5':<6} {'P@10':<6} {'Skip%':<7} {'Miss':<6} {'Saved $':<9}")
    print("-" * 102)
    for k, v in summary_for_repo["predictor_metrics"].items():
        ci_str = f"{v['auroc']:.3f} [{v['auroc_95ci'][0]:.3f}, {v['auroc_95ci'][1]:.3f}]"
        tr = v["triage"]
        print(f"{k:<32} {ci_str:<22} {v['precision_at_3']:<6.2f} {v['precision_at_5']:<6.2f} {v['precision_at_10']:<6.2f} "
              f"{tr['skip_rate']*100:<6.1f}% {tr['n_missed']:<6} ${tr['saved_modal_cost_usd']:<8.2f}")


if __name__ == "__main__":
    main()
