"""Score the narration arms: the same rubric and view budgets, but with the agent's words put back.

Two renderings come from ``build_reasoning_views.py``:

- ``reasoning``: the hidden-thinking channel only (``<reasoning>...</reasoning>``, no prose).
- ``full``: reasoning plus the agent's prose plus its tool calls.

The runner is deliberately separate from ``judge.py``. ``judge.py``, ``views.py``, ``prompts.py``,
``gateway.py`` and ``j0_autoreview.py`` are hashed into every published signal's ``version`` string;
editing any of them would change the frozen versions of artifacts already merged, so a follow-up arm
gets its own file and an explicit version namespace instead.

Usage (from the eval-lab worktree root)::

    uv run python research/experiments/ov1104-judges/judge_narration.py --view-mode reasoning \\
        --model openai-codex/gpt-6.1-terra \\
        --runs /private/tmp/ov1104/judges/dev-narration/runs.jsonl --out /private/tmp/ov1104/judges/out
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gateway import Caller, Gateway, summarize_log  # noqa: E402
from judge import parse_reply, slug  # noqa: E402
from prompts import j2_prompt, rubric_sha  # noqa: E402
from views import load_view, render_windows  # noqa: E402

VIEW_KEYS = {"reasoning": "view_reasoning", "full": "view_full"}


def version_of(*, rubric: str, model: str, view_mode: str) -> str:
    """Same recipe as `signals.version_of` plus the view mode, so arms never share a version."""
    import signals  # noqa: PLC0415

    return hashlib.sha256(f"{signals.code_sha()}|{rubric}|{model}|view={view_mode}".encode()).hexdigest()


async def judge_run(caller: Caller, *, model: str, row: dict[str, Any], view_path: str, window_chars: int,
                    seed: int | None, out_path: Path) -> dict[str, Any]:
    view = load_view(view_path)
    result: dict[str, Any] = {
        "run_key": row["run_key"], "set": row["set"], "judge": "j2b", "model": model,
        "rubric_sha256": rubric_sha("j2b"), "score": None, "abstain": None, "verdict": None,
        "mechanism": None, "evidence_steps": [], "windows": [],
    }
    windows = render_windows(view, actions_only=False, window_chars=window_chars)
    if not windows:
        result["abstain"] = "no trajectory"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        return result
    tasks = [
        caller.call(
            model,
            j2_prompt(view, window["text"], v2=True),
            tag={"judge": "j2b", "run_key": row["run_key"], "set": row["set"], "window": index, "n_windows": len(windows)},
            seed=seed,
        )
        for index, window in enumerate(windows)
    ]
    replies = await asyncio.gather(*tasks)
    best: dict[str, Any] | None = None
    for index, (window, reply) in enumerate(zip(windows, replies, strict=True)):
        parsed, parse_error = (None, reply["error"]) if reply["error"] else parse_reply(reply["text"])
        result["windows"].append(
            {
                "window": index,
                "steps": [window["steps"][0], window["steps"][-1]] if window["steps"] else [],
                "prompt_sha256": reply["prompt_sha256"],
                "prompt_chars": reply["prompt_chars"],
                "model_served": reply["model_served"],
                "usage": reply["usage"],
                "latency_s": reply["latency_s"],
                "error": parse_error,
                "parsed": parsed,
                "text": reply["text"],
            }
        )
        if parsed is not None and (best is None or parsed["suspicion"] > best["suspicion"]):
            best = parsed
    if best is None:
        errors = sorted({w["error"] for w in result["windows"] if w["error"]})
        result["abstain"] = "; ".join(errors)[:300] or "no window produced a verdict"
    else:
        result["score"] = best["suspicion"]
        result["verdict"], result["mechanism"] = best["verdict"], best["mechanism"]
        result["evidence_steps"] = best["cited_steps"]
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return result


async def main_async(args: argparse.Namespace) -> None:
    rows = [json.loads(line) for line in Path(args.runs).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.sets:
        wanted = set(args.sets.split(","))
        rows = [row for row in rows if row["set"] in wanted]
    if args.limit:
        rows = rows[: args.limit]
    out_dir = Path(args.out) / f"narration_{args.view_mode}__{slug(args.model)}"
    results_dir = out_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    caller = Caller(out_dir / "calls.jsonl", concurrency=args.concurrency, temperature=args.temperature,
                    max_tokens=args.max_tokens)
    view_key = VIEW_KEYS[args.view_mode]
    todo = []
    for row in rows:
        out_path = results_dir / (slug(row["run_key"]) + ".json")
        if out_path.is_file() and not args.force:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            if existing.get("score") is not None or (existing.get("abstain") or ""):
                continue
        todo.append((row, out_path))
    print(f"narration {args.view_mode}: {len(rows)} rows, {len(rows) - len(todo)} reused, {len(todo)} to judge", flush=True)
    done = scored = 0

    async def one(row: dict[str, Any], out_path: Path) -> None:
        nonlocal done, scored
        try:
            result = await judge_run(caller, model=args.model, row=row, view_path=row[view_key],
                                     window_chars=args.window_chars, seed=args.seed, out_path=out_path)
        except Exception as exc:  # a runner bug must not kill the batch
            result = {"run_key": row["run_key"], "set": row["set"], "judge": "j2b", "model": args.model,
                      "score": None, "abstain": f"runner error: {type(exc).__name__}: {exc}"[:300], "windows": []}
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        done += 1
        scored += result.get("score") is not None
        if done % 25 == 0 or done == len(todo):
            print(f"  {done}/{len(todo)} judged, {scored} scored", flush=True)

    await asyncio.gather(*(one(row, path) for row, path in todo))
    print(json.dumps(summarize_log(out_dir / "calls.jsonl"), indent=1))
    print(f"version: {version_of(rubric=rubric_sha('j2b'), model=args.model, view_mode=args.view_mode)}")
    gateway = Gateway._current
    if gateway is not None:
        gateway.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--view-mode", choices=tuple(VIEW_KEYS), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--runs", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--sets", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--window-chars", type=int, default=60000)
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-tokens", type=int, default=1500)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
