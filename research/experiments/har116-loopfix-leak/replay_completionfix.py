"""HAR-116: replay the HAR-96 completion fix on the 82 HAR-114 runs. $0, read-only.

The literal rule under test (the same ``has_native_completion`` the
completion-fix tree applies live): once the confirm prompt is pending,
accept a native ``task_complete`` tool call, or ``"task_complete": true``
inside a native call, as the confirmation.

Compared on the same runs against HAR-100's A1 (the first claim ends the
episode) and A3 (any echo-done turn after the first claim confirms).

Per run the script reports the turn each rule would have accepted, the
per-step prompt tokens after that turn (the saving), and whether a pass
would have been lost (the accept turn comes before the passing edit, i.e.
HAR-114's last useful edit, for reward-1 runs).

The pending flag is simulated exactly as upstream Terminus-2 keeps it: an
accepted completion triggers the prompt (pending for the next turn) unless
already pending, when the episode ends; any other turn clears it. A1 is the
first harness-accepted completion (HAR-100's prompt turn). Raw model text
comes from the recorded step layers' proposed message, falling back to the
assembled message.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "research/explorations/trace-lab/probe-03-capabilities"))

import capabilities as p03  # noqa: E402

from evallab.mimo_tool_calls import has_native_completion  # noqa: E402

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

#: `task_complete` mentioned anywhere in a turn's text.
TASK_COMPLETE_RE = re.compile(r"task_complete", re.IGNORECASE)

#: HAR-100's echo-done: a turn whose every command matches this.
ECHO_DONE_RE = re.compile(
    r"""^echo\s+['"]?(the\s+)?(task completed?|task is complete|done|completed|all done|finished)(['"\s]|$)""",
    re.IGNORECASE,
)


def _is_claim(message: str, layer: dict) -> bool:
    """Traces' HAR-119 claim-shaped rule on one assembled agent step."""
    if layer.get("task_complete") is True or layer.get("kind") == "prose_completion":
        return True
    return bool(
        p03.COMPLETION_CLAIM_RE.search(message)
        or p03.ECHO_TASK_COMPLETE_RE.search(message)
        or TASK_COMPLETE_RE.search(message)
    )


def _trial_dir(job: str, trial: str) -> Path | None:
    for root in (*ROOTS, *NESTED):
        candidate = root / job / trial
        if (candidate / "agent").is_dir():
            return candidate
    return None


def _raw_text(step: dict, layer: dict) -> str:
    proposed = layer.get("proposed") or {}
    raw = proposed.get("message") if isinstance(proposed, dict) else None
    if isinstance(raw, str) and raw:
        return raw
    return str(step.get("message") or "")


def _proposed_commands(step: dict, layer: dict) -> list[str]:
    """Command texts one turn proposed: recorded calls first, text fallback."""
    calls = (layer.get("accepted") or {}).get("calls") or []
    keyed = [c.get("keystrokes") for c in calls if isinstance(c, dict)]
    keyed = [k.strip() for k in keyed if isinstance(k, str) and k.strip()]
    if keyed:
        return keyed
    try:
        return [
            c.strip() for c in p03._proposed_commands(str(step.get("message") or "")) if c.strip()
        ]
    except Exception:  # noqa: BLE001
        return []


def _is_echo_done(step: dict, layer: dict) -> bool:
    commands = _proposed_commands(step, layer)
    return bool(commands) and all(ECHO_DONE_RE.match(c) for c in commands)


def _analyse(row: dict) -> dict:
    trial_dir = _trial_dir(row["job"], row["trial"])
    if trial_dir is None:
        raise RuntimeError(f"trial dir not found: {row['job']}/{row['trial']}")
    _coverage, assembled = p03.assemble_trial(trial_dir)
    seq = [
        (doc, step)
        for doc, step in assembled
        if str(step.get("source", "")).lower() in p03.probe02.AGENT_SOURCES
    ]
    layers = [p03.layer_status(step) or {} for _doc, step in seq]
    raws = [_raw_text(step, layer) for (_doc, step), layer in zip(seq, layers, strict=True)]
    tokens = [p03._step_tokens(step)[0] for _doc, step in seq]
    step_ids = [step.get("step_id") for _doc, step in seq]
    # An accepted completion is a recorded task_complete=true; mapped prose
    # completions carry task_complete true in their layer as well.
    live_tc = [bool(layer.get("task_complete") is True) for layer in layers]
    live_confirmed = bool(live_tc and live_tc[-1])

    first_prompt = next(
        (
            i
            for i, (_d, step) in enumerate(seq)
            if p03.CONFIRM_PROMPT_RE.search(str(p03.obs_content(step) or ""))
        ),
        None,
    )

    def _saved(accept: int | None) -> int | None:
        if accept is None:
            return None
        return sum(t for t in tokens[accept + 1 :] if isinstance(t, int))

    # Literal rule: simulate the pending flag exactly as upstream does. A
    # turn with an accepted completion triggers the prompt (pending for the
    # next turn) unless it was already pending, in which case the episode
    # ends there. Any other turn clears it. The fix accepts a turn as the
    # confirmation iff it is pending and carries a native completion
    # signal. The fix recovers the run only when that turn was not already
    # accepted live.
    prompted = [
        bool(p03.CONFIRM_PROMPT_RE.search(str(p03.obs_content(step) or ""))) for _doc, step in seq
    ]
    literal = None
    literal_recovered = False
    pending = False
    for i in range(len(seq)):
        if pending and has_native_completion(raws[i]):
            literal = i
            literal_recovered = not live_tc[i]
            break
        if live_tc[i] and pending:
            literal = i
            literal_recovered = False
            break
        pending = bool(live_tc[i] and prompted[i])

    # A1 (HAR-100): the first completion the harness accepts ends the
    # episode — no double confirmation. This is the prompt turn on runs that
    # reached one, never a merely claim-flavored verify turn.
    first_accepted = next((i for i in range(len(seq)) if live_tc[i]), None)
    a1 = first_accepted
    # A3 (HAR-100): the first echo-done turn at or after the first
    # claim-shaped turn confirms. Claim-shaped is the HAR-119 message rule
    # (accepted, prose-shaped, or mentioning completion), which is what
    # reproduces HAR-100's 9 pilot runs.
    msgs = [str(step.get("message") or "") for _doc, step in seq]
    first_shaped = next((i for i in range(len(seq)) if _is_claim(msgs[i], layers[i])), None)
    a3 = None
    if first_shaped is not None:
        for i in range(first_shaped, len(seq)):
            if _is_echo_done(seq[i][1], layers[i]):
                a3 = i
                break

    edit = (row.get("token_flow") or {}).get("last_useful_edit") or {}
    edit_step = edit.get("step_id")
    reward = row.get("reward")

    def _lost(accept: int | None) -> str | None:
        if reward != 1.0 or accept is None:
            return None
        if not isinstance(edit_step, int):
            return "kept-no-edit"
        if not isinstance(step_ids[accept], int):
            return "unknown"
        return "lost" if step_ids[accept] < edit_step else "kept"

    def _col(accept: int | None) -> tuple[int | None, int | None]:
        if accept is None:
            return None, None
        sid = step_ids[accept]
        return (sid if isinstance(sid, int) else None), _saved(accept)

    literal_step, literal_saved = _col(literal)
    a1_step, a1_saved = _col(a1)
    a3_step, a3_saved = _col(a3)
    return {
        "source": row["source"],
        "arm": row["arm"],
        "task": row["task"],
        "trial": row["trial"],
        "reward": reward,
        "n_agent_steps": len(seq),
        "live_confirmed": live_confirmed,
        "first_prompt_step": step_ids[first_prompt] if first_prompt is not None else None,
        "turns_after_prompt": len(seq) - first_prompt - 1 if first_prompt is not None else 0,
        "last_edit_step": edit_step,
        "literal_step": literal_step,
        "literal_saved": literal_saved,
        "literal_recovered": literal_recovered,
        "literal_pass": _lost(literal),
        "a1_step": a1_step,
        "a1_saved": a1_saved,
        "a1_pass": _lost(a1),
        "a3_step": a3_step,
        "a3_saved": a3_saved,
        "a3_pass": _lost(a3),
    }


def main() -> None:
    rows = [json.loads(line) for line in RUNS.read_text(encoding="utf-8").splitlines() if line]
    out_rows = [_analyse(row) for row in rows]
    (OUT / "replay_completionfix.jsonl").write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in out_rows), encoding="utf-8"
    )
    with (OUT / "replay_completionfix.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(out_rows[0].keys()))
        writer.writeheader()
        writer.writerows(out_rows)

    har119 = [r for r in out_rows if r["source"] in ("HAR-104", "HAR-110")]
    har81 = [r for r in out_rows if r["source"] == "HAR-81"]
    for label, group in (("HAR-119/38", har119), ("HAR-81/44", har81)):
        rec = sum(1 for r in group if r["literal_recovered"])
        lit = sum(r["literal_saved"] or 0 for r in group)
        a1 = sum(r["a1_saved"] or 0 for r in group)
        a3 = sum(r["a3_saved"] or 0 for r in group)
        print(f"{label}: runs {len(group)} literal-recovered {rec}")
        print(f"  saved literal {lit}  A1 {a1}  A3 {a3}")
        print(
            "  pass lost: literal "
            + str(sum(1 for r in group if r["literal_pass"] == "lost"))
            + "  A1 "
            + str(sum(1 for r in group if r["a1_pass"] == "lost"))
            + "  A3 "
            + str(sum(1 for r in group if r["a3_pass"] == "lost"))
        )


if __name__ == "__main__":
    main()
