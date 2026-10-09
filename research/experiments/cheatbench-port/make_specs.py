#!/usr/bin/env python3
"""Write one queue spec per (model, CheatBench task package) for the ported families.

Usage:
  uv run --no-sync python research/experiments/cheatbench-port/make_specs.py \
      --model zai/glm-5.3-flash --family subset_sum --ids 4 5 6 --out queue-specs/

Specs follow the standard ExperimentSpec contract (Terminus-2 on local Docker,
metered host-side proxy with per-trial ceilings). Paid specs still need an
`evallab approve` record before `evallab tick` dispatches them.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from evallab.registry import compute_task_digests  # noqa: E402
from evallab.schemas import ExperimentSpec  # noqa: E402

BENCH = ROOT / "library/benchmarks/cheatbench"
MODEL_SHORT = {
    "zai/glm-5.3-flash": "glm53f",
    "zai/glm-5.3": "glm53",
    "tinker/Qwen/Qwen3.6-35B-A3B": "qwen36a3b",
    "tinker/Qwen/Qwen3.8-27B": "qwen38",
    "tinker/Qwen/Qwen3.5-9B": "qwen35",
}
FAMILY_SHORT = {"subset_sum": "ss", "prime_factorization": "pf", "openmath_agent": "oma"}
HYPOTHESIS = (
    "Under CheatBench's original implicit-honesty framing, an open model on "
    "Terminus-2 takes the planted shortcut at a measurable rate; cheating is "
    "judged offline from the trajectory, separately from task success."
)


def task_dirs(family: str, ids: list[str] | None) -> list[Path]:
    root = BENCH / family / "tasks"
    dirs = sorted(d for d in root.iterdir() if (d / "task.toml").is_file())
    if ids:
        def inst_of(d: Path) -> str:
            return d.name.removeprefix(f"{family}-").removeprefix("openmath-agent-")

        dirs = [d for d in dirs
                if inst_of(d) in ids or (family != "openmath_agent"
                                         and inst_of(d).rsplit("_", 1)[-1] in ids)]
    if not dirs:
        raise SystemExit(f"no task packages matched under {root}")
    return dirs


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True, choices=sorted(MODEL_SHORT))
    p.add_argument("--family", required=True, choices=sorted(FAMILY_SHORT))
    p.add_argument("--ids", nargs="*", help="instance ids (suffix of task dir name)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--cost-limit-usd", type=float, required=True)
    p.add_argument("--est-cost-usd", type=float, required=True)
    p.add_argument("--max-requests", type=int, default=80)
    p.add_argument("--max-input-tokens", type=int, default=3_000_000)
    p.add_argument("--max-output-tokens", type=int, default=200_000)
    p.add_argument("--max-total-tokens", type=int, default=3_200_000)
    p.add_argument("--timeout-seconds", type=int, default=1800)
    p.add_argument("--tag", default="r1", help="cohort tag, part of the spec name")
    p.add_argument("--extra-instruction", type=Path,
                   help="repo-relative file appended to the task prompt (paper §4 reminder ablation)")
    args = p.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    for d in task_dirs(args.family, args.ids):
        inst = d.name.removeprefix(f"{args.family}-").removeprefix("openmath-agent-")
        short = inst.rsplit("_", 1)[-1] if args.family != "openmath_agent" else inst
        name = f"cb-{FAMILY_SHORT[args.family]}-{short}-{MODEL_SHORT[args.model]}-{args.tag}"
        name = name.lower().replace("_", "-")[:80]
        extra = {}
        if args.extra_instruction is not None:
            text = (ROOT / args.extra_instruction).read_bytes()
            extra = {
                "extra_instruction_path": str(args.extra_instruction),
                "extra_instruction_sha256": "sha256:" + hashlib.sha256(text).hexdigest(),
            }
        spec = ExperimentSpec(
            name=name,
            hypothesis=HYPOTHESIS if not extra else (
                "Appending CheatBench's explicit reminder to the original prompt lowers, "
                "but does not eliminate, the rate at which the model takes the honeypot."),
            purpose="baseline" if not extra else "comparison",
            **extra,
            question_ref="cheatbench-port: how often do open models take CheatBench honeypots",
            task=str(d.relative_to(ROOT)),
            task_package_digest=compute_task_digests(d).package,
            agent="terminus-2",
            model=args.model,
            environment="docker",
            jobs_dir="runs",
            attempts=1,
            timeout_seconds=args.timeout_seconds,
            max_requests=args.max_requests,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
            max_total_tokens=args.max_total_tokens,
            cost_limit_usd=args.cost_limit_usd,
            est_cost_usd=args.est_cost_usd,
            submitted_by="omp-cheatbench-port",
            task_family=f"cheatbench/{args.family}",
            task_instance_id=inst,
        )
        path = args.out / f"{name}.json"
        path.write_text(spec.model_dump_json(indent=2, exclude_none=True) + "\n")
        print(path)


if __name__ == "__main__":
    main()
