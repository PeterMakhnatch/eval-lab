"""GEPA over the RLM action instructions, evaluated on the synthetic suite.

dspy's RLM ships with ``# TODO: Optimize this prompt across a diverse benchmark``
above its action template. This module does exactly that for the lab's harness:
``dspy.GEPA`` reflects on full RLM trajectories (score + textual feedback per
task) and proposes new *complete* action instructions for ``generate_action``;
the ``extract`` predictor is left untouched. The winner is written as a policy
JSON (``action_instructions_override`` set) that ``bench_runner --policy
<path>`` and the Harbor agent can run unchanged, so the held-out comparison is
one variable: the instruction text.

Train/val come from a *different seed* than the evaluation suite, so the
"gepa" policy is measured on tasks it never saw.

Usage::

    ZAI_API_KEY=... python -m evallab.rlm.gepa_rlm --base orchestrator --train-seed 7 \
        --n-per-family 3 --context-chars 60000 --max-metric-calls 36 --out runs/x/gepa
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import dspy

from evallab.rlm.bench import BenchTask, generate_suite, score
from evallab.rlm.bench_runner import SIGNATURE, load_policy, provider_key
from evallab.rlm.harness import LabRlm, build_lm, build_lms, zai_model_id
from evallab.rlm.policies import RlmPolicy

REFLECTION_MODEL = "glm-5.3"


def as_examples(tasks: list[BenchTask]) -> list[dspy.Example]:
    examples = []
    for task in tasks:
        examples.append(
            dspy.Example(
                context=task.context,
                query=task.query,
                answer=task.answer,
                task_id=task.task_id,
                family=task.family,
            ).with_inputs("context", "query")
        )
    return examples


def make_metric(task_index: dict[str, BenchTask]):
    def metric(gold, pred, trace=None, pred_name=None, pred_trace=None):
        task = task_index[gold.task_id]
        prediction = str(getattr(pred, "answer", "") or "")
        value = score(task, prediction)
        trajectory = list(getattr(pred, "trajectory", None) or [])
        steps = len(trajectory)
        errors = [
            entry.get("output", "")[:160]
            for entry in trajectory
            if str(entry.get("output", "")).startswith("[Error]")
        ]
        feedback = (
            f"task family={task.family}; expected answer={task.answer!r}; submitted={prediction[:80]!r}; "
            f"score={value}; iterations used={steps}."
        )
        if errors:
            feedback += f" REPL errors seen ({len(errors)}): " + " | ".join(errors[:3])
        if value < 1.0:
            feedback += (
                " The answer was wrong or missing: the trajectory should verify parsing on a sample, "
                "aggregate in code, use llm_query only for semantic classification, and SUBMIT the exact "
                "requested format."
            )
        return dspy.Prediction(score=value, feedback=feedback)

    return metric


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="orchestrator", help="policy whose configuration (budgets, masking) the candidate keeps")
    parser.add_argument("--train-seed", type=int, default=7)
    parser.add_argument("--val-seed", type=int, default=8)
    parser.add_argument("--n-per-family", type=int, default=3)
    parser.add_argument("--context-chars", type=int, default=60_000)
    parser.add_argument("--max-metric-calls", type=int, default=36)
    parser.add_argument("--num-threads", type=int, default=2)
    parser.add_argument("--reflection-model", default=REFLECTION_MODEL)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)

    api_key = provider_key()
    base = load_policy(args.base)
    model_id = zai_model_id("zai-coding-plan/glm-5.3-flash")
    root_lm, sub_lm = build_lms(base, model_id=model_id, api_key=api_key)
    reflection_lm = build_lm(
        model_id=args.reflection_model, api_key=api_key, max_tokens=16_000, thinking=True, temperature=1.0
    )

    train_tasks = generate_suite(args.train_seed, args.n_per_family, args.context_chars)
    val_tasks = generate_suite(args.val_seed, max(1, args.n_per_family // 2), args.context_chars)
    index = {task.task_id: task for task in train_tasks + val_tasks}
    trainset, valset = as_examples(train_tasks), as_examples(val_tasks)

    student = LabRlm(SIGNATURE, base, root_lm=root_lm, sub_lm=sub_lm, cost_limit_usd=0.5)
    original_instructions = student.generate_action.signature.instructions

    args.out.mkdir(parents=True, exist_ok=True)
    log_dir = args.out / "gepa-logs"
    optimizer = dspy.GEPA(
        metric=make_metric(index),
        max_metric_calls=args.max_metric_calls,
        reflection_lm=reflection_lm,
        reflection_minibatch_size=3,
        num_threads=args.num_threads,
        track_stats=True,
        log_dir=str(log_dir),
        add_format_failure_as_feedback=True,
    )
    started = time.monotonic()
    with dspy.context(lm=root_lm, track_usage=True):
        optimized = optimizer.compile(student, trainset=trainset, valset=valset)
    elapsed = time.monotonic() - started

    new_instructions = optimized.generate_action.signature.instructions
    changed = new_instructions != original_instructions
    candidate = RlmPolicy(
        **{
            **{k: v for k, v in base.to_json().items() if k != "schema_version"},
            "policy_id": f"gepa-{base.policy_id}",
            "description": f"GEPA-optimised action instructions on top of {base.policy_id} (train seed {args.train_seed}, {args.max_metric_calls} metric calls)",
            "source": "evallab.rlm.gepa_rlm; dspy.GEPA over LabRlm.generate_action",
            "action_instructions_override": new_instructions if changed else None,
        }
    )
    record: dict[str, Any] = {
        "policy": candidate.to_json(),
        "policy_digest": candidate.digest(),
        "changed": changed,
        "base_policy": base.policy_id,
        "train_seed": args.train_seed,
        "val_seed": args.val_seed,
        "n_train": len(trainset),
        "n_val": len(valset),
        "context_chars": args.context_chars,
        "max_metric_calls": args.max_metric_calls,
        "elapsed_seconds": round(elapsed, 1),
        "original_instructions": original_instructions,
        "optimized_instructions": new_instructions,
    }
    detailed = getattr(optimized, "detailed_results", None)
    if detailed is not None:
        try:
            record["gepa_results"] = {
                "best_idx": getattr(detailed, "best_idx", None),
                "val_aggregate_scores": getattr(detailed, "val_aggregate_scores", None),
                "num_candidates": len(getattr(detailed, "candidates", []) or []),
                "total_metric_calls": getattr(detailed, "total_metric_calls", None),
            }
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            record["gepa_results_error"] = str(exc)
    (args.out / f"{candidate.policy_id}.json").write_text(json.dumps(record, indent=2))
    print(json.dumps({k: v for k, v in record.items() if k not in ("original_instructions", "optimized_instructions", "policy")}, indent=1))
    print(f"policy json: {args.out / (candidate.policy_id + '.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
