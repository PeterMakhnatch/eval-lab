"""HAR-119 part 1: completion-claim loops vs genuine repetition ($0, no model).

For each of the 38 Python runs (HAR-104's 10 + HAR-110's 28), join HAR-114's
token_flow row (loop onset, last useful edit) with a per-step read of the
stitched trajectory (probe-03 assembly).

Rules (frozen before the per-run table was written; a first draft keyed
"confirmed" off HAR-114's stop field, which is null for confirmed runs, so the
claim phase no longer depends on the final confirmation):

- claim phase: agent steps after the harness's first "Are you sure you want
  to mark the task as complete?" prompt (the model said it was done; the
  harness carried on). This is the HAR-96 shape. "strict" tokens count only
  claim-bearing steps in that phase; "broad" counts every step in it.
- post-edit: agent steps after HAR-114's last useful edit (all steps when
  HAR-114 found no edit).
- claim-bearing step: the recorded harness layer says task_complete=true
  (prose_completion), or the message matches probe-03 COMPLETION_CLAIM_RE
  ("the task is complete"), ECHO_TASK_COMPLETE_RE, or mentions task_complete.

Loop kind per run:
- completion-claim: >= 10 agent turns after the first confirm prompt and
  >= 50% of them are claim-bearing;
- repetition: HAR-114 found a loop onset (normalized command run) and the
  run is not completion-claim;
- none: otherwise.

Usage: python loop_kind.py <HAR-114 runs.jsonl> <trial dirs file> <out dir>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "probe-03-capabilities"))
import capabilities as p03  # noqa: E402

TASK_COMPLETE_RE = re.compile(r"task_complete", re.IGNORECASE)
MIN_TURNS_AFTER_PROMPT = 10
MIN_CLAIM_SHARE = 0.5


def is_claim(step: dict) -> bool:
    layer = p03.layer_status(step) or {}
    if layer.get("task_complete") is True or layer.get("kind") == "prose_completion":
        return True
    message = str(step.get("message") or "")
    return bool(
        p03.COMPLETION_CLAIM_RE.search(message)
        or p03.ECHO_TASK_COMPLETE_RE.search(message)
        or TASK_COMPLETE_RE.search(message)
    )


def parse_error_turns(seq) -> int:
    return sum(1 for _d, s in seq if (p03.layer_status(s) or {}).get("kind") == "parse_error")


def analyse(trial_dir: Path, flow: dict, stop_reason: str | None) -> dict:
    _coverage, assembled = p03.assemble_trial(trial_dir)
    seq = [
        (doc, step)
        for doc, step in assembled
        if str(step.get("source", "")).lower() in p03.probe02.AGENT_SOURCES
    ]
    tf = flow["token_flow"]
    edit_step = (tf.get("last_useful_edit") or {}).get("step_id")
    loop_step = (tf.get("loop_onset") or {}).get("step_id")

    def index_of(step_id):
        if step_id is None:
            return None
        for index, (_doc, step) in enumerate(seq):
            sid = step.get("step_id")
            if isinstance(sid, int) and sid >= step_id:
                return index
        return len(seq)

    first_prompt = next(
        (
            i
            for i, (_d, s) in enumerate(seq)
            if p03.CONFIRM_PROMPT_RE.search(str(p03.obs_content(s) or ""))
        ),
        None,
    )
    edit_idx = index_of(edit_step)
    post_start = edit_idx + 1 if edit_idx is not None else 0

    total_in = post_in = broad_in = strict_in = post_claim_in = unmetered = 0
    for index, (_doc, step) in enumerate(seq):
        tokens, _ = p03._step_tokens(step)
        if tokens is None:
            unmetered += 1
            continue
        total_in += tokens
        in_phase = first_prompt is not None and index > first_prompt
        if in_phase:
            broad_in += tokens
            if is_claim(step):
                strict_in += tokens
        if index >= post_start:
            post_in += tokens
            if in_phase and is_claim(step):
                post_claim_in += tokens

    after = seq[first_prompt + 1 :] if first_prompt is not None else []
    claims_after = sum(1 for _d, s in after if is_claim(s))
    if len(after) >= MIN_TURNS_AFTER_PROMPT and claims_after / len(after) >= MIN_CLAIM_SHARE:
        kind = "completion-claim"
    elif loop_step is not None:
        kind = "repetition"
    else:
        kind = "none"

    return {
        "trial": flow["trial"],
        "source": flow["source"],
        "arm": flow["arm"],
        "task": flow.get("task"),
        "reward": flow.get("reward"),
        "stop": stop_reason,
        "n_agent_steps": len(seq),
        "last_edit_step": edit_step,
        "har114_loop_onset_step": loop_step,
        "har114_loop_detail": (tf.get("loop_onset") or {}).get("detail"),
        "first_confirm_prompt_step": seq[first_prompt][1].get("step_id")
        if first_prompt is not None
        else None,
        "turns_after_prompt": len(after),
        "claim_turns_after_prompt": claims_after,
        "echo_task_complete_turns": sum(
            1 for _d, s in seq if p03.ECHO_TASK_COMPLETE_RE.search(str(s.get("message") or ""))
        ),
        "loop_kind": kind,
        "input_total": total_in,
        "input_post_edit": post_in,
        "input_post_edit_in_claim_phase_strict": post_claim_in,
        "input_claim_phase_broad": broad_in,
        "input_claim_phase_strict": strict_in,
        "steps_without_metrics": unmetered,
        "parse_error_turns": parse_error_turns(seq),
    }


def main() -> int:
    runs_path, dirs_path, p03_path, out_dir = map(Path, sys.argv[1:5])
    flows = {
        row["trial"]: row
        for row in map(json.loads, runs_path.read_text().splitlines())
        if row["source"] in {"HAR-104", "HAR-110"}
    }
    stops = {
        row["trial"]: (row.get("stop") or {}).get("reason")
        for row in map(json.loads, p03_path.read_text().splitlines())
    }
    dirs = [Path(line) for line in dirs_path.read_text().split() if line]
    rows = [analyse(d, flows[d.name], stops.get(d.name)) for d in dirs]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "loop_kind.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    looped = [r for r in rows if r["loop_kind"] != "none"]
    claim = [r for r in looped if r["loop_kind"] == "completion-claim"]
    post = sum(r["input_post_edit"] for r in rows)
    post_claim = sum(r["input_post_edit_in_claim_phase_strict"] for r in rows)
    summary = {
        "runs": len(rows),
        "looped": len(looped),
        "completion_claim_loops": len(claim),
        "share_of_looped_runs": round(len(claim) / len(looped), 3) if looped else None,
        "input_total": sum(r["input_total"] for r in rows),
        "input_post_edit": post,
        "input_post_edit_claim_strict": post_claim,
        "share_post_edit_claim_strict": round(post_claim / post, 3) if post else None,
        "input_claim_phase_broad": sum(r["input_claim_phase_broad"] for r in rows),
        "input_claim_phase_strict": sum(r["input_claim_phase_strict"] for r in rows),
        "thresholds": {"share_of_looped_runs": 0.25, "share_post_edit": 0.20},
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
