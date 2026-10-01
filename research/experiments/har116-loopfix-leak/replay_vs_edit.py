"""HAR-116 addendum: break stop vs last real edit, plus rater check. $0.

(a) Compares the online rule's onset (``loopfix.loop_decision`` nudge) with
    ``token_flow.loop_onset`` on the same 82 runs.
(b) Joins each run's stop call with its ``token_flow.last_useful_edit``:
    a stop before the last real edit cuts live work. Also lists scored-1
    runs the break would cut (HAR-120 adopts loopfix only if there are none
    beyond the two known late passes).
(c) Runs the same decision on Traces' 12 rated runs and joins it with the
    blind rater loop labels.

Usage (from the worktree root)::

    uv run --no-sync python research/experiments/har116-loopfix-leak/replay_vs_edit.py

Writes ``replay-vs-edit.csv`` (82 rows) and ``replay-vs-edit.md`` next to it.
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
REPLAY = OUT / "replay.jsonl"
LABELS = REPO / "research" / "explorations" / "trace-lab" / "har119" / "labels"
SELECTION = REPO / "research" / "explorations" / "trace-lab" / "har119" / "selection.json"


def _agent_steps(trial_dir: Path) -> list[dict]:
    steps, reason = _stitched_steps(trial_dir)
    if steps is None:
        raise RuntimeError(f"{trial_dir}: {reason or 'trajectory unreadable'}")
    return [step for step in steps if step.get("source") in ("agent", "assistant")]


def _cut(stop: int | None, last_edit: int | None) -> str:
    if stop is None:
        return "no-stop"
    if not isinstance(last_edit, int):
        return "unknown"
    return "cut" if stop < last_edit else "kept"


def main() -> None:
    rows = [json.loads(line) for line in RUNS.read_text(encoding="utf-8").splitlines() if line]
    replay = {
        (row["job"], row["trial"]): row
        for row in (
            json.loads(line) for line in REPLAY.read_text(encoding="utf-8").splitlines() if line
        )
    }
    out_rows = []
    agree = disagree = 0
    for row in rows:
        rep = replay[(row["job"], row["trial"])]
        flow = row.get("token_flow") or {}
        onset = flow.get("loop_onset") or {}
        onset_call = onset.get("call_index")
        onset_detector = onset.get("detector")
        # Same run, different reporting call by construction: loop_onset is
        # the run's first call; the nudge is the first call the run
        # qualifies (start+3 for a 4-command run, start+9 for a 10-message
        # run). A "both" tie nudges at the command reach (start+3).
        shift = {"normalized_command_run": 3, "identical_message_run": 9, "both": 3}
        if rep["nudge_call"] is None and onset_call is None:
            same = True
        elif rep["nudge_call"] is None or onset_call is None:
            same = False
        else:
            same = rep["nudge_call"] - onset_call == shift[rep["detector"]]
        agree, disagree = (agree + 1, disagree) if same else (agree, disagree + 1)
        last_edit = (flow.get("last_useful_edit") or {}).get("call_index")
        last_edit = last_edit if isinstance(last_edit, int) else None
        verdict = _cut(rep["stop_call"], last_edit)
        out_rows.append(
            {
                "source": row["source"],
                "task": row["task"],
                "job": row["job"],
                "trial": row["trial"],
                "reward": row["reward"],
                "n_calls": rep["n_calls"],
                "onset_call": rep["nudge_call"],
                "onset_detector": rep["detector"],
                "loop_onset_call": onset_call,
                "loop_onset_detector": onset_detector,
                "onset_agree": same,
                "stop_call": rep["stop_call"],
                "last_edit_call": last_edit,
                "live_work": verdict,
                "scored1_cut": verdict == "cut" and row["reward"] == 1.0,
            }
        )
    with (OUT / "replay-vs-edit.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        writer.writeheader()
        writer.writerows(out_rows)

    cut = [row for row in out_rows if row["live_work"] == "cut"]
    scored1 = [row for row in cut if row["scored1_cut"]]
    unknown = [row for row in out_rows if row["live_work"] == "unknown"]

    selection = json.loads(SELECTION.read_text(encoding="utf-8"))["runs"]
    rated = []
    for entry in selection:
        trial = entry["trial"]
        kinds = {}
        for rater in ("rater_a", "rater_b"):
            label = json.loads((LABELS / rater / f"{trial}.json").read_text(encoding="utf-8"))
            kinds[rater] = label.get("loop_kind")
        decision = loop_decision(_agent_steps(Path(entry["trial_dir"])))
        agreed_free = kinds["rater_a"] == "none" and kinds["rater_b"] == "none"
        rated.append(
            {
                "trial": trial,
                "reward": entry["reward"],
                "rater_a": kinds["rater_a"],
                "rater_b": kinds["rater_b"],
                "agreed_loop_free": agreed_free,
                "nudge_call": decision["nudge_call"],
                "detector": decision["detector"],
                "stop_call": decision["stop_call"],
            }
        )
    free = [row for row in rated if row["agreed_loop_free"]]
    free_fired = [row for row in free if row["stop_call"] is not None]

    lines = []
    lines.append("# HAR-116 addendum: stop vs last real edit, rater check")
    lines.append("")
    lines.append("## (a) Is the online onset the same rule?")
    lines.append("")
    lines.append("Yes: same features, same thresholds, same run. The reported call")
    lines.append("differs by construction: `loop_onset` is the run's first call, the")
    lines.append("nudge is the first call the run qualifies (start+3 for a 4-command")
    lines.append("run, start+9 for a 10-message run; a `both` tie nudges at the command")
    lines.append("reach, which is the earliest a live agent can know). Code path:")
    lines.append("`harbor_terminus._apply_loop_fix` -> `loopfix.live_loop_action` ->")
    lines.append("`loopfix._onset` over `step_features` (signature from")
    lines.append("`token_flow.normalized_command`, edit flag from `token_flow._is_edit`,")
    lines.append("message runs at `probe03.LOOP_MIN_RUN` (10), command runs at")
    lines.append("`token_flow.COMMAND_RUN_MIN` (4) with no edit inside — the feature set")
    lines.append("`token_flow._loop_onset` scans. Over the 82 runs the replay nudge")
    lines.append(f"starts the stored onset's run on {agree} runs, disagrees on {disagree}.")
    lines.append("")
    lines.append("## (b) Stop vs last real edit on the 82 runs")
    lines.append("")
    lines.append(f"Stops before the last real edit (live work cut): {len(cut)} of 82.")
    lines.append(f"Stops with no earlier edit to compare: {len(unknown)}.")
    lines.append(f"Scored-1 runs the break would cut: {len(scored1)}.")
    lines.append("")
    if cut:
        lines.append("| run | trial | reward | stop | last edit |")
        lines.append("|---|---|---|---|---|")
        for row in cut:
            lines.append(
                f"| {row['source']} {row['task']} | {row['trial']} | {row['reward']} "
                f"| {row['stop_call']} | {row['last_edit_call']} |"
            )
        lines.append("")
    lines.append("## (c) Traces' 12 rated runs vs rater labels")
    lines.append("")
    lines.append(
        f"Agreed loop-free (both raters `none`): {len(free)} of 12. "
        f"Break fires (stop) on {len(free_fired)} of them."
    )
    lines.append("")
    lines.append("| trial | reward | rater_a | rater_b | nudge | stop |")
    lines.append("|---|---|---|---|---|---|")
    for row in rated:
        lines.append(
            f"| {row['trial']} | {row['reward']} | {row['rater_a']} | {row['rater_b']} "
            f"| {row['nudge_call']} | {row['stop_call']} |"
        )
    lines.append("")
    (OUT / "replay-vs-edit.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"onset agree {agree} differ {disagree}")
    print(f"cut {len(cut)} scored1_cut {len(scored1)} unknown {len(unknown)}")
    print(f"rated loop-free {len(free)}, break fires on {len(free_fired)}")
    for row in scored1:
        print(
            f"scored1 cut: {row['source']} {row['task']} stop {row['stop_call']} edit {row['last_edit_call']}"
        )
    for row in free_fired:
        print(f"loop-free fired: {row['trial']} nudge {row['nudge_call']} stop {row['stop_call']}")


if __name__ == "__main__":
    main()
