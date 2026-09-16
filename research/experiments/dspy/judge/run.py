"""Runner: baseline, optimize and evaluate the DSPy calibration judge.

Usage (from the worktree root, after the requirements overlay is installed;
``D=research/experiments/dspy``):

    PYTHONPATH=$D uv run python -m judge.run baseline --family checkout-pool-exhaustion
    PYTHONPATH=$D uv run python -m judge.run optimize --train-family checkout-pool-exhaustion \
        --optimizer gepa --run-id gepa-checkout
    PYTHONPATH=$D uv run python -m judge.run evaluate --program $D/judge/artifacts/gepa-checkout/program.json \
        --family retry-storm-backlog --run-id gepa-checkout

Every run writes under ``artifacts/<run-id>/``: ``metrics-<family>-<split>.json``,
a ``bundle-<family>.json`` JudgePredictionBundle for full-family evaluations, and
``optimize-summary.json`` with token/cost totals from the LM history. Records are
produced by the existing CLI:
``uv run evallab calibrate <family> --predictions <bundle> --skip-catalog``.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import dspy
from lm import DEFAULT_MODEL, configure, zai_lm

from evallab.calibrate import dspy_prediction_bundle
from evallab.schemas import JudgePredictionBundle

from .data import FamilySets, family_sets
from .metric import agreement, agreement_with_feedback, cell_outcomes
from .program import CalibrationJudge, flatten_verdicts

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
ARTIFACTS = HERE / "artifacts"


def task_lm(args: argparse.Namespace) -> dspy.LM:
    """Task LM; ``--thinking`` re-enables GLM's hidden reasoning with a larger budget."""
    if getattr(args, "thinking", False):
        return configure(args.model, thinking=True, max_tokens=16000)
    return configure(args.model)


def _usage(lm: dspy.LM, since: int) -> dict:
    entries = lm.history[since:]
    prompt = sum((e.get("usage") or {}).get("prompt_tokens", 0) or 0 for e in entries)
    completion = sum((e.get("usage") or {}).get("completion_tokens", 0) or 0 for e in entries)
    cost = sum(e.get("cost") or 0.0 for e in entries)
    return {
        "calls": len(entries),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "litellm_estimated_cost_usd": round(cost, 6),
    }


def _bundle(
    family: str,
    results: list[tuple[dspy.Example, dspy.Prediction, float]],
    *,
    judge_backend: str,
    judge_model: str,
) -> tuple[JudgePredictionBundle, int]:
    return dspy_prediction_bundle(
        REPO_ROOT,
        family,
        [(example.document_id, prediction) for example, prediction, _ in results],
        judge_backend=judge_backend,
        judge_model=judge_model,
        judge_engine_version=f"dspy {dspy.__version__}",
    )


def _per_criterion(results) -> dict[str, dict[str, int]]:
    table: dict[str, dict[str, int]] = {}
    for example, prediction, _ in results:
        for dimension, name, expected, observed in cell_outcomes(example, prediction):
            cell = table.setdefault(
                f"{dimension}.{name}", {"agreements": 0, "total": 0, "missing": 0}
            )
            cell["total"] += 1
            cell["agreements"] += int(expected == observed)
            cell["missing"] += int(observed is None)
    return table


def evaluate_set(
    program: dspy.Module,
    examples: list[dspy.Example],
    *,
    out_dir: Path,
    family: str,
    split: str,
    judge_backend: str,
    judge_model: str,
    lm: dspy.LM,
    threads: int,
) -> dict:
    since = len(lm.history)
    started = time.time()
    evaluator = dspy.Evaluate(
        devset=examples,
        metric=agreement,
        num_threads=threads,
        display_progress=True,
        provide_traceback=True,
        max_errors=len(examples),
    )
    result = evaluator(program)
    results = list(result.results)
    # A rate-limited or unparseable call leaves an empty prediction. Retry those
    # sequentially once; a judge run that still has failures is not a measurement.
    failed = [i for i, (_, pred, _) in enumerate(results) if not flatten_verdicts(pred)]
    if failed:
        print(f"retrying {len(failed)} failed document(s) sequentially")
        for i in failed:
            example = results[i][0]
            time.sleep(2)
            pred = program(**example.inputs())
            results[i] = (example, pred, agreement(example, pred))
    still_failed = [ex.document_id for ex, pred, _ in results if not flatten_verdicts(pred)]
    if still_failed:
        raise RuntimeError(f"judge returned no verdicts for {still_failed}; refusing to record")
    scores = [float(score) for _, _, score in results]
    metrics = {
        "family": family,
        "split": split,
        "documents": len(examples),
        "mean_agreement": sum(scores) / len(scores) if scores else 0.0,
        "per_document": {ex.document_id: round(s, 4) for ex, _, s in results},
        "per_criterion": _per_criterion(results),
        "judge_backend": judge_backend,
        "judge_model": judge_model,
        "elapsed_s": round(time.time() - started, 1),
        "usage": _usage(lm, since),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    if split == "all":
        bundle, omitted = _bundle(
            family, results, judge_backend=judge_backend, judge_model=judge_model
        )
        bundle_path = out_dir / f"bundle-{family}.json"
        bundle_path.write_text(bundle.model_dump_json(indent=2) + "\n", encoding="utf-8")
        metrics["bundle"] = bundle_path.relative_to(REPO_ROOT).as_posix()
        metrics["omitted_cells_filled_no"] = omitted
    (out_dir / f"metrics-{family}-{split}.json").write_text(
        json.dumps(metrics, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"[{judge_backend}] {family}/{split}: mean agreement {metrics['mean_agreement']:.4f} "
        f"over {len(examples)} docs, {metrics['usage']['calls']} calls, "
        f"~${metrics['usage']['litellm_estimated_cost_usd']:.4f}"
    )
    return metrics


def load_program(path: Path | None) -> dspy.Module:
    program = CalibrationJudge()
    if path is not None:
        program.load(str(path))
    return program


GEPA_METRIC_CALLS = {"light": 120, "medium": 240, "heavy": 480}


def build_optimizer(name: str, *, reflection_lm: dspy.LM, threads: int, budget: str, log_dir: Path):
    if name == "bootstrap":
        return dspy.BootstrapFewShot(
            metric=agreement, max_bootstrapped_demos=2, max_labeled_demos=2
        )
    if name == "bootstrap-rs":
        return dspy.BootstrapFewShotWithRandomSearch(
            metric=agreement,
            max_bootstrapped_demos=2,
            max_labeled_demos=2,
            num_candidate_programs=6,
            num_threads=threads,
        )
    if name == "mipro":
        return dspy.MIPROv2(
            metric=agreement,
            prompt_model=reflection_lm,
            auto=budget,
            max_bootstrapped_demos=2,
            max_labeled_demos=2,
            num_threads=threads,
            log_dir=str(log_dir),
        )
    if name == "gepa":
        # Explicit cap instead of ``auto``: every metric call is one judge call
        # (~$0.0015 at Flash prices), so the spend is bounded up front.
        return dspy.GEPA(
            metric=agreement_with_feedback,
            max_metric_calls=GEPA_METRIC_CALLS[budget],
            reflection_lm=reflection_lm,
            reflection_minibatch_size=3,
            num_threads=threads,
            track_stats=True,
            log_dir=str(log_dir),
        )
    if name == "simba":
        return dspy.SIMBA(
            metric=lambda ex, pred: agreement(ex, pred),
            bsize=8,
            num_candidates=4,
            max_steps=4,
            max_demos=2,
            num_threads=threads,
        )
    raise ValueError(f"unknown optimizer {name!r}")


def cmd_baseline(args: argparse.Namespace) -> int:
    lm = task_lm(args)
    out_dir = ARTIFACTS / args.run_id
    sets = family_sets(REPO_ROOT, args.family)
    program = load_program(None)
    evaluate_set(
        program,
        getattr(sets, args.split),
        out_dir=out_dir,
        family=args.family,
        split=args.split,
        judge_backend="dspy-cot-unoptimized",
        judge_model=args.model.removeprefix("zai/"),
        lm=lm,
        threads=args.threads,
    )
    return 0


FAMILIES = ("checkout-pool-exhaustion", "retry-storm-backlog")


def training_sets(train_family: str) -> FamilySets:
    """One family's frozen split, or ``both``: the two families' train/val/heldout concatenated.

    Joint training lets the optimizer see two rubrics, so an instruction that
    hardcodes one family's facts is penalised on the other family's validation docs.
    Held-out documents of both families remain unseen.
    """
    if train_family != "both":
        return family_sets(REPO_ROOT, train_family)
    parts = [family_sets(REPO_ROOT, family) for family in FAMILIES]
    return FamilySets(
        family="both",
        all=[e for p in parts for e in p.all],
        train=[e for p in parts for e in p.train],
        val=[e for p in parts for e in p.val],
        heldout=[e for p in parts for e in p.heldout],
    )


def cmd_optimize(args: argparse.Namespace) -> int:
    lm = task_lm(args)
    reflection = zai_lm(args.reflection_model, max_tokens=16000, temperature=1.0)
    out_dir = ARTIFACTS / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    sets = training_sets(args.train_family)
    student = load_program(args.seed_program)
    optimizer = build_optimizer(
        args.optimizer,
        reflection_lm=reflection,
        threads=args.threads,
        budget=args.budget,
        log_dir=out_dir / "optimizer-log",
    )
    since_task, since_reflect = len(lm.history), len(reflection.history)
    started = time.time()
    if args.optimizer in ("bootstrap", "simba"):
        compiled = optimizer.compile(student, trainset=sets.train)
    elif args.optimizer == "bootstrap-rs":
        compiled = optimizer.compile(student, trainset=sets.train, valset=sets.val)
    elif args.optimizer == "mipro":
        compiled = optimizer.compile(
            student, trainset=sets.train, valset=sets.val, requires_permission_to_run=False
        )
    else:
        compiled = optimizer.compile(student, trainset=sets.train, valset=sets.val)
    elapsed = time.time() - started
    program_path = out_dir / "program.json"
    compiled.save(str(program_path))
    instructions = {name: pred.signature.instructions for name, pred in compiled.named_predictors()}
    demos = {name: len(pred.demos) for name, pred in compiled.named_predictors()}
    summary = {
        "run_id": args.run_id,
        "optimizer": args.optimizer,
        "budget": args.budget,
        "train_family": args.train_family,
        "train_ids": [e.document_id for e in sets.train],
        "val_ids": [e.document_id for e in sets.val],
        "heldout_ids_never_shown": [e.document_id for e in sets.heldout],
        "task_model": args.model,
        "reflection_model": args.reflection_model,
        "elapsed_s": round(elapsed, 1),
        "task_lm_usage": _usage(lm, since_task),
        "reflection_lm_usage": _usage(reflection, since_reflect),
        "compiled_instructions": instructions,
        "compiled_demo_counts": demos,
        "program": program_path.relative_to(REPO_ROOT).as_posix(),
        "dspy_version": dspy.__version__,
    }
    detailed = getattr(compiled, "detailed_results", None)
    if detailed is not None:
        try:
            summary["gepa_val_aggregate_scores"] = list(
                getattr(detailed, "val_aggregate_scores", [])
            )
            summary["gepa_best_idx"] = getattr(detailed, "best_idx", None)
            summary["gepa_num_candidates"] = len(getattr(detailed, "candidates", []))
        except Exception as exc:  # noqa: BLE001
            summary["gepa_detail_error"] = repr(exc)
    (out_dir / "optimize-summary.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "compiled_instructions"}, indent=2))
    print("--- compiled instructions ---")
    for name, text in instructions.items():
        print(f"[{name}]\n{text}\n")
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    lm = task_lm(args)
    out_dir = ARTIFACTS / args.run_id
    program = load_program(Path(args.program) if args.program else None)
    backend = args.judge_backend or (
        "dspy-cot-unoptimized" if not args.program else f"dspy-{Path(args.program).parent.name}"
    )
    for family in args.family:
        sets = family_sets(REPO_ROOT, family)
        for split in args.split:
            evaluate_set(
                program,
                getattr(sets, split),
                out_dir=out_dir,
                family=family,
                split=split,
                judge_backend=backend,
                judge_model=args.model.removeprefix("zai/"),
                lm=lm,
                threads=args.threads,
            )
    return 0


def cmd_export_sft(args: argparse.Namespace) -> int:
    """Bootstrap teacher traces and write them in the chat-messages shape a trainer consumes.

    This is the data half of DSPy's ``BootstrapFinetune`` (BetterTogether, arXiv
    2407.10930): run the (prompt-optimized) program as teacher, keep traces whose
    metric clears a threshold, format each predictor call as messages. No training
    happens here and no trainer is named; the file shows what the SFT input would be.
    """
    from dspy.adapters import ChatAdapter
    from dspy.teleprompt.bootstrap_finetune import bootstrap_trace_data, build_call_data_from_trace

    lm = task_lm(args)
    out_dir = ARTIFACTS / args.run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    program = load_program(Path(args.program) if args.program else None)
    sets = family_sets(REPO_ROOT, args.family)
    examples = getattr(sets, args.split)
    since = len(lm.history)
    traces = bootstrap_trace_data(
        program, examples, metric=agreement, num_threads=args.threads, raise_on_error=False
    )
    adapter = ChatAdapter()
    kept, dropped = [], []
    for item in traces:
        score = float(item["score"] or 0.0)
        doc = item["example"].document_id
        if score < args.min_score:
            dropped.append({"document_id": doc, "score": round(score, 4)})
            continue
        for index in range(len(item["trace"])):
            call = build_call_data_from_trace(item["trace"], index, adapter, exclude_demos=True)
            kept.append({"document_id": doc, "score": round(score, 4), **call})
    path = out_dir / f"sft-{args.family}-{args.split}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for row in kept:
            handle.write(json.dumps(row) + "\n")
    provenance = {
        "teacher_program": args.program or "unoptimized",
        "family": args.family,
        "split": args.split,
        "min_score": args.min_score,
        "examples": len(examples),
        "kept_calls": len(kept),
        "dropped_examples": dropped,
        "format": "chat messages per predictor call (dspy ChatAdapter.format_finetune_data), demos excluded",
        "trainer": None,
        "usage": _usage(lm, since),
        "note": "data-shape demonstration only; nothing was trained",
    }
    (out_dir / f"sft-{args.family}-{args.split}.provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(provenance, indent=2))
    if kept:
        sample = kept[0]["messages"]
        print("--- first kept call: roles/lengths ---")
        for message in sample:
            print(f"  {message['role']:9} {len(message['content'])} chars")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument(
        "--thinking", action="store_true", help="enable GLM hidden reasoning (default off)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    baseline = sub.add_parser("baseline")
    baseline.add_argument("--family", required=True)
    baseline.add_argument("--split", default="all", choices=("all", "train", "val", "heldout"))
    baseline.add_argument("--run-id", default="baseline")
    baseline.set_defaults(func=cmd_baseline)

    optimize = sub.add_parser("optimize")
    optimize.add_argument("--train-family", required=True)
    optimize.add_argument(
        "--optimizer",
        required=True,
        choices=("bootstrap", "bootstrap-rs", "mipro", "gepa", "simba"),
    )
    optimize.add_argument("--budget", default="light", choices=("light", "medium", "heavy"))
    optimize.add_argument("--reflection-model", default=DEFAULT_MODEL)
    optimize.add_argument("--seed-program", type=Path, default=None)
    optimize.add_argument("--run-id", required=True)
    optimize.set_defaults(func=cmd_optimize)

    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--program", default=None)
    evaluate.add_argument("--family", action="append", required=True)
    evaluate.add_argument("--split", action="append", default=None)
    evaluate.add_argument("--judge-backend", default=None)
    evaluate.add_argument("--run-id", required=True)
    evaluate.set_defaults(func=cmd_evaluate)

    export = sub.add_parser("export-sft")
    export.add_argument("--program", default=None)
    export.add_argument("--family", required=True)
    export.add_argument("--split", default="train", choices=("all", "train", "val", "heldout"))
    export.add_argument("--min-score", type=float, default=0.9)
    export.add_argument("--run-id", required=True)
    export.set_defaults(func=cmd_export_sft)

    args = parser.parse_args(argv)
    if args.command == "evaluate" and not args.split:
        args.split = ["all"]
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
