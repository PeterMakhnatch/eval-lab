"""HAR-126 G0: replay the lf2 loop break over every recorded Python run.

Corpus ($0, read-only):
- HAR-104 (10), HAR-110 (all trials) and HAR-81 (44) via
  research/experiments/har114-tokenflow/runs.jsonl (82 rows), resolving
  trial dirs from the surviving worktree runs/ roots or the results home.
- HAR-116 baseline + loopfix-r2 + Part B (30 runs) from the results home.

Every decision uses the same detector code the live path uses
(``evallab.loopfix.loop_decision``), with the lf2 threshold parameters.
A stop cuts a scored-1 run when it lands before that run's last real edit
(``token_flow`` last_useful_edit call). The sweep picks the thresholds that
cut no scored-1 run and save the most input tokens.

Usage (from the repo root)::

    uv run --no-sync python research/experiments/har126-lf2/replay_lf2.py

Writes ``replay.csv`` (one row per run at the winning thresholds),
``replay_sweep.csv`` (one row per threshold setting) and prints the table
REPLAY.md embeds.
"""

from __future__ import annotations

import csv
import glob
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))

from evallab.loopfix import loop_decision  # noqa: E402
from evallab.token_flow import _stitched_steps, analyze_token_flow  # noqa: E402

OUT = Path(__file__).resolve().parent
RUNS = REPO / "research" / "experiments" / "har114-tokenflow" / "runs.jsonl"
RESULTS_HOME = Path.home() / "Developer" / "eval-lab-results"

WORKTREE_ROOTS = (
    Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har110-live/runs"),
    Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har104-runs/runs"),
    Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-528/runs"),
    Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har81-dispatch-531/runs"),
    Path("/Users/petermakhnatch/Developer/eval-lab/.worktrees/har110-live/runs")
    / "_aborted-har110-verifier-digest",
)

COMMAND_MINS = (4, 6, 8)
MESSAGE_MINS = (10, 15, 20)
GRACES = (5, 10, 15, 20, 30)


def _trial_dir_from_worktrees(job: str, trial: str) -> Path | None:
    for root in WORKTREE_ROOTS:
        candidate = root / job / trial
        if (candidate / "agent").is_dir():
            return candidate
    return None


def _trial_dir_from_results(job: str, trial: str) -> Path | None:
    for candidate in glob.glob(str(RESULTS_HOME / "*" / f"*-{job}" / trial)):
        if (Path(candidate) / "agent").is_dir():
            return Path(candidate)
    return None


def _agent_steps(trial_dir: Path) -> list[dict]:
    steps, reason = _stitched_steps(trial_dir)
    if steps is None:
        raise RuntimeError(f"{trial_dir}: {reason or 'trajectory unreadable'}")
    return [step for step in steps if step.get("source") in ("agent", "assistant")]


def _saved(prompt_series: list[dict], stop_call: int | None) -> int | None:
    if stop_call is None:
        return None
    return sum(
        entry["prompt_tokens"]
        for entry in prompt_series
        if isinstance(entry.get("prompt_tokens"), int) and entry.get("call_index", 0) > stop_call
    )


def _load_corpus_a() -> tuple[list[dict], list[str]]:
    """The 82 HAR-114 rows: steps re-read, reward/edit/tokens from the row."""
    rows = [json.loads(line) for line in RUNS.read_text(encoding="utf-8").splitlines() if line]
    runs: list[dict] = []
    missing: list[str] = []
    for row in rows:
        trial_dir = _trial_dir_from_worktrees(row["job"], row["trial"])
        if trial_dir is None:
            trial_dir = _trial_dir_from_results(row["job"], row["trial"])
        if trial_dir is None:
            missing.append(f"{row['job']}/{row['trial']}")
            continue
        flow = row.get("token_flow") or {}
        edit = flow.get("last_useful_edit") or {}
        runs.append(
            {
                "source": row["source"],
                "task": str(row.get("task")),
                "job": row["job"],
                "trial": row["trial"],
                "arm": row.get("arm"),
                "reward": row.get("reward"),
                "trial_dir": trial_dir,
                "prompt_series": flow.get("prompt_series") or [],
                "n_calls": (flow.get("prompt_summary") or {}).get("n_calls"),
                "last_edit_call": edit.get("call_index"),
            }
        )
    return runs, missing


def _har116_jobs() -> list[tuple[str, str]]:
    """(job, arm) for baseline + loopfix-r2 + Part B; crashed wave-A excluded."""
    jobs: list[tuple[str, str]] = []
    for task in (
        "000383",
        "000495",
        "000587",
        "001161",
        "001181",
        "001832",
        "001896",
        "002256",
        "002391",
        "002864",
    ):
        jobs.append((f"har116-a-{task}-baseline", "baseline"))
        jobs.append((f"har116-a-{task}-loopfix-r2", "loopfix-r2"))
    for task in ("000146", "000226", "000927", "002308", "002402"):
        jobs.append((f"har116-b-{task}-original", "original"))
        jobs.append((f"har116-b-{task}-leakclosed", "leakclosed"))
    return jobs


def _reward_from_trial(trial_dir: Path) -> float | None:
    reward_txt = trial_dir / "verifier" / "reward.txt"
    try:
        return float(reward_txt.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _load_corpus_b() -> tuple[list[dict], list[str]]:
    """The 30 HAR-116 runs: fresh token_flow per trial dir."""
    runs: list[dict] = []
    missing: list[str] = []
    for job, arm in _har116_jobs():
        found = False
        for trial_dir in glob.glob(
            str(RESULTS_HOME / "2026-10-01" / f"HAR-116-{job}" / f"{job}__*")
        ):
            trial_path = Path(trial_dir)
            if not (trial_path / "agent").is_dir():
                continue
            found = True
            flow = analyze_token_flow(trial_path)
            edit = flow.get("last_useful_edit") or {}
            series = flow.get("prompt_series") or []
            runs.append(
                {
                    "source": "HAR-116",
                    "task": job.split("-")[2],
                    "job": job,
                    "trial": trial_path.name,
                    "arm": arm,
                    "reward": _reward_from_trial(trial_path),
                    "trial_dir": trial_path,
                    "prompt_series": series,
                    "n_calls": (flow.get("prompt_summary") or {}).get("n_calls"),
                    "last_edit_call": edit.get("call_index"),
                }
            )
        if not found:
            missing.append(job)
    return runs, missing


def main() -> None:
    t0 = __import__("time").time()
    runs_a, missing_a = _load_corpus_a()
    runs_b, missing_b = _load_corpus_b()
    if missing_a or missing_b:
        raise SystemExit("trial dirs not found: " + ", ".join([*missing_a, *missing_b]))
    runs = runs_a + runs_b
    print(f"corpus: HAR-104/110/81={len(runs_a)} HAR-116={len(runs_b)} total={len(runs)}")

    steps_by_run = [_agent_steps(run["trial_dir"]) for run in runs]

    sweep: list[dict] = []
    decisions: dict[tuple[int, int, int], list[dict]] = {}
    for command_min in COMMAND_MINS:
        for message_min in MESSAGE_MINS:
            for grace in GRACES:
                key = (command_min, message_min, grace)
                per_run: list[dict] = []
                for _run, steps in zip(runs, steps_by_run, strict=True):
                    per_run.append(
                        loop_decision(
                            steps,
                            command_run_min=command_min,
                            message_run_min=message_min,
                            grace_calls=grace,
                        )
                    )
                decisions[key] = per_run
                scored1_cut = 0
                scored1_unknown = 0
                scored0_cut_before_edit = 0
                saved_total = 0
                for run, decision in zip(runs, per_run, strict=True):
                    stop = decision["stop_call"]
                    if stop is None:
                        continue
                    saved = _saved(run["prompt_series"], stop) or 0
                    saved_total += saved
                    if run["reward"] == 1.0 or run["reward"] == 1:
                        edit = run["last_edit_call"]
                        if not isinstance(edit, int):
                            scored1_unknown += 1
                        elif stop < edit:
                            scored1_cut += 1
                    elif run["reward"] == 0.0 or run["reward"] == 0:
                        edit = run["last_edit_call"]
                        if isinstance(edit, int) and stop < edit:
                            scored0_cut_before_edit += 1
                sweep.append(
                    {
                        "command_run_min": command_min,
                        "message_run_min": message_min,
                        "grace_calls": grace,
                        "scored1_cut": scored1_cut,
                        "scored1_unknown_edit": scored1_unknown,
                        "scored0_cut_before_edit": scored0_cut_before_edit,
                        "input_tokens_saved": saved_total,
                    }
                )
    eligible = [
        row for row in sweep if row["scored1_cut"] == 0 and row["scored1_unknown_edit"] == 0
    ]
    if not eligible:
        raise SystemExit("no threshold setting keeps every scored-1 run: lf2 is cap-only")
    winner = max(eligible, key=lambda row: row["input_tokens_saved"])
    print(
        "winner: command_min={command_run_min} message_min={message_run_min} "
        "grace={grace_calls} scored1_cut={scored1_cut} "
        "scored0_cut_before_edit={scored0_cut_before_edit} "
        "saved={input_tokens_saved}".format(**winner)
    )

    key = (winner["command_run_min"], winner["message_run_min"], winner["grace_calls"])
    rows = []
    for run, decision in zip(runs, decisions[key], strict=True):
        stop = decision["stop_call"]
        edit = run["last_edit_call"]
        if stop is None:
            verdict = "no_stop"
        elif isinstance(edit, int):
            verdict = "cut" if stop < edit else "kept"
        else:
            verdict = "stop_no_edit"
        rows.append(
            {
                "source": run["source"],
                "task": run["task"],
                "arm": run["arm"],
                "job": run["job"],
                "trial": run["trial"],
                "reward": run["reward"],
                "n_calls": run["n_calls"],
                "command_run_min": winner["command_run_min"],
                "message_run_min": winner["message_run_min"],
                "grace_calls": winner["grace_calls"],
                "nudge_call": decision["nudge_call"],
                "detector": decision["detector"],
                "stop_call": stop,
                "broke_at_call": decision["broke_at_call"],
                "last_edit_call": edit,
                "verdict": verdict,
                "input_tokens_saved": _saved(run["prompt_series"], stop),
            }
        )
    with (OUT / "replay_sweep.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(sweep[0].keys()))
        writer.writeheader()
        writer.writerows(sweep)
    with (OUT / "replay.csv").open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    (OUT / "winner.json").write_text(json.dumps(winner, indent=2, sort_keys=True) + "\n")
    cuts = [row for row in rows if row["verdict"] == "cut" and row["reward"] == 1]
    print(f"scored-1 runs cut at winner: {len(cuts)}")
    for row in cuts:
        print(
            f"  CUT {row['source']} {row['job']} reward=1 stop={row['stop_call']} edit={row['last_edit_call']}"
        )
    import time

    print(f"elapsed {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
