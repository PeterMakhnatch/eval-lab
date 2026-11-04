"""Build the two narration views this lane's main arms deliberately exclude, for the DEV corpora.

`views.atif_to_view` drops every reasoning key on purpose: the lane's thesis is that a reader which
sees the agent's own account follows it. This script keeps that default and adds two *additional*
view trees, so the claim can be tested instead of assumed:

- ``reasoning``: each agent step carries only ``<reasoning>...</reasoning>``, no prose, no tool calls
  are removed (they stay in ``tool_calls``), so the rendering is the hidden-thinking channel alone.
- ``full``: reasoning prefix plus the agent's prose, plus tool calls.

Both trees keep the same step indices and the same task text as the main views, so a run scored in one
arm is directly comparable with the same run scored in another. Labels never enter a view.

Usage (from the eval-lab worktree root)::

    uv run python research/experiments/ov1104-judges/build_reasoning_views.py --out /private/tmp/ov1104/judges/dev-narration
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from views import atif_to_view  # noqa: E402

from evallab import hack_drop as hd  # noqa: E402

REPLAY = Path.home() / "Developer/eval-lab-results/line/replay-v1/inputs"
CORPORA = ("goodfire", "cheatbench", "terminal-wrench", "c1c5", "sol-astra")
REASONING_KEYS = ("reasoning_content", "reasoning", "thinking")


def reasoning_of(step: Mapping[str, Any]) -> str:
    for key in REASONING_KEYS:
        value = step.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            joined = "\n".join(str(part) for part in value if isinstance(part, str))
            if joined.strip():
                return joined
    return ""


def narration_view(base: dict[str, Any], raw_steps: Iterable[Mapping[str, Any]], *, mode: str) -> dict[str, Any]:
    """Copy of `base` with reasoning (and, in `full` mode, prose) put back into the step text."""
    steps = list(base["steps"])
    raw = list(raw_steps)
    if len(raw) != len(steps):
        raise SystemExit(f"{base['run_key']}: {len(raw)} raw steps but {len(steps)} view steps")
    for step, raw_step in zip(steps, raw, strict=True):
        reasoning = reasoning_of(raw_step)
        prose = step.get("text") or "" if step.get("role") == "agent" else ""
        parts = []
        if reasoning:
            parts.append(f"<reasoning>{reasoning}</reasoning>")
        if mode == "full" and prose:
            parts.append(prose)
        step["text"] = "\n".join(parts) if parts else None
    return {**base, "view_mode": mode}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--replay", type=Path, default=REPLAY)
    parser.add_argument("--corpora", default=",".join(CORPORA))
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    for corpus in [c for c in args.corpora.split(",") if c]:
        manifest_path = args.replay / f"corpora/{corpus}-manifest.jsonl"
        manifest = [json.loads(line) for line in manifest_path.read_text().splitlines() if line.strip()]
        by_path = {str(Path(row["path"]).resolve()): row for row in manifest}
        for item in hd.load_rollouts([manifest_path]):
            row = by_path.get(str(Path(item.source_path).resolve()))
            if row is None or item.run is None:
                continue
            native_id = row["id"]
            run_key = f"{corpus}:{native_id}"
            stem = native_id.replace("/", "__").replace(":", "__") + ".json"
            steps = [s for s in item.run.doc.get("steps") or [] if isinstance(s, Mapping)]
            base = atif_to_view(run_key, steps, diff=item.diff_text, reward=None, task_text=item.run.prompt)
            n_reasoning = sum(1 for s in steps if reasoning_of(s))
            paths = {}
            for mode in ("reasoning", "full"):
                target = args.out / "views" / mode / corpus / stem
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(json.dumps(narration_view(base, steps, mode=mode), ensure_ascii=False), encoding="utf-8")
                paths[mode] = str(target)
            rows.append(
                {
                    "run_key": run_key,
                    "set": corpus,
                    "split": "dev",
                    "native_id": native_id,
                    "cluster": str(row.get("task_id") or item.task_id),
                    "n_steps": len(steps),
                    "n_steps_with_reasoning": n_reasoning,
                    "view_reasoning": paths["reasoning"],
                    "view_full": paths["full"],
                }
            )
    args.out.mkdir(parents=True, exist_ok=True)
    with (args.out / "runs.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with_reasoning = sum(1 for r in rows if r["n_steps_with_reasoning"])
    print(f"{len(rows)} runs ({with_reasoning} with reasoning) -> {args.out / 'runs.jsonl'}")
    print(f"  goodfire runs with reasoning: {sum(1 for r in rows if r['set'] == 'goodfire' and r['n_steps_with_reasoning'])}")


if __name__ == "__main__":
    main()
