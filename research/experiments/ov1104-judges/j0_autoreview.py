"""J0: the deployed AutoReview reading for every replay-v1 run, in the judge result format.

Cached readings (``replay-v1/inputs/cached-autoreview``) are reused as-is. Runs AutoReview never
read (L0 made them ineligible, 300 runs) get the lab's own stage-1 prompt
(``evallab.autoreview_monitor.stage1_prompt`` on ``build_compact_view``) through the $0 gateway,
same model as the cache, so J0 covers the whole set with the deployed rubric.

Usage::

    uv run python research/experiments/ov1104-judges/j0_autoreview.py \\
        --runs /private/tmp/ov1104/judges/dev/runs.jsonl --out /private/tmp/ov1104/judges/out
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gateway import Caller, Gateway, summarize_log  # noqa: E402
from judge import slug  # noqa: E402

from evallab import hack_drop as hd  # noqa: E402
from evallab.autoreview_monitor import (  # noqa: E402
    build_anatomy,
    build_compact_view,
    parse_verdict,
    stage1_prompt,
)

REPLAY = Path.home() / "Developer/eval-lab-results/line/replay-v1/inputs"
MODEL = "google-antigravity/gemini-3.8-flash"


def cached_readings(cache_dir: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name in ("kept.jsonl", "dropped.jsonl", "review.jsonl"):
        for line in (cache_dir / name).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            stage = (row.get("stage_decisions") or {}).get("autoreview") or {}
            details = stage.get("details") or {}
            if "suspicion" in details:
                out[row["id"]] = {
                    "suspicion": float(details["suspicion"]), "verdict": details.get("verdict"), "model": details.get("model"),
                    "attested": details.get("attested"), "evidence_steps": list(row.get("evidence_steps") or []),
                    "reasons": list(stage.get("reasons") or []),
                }
    return out


async def main_async(args: argparse.Namespace) -> None:
    rows = [json.loads(line) for line in Path(args.runs).read_text(encoding="utf-8").splitlines() if line.strip()]
    cache = cached_readings(args.replay / "cached-autoreview")
    out_dir = Path(args.out) / f"j0__{slug(MODEL)}"
    results_dir = out_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    items: dict[str, Any] = {}
    for corpus in sorted({row["set"] for row in rows}):
        manifest = args.replay / f"corpora/{corpus}-manifest.jsonl"
        by_path = {str(Path(json.loads(line)["path"]).resolve()): json.loads(line)["id"] for line in manifest.read_text().splitlines() if line.strip()}
        for item in hd.load_rollouts([manifest]):
            items[f"{corpus}:{by_path[str(Path(item.source_path).resolve())]}"] = item
    caller = Caller(out_dir / "calls.jsonl", concurrency=args.concurrency, max_tokens=1200)
    n_cached = n_called = 0
    todo = []
    for row in rows:
        native = row["run_key"].split(":", 1)[1]
        out_path = results_dir / (slug(row["run_key"]) + ".json")
        base = {"run_key": row["run_key"], "set": row["set"], "judge": "j0", "model": MODEL, "score": None, "abstain": None,
                "verdict": None, "mechanism": None, "evidence_steps": [], "windows": []}
        reading = cache.get(native)
        if reading is not None:
            base.update({"score": reading["suspicion"], "verdict": reading["verdict"], "evidence_steps": reading["evidence_steps"],
                         "source": "cached-autoreview", "cached_model": reading["model"], "cached_attested": reading["attested"]})
            out_path.write_text(json.dumps(base, ensure_ascii=False, indent=1), encoding="utf-8")
            n_cached += 1
            continue
        if out_path.is_file() and json.loads(out_path.read_text()).get("score") is not None and not args.force:
            continue
        item = items.get(row["run_key"])
        if item is None or item.run is None:
            base["abstain"] = "no trajectory parsed"
            out_path.write_text(json.dumps(base, ensure_ascii=False, indent=1), encoding="utf-8")
            continue
        todo.append((row, item, base, out_path))
    print(f"j0: {len(rows)} rows, {n_cached} cached, {len(todo)} stage-1 calls", flush=True)

    async def one(row: Mapping[str, Any], item: Any, base: dict[str, Any], out_path: Path) -> None:
        nonlocal n_called
        try:
            run = hd._normalize_legacy_run(item.run)
            view = build_compact_view(build_anatomy(run), run)
            prompt = stage1_prompt(view)
        except Exception as exc:
            base["abstain"] = f"compact view failed: {type(exc).__name__}: {exc}"[:300]
            out_path.write_text(json.dumps(base, ensure_ascii=False, indent=1), encoding="utf-8")
            return
        reply = await caller.call(MODEL, prompt, tag={"judge": "j0", "run_key": row["run_key"], "set": row["set"], "window": 0, "n_windows": 1})
        parsed = None if reply["error"] else parse_verdict(reply["text"])
        window = {"window": 0, "prompt_sha256": reply["prompt_sha256"], "prompt_chars": reply["prompt_chars"], "model_served": reply["model_served"],
                  "usage": reply["usage"], "latency_s": reply["latency_s"], "error": reply["error"] or (None if parsed else "unparseable reply"),
                  "parsed": parsed, "text": reply["text"]}
        base["windows"] = [window]
        base["source"] = "stage1-live"
        if parsed is None:
            base["abstain"] = window["error"]
        else:
            base["score"] = float(parsed["suspicion"])
            base["verdict"] = parsed["verdict"]
            base["evidence_steps"] = parsed["cited_steps"][:5]
        out_path.write_text(json.dumps(base, ensure_ascii=False, indent=1), encoding="utf-8")
        n_called += 1
        if n_called % 25 == 0:
            print(f"  {n_called}/{len(todo)}", flush=True)

    await asyncio.gather(*(one(*entry) for entry in todo))
    print(json.dumps(summarize_log(out_dir / "calls.jsonl"), indent=1))
    if Gateway._current is not None:
        Gateway._current.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--replay", type=Path, default=REPLAY)
    parser.add_argument("--concurrency", type=int, default=6)
    parser.add_argument("--force", action="store_true")
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
