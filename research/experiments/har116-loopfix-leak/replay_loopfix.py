"""HAR-116: replay the loop break over the 82 HAR-114 runs. $0, read-only.

The rule is the live one (:func:`evallab.loopfix.loop_decision`): one nudge
when a run of identical commands or messages is detected, then a stop five
calls later only if the repetition never broke. A stop cuts every call
after it, so the saving is those calls' input tokens.

Usage (from the worktree root)::

    uv run --no-sync python research/experiments/har116-loopfix-leak/replay_loopfix.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from evallab.loopfix import loop_decision  # noqa: E402
from evallab.token_flow import _stitched_steps  # noqa: E402

OUT = Path(__file__).resolve().parent
RUNS = REPO / "research" / "experiments" / "har114-tokenflow" / "runs.jsonl"

ROOTS = (
    Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har110-live/runs"),
    Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har104-runs/runs"),
    Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs"),
    Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs"),
)

#: One HAR-110 job was re-run; its first trial survives a level deeper.
NESTED = (
    Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har110-live/runs")
    / "_aborted-har110-verifier-digest",
)

#: The passes the card asks about. Whether the stop cuts one is decided
#: from the call that earned it (the run's last useful edit), not assumed.
LATE_PASSES = {
    ("HAR-81", "candidate-2684-security-appsec", 84): "har81-2684",
    ("HAR-81", "candidate-1271-media-games", 95): "har81-1271",
    ("HAR-104", "002391", 102): "har104-002391",
}


def _trial_dir(job: str, trial: str) -> Path | None:
    for root in (*ROOTS, *NESTED):
        candidate = root / job / trial
        if (candidate / "agent").is_dir():
            return candidate
    return None


def _agent_steps(trial_dir: Path) -> list[dict]:
    steps, reason = _stitched_steps(trial_dir)
    if steps is None:
        raise RuntimeError(reason or "trajectory unreadable")
    return [step for step in steps if step.get("source") in ("agent", "assistant")]


def _savings(series: list[dict], stop_call: int | None) -> int | None:
    """Input tokens of the calls after the stop — the calls that never run."""
    if stop_call is None:
        return None
    return sum(
        entry["prompt_tokens"]
        for entry in series
        if isinstance(entry.get("prompt_tokens"), int) and entry.get("call_index", 0) > stop_call
    )


def main() -> None:
    rows = [json.loads(line) for line in RUNS.read_text(encoding="utf-8").splitlines() if line]
    out_rows = []
    missing = []
    for row in rows:
        trial_dir = _trial_dir(row["job"], row["trial"])
        if trial_dir is None:
            missing.append(f"{row['job']}/{row['trial']}")
            continue
        steps = _agent_steps(trial_dir)
        decision = loop_decision(steps)
        series = (row.get("token_flow") or {}).get("prompt_series") or []
        n_calls = (row.get("token_flow") or {}).get("prompt_summary", {}).get("n_calls")
        saved = _savings(series, decision["stop_call"])
        key = (row["source"], row["task"], n_calls)
        label = LATE_PASSES.get(key)
        earned_at = ((row.get("token_flow") or {}).get("last_useful_edit") or {}).get("call_index")
        verdict = None
        if label is not None:
            # Cut only when the stop lands before the call that earned the
            # pass. A stop after that call keeps the pass and drops the loop.
            cut = (
                decision["stop_call"] is not None
                and isinstance(earned_at, int)
                and decision["stop_call"] < earned_at
            )
            verdict = "cut" if cut else "kept"
        out_rows.append(
            {
                "source": row["source"],
                "arm": row["arm"],
                "task": row["task"],
                "job": row["job"],
                "trial": row["trial"],
                "reward": row["reward"],
                "n_calls": n_calls,
                "nudge_call": decision["nudge_call"],
                "detector": decision["detector"],
                "stop_call": decision["stop_call"],
                "broke_at_call": decision["broke_at_call"],
                "input_tokens_saved": saved,
                "pass_earned_at_call": earned_at if label else None,
                "late_pass": label,
                "late_pass_verdict": verdict,
            }
        )
    if missing:
        raise SystemExit("trial dirs not found: " + ", ".join(missing))
    (OUT / "replay.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in out_rows), encoding="utf-8"
    )
    with (OUT / "replay.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        writer.writeheader()
        writer.writerows(out_rows)
    fired = [row for row in out_rows if row["nudge_call"] is not None]
    stopped = [row for row in fired if row["stop_call"] is not None]
    saved_total = sum(row["input_tokens_saved"] or 0 for row in stopped)
    print(f"runs {len(out_rows)}  nudged {len(fired)}  stopped {len(stopped)}")
    print(f"input tokens saved {saved_total}")
    for row in out_rows:
        if row["late_pass"]:
            print(
                f"{row['late_pass']}: earned at {row['pass_earned_at_call']} "
                f"nudge {row['nudge_call']} stop {row['stop_call']} "
                f"verdict {row['late_pass_verdict']} saved {row['input_tokens_saved']}"
            )


if __name__ == "__main__":
    main()
