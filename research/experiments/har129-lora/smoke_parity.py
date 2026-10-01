#!/usr/bin/env python3
"""HAR-129 serving smoke: prompts in, scores out.

``prompts``: the exact first user message (the Terminus-2 prompt) of the
given recorded trajectories, written as the input of
``tools/modal-mimo-serve/lora_smoke.py``.

``score``: for each prompt's raw responses,
- validity: a reply is valid Terminus-2 when its content is a Terminus JSON
  object (``json``) or when the lab's normalizer
  (``evallab.mimo_tool_calls.normalize_mimo_tool_calls``, the lf2 path) turns
  its native tool calls into one (``normalized``);
- parity: the LoRA server's base name against production, content and
  reasoning compared byte for byte at temperature 0;
- whether the adapter name answers differently from the base name.

Usage (worktree root)::

    uv run python research/experiments/har129-lora/smoke_parity.py prompts OUT.json TRAJ...
    uv run python research/experiments/har129-lora/smoke_parity.py score RAW.json OUT.json
"""

from __future__ import annotations

import difflib
import json
import sys
from pathlib import Path
from typing import Any

from evallab.mimo_tool_calls import normalize_mimo_tool_calls

TERMINUS_KEYS = {"analysis", "plan", "commands", "task_complete"}
ARMS = ("lora_base", "lora_adapter", "production")


def prompts(out: Path, trajectories: list[Path]) -> None:
    rows = []
    for trajectory in trajectories:
        steps = json.loads(trajectory.read_text())["steps"]
        prompt = next(step["message"] for step in steps if step["source"] == "user")
        rows.append({"source": str(trajectory), "prompt": prompt})
    out.write_text(json.dumps(rows, indent=1) + "\n")
    print(f"{len(rows)} prompts -> {out}")


def terminus_validity(content: str) -> str:
    try:
        value = json.loads(content)
    except ValueError:
        value = None
    if isinstance(value, dict) and set(value) <= TERMINUS_KEYS and "commands" in value:
        return "json"
    rewritten = normalize_mimo_tool_calls(content)
    if rewritten is not None:
        try:
            if isinstance(json.loads(rewritten), dict):
                return "normalized"
        except ValueError:
            pass
    return "invalid"


def first_difference(a: str, b: str) -> dict[str, Any] | None:
    if a == b:
        return None
    common = zip(a, b, strict=False)  # the strings may differ in length
    index = next((i for i, (x, y) in enumerate(common) if x != y), min(len(a), len(b)))
    return {
        "char": index,
        "lengths": [len(a), len(b)],
        "similarity": round(difflib.SequenceMatcher(None, a, b).ratio(), 3),
        "lora_base": a[max(0, index - 60) : index + 60],
        "production": b[max(0, index - 60) : index + 60],
    }


def score(raw: Path, out: Path) -> None:
    rows = []
    for item in json.loads(raw.read_text()):
        base, tuned, prod = (item[arm] for arm in ARMS)
        row = {
            "source": item["source"],
            "validity": {arm: terminus_validity(item[arm]["content"]) for arm in ARMS},
            "finish_reason": {arm: item[arm]["finish_reason"] for arm in ARMS},
            "content_identical": base["content"] == prod["content"],
            "reasoning_identical": base["reasoning"] == prod["reasoning"],
            "content_diff": first_difference(base["content"], prod["content"]),
            "reasoning_diff": first_difference(base["reasoning"], prod["reasoning"]),
            "adapter_differs_from_base": (tuned["content"], tuned["reasoning"])
            != (base["content"], base["reasoning"]),
        }
        rows.append(row)
        print(json.dumps({k: v for k, v in row.items() if not k.endswith("_diff")}))
    out.write_text(json.dumps(rows, indent=1) + "\n")


if __name__ == "__main__":
    command, target, *rest = sys.argv[1:]
    if command == "prompts":
        prompts(Path(target), [Path(p) for p in rest])
    else:
        score(Path(target), Path(rest[0]))
