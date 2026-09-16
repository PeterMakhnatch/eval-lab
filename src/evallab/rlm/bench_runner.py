"""Run one RLM harness policy over the synthetic long-context suite.

Host-only development harness (no Harbor, no containers): each task becomes a
``dspy.RLM("context, query -> answer")`` execution under ``LabRlm`` with the
selected policy, scored by the suite's deterministic scorer. One JSONL row per
task plus one trajectory file per task, so runs can be compared and inspected
after the fact.

Usage::

    ZAI_API_KEY=... python -m evallab.rlm.bench_runner --policy stock --seed 1 \
        --n-per-family 6 --context-chars 120000 --workers 4 --out runs/x/bench

The provider key is read from ``ZAI_API_KEY`` (or the runner's
``EVALLAB_ZAI_SECRET_FILE``) and never written anywhere.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from evallab.rlm.bench import BenchTask, generate_suite, score
from evallab.rlm.harness import LabRlm, build_lms, run_rlm, zai_model_id
from evallab.rlm.policies import RlmPolicy, policy_from_json, resolve_policy

DEFAULT_MODEL = "zai-coding-plan/glm-5.3-flash"
SIGNATURE = "context, query -> answer"


def provider_key() -> str:
    value = os.environ.get("ZAI_API_KEY")
    if value:
        return value
    secret_file = os.environ.get("EVALLAB_ZAI_SECRET_FILE")
    if secret_file:
        from evallab.execution_contracts import read_owner_secret_file

        return read_owner_secret_file(Path(secret_file))
    raise SystemExit("ZAI_API_KEY (or EVALLAB_ZAI_SECRET_FILE) is required")


def load_policy(spec: str) -> RlmPolicy:
    """``spec`` is a catalog id or a path to a policy JSON (e.g. a GEPA candidate)."""
    path = Path(spec)
    if path.suffix == ".json" and path.is_file():
        payload = json.loads(path.read_text())
        return policy_from_json(payload.get("policy", payload))
    return resolve_policy(spec)


def run_task(
    task: BenchTask,
    policy: RlmPolicy,
    *,
    api_key: str,
    model_id: str,
    cost_limit_usd: float,
    trajectory_dir: Path,
) -> dict[str, Any]:
    root_lm, sub_lm = build_lms(policy, model_id=model_id, api_key=api_key)
    rlm = LabRlm(
        SIGNATURE,
        policy,
        root_lm=root_lm,
        sub_lm=sub_lm,
        cost_limit_usd=cost_limit_usd,
    )
    result = run_rlm(rlm, root_lm, sub_lm, context=task.context, query=task.query)
    prediction = str(result.outputs.get("answer") or "")
    task_score = score(task, prediction) if not result.error else 0.0
    trajectory_path = trajectory_dir / f"{task.task_id}.json"
    trajectory_path.write_text(
        json.dumps(
            {
                "task_id": task.task_id,
                "family": task.family,
                "query": task.query,
                "answer": task.answer,
                "prediction": prediction,
                "score": task_score,
                "policy": policy.policy_id,
                "trajectory": result.trajectory,
                "final_reasoning": result.final_reasoning,
                "iteration_wall_seconds": rlm.iteration_wall_seconds,
            },
            indent=1,
            default=str,
        )
    )
    row = {
        "policy": policy.policy_id,
        "policy_digest": policy.digest(),
        "model": model_id,
        "task_id": task.task_id,
        "family": task.family,
        "difficulty": task.meta.get("difficulty"),
        "context_chars": len(task.context),
        "score": task_score,
        "prediction": prediction[:500],
        "answer": task.answer,
        "iterations": result.iterations,
        "root_calls": result.root_usage.calls,
        "sub_calls": result.sub_usage.calls if sub_lm is not None else max(0, result.root_usage.calls - result.iterations),
        "input_tokens": result.root_usage.input_tokens + result.sub_usage.input_tokens,
        "output_tokens": result.root_usage.output_tokens + result.sub_usage.output_tokens,
        "reasoning_tokens": result.root_usage.reasoning_tokens + result.sub_usage.reasoning_tokens,
        "cost_usd": round(result.cost_usd, 6),
        "wall_seconds": round(result.wall_seconds, 2),
        "budget_stopped": result.budget_stopped,
        "parse_failures": result.parse_failures,
        "salvaged_actions": result.salvaged_actions,
        "error": result.error,
        "trajectory_path": str(trajectory_path),
    }
    return row


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_family: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_family.setdefault(row["family"], []).append(row)

    def block(items: list[dict[str, Any]]) -> dict[str, Any]:
        n = len(items)
        if n == 0:
            return {"n": 0}
        return {
            "n": n,
            "accuracy": round(sum(r["score"] for r in items) / n, 4),
            "errors": sum(1 for r in items if r["error"]),
            "budget_stopped": sum(1 for r in items if r["budget_stopped"]),
            "mean_iterations": round(sum(r["iterations"] for r in items) / n, 2),
            "mean_sub_calls": round(sum(r["sub_calls"] for r in items) / n, 2),
            "mean_input_tokens": round(sum(r["input_tokens"] for r in items) / n),
            "mean_output_tokens": round(sum(r["output_tokens"] for r in items) / n),
            "mean_reasoning_tokens": round(sum(r["reasoning_tokens"] for r in items) / n),
            "mean_cost_usd": round(sum(r["cost_usd"] for r in items) / n, 4),
            "total_cost_usd": round(sum(r["cost_usd"] for r in items), 4),
            "mean_wall_seconds": round(sum(r["wall_seconds"] for r in items) / n, 1),
        }

    return {
        "overall": block(rows),
        "by_family": {family: block(items) for family, items in sorted(by_family.items())},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--policy", required=True, help="catalog id or policy JSON path")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--n-per-family", type=int, default=6)
    parser.add_argument("--context-chars", type=int, default=120_000)
    parser.add_argument("--families", default="", help="comma-separated family filter")
    parser.add_argument("--task-ids", default="", help="comma-separated task id filter")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--cost-limit-usd", type=float, default=0.6)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--repeat", type=int, default=1, help="repeat index recorded in the file name")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    policy = load_policy(args.policy)
    api_key = provider_key()
    model_id = zai_model_id(args.model)
    tasks = generate_suite(args.seed, args.n_per_family, args.context_chars)
    if args.families:
        wanted = {name.strip() for name in args.families.split(",") if name.strip()}
        tasks = [task for task in tasks if task.family in wanted]
    if args.task_ids:
        wanted_ids = {name.strip() for name in args.task_ids.split(",") if name.strip()}
        tasks = [task for task in tasks if task.task_id in wanted_ids]
    if not tasks:
        raise SystemExit("no tasks selected")

    out_dir: Path = args.out
    trajectory_dir = out_dir / "traj" / f"{policy.policy_id}-s{args.seed}-r{args.repeat}"
    trajectory_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / f"{policy.policy_id}-s{args.seed}-r{args.repeat}.jsonl"
    summary_path = out_dir / f"{policy.policy_id}-s{args.seed}-r{args.repeat}.summary.json"

    print(
        f"policy={policy.policy_id} digest={policy.digest()[:19]} model={model_id} "
        f"tasks={len(tasks)} seed={args.seed} context_chars={args.context_chars} workers={args.workers}",
        flush=True,
    )
    started = time.monotonic()
    rows: list[dict[str, Any]] = []
    lock = threading.Lock()
    with rows_path.open("w") as sink, ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                run_task,
                task,
                policy,
                api_key=api_key,
                model_id=model_id,
                cost_limit_usd=args.cost_limit_usd,
                trajectory_dir=trajectory_dir,
            ): task
            for task in tasks
        }
        for future in as_completed(futures):
            task = futures[future]
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001 - one bad task must not kill the batch
                row = {
                    "policy": policy.policy_id,
                    "policy_digest": policy.digest(),
                    "model": model_id,
                    "task_id": task.task_id,
                    "family": task.family,
                    "difficulty": task.meta.get("difficulty"),
                    "context_chars": len(task.context),
                    "score": 0.0,
                    "prediction": "",
                    "answer": task.answer,
                    "iterations": 0,
                    "root_calls": 0,
                    "sub_calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "cost_usd": 0.0,
                    "wall_seconds": 0.0,
                    "budget_stopped": False,
                    "parse_failures": 0,
                    "salvaged_actions": 0,
                    "error": f"runner: {type(exc).__name__}: {exc}"[:500],
                    "trajectory_path": None,
                }
            with lock:
                rows.append(row)
                sink.write(json.dumps(row) + "\n")
                sink.flush()
            status = "ok " if row["score"] == 1.0 else "ERR" if row["error"] else "bad"
            print(
                f"[{len(rows):>3}/{len(tasks)}] {status} {row['task_id']:<28} iters={row['iterations']:<3} "
                f"sub={row['sub_calls']:<3} cost=${row['cost_usd']:.3f} wall={row['wall_seconds']:.0f}s "
                f"pred={row['prediction'][:40]!r} ans={row['answer'][:24]!r}",
                flush=True,
            )
    summary = summarize(rows)
    summary["policy"] = policy.to_json()
    summary["policy_digest"] = policy.digest()
    summary["model"] = model_id
    summary["seed"] = args.seed
    summary["context_chars"] = args.context_chars
    summary["elapsed_seconds"] = round(time.monotonic() - started, 1)
    summary_path.write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary["overall"], indent=1))
    for family, block in summary["by_family"].items():
        print(f"{family:<16} acc={block['accuracy']:.3f} n={block['n']} cost=${block['mean_cost_usd']:.3f} iters={block['mean_iterations']}")
    print(f"rows: {rows_path}\nsummary: {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
