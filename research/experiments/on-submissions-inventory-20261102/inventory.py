#!/usr/bin/env python3
"""A0-SUBMISSIONS-INVENTORY: Real agent submissions inventory for MiMo packages.

Scans eval-lab-results and stored trial records for real agent submissions
on MiMo tasks, extracts patch paths, sha256 checksums, and original grader
outcomes, and joins against A1's gold universe.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

from evallab.production.corpus_queue import CorpusRegistry


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials."""
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    denom = 1.0 + (z * z) / n
    centre = (p + (z * z) / (2.0 * n)) / denom
    half = (z / denom) * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * n)) / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def fmt(k: int, n: int) -> str:
    """Format k/n with percent and Wilson interval."""
    if n == 0:
        return f"{k}/{n} = nan"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / n:.1f}% ({100 * lo:.1f}-{100 * hi:.1f}%)"


def get_file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def normalize_task_id(raw: str) -> str:
    m = re.search(r"format-code-task-(\d+)", raw)
    if m:
        num = m.group(1).zfill(6)
        return f"format-code-task-{num}"
    m_any = re.search(r"(\d{6})", raw)
    if m_any:
        return f"format-code-task-{m_any.group(1)}"
    return raw.rsplit("/", 1)[-1].split("@", 1)[0]


def collect_trials(results_root: Path, registry: CorpusRegistry) -> list[dict[str, Any]]:
    trials: list[dict[str, Any]] = []
    seen_trial_dirs: set[str] = set()

    # 1. Spec-based jobs
    for spec_path in sorted(results_root.glob("2026-*/**/experiment-spec.json")):
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        task_raw = str(spec.get("task", "")) or str(spec.get("task_path", ""))
        if not ("mimo" in task_raw.lower() or "format-code" in task_raw.lower()):
            continue

        agent = str(spec.get("agent", ""))
        model = str(spec.get("model", ""))
        if agent.lower() in ("nop", "oracle", "cheat", "tamper", "exploit", "probe", "mutant"):
            continue
        if any(x in model.lower() for x in ["none", "null"]):
            continue

        job_dir = spec_path.parent
        spec_task_id = normalize_task_id(task_raw)

        for c in sorted(job_dir.iterdir()):
            if not (c.is_dir() and (c / "config.json").is_file()):
                continue

            trial_dir = c
            if str(trial_dir) in seen_trial_dirs:
                continue
            seen_trial_dirs.add(str(trial_dir))

            trial_cfg = {}
            with contextlib.suppress(Exception):
                trial_cfg = json.loads((trial_dir / "config.json").read_text(encoding="utf-8"))

            task_id = spec_task_id
            trial_task = trial_cfg.get("task", {})
            trial_task_path = str(trial_task.get("path", "")) if isinstance(trial_task, dict) else str(trial_task)
            if trial_task_path:
                task_id = normalize_task_id(trial_task_path)

            ag_obj = trial_cfg.get("agent", {})
            tr_agent = (ag_obj.get("name") if isinstance(ag_obj, dict) else str(ag_obj)) or agent
            tr_model = (ag_obj.get("model_name") if isinstance(ag_obj, dict) else None) or model

            # Reward
            reward = None
            reward_file = trial_dir / "verifier/reward.txt"
            if not reward_file.is_file():
                reward_file = trial_dir / "reward.txt"
            if reward_file.is_file():
                with contextlib.suppress(Exception):
                    reward = float(reward_file.read_text(encoding="utf-8").strip())

            if reward is None:
                res_file = trial_dir / "result.json"
                if res_file.is_file():
                    with contextlib.suppress(Exception):
                        rdata = json.loads(res_file.read_text(encoding="utf-8"))
                        if "reward" in rdata and rdata["reward"] is not None:
                            reward = float(rdata["reward"])

            if reward is None:
                jres_file = job_dir / "result.json"
                if jres_file.is_file():
                    with contextlib.suppress(Exception):
                        jrdata = json.loads(jres_file.read_text(encoding="utf-8"))
                        evals = jrdata.get("stats", {}).get("evals", {})
                        for _ev_key, ev_val in evals.items():
                            rew_stats = ev_val.get("reward_stats", {}).get("reward", {})
                            for r_val, t_list in rew_stats.items():
                                if trial_dir.name in t_list:
                                    reward = float(r_val)
                                    break

            # Recoverable patch
            patch_path = None
            patch_sha256 = None
            patch_size = 0
            for cand in [
                trial_dir / "verifier/agent.kept.diff",
                trial_dir / "verifier/agent.diff",
                trial_dir / "verifier/agent.full.diff",
                trial_dir / "agent.diff",
                job_dir / "uncommitted.diff",
            ]:
                if cand.is_file() and cand.stat().st_size > 0:
                    patch_path = cand
                    patch_size = cand.stat().st_size
                    patch_sha256 = get_file_sha256(cand)
                    break

            run_id = trial_cfg.get("job_id") or trial_cfg.get("trial_name") or trial_dir.name
            is_dev = registry.sealed_identity(task_id=task_id) is None

            trials.append({
                "task_id": task_id,
                "agent_name": tr_agent,
                "agent_model": tr_model,
                "run_id": run_id,
                "trial_dir": str(trial_dir),
                "original_reward": reward,
                "has_patch": patch_path is not None,
                "patch_path": str(patch_path) if patch_path else None,
                "patch_sha256": patch_sha256,
                "patch_size": patch_size,
                "is_dev": is_dev,
                "source": "spec_job",
            })

    # 2. Regrade receipts in cheatbench-mimo-cheat-set-v7
    regrade_root = results_root / "2026-10-19/cheatbench-mimo-cheat-set-v7/state/regrade"
    if regrade_root.is_dir():
        for rc in sorted(regrade_root.glob("**/regrade-receipt.json")):
            r_dir = rc.parent
            if str(r_dir) in seen_trial_dirs:
                continue
            seen_trial_dirs.add(str(r_dir))

            try:
                r_receipt = json.loads(rc.read_text(encoding="utf-8"))
            except Exception:
                continue

            src = r_receipt.get("source", {})
            task_name = src.get("task_name", "")
            task_id = normalize_task_id(task_name)
            model = src.get("model_name") or "selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
            agent = src.get("agent_name") or "mimoagent"
            run_id = src.get("trial_name") or r_dir.name

            orig_rewards = r_receipt.get("recorded", {}).get("rewards", {})
            reward = orig_rewards.get("reward")
            if reward is None and (r_dir / "verifier/reward.txt").is_file():
                with contextlib.suppress(Exception):
                    reward = float((r_dir / "verifier/reward.txt").read_text(encoding="utf-8").strip())

            patch_path = None
            patch_sha256 = None
            patch_size = 0
            for cand in [
                r_dir / "verifier/agent.kept.diff",
                r_dir / "verifier/agent.diff",
                r_dir / "verifier/agent.full.diff",
            ]:
                if cand.is_file() and cand.stat().st_size > 0:
                    patch_path = cand
                    patch_size = cand.stat().st_size
                    patch_sha256 = get_file_sha256(cand)
                    break

            is_dev = registry.sealed_identity(task_id=task_id) is None

            trials.append({
                "task_id": task_id,
                "agent_name": agent,
                "agent_model": model,
                "run_id": run_id,
                "trial_dir": str(r_dir),
                "original_reward": reward,
                "has_patch": patch_path is not None,
                "patch_path": str(patch_path) if patch_path else None,
                "patch_sha256": patch_sha256,
                "patch_size": patch_size,
                "is_dev": is_dev,
                "source": "regrade_receipt",
            })

    return trials


def run_inventory(
    results_root: Path,
    gold_universe_path: Path,
    out_dir_a0: Path,
    gates_dir: Path,
    receipts_dir: Path,
    repo_experiment_dir: Path,
) -> dict[str, Any]:
    registry = CorpusRegistry()

    trials = collect_trials(results_root, registry)
    total_trials = len(trials)
    trials_with_patch = sum(1 for t in trials if t["has_patch"])
    trials_with_reward = sum(1 for t in trials if t["original_reward"] is not None)
    trials_accepted = sum(1 for t in trials if t["original_reward"] == 1.0)
    trials_rejected = sum(1 for t in trials if t["original_reward"] == 0.0)
    trials_accepted_with_patch = sum(1 for t in trials if t["original_reward"] == 1.0 and t["has_patch"])
    trials_rejected_with_patch = sum(1 for t in trials if t["original_reward"] == 0.0 and t["has_patch"])

    dev_trials = [t for t in trials if t["is_dev"]]
    test_trials = [t for t in trials if not t["is_dev"]]

    # Model breakdown
    models = sorted(set(t["agent_model"] for t in trials))
    model_breakdown = {}
    for m in models:
        m_list = [t for t in trials if t["agent_model"] == m]
        m_total = len(m_list)
        m_patch = sum(1 for t in m_list if t["has_patch"])
        m_r1 = sum(1 for t in m_list if t["original_reward"] == 1.0)
        m_r0 = sum(1 for t in m_list if t["original_reward"] == 0.0)
        m_acc_patch = sum(1 for t in m_list if t["original_reward"] == 1.0 and t["has_patch"])
        m_dev = sum(1 for t in m_list if t["is_dev"])
        model_breakdown[m] = {
            "total_trials": m_total,
            "trials_with_patch": m_patch,
            "trials_with_reward_1": m_r1,
            "trials_with_reward_0": m_r0,
            "accepted_with_patch": m_acc_patch,
            "dev_trials": m_dev,
            "non_dev_trials": m_total - m_dev,
        }

    # Gold universe join
    gold_ok_tasks: set[str] = set()
    gold_total_candidates = 0
    if gold_universe_path.is_file():
        gu_data = json.loads(gold_universe_path.read_text(encoding="utf-8"))
        gold_total_candidates = len(gu_data.get("tasks", []))
        for t in gu_data.get("tasks", []):
            if t.get("gold_ok"):
                gold_ok_tasks.add(t["task_id"])

    gold_task_coverage = {}
    gold_ok_with_ge_3 = []
    gold_ok_with_ge_1 = []
    gold_ok_zero = []

    for tid in sorted(gold_ok_tasks):
        t_trials = [t for t in trials if t["task_id"] == tid]
        t_acc = [t for t in t_trials if t["original_reward"] == 1.0 and t["has_patch"]]
        t_rej = [t for t in t_trials if t["original_reward"] == 0.0 and t["has_patch"]]
        count_acc = len(t_acc)
        gold_task_coverage[tid] = {
            "total_trials": len(t_trials),
            "accepted_with_patch": count_acc,
            "rejected_with_patch": len(t_rej),
            "has_ge_3_accepted": count_acc >= 3,
        }
        if count_acc >= 3:
            gold_ok_with_ge_3.append(tid)
        if count_acc >= 1:
            gold_ok_with_ge_1.append(tid)
        if len(t_trials) == 0:
            gold_ok_zero.append(tid)

    # Cost estimate for producing missing data
    # Missing tasks in gold_ok: 32 - 2 = 30 tasks need top-up to reach >= 3 accepted submissions
    # 25 tasks have 0 trials -> need at least 3-5 trials each
    # At $0.30 per trial (model + Modal sandbox execution), 100 trials = $30.00
    topup_cost_estimate = {
        "gold_ok_tasks_total": len(gold_ok_tasks),
        "gold_ok_tasks_ge_3": len(gold_ok_with_ge_3),
        "gold_ok_tasks_lacking_ge_3": len(gold_ok_tasks) - len(gold_ok_with_ge_3),
        "gold_ok_tasks_zero_trials": len(gold_ok_zero),
        "recommended_new_trials_per_lacking_task": 4,
        "total_recommended_trials": (len(gold_ok_tasks) - len(gold_ok_with_ge_3)) * 4,
        "estimated_cost_per_trial_usd": 0.30,
        "estimated_total_cost_usd": round((len(gold_ok_tasks) - len(gold_ok_with_ge_3)) * 4 * 0.30, 2),
        "note": "Re-running 4 frontier agent trials (e.g. Qwen-2.5-Coder-32B or MiMo) on Modal per lacking task provides >= 3 accepted solutions with 75% pass@4 assuming ~40-50% pass rate.",
    }

    # Format deliverables
    submissions_export = [
        {
            "task_id": t["task_id"],
            "agent_model": t["agent_model"],
            "run_id": t["run_id"],
            "patch_path": t["patch_path"],
            "patch_sha256": t["patch_sha256"],
            "original_reward": t["original_reward"],
            "is_dev": t["is_dev"],
        }
        for t in trials
        if t["has_patch"] and t["original_reward"] is not None
    ]

    inventory_doc = {
        "schema_version": "submissions-inventory/v1",
        "description": "Inventory of real agent submissions on MiMo packages with original grader outcomes",
        "headline": f"{trials_accepted_with_patch} accepted real submissions across {len(set(t['task_id'] for t in trials if t['original_reward'] == 1.0 and t['has_patch']))} tasks; {len(gold_ok_with_ge_3)}/{len(gold_ok_tasks)} gold_ok tasks have >= 3 accepted submissions",
        "counts": {
            "total_trials": total_trials,
            "trials_with_patch": trials_with_patch,
            "trials_with_reward": trials_with_reward,
            "trials_accepted_original_reward_1": trials_accepted,
            "trials_rejected_original_reward_0": trials_rejected,
            "trials_accepted_with_patch": trials_accepted_with_patch,
            "trials_rejected_with_patch": trials_rejected_with_patch,
            "strictly_dev_trials": len(dev_trials),
            "non_dev_trials": len(test_trials),
            "usable_submissions_exported": len(submissions_export),
        },
        "gold_universe_join": {
            "gold_total_candidates": gold_total_candidates,
            "gold_ok_tasks": len(gold_ok_tasks),
            "gold_ok_with_ge_3_accepted": len(gold_ok_with_ge_3),
            "gold_ok_with_ge_1_accepted": len(gold_ok_with_ge_1),
            "gold_ok_zero_trials": len(gold_ok_zero),
            "coverage_fmt": fmt(len(gold_ok_with_ge_3), len(gold_ok_tasks)),
            "coverage_tasks_ge_3": sorted(gold_ok_with_ge_3),
            "task_breakdown": gold_task_coverage,
        },
        "model_breakdown": model_breakdown,
        "cost_estimate_for_missing": topup_cost_estimate,
        "submissions": submissions_export,
    }

    # Write deliverables
    out_dir_a0.mkdir(parents=True, exist_ok=True)
    gates_dir.mkdir(parents=True, exist_ok=True)
    receipts_dir.mkdir(parents=True, exist_ok=True)
    repo_experiment_dir.mkdir(parents=True, exist_ok=True)

    # 1. submissions-inventory.json
    sub_inv_path_a0 = out_dir_a0 / "submissions-inventory.json"
    sub_inv_path_gates = gates_dir / "submissions-inventory.json"
    sub_inv_text = json.dumps(inventory_doc, indent=2) + "\n"
    sub_inv_path_a0.write_text(sub_inv_text, encoding="utf-8")
    sub_inv_path_gates.write_text(sub_inv_text, encoding="utf-8")

    # 2. receipts
    (receipts_dir / "inventory_receipt.json").write_text(sub_inv_text, encoding="utf-8")
    (out_dir_a0 / "inventory_receipt.json").write_text(sub_inv_text, encoding="utf-8")

    # Write patch manifest JSONL
    patch_manifest_lines = [json.dumps(row) for row in submissions_export]
    patch_manifest_text = "\n".join(patch_manifest_lines) + "\n"
    (receipts_dir / "patch_manifest.jsonl").write_text(patch_manifest_text, encoding="utf-8")
    (out_dir_a0 / "patch_manifest.jsonl").write_text(patch_manifest_text, encoding="utf-8")

    # 3. repo counts and summary
    summary_counts = {
        "schema_version": "submissions-inventory-counts/v1",
        "counts": inventory_doc["counts"],
        "gold_universe_join": {
            "gold_ok_tasks": len(gold_ok_tasks),
            "gold_ok_with_ge_3_accepted": len(gold_ok_with_ge_3),
            "gold_ok_with_ge_1_accepted": len(gold_ok_with_ge_1),
            "coverage_fmt": fmt(len(gold_ok_with_ge_3), len(gold_ok_tasks)),
            "coverage_tasks_ge_3": sorted(gold_ok_with_ge_3),
        },
        "model_breakdown": model_breakdown,
        "cost_estimate_for_missing": topup_cost_estimate,
        "submissions_inventory_sha256": hashlib.sha256(sub_inv_text.encode("utf-8")).hexdigest(),
        "patch_manifest_sha256": hashlib.sha256(patch_manifest_text.encode("utf-8")).hexdigest(),
    }
    counts_path = repo_experiment_dir / "counts.json"
    counts_path.write_text(json.dumps(summary_counts, indent=2) + "\n", encoding="utf-8")

    return inventory_doc


def main() -> None:
    parser = argparse.ArgumentParser(description="Inventory real agent submissions on MiMo packages")
    parser.add_argument(
        "--results-root",
        type=Path,
        default=Path("/Users/petermakhnatch/Developer/eval-lab-results"),
        help="Path to eval-lab-results root directory",
    )
    parser.add_argument(
        "--gold-universe",
        type=Path,
        default=Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates/gold-universe.json"),
        help="Path to A1 gold-universe.json",
    )
    parser.add_argument(
        "--out-dir-a0",
        type=Path,
        default=Path("/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A0"),
        help="Path to A0 output directory",
    )
    parser.add_argument(
        "--gates-dir",
        type=Path,
        default=Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/gates"),
        help="Path to overnight gates directory",
    )
    parser.add_argument(
        "--receipts-dir",
        type=Path,
        default=Path("/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/cert/raw/A0-SUBMISSIONS-INVENTORY/receipts"),
        help="Path to receipts directory",
    )
    parser.add_argument(
        "--repo-experiment-dir",
        type=Path,
        default=Path("research/experiments/on-submissions-inventory-20261102"),
        help="Path to committed experiment directory",
    )
    args = parser.parse_args()

    doc = run_inventory(
        results_root=args.results_root,
        gold_universe_path=args.gold_universe,
        out_dir_a0=args.out_dir_a0,
        gates_dir=args.gates_dir,
        receipts_dir=args.receipts_dir,
        repo_experiment_dir=args.repo_experiment_dir,
    )

    print("Inventory complete.")
    print(f"Total trials: {doc['counts']['total_trials']}")
    print(f"Trials with patch: {doc['counts']['trials_with_patch']}")
    print(f"Trials with reward 1.0 (accepted): {doc['counts']['trials_accepted_original_reward_1']}")
    print(f"Trials accepted with recoverable patch: {doc['counts']['trials_accepted_with_patch']}")
    print(f"Gold universe coverage (>= 3 accepted): {doc['gold_universe_join']['coverage_fmt']}")
    print(f"Output written to {args.out_dir_a0 / 'submissions-inventory.json'}")


if __name__ == "__main__":
    main()
