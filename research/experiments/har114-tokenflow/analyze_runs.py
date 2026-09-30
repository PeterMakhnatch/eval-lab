"""HAR-114 token-flow sweep: run token_flow over all HAR-110/104/81 trials.

$0, read-only over the locked evidence worktrees. Writes runs.jsonl (full
records), runs.csv (compact table) and aggregates.json next to this script.

Usage (from the worktree root)::

    uv run --no-sync python research/experiments/har114-token-flow/analyze_runs.py
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from evallab.token_flow import analyze_token_flow  # noqa: E402

OUT = Path(__file__).resolve().parent

HAR110 = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har110-live/runs")
HAR104 = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har104-runs/runs")
HAR81A = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs")
HAR81B = Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs")

GOLDEN_JOB = "har81-l-d-a2-arvo-18737"

# (job) -> (source, arm, task) for the split-v2 table
# (research/experiments/har110-python-gepa/results-v2-trials.jsonl).
V2_ARMS = {
    "har110-dev-000383-cfe31418": ("HAR-110", "gepa-candidate", "000383"),
    "har110-dev-001832-cfe31418": ("HAR-110", "gepa-candidate", "001832"),
    "har110-dev-002256-cfe31418": ("HAR-110", "gepa-candidate", "002256"),
    "har110-dev-002391-cfe31418": ("HAR-110", "gepa-candidate", "002391"),
    "har110-dev-002864-cfe31418": ("HAR-110", "gepa-candidate", "002864"),
    "gepa-terminus-2-format-code-task-001-d1471e1b2701d6d1765ff1df": (
        "HAR-110",
        "gepa-candidate",
        "001896",
    ),
    "har110-000495-gepa": ("HAR-110", "gepa-candidate", "000495"),
    "har110-000587-gepa": ("HAR-110", "gepa-candidate", "000587"),
    "har110-001161-gepa": ("HAR-110", "gepa-candidate", "001161"),
    "har110-001181-gepa": ("HAR-110", "gepa-candidate", "001181"),
    "har110-000495-plain": ("HAR-110", "plain-heldout", "000495"),
    "har110-000587-plain": ("HAR-110", "plain-heldout", "000587"),
    "har110-001161-plain": ("HAR-110", "plain-heldout", "001161"),
    "har110-001181-plain": ("HAR-110", "plain-heldout", "001181"),
    "har110-000495-seed": ("HAR-110", "seed-addendum", "000495"),
    "har110-000587-seed": ("HAR-110", "seed-addendum", "000587"),
    "har110-001161-seed": ("HAR-110", "seed-addendum", "001161"),
    "har110-001181-seed": ("HAR-110", "seed-addendum", "001181"),
    "gepa-terminus-2-format-code-task-000-9862796a5e5624e818ab5f04": (
        "HAR-110",
        "seed-addendum",
        "000383",
    ),
    "gepa-terminus-2-format-code-task-001-c074cb87ddff7e1368343d33": (
        "HAR-110",
        "seed-addendum",
        "001832",
    ),
    "gepa-terminus-2-format-code-task-001-f1d48fcf652ab5d801068db1": (
        "HAR-110",
        "seed-addendum",
        "001896",
    ),
    "gepa-terminus-2-format-code-task-002-2700aa59352626e20aff4ed8": (
        "HAR-110",
        "seed-addendum",
        "002256",
    ),
    "gepa-terminus-2-format-code-task-002-a951229932dc90303c90215d": (
        "HAR-110",
        "seed-addendum",
        "002391",
    ),
    "gepa-terminus-2-format-code-task-002-f785358f50e70ab387a1ba6c": (
        "HAR-110",
        "seed-addendum",
        "002864",
    ),
    "gepa-terminus-2-format-code-task-000-9089903b46c8c3c7410657ba": (
        "HAR-110",
        "seed-addendum-v1",
        "000226",
    ),
    "gepa-terminus-2-format-code-task-002-12c6d0cae5191408009f5181": (
        "HAR-110",
        "seed-addendum-v1",
        "002259",
    ),
    "gepa-terminus-2-format-code-task-002-df7dfd07ef80225115d97bb1": (
        "HAR-110",
        "seed-addendum-v1",
        "002407",
    ),
}

CAP_CHARS = 2000  # candidate: max terminal-output chars fed back per step
SUMM_EARLY_TOKENS = 32768  # candidate: summarise once used prompt exceeds this
VERIFY_GRACE_CALLS = 5  # stop-after-verify / loop-break grace period


def _job_trials(job: Path) -> list[Path]:
    """Trial dirs: subdirs with result.json, else the job dir itself."""
    if not job.is_dir():
        return []
    subs = sorted(e for e in job.iterdir() if e.is_dir() and (e / "result.json").is_file())
    if subs:
        return subs
    if (job / "agent" / "trajectory.json").is_file():
        return [job]
    return []


def _reward(trial: Path):
    try:
        result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    verifier = result.get("verifier_result") or {}
    rewards = verifier.get("rewards") or {}
    reward = rewards.get("reward")
    task = result.get("task_name")
    return (reward if isinstance(reward, (int, float)) else None, task)


def _ledger(job: Path) -> dict:
    try:
        lab = json.loads((job / "lab-metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    usage = lab.get("provider_usage") or {}
    totals = usage.get("totals") or {}
    calls = usage.get("calls") or []
    return {
        "ledger_calls": len(calls),
        "ledger_in": totals.get("input_tokens"),
        "ledger_out": totals.get("output_tokens"),
        "ledger_unresolved": usage.get("unresolved_requests"),
    }


def _savings(flow: dict) -> dict:
    """Expected input-token savings of 4 candidate changes on this run.

    A cap2000: per-step fed-back output capped at 2000 chars. Each excess
      char is re-sent in every later prompt (full-history chat), so the
      saving is excess_chars(k) * calls_after(k) // 4, where calls_after
      stops at the next summarisation handoff (prompt drops > 15000),
      which resets the history. Top-5 outputs only: a lower bound.
    B summ32k: summarise once the used prompt first exceeds 32768 instead
      of ~49152. The handoff drops the prompt to ~4000 (observed median
      post-handoff level); it then regrows at the run's own fitted slope
      (capped at 53000, where a second proactive fire would trigger).
      Saving = sum over post-crossing calls of prompt - regrown level,
      stopping at the run's actual summarisation fire if any.
    C stopverify: stop 5 calls after the last-useful-edit call; saving =
      input tokens in later calls. None when no edit was detected.
    D loopbreak: stop 5 calls after the loop-onset call; saving = input
      tokens in later calls. None when no onset was detected.
    """
    series = flow.get("prompt_series") or []
    tops = flow.get("top_terminal_outputs") or []
    prompts = {
        e.get("call_index"): e.get("prompt_tokens")
        for e in series
        if isinstance(e.get("prompt_tokens"), int)
    }
    ordered = sorted(prompts)
    n_calls = flow.get("prompt_summary", {}).get("n_calls") or 0
    slope = (flow.get("prompt_summary") or {}).get("slope_per_call") or 0
    slope = max(0, slope)
    out: dict = {}
    # Summarisation handoffs reset full-history re-sends.
    resets = set()
    for i in range(1, len(ordered)):
        if prompts[ordered[i - 1]] - prompts[ordered[i]] > 15000:
            resets.add(ordered[i])
    horizon = sorted(resets) + [n_calls + 1]
    save_a = 0
    for top in tops:
        call = top.get("call_index") or n_calls
        stop_at = next(h for h in horizon if h > call)
        excess = max(0, (top.get("chars") or 0) - CAP_CHARS)
        save_a += excess * max(0, stop_at - call) // 4
    out["save_cap2000_in"] = save_a
    first_cross = next((c for c in ordered if prompts[c] > SUMM_EARLY_TOKENS), None)
    out["summ32k_first_cross_call"] = first_cross
    if first_cross is None:
        out["save_summ32k_in"] = 0
    else:
        fire = next((c for c in ordered if c > first_cross and c in resets), None)
        end = fire if fire is not None else n_calls + 1
        out["save_summ32k_in"] = sum(
            max(0, prompts[c] - min(4000 + slope * (c - first_cross), 53000))
            for c in ordered
            if first_cross < c < end
        )
    # C: stop 5 calls after last edit.
    edit = flow.get("last_useful_edit") or {}
    if edit.get("step_id") is not None and edit.get("call_index") is not None:
        cutoff = edit["call_index"] + VERIFY_GRACE_CALLS
        out["save_stopverify_in"] = sum(p for c, p in prompts.items() if c > cutoff)
    else:
        out["save_stopverify_in"] = None
    # D: stop 5 calls after loop onset.
    onset = flow.get("loop_onset") or {}
    if onset.get("step_id") is not None and onset.get("call_index") is not None:
        cutoff = onset["call_index"] + VERIFY_GRACE_CALLS
        out["save_loopbreak_in"] = sum(p for c, p in prompts.items() if c > cutoff)
    else:
        out["save_loopbreak_in"] = None
    return out


def collect() -> list[dict]:
    rows: list[dict] = []
    # HAR-110 live jobs.
    for job in sorted(HAR110.iterdir()):
        if not job.is_dir() or job.name.startswith("."):
            continue
        if job.name.startswith("_aborted"):
            continue  # nested jobs handled trial by trial below
        if job.name == "gepa-har110-python-train-search":
            # GEPA optimizer state (attempt-*/result.json are proposer
            # records, not agent trials); the rollout trials live in the
            # standalone gepa-terminus-2-* jobs.
            continue
        if job.name in V2_ARMS:
            label = V2_ARMS[job.name]
        else:
            continue
        for trial in _job_trials(job):
            rows.append((job, trial, *label))
    # Aborted leftovers with finished trials live one level deeper.
    for sub in sorted((HAR110 / "_aborted-har110-verifier-digest").iterdir()):
        if not sub.is_dir():
            continue
        for trial in _job_trials(sub):
            if trial.name.startswith("gepa-terminus-2-format-code-task__"):
                rows.append((sub, trial, "HAR-110", "aborted-v1-seed", "002407"))
    # HAR-104 dev batch.
    for job in sorted(HAR104.iterdir()):
        if not job.is_dir() or not job.name.startswith("har104-d-"):
            continue
        task = job.name.split("har104-d-")[-1]
        for trial in _job_trials(job):
            rows.append((job, trial, "HAR-104", "plain-dev", task))
    # HAR-81 arms (same model, SFT harness settings).
    for root in (HAR81A, HAR81B):
        for job in sorted(root.iterdir()):
            if not job.is_dir() or not job.name.startswith("har81-"):
                continue
            parts = job.name.split("-")
            arm = "-".join(parts[1:4]) if len(parts) > 3 else "unknown"
            for trial in _job_trials(job):
                rows.append((job, trial, "HAR-81", arm, None))
    out: list[dict] = []
    for job, trial, source, arm, task in rows:
        reward, task_name = _reward(trial) or (None, None)
        flow = analyze_token_flow(trial, job)
        row = {
            "trial": trial.name,
            "job": job.name,
            "source": source,
            "arm": arm,
            "task": task or (task_name or "").split("/")[-1] or None,
            "task_name": task_name,
            "reward": reward,
            "golden": job.name == GOLDEN_JOB,
            **_ledger(job),
            "token_flow": flow,
            "savings": _savings(flow),
        }
        out.append(row)
    return out


def compact(row: dict) -> dict:
    flow = row["token_flow"]
    edit = flow.get("last_useful_edit") or {}
    after = flow.get("tokens_after_last_edit") or {}
    onset = flow.get("loop_onset") or {}
    summary = flow.get("prompt_summary") or {}
    summ = flow.get("summarisations") or {}
    events = summ.get("events") or []
    ok = [e for e in events if e.get("success") is True]
    tops = flow.get("top_terminal_outputs") or []
    stop = flow.get("stop") or {}
    sav = row.get("savings") or {}
    top_chars = [(t.get("step_id"), t.get("chars")) for t in tops[:3]]
    while len(top_chars) < 3:
        top_chars.append((None, None))
    first_event = events[0] if events else {}
    return {
        "trial": row["trial"],
        "job": row["job"],
        "source": row["source"],
        "arm": row["arm"],
        "task": row["task"],
        "reward": row["reward"],
        "stop": stop.get("stop_reason"),
        "golden": row["golden"],
        "n_calls": summary.get("n_calls"),
        "ledger_in": row.get("ledger_in"),
        "ledger_calls": row.get("ledger_calls"),
        "last_edit_step": edit.get("step_id"),
        "last_edit_call": edit.get("call_index"),
        "persists": edit.get("persists_in_final_diff"),
        "after_in": after.get("input_tokens"),
        "after_share_in": after.get("share_input"),
        "loop_onset_step": onset.get("step_id"),
        "loop_detector": onset.get("detector"),
        "prompt_first": summary.get("first"),
        "prompt_max": summary.get("max"),
        "prompt_max_step": summary.get("max_step_id"),
        "prompt_median": summary.get("median"),
        "prompt_slope": summary.get("slope_per_call"),
        "summ_meta": summ.get("metadata_count"),
        "summ_events": len(events),
        "summ_ok": len(ok),
        "summ_before": (first_event.get("prompt_before") or {}).get("prompt_tokens"),
        "summ_after": (first_event.get("prompt_after") or {}).get("prompt_tokens"),
        "top1": f"{top_chars[0][0]}:{top_chars[0][1]}",
        "top2": f"{top_chars[1][0]}:{top_chars[1][1]}",
        "top3": f"{top_chars[2][0]}:{top_chars[2][1]}",
        "save_cap2000_in": sav.get("save_cap2000_in"),
        "summ32k_cross_call": sav.get("summ32k_first_cross_call"),
        "save_stopverify_in": sav.get("save_stopverify_in"),
        "save_loopbreak_in": sav.get("save_loopbreak_in"),
    }


def main() -> None:
    rows = collect()
    print(f"trials: {len(rows)}", file=sys.stderr)
    with open(OUT / "runs.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    compacts = [compact(row) for row in rows]
    for row, comp in zip(rows, compacts, strict=True):
        comp["save_summ32k_in"] = (row.get("savings") or {}).get("save_summ32k_in", 0)
    with open(OUT / "runs.csv", "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(compacts[0].keys()))
        writer.writeheader()
        writer.writerows(compacts)
    reductions = []
    for row in rows:
        for event in (row["token_flow"].get("summarisations") or {}).get("events", []):
            before = (event.get("prompt_before") or {}).get("prompt_tokens")
            after = (event.get("prompt_after") or {}).get("prompt_tokens")
            if event.get("success") is True and before and after:
                reductions.append(before - after)
    reductions.sort()
    median_reduction = reductions[len(reductions) // 2] if reductions else 0
    agg = {
        "n_trials": len(rows),
        "summ_reduction_median": median_reduction,
        "summ_reductions": reductions,
    }
    with open(OUT / "aggregates.json", "w", encoding="utf-8") as fh:
        fh.write(json.dumps(agg, indent=2, sort_keys=True) + "\n")
    print(
        f"wrote runs.jsonl runs.csv aggregates.json "
        f"(median summarisation reduction {median_reduction})"
    )


if __name__ == "__main__":
    main()
