"""Render a DetBench-format view to text (CONTRACT.md "Rendering a view to text").

Pure Python, deterministic. `render_view(view)` returns the rendered text and,
for each step (in order), its (char_start, char_end) span in that text, so a
token window can be mapped back to a step range.

Layout (blocks joined by a single "\n"):
    [TASK]\n<task_text[:3000]>                 (only when task_text present)
    [STEP i ROLE]\n<text>\nTOOL name: args\n<observation>   (per step)
    [FINAL diff]\n<diff[:3000]>                (only when final.diff present)
A step's span starts at its "[STEP" header and ends at the end of its last
line (exclusive, before the joining "\n").
"""
from __future__ import annotations

import json
import sys

TASK_MAX = 3000
ARGS_MAX = 2000
OBS_MAX = 1500
OBS_HEAD = 1000
OBS_TAIL = 500
DIFF_MAX = 3000


def _s(x) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    return json.dumps(x, ensure_ascii=False, sort_keys=True)


def _trunc_obs(obs: str) -> str:
    if len(obs) <= OBS_MAX:
        return obs
    return obs[:OBS_HEAD] + " ... " + obs[-OBS_TAIL:]


def render_step(step: dict) -> str:
    role = _s(step.get("role")) or "unknown"
    lines = [f"[STEP {int(step['i'])} {role.upper()}]"]
    text = _s(step.get("text"))
    if text:
        lines.append(text)
    for tc in step.get("tool_calls") or []:
        name = _s(tc.get("name")) or "unknown"
        args = _s(tc.get("args"))[:ARGS_MAX]
        lines.append(f"TOOL {name}: {args}")
    obs = _s(step.get("observation"))
    if obs:
        lines.append(_trunc_obs(obs))
    return "\n".join(lines)


def render_view(view: dict) -> tuple[str, list[tuple[int, int]]]:
    blocks: list[str] = []
    task_text = _s(view.get("task_text"))
    if task_text:
        blocks.append("[TASK]\n" + task_text[:TASK_MAX])
    steps = view.get("steps") or []
    step_block_idx: list[int] = []
    for step in steps:
        step_block_idx.append(len(blocks))
        blocks.append(render_step(step))
    final = view.get("final") or {}
    diff = _s(final.get("diff"))
    if diff:
        blocks.append("[FINAL diff]\n" + diff[:DIFF_MAX])

    # compute char offsets of each block in the joined text
    starts: list[int] = []
    pos = 0
    for b in blocks:
        starts.append(pos)
        pos += len(b) + 1  # "\n" joiner
    text = "\n".join(blocks)
    spans = [(starts[k], starts[k] + len(blocks[k])) for k in step_block_idx]
    return text, spans


def step_ids(view: dict) -> list[int]:
    return [int(s["i"]) for s in (view.get("steps") or [])]


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: render.py <view.json>", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], encoding="utf-8") as f:
        view = json.load(f)
    text, spans = render_view(view)
    print(json.dumps({
        "run_key": view.get("run_key"),
        "chars": len(text),
        "steps": len(spans),
        "lines": text.count("\n") + 1,
        "has_task": bool(_s(view.get("task_text"))),
        "has_diff": bool(_s((view.get("final") or {}).get("diff"))),
        "first_span": spans[0] if spans else None,
        "last_span": spans[-1] if spans else None,
    }))
