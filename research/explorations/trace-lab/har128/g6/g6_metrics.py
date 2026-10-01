"""HAR-128 G6: deterministic per-trial behaviour metrics on arm-blind ids (G6_PLAN.md).

Reads the sealed arm map only to locate each trial folder; the output carries the
opaque id and the metrics, never the arm or the trial name.

    uv run python research/explorations/trace-lab/har128/g6/g6_metrics.py \
        --map ~/Developer/eval-lab/derived/trace-lab/har128-g6/SEALED_arm_map.json \
        --results ~/Developer/eval-lab-results/2026-10-01 \
        --out research/explorations/trace-lab/har128/g6/metrics_blind.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evallab.token_flow import _is_edit, _is_ephemeral_only, _touched_paths


def first_edit_step(trial_dir: Path) -> int | None:
    """First agent step that edits the repo: the last_useful_edit rule scanned forward."""
    steps = json.loads((trial_dir / "agent" / "trajectory.json").read_text())["steps"]
    for step in (s for s in steps if s.get("source") == "agent"):
        is_edit, _ = _is_edit(step)
        if is_edit and not _is_ephemeral_only(_touched_paths(step)):
            step_id = step.get("step_id")
            return step_id if isinstance(step_id, int) else None
    return None


def metrics(trial_dir: Path) -> dict[str, Any]:
    processed = next(trial_dir.parent.glob("processed/trial-*.json"))
    p = json.loads(processed.read_text())
    flow = p.get("token_flow") or {}
    hs = p.get("handshake") or {}
    after = flow.get("tokens_after_last_edit") or {}
    result = json.loads((trial_dir / "result.json").read_text())
    loop_break = ((result.get("agent_result") or {}).get("metadata") or {}).get("loop_break") or {}
    return {
        "verdict": (p.get("counts") or {}).get("verdict"),
        "reward": ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward"),
        "stop_reason": p.get("stop_reason"),
        "loop_break_fired": bool(loop_break.get("fired")),
        "loop_break_stopped": loop_break.get("stop_call") is not None,
        "handshake_present": bool(hs),
        "handshake_confirmed": hs.get("confirmed") if hs else None,
        "turns_after_first_prompt": hs.get("turns_after_first_prompt") if hs else None,
        "echo_task_complete_turns": hs.get("echo_task_complete_turns") if hs else None,
        "loop_onset": (flow.get("loop_onset") or {}).get("step_id") is not None,
        "loop_suspicion_score": (p.get("loop_suspicion") or {}).get("score"),
        "unparseable": (p.get("shape_counts") or {}).get("unparseable", 0),
        "rejection_causes": p.get("rejection_causes") or {},
        "first_edit_step": first_edit_step(trial_dir),
        "tokens_after_last_edit_input": after.get("input_tokens"),
        "tokens_total_proxy": (p.get("tokens_proxy") or {}).get("total_tokens"),
        "agent_steps": p.get("agent_steps"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", type=Path, required=True)
    ap.add_argument("--results", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    arm_map = json.loads(args.map.read_text())
    rows = []
    for oid in sorted(arm_map):
        trial = arm_map[oid]["trial"]
        (trial_dir,) = args.results.glob(f"*/{trial}")
        rows.append({"id": oid, "task": arm_map[oid]["task"], **metrics(trial_dir)})
    args.out.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    print(f"wrote {len(rows)} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
