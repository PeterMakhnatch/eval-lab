"""Measure, budget, and run option-B auditor SFT through the native Tinker launcher.

The $15 authorization covers training, twenty capped evaluation trials, and a
private 24-hour checkpoint lease. No evaluation or paid retry is launched here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from evallab import sft_tinker
from evallab.tracing import TraceError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
RUNTIME = ROOT / "derived/auditor-sft-v1"
MODEL = "Qwen/Qwen3.6-35B-A3B"
RENDERER = "qwen3_5_disable_thinking"
MASK = "last_assistant_message"
EPOCHS = 2
MAX_LENGTH = 16384
SAVE_EVERY = 100  # periodic, leased checkpoints: a billing abort must not lose the run
HARD_CAP = Decimal("15")
EVALUATION_RESERVE = Decimal("5")
STORAGE_RESERVE = Decimal("2.25")
TRAIN_PRICE = Decimal("1.177")
PRICING_URL = "https://tinker-docs.thinkingmachines.ai/tinker/models/models_and_pricing/"


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


HOLDOUT = HERE / "holdout_manifest.json"
def dollars(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.000001"), rounding=ROUND_CEILING))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as handle:
        handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")


def budget_for_tokens(tokens: int) -> dict[str, Any]:
    """Price every sequence token; never round down before the hard-cap check."""
    if tokens <= 0:
        raise TraceError("training requires a positive rendered-token count")
    training_cost = Decimal(tokens) * EPOCHS * TRAIN_PRICE / 1_000_000
    total = training_cost + EVALUATION_RESERVE + STORAGE_RESERVE
    if total > HARD_CAP:
        raise TraceError(f"whole-job estimate ${total} exceeds the $15 cap; no paid work permitted")
    return {
        "hard_cap_usd": float(HARD_CAP), "training_upper_bound_usd": dollars(training_cost),
        "evaluation_reserve_usd": float(EVALUATION_RESERVE),
        "storage_reserve_usd": float(STORAGE_RESERVE), "whole_job_upper_bound_usd": dollars(total),
        "evaluation": "20 trials, $0.25 per-trial native proxy cap; no extra paid probe/retry",
        "training_basis": "all rendered tokens, including masked context; conservatively includes one shift token per example",
        "pricing_url": PRICING_URL, "storage_retention_seconds": 86400,
    }


def make_plan(data: Path) -> dict[str, Any]:
    manifest = sft_tinker.verify_export_dir(data)
    count = manifest["counts"]["histories"]
    if not 200 <= count <= 500 or manifest["split"]["train_histories"] != count:
        raise TraceError("this authorization requires 200–500 training histories")
    if manifest.get("train_on_what") != MASK or manifest["split"]["val_histories"]:
        raise TraceError("the training export must contain only task-disjoint training prefixes")
    sealed_holdout = manifest["holdout"]
    snapshot = ROOT / sealed_holdout["path"]
    if digest(snapshot) != sealed_holdout["sha256"]:
        raise TraceError("builder's held-out snapshot changed after the training split was sealed")
    canonical = json.loads(HOLDOUT.read_text())
    canonical_ids = sorted(task["task_id"] for task in canonical["tasks"])
    if canonical_ids != sorted(sealed_holdout["excluded_task_ids"]):
        raise TraceError("tracked benchmark tasks differ from those excluded from training")
    report = sft_tinker.dry_run(
        data, model=MODEL, renderer_name=RENDERER, max_length=MAX_LENGTH,
        epochs=EPOCHS, train_on_what=MASK,
    )
    stats = report["render"]
    if stats["truncated_conversations"] or stats["supervised_tokens_after_truncation"] <= 0:
        raise TraceError("refusing truncated histories or an empty supervision mask")
    examples = manifest["counts"]["supervised_examples"]
    if stats["conversations"] != examples:
        raise TraceError("supervised-example count does not match the sealed export")
    if Decimal(str(report["price_per_mtoken_usd"])) != TRAIN_PRICE:
        raise TraceError("native training price changed; re-estimate the whole job")
    budget = budget_for_tokens(stats["total_tokens_after_truncation"])
    # Cookbook 0.5.7 drops incomplete batches. A divisor preserves every target.
    batch_size = math.gcd(16, examples)
    return {
        "contract": "auditor-sft-v1.training/1", "created_at": datetime.now(UTC).isoformat(),
        "approval": "Peter's 2026-10-13 overnight option-B instruction; hard $15 Tinker cap; no $40 L1 test",
        "data_dir": data.resolve().as_posix(), "data_manifest_sha256": digest(data / "manifest.json"),
        "holdout_sha256": digest(HOLDOUT), "holdout_snapshot_sha256": digest(snapshot),
        "held_out_task_ids": canonical_ids, "measurement": report,
        "trainer": {"model": MODEL, "renderer": RENDERER, "train_on_what": MASK,
                    "num_epochs": EPOCHS, "max_length": MAX_LENGTH, "batch_size": batch_size,
                    "lora_rank": 32, "learning_rate": 1e-4, "save_every": SAVE_EVERY, "eval_every": 0},
        "budget": budget,
        "toolchain_sha256": digest(sft_tinker.PROJECT_DIR / "uv.lock"),
    }


def record_spend(event: dict[str, Any]) -> None:
    event = {"ts": datetime.now(UTC).isoformat(), "provider": "tinker", "experiment": "auditor-sft-v1", **event}
    with (HERE / "spend.jsonl").open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def train(plan_path: Path, *, confirm_spend: bool) -> dict[str, Any]:
    if not confirm_spend:
        raise TraceError("training requires --confirm-spend and the saved whole-job plan")
    if not os.environ.get(sft_tinker.TINKER_API_KEY_ENV):
        raise TraceError("TINKER_API_KEY is not set; use keys run without exposing its value")
    plan = json.loads(plan_path.read_text())
    data = Path(plan["data_dir"])
    # Re-measure before payment, not merely trust an editable estimate or old digest.
    fresh = make_plan(data)
    for key in ("data_manifest_sha256", "holdout_sha256", "holdout_snapshot_sha256", "held_out_task_ids", "trainer", "budget", "toolchain_sha256"):
        if fresh[key] != plan[key]:
            raise TraceError(f"preflight changed ({key}); do not spend against the old plan")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    # Exclusive one-shot reservation: an interrupted/uncertain run is never retried automatically.
    write_json(RUNTIME / "training-intent.json", {
        "created_at": datetime.now(UTC).isoformat(), "plan_sha256": digest(plan_path),
        "training_upper_bound_usd": plan["budget"]["training_upper_bound_usd"],
        "state": "reserved; inspect training outcome before any further paid work",
    })
    log_dir = RUNTIME / "training"

    def execute(command: list[str]) -> int:
        command.extend([f"save_every={SAVE_EVERY}", "eval_every=0", "infrequent_eval_every=0", "rolling_save_every=0"])
        environment = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"}
        with (RUNTIME / "training.stdout.log").open("x") as console:
            return subprocess.run(command, env=environment, stdout=console, stderr=subprocess.STDOUT, check=False).returncode

    try:
        result = sft_tinker.run_training(
            data, model=MODEL, renderer_name=RENDERER, log_dir=log_dir,
            learning_rate=1e-4, lora_rank=32, batch_size=plan["trainer"]["batch_size"],
            num_epochs=EPOCHS, max_length=MAX_LENGTH, train_on_what=MASK,
            confirm_spend=True, runner=execute,
        )
    except Exception:
        record_spend({"operation": "training-unresolved", "cost_usd": None,
                      "cost_upper_bound_usd": plan["budget"]["training_upper_bound_usd"],
                      "note": "not zero spend; retain full reservation and stop paid work pending reconciliation"})
        raise
    tokens = result["final_checkpoint"].get("elapsed_tokens")
    if not isinstance(tokens, int) or tokens <= 0:
        raise TraceError("final checkpoint lacks settled training token accounting; stop paid work")
    cost = Decimal(tokens) * TRAIN_PRICE / 1_000_000
    record_spend({"operation": "training", "model": MODEL, "input_tokens": tokens,
                  "cost_usd": dollars(cost), "price_per_million_tokens_usd": float(TRAIN_PRICE),
                  "basis": "canonical trainer final elapsed_tokens at published price; not provider invoice",
                  "receipt_sha256": digest(log_dir / sft_tinker.TRAINING_MANIFEST_FILE)})
    lease_path = RUNTIME / "checkpoint-lease.json"
    lease_command = [
        "uv", "run", "--project", str(sft_tinker.PROJECT_DIR), "--locked", "python",
        str(sft_tinker.PROJECT_DIR / "checkpoint_lease.py"),
        "--checkpoints", str(log_dir / sft_tinker.CHECKPOINTS_FILE),
        "--out", str(lease_path), "--export-dir", str(RUNTIME / "private-checkpoints"),
        "--ttl-seconds", "86400", "--storage-cap-usd", str(STORAGE_RESERVE),
    ]
    lease_status = subprocess.run(lease_command, check=False).returncode
    if lease_path.is_file():
        lease = json.loads(lease_path.read_text())
        record_spend({"operation": "checkpoint-storage-commitment", "cost_usd": lease["storage_cost_upper_bound_usd"],
                      "basis": lease["storage_basis"], "receipt_sha256": digest(lease_path)})
    else:
        raise TraceError("checkpoint retention is unconfirmed; stop paid work and retain storage reservation")
    if lease_status or cost > Decimal(str(plan["budget"]["training_upper_bound_usd"])):
        raise TraceError("training/retention accounting did not close within the plan; stop paid work")
    receipt = {"training": result, "lease": lease, "budget": plan["budget"],
               "training_cost_usd": dollars(cost), "ready_for_capped_evaluation": True}
    write_json(RUNTIME / "training-result.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    measure = sub.add_parser("plan", help="free tokenizer-only whole-job estimate")
    measure.add_argument("--data", type=Path, default=RUNTIME / "data")
    measure.add_argument("--out", type=Path, default=RUNTIME / "budget-plan.json")
    run = sub.add_parser("train", help="one approved native training run; no paid retries")
    run.add_argument("--plan", type=Path, default=RUNTIME / "budget-plan.json")
    run.add_argument("--confirm-spend", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "plan":
            result = make_plan(args.data)
            write_json(args.out, result)
        else:
            result = train(args.plan, confirm_spend=args.confirm_spend)
    except (TraceError, FileExistsError) as exc:
        print(f"error: {exc}")
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
