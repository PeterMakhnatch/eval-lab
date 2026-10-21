#!/usr/bin/env python3
"""Generation-budget sensitivity of the seeker-gym generator (pE-SELF-8).

`seeker-gym-v0`'s generator asks one OpenRouter model for a whole submission as a
single JSON object with `max_tokens: 1200`, and its v0 run recorded `14/32`
candidates as `unparsed_generation`. This measures the same prompt and the same
tasks at several budgets to separate "the model cannot produce a submission" from
"the budget cut the JSON off".

It reuses the gym's own `PROMPT`, `task_view` and `MODELS` by importing
`seeker_gym`, and re-implements only the request body so `max_tokens` is a
parameter (the gym hardcodes it).

Usage:
    keys run -- .venv/bin/python research/experiments/seeker-corpus-audit-20261021/gym_budget_probe.py \
        --budgets 1200,3072 --k 8 --tasks atrx-vep-crispr,cad-model,cargo-flight-dispatch
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import urllib.request
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[2]
GYM_DIR = REPO / "research/experiments/seeker-gym-v0"
sys.path.insert(0, str(GYM_DIR))

import seeker_gym  # noqa: E402


def _generate(
    model_id: str, provider: dict[str, Any], prompt: str, key: str, max_tokens: int
) -> tuple[dict[str, Any] | None, float, int, str]:
    body = {
        "model": model_id,
        "provider": provider,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.9,
        "max_tokens": max_tokens,
        "usage": {"include": True},
        "reasoning": {"effort": "medium", "exclude": True},
    }
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        data = json.loads(response.read())
    content = data["choices"][0]["message"].get("content") or ""
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content)
    cost = (data.get("usage") or {}).get("cost", 0.0)
    finish = str(data["choices"][0].get("finish_reason") or "")
    try:
        parsed = json.loads(content[content.index("{") : content.rindex("}") + 1])
    except Exception:
        parsed = None
    return parsed, cost, len(content), finish


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="gpt-oss-120b", choices=sorted(seeker_gym.MODELS))
    parser.add_argument("--budgets", default="1200,3072")
    parser.add_argument("--k", type=int, default=8)
    parser.add_argument("--tasks", default="atrx-vep-crispr,cad-model,cargo-flight-dispatch")
    parser.add_argument(
        "--grade", action="store_true", help="grade parsed candidates with local Docker"
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--run-root",
        type=pathlib.Path,
        default=pathlib.Path.home() / "Developer/eval-lab-results/2026-10-21/seeker-gym-probe",
        help="Docker evidence root; kept outside the repo (each graded run is ~40 MB)",
    )
    parser.add_argument("--out", type=pathlib.Path, default=HERE / "gym-budget-probe.jsonl")
    args = parser.parse_args()

    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is not set; run under `keys run --`")

    gym = json.loads((GYM_DIR / "gym.json").read_text(encoding="utf-8"))
    wanted = [name for name in args.tasks.split(",") if name]
    pairs = [
        pair
        for pair in gym["pairs"]
        if pair["task"] in wanted and pair.get("runnable") and pair["family"] == "tb4-dev"
    ]
    model_id, provider = seeker_gym.MODELS[args.model]
    budgets = [int(value) for value in args.budgets.split(",") if value]

    rows: list[dict[str, Any]] = []
    for budget in budgets:
        for pair in pairs:
            prompt = seeker_gym.task_view(pathlib.Path(pair["original_task"]))
            for index in range(args.k):
                parsed, cost, chars, finish = _generate(model_id, provider, prompt, key, budget)
                row = {
                    "budget": budget,
                    "pair_id": pair["pair_id"],
                    "task": pair["task"],
                    "k": index,
                    "parsed": parsed is not None,
                    "has_solve_sh": bool(parsed and "solve.sh" in (parsed.get("files") or {})),
                    "content_chars": chars,
                    "finish_reason": finish,
                    "gen_cost_usd": cost,
                    "files": (parsed or {}).get("files") if parsed else None,
                    "mechanism": (parsed or {}).get("mechanism") if parsed else None,
                }
                if args.grade and row["has_solve_sh"]:
                    from evallab.candidate_runner import run_candidate

                    run_dir = args.run_root / f"{pair['task']}-{budget}-{index}"
                    upstream = run_candidate(
                        pathlib.Path(pair["original_task"]),
                        files=row["files"],
                        grader="upstream",
                        backend="docker",
                        timeout_seconds=args.timeout,
                        output_dir=run_dir / "up",
                    )
                    row["upstream"] = {
                        "status": upstream.get("status"),
                        "reward": upstream.get("reward"),
                    }
                    row["reward"] = 0
                    if upstream.get("reward") == 1.0:
                        fixed = run_candidate(
                            pathlib.Path(pair["original_task"]),
                            files=row["files"],
                            grader="port",
                            port_task=pathlib.Path(pair["fixed_task"]).resolve(),
                            backend="docker",
                            timeout_seconds=args.timeout,
                            output_dir=run_dir / "port",
                        )
                        row["fixed"] = {
                            "status": fixed.get("status"),
                            "reward": fixed.get("reward"),
                        }
                        row["reward"] = 1 if fixed.get("reward") == 0.0 else 0
                rows.append(row)
                with args.out.open("a") as handle:
                    handle.write(json.dumps(row) + "\n")
                print(json.dumps(row), flush=True)

    by_budget: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = by_budget.setdefault(
            str(row["budget"]),
            {
                "candidates": 0,
                "parsed": 0,
                "with_solve_sh": 0,
                "graded": 0,
                "upstream_reward1": 0,
                "verified": 0,
                "cost_usd": 0.0,
                "finish_reasons": {},
            },
        )
        bucket["candidates"] += 1
        bucket["parsed"] += 1 if row["parsed"] else 0
        bucket["with_solve_sh"] += 1 if row["has_solve_sh"] else 0
        bucket["graded"] += 1 if row.get("upstream") else 0
        bucket["upstream_reward1"] += 1 if (row.get("upstream") or {}).get("reward") == 1.0 else 0
        bucket["verified"] += 1 if row.get("reward") == 1 else 0
        bucket["cost_usd"] = round(bucket["cost_usd"] + (row["gen_cost_usd"] or 0.0), 6)
        reason = row["finish_reason"] or "unknown"
        bucket["finish_reasons"][reason] = bucket["finish_reasons"].get(reason, 0) + 1
    print(json.dumps({"by_budget": by_budget}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
