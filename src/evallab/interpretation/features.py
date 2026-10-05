"""Per-trial feature table: one deterministic row per Harbor trial.

``evallab features`` reuses the existing detectors -- it implements no new
detection logic of its own:

* per-trial signals (:func:`evallab.live_watch.trial_signals`): steps,
  episodes, token sums, first repo edit, upstream attempts, identical runs;
* repo-edit detector (:mod:`evallab.token_flow`, via live_watch);
* command verbs (:mod:`evallab.interpretation.outside_fetch` quote-aware
  segment/program splitters);
* loop onset (:func:`evallab.token_flow.analyze_token_flow`);
* copy taint (:func:`evallab.copy_check.copy_check`) folded into the
  :func:`evallab.counts.classify_counts` verdict;
* stop reason (:func:`evallab.step_layers.classify_stop_reason`) with the
  binding ceiling from :func:`evallab.probe03.ceiling_which`.

Everything else in this module is mechanical projection (regexes over
observation text are documented in ``docs/features.md`` trust notes).
"""

from __future__ import annotations

import argparse
import csv
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

FEATURES_SCHEMA = "evallab.features/v1"

#: Ordered output columns. Step-anchored values carry their own ``*_step`` id.
COLUMNS: tuple[str, ...] = (
    "trial",
    "job",
    "task_id",
    "arm",
    "n_steps",
    "n_agent_calls",
    "wall_time_s",
    "input_tokens",
    "output_tokens",
    "mean_input_per_call",
    "mean_output_per_call",
    "tools_used",
    "tool_counts_json",
    "first_edit_step",
    "first_edit_paths",
    "n_test_runs",
    "test_runs_passed",
    "test_runs_failed",
    "last_test_step",
    "n_errors",
    "n_exceptions",
    "loop_onset_step",
    "max_identical_run",
    "n_fetch_attempts",
    "first_fetch_step",
    "n_fetch_confirmed",
    "copy_verdict",
    "stop_reason",
    "limit_hit",
    "reward",
    "final_diff_lines",
    "files_touched",
    "diff_source",
)

_BASH_FUNCTIONS = frozenset({"bash_command", "bash", "shell", "exec"})

#: Job/trial name segments that name an experiment arm (best effort).
KNOWN_ARMS = frozenset(
    {
        "gepa",
        "stock",
        "tuned",
        "direct",
        "wrapper",
        "domain",
        "allow-cidr",
        "allow-domain",
    }
)

#: ``pytest`` invocation in command text (word match; ``pytest-`` plugins count).
_PYTEST_CMD_RE = re.compile(r"(?<![\w-])pytest(?![\w-])", re.IGNORECASE)

#: A pytest terminal summary bar: ``=== 3 failed, 12 passed in 4.2s ===``.
_PYTEST_BAR_RE = re.compile(r"(?m)^=.*(?:passed|failed|error).*=$")

#: ``12 passed``, ``3 failed``, ``1 error`` tokens inside a test observation.
_PYTEST_COUNT_RE = re.compile(
    r"(\d+)\s+(failed|passed|skipped|errors?|xfailed|xpassed|warnings?)", re.IGNORECASE
)

#: Traceback marker for the ``n_exceptions`` column.
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")

#: Tool-error output markers for the ``n_errors`` column.
_ERROR_RE = re.compile(
    r"(?m)^(E |FAILED |ERROR )"
    r"|Traceback \(most recent call last\)"
    r"|\bcommand not found\b"
    r"|\b(option|invalid|unknown).*?(argument|option|command)\b"
    r"|exit (code|status):? [1-9]",
    re.IGNORECASE,
)

_COMPLETION_RE = re.compile(r"task_complete|mark_task_complete", re.IGNORECASE)


def _read_json(path: Path) -> dict[str, Any]:
    import json

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _task_id(result: dict[str, Any], trial_name: str) -> str:
    """Task id from ``task_name``, never the ``task_id`` dict (QUIRKS Q23)."""
    name = result.get("task_name")
    if isinstance(name, str) and name.strip():
        return name.strip().rsplit("/", 1)[-1]
    return trial_name.split("__")[0]


def _arm(job_name: str, trial_name: str) -> str | None:
    for candidate in (trial_name.split("__")[0], job_name):
        for segment in candidate.split("-"):
            if segment in KNOWN_ARMS:
                return segment
    return None


def _parse_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _wall_time_s(result: dict[str, Any]) -> float | None:
    agent_exec = result.get("agent_execution")
    if isinstance(agent_exec, dict):
        start = _parse_time(agent_exec.get("started_at"))
        end = _parse_time(agent_exec.get("finished_at"))
        if start is not None and end is not None and end >= start:
            return round(end - start, 3)
    start = _parse_time(result.get("started_at"))
    end = _parse_time(result.get("finished_at"))
    if start is not None and end is not None and end >= start:
        return round(end - start, 3)
    return None


def _observation_text(step: dict[str, Any]) -> str:
    obs = step.get("observation")
    if not isinstance(obs, dict):
        return ""
    results = obs.get("results")
    if not isinstance(results, list):
        return ""
    return "\n".join(
        str(item.get("content") or "")
        for item in results
        if isinstance(item, dict)
    )


def _tool_verbs(agent_steps: list[dict[str, Any]]) -> dict[str, int]:
    """Bash command verbs (quote-aware) plus ``@tool`` tallies for the rest."""
    from evallab.interpretation.outside_fetch import _program, _segments
    from evallab.token_flow import _step_calls

    counts: dict[str, int] = {}
    for step in agent_steps:
        for name, keys in _step_calls(step):
            lowered = (name or "").lower()
            if lowered in _BASH_FUNCTIONS or name is None:
                if not keys.strip():
                    continue
                try:
                    segments = _segments(keys)
                except Exception:  # noqa: BLE001 -- one odd command skips
                    continue
                for tokens in segments:
                    try:
                        program = _program(tokens)
                    except Exception:  # noqa: BLE001 -- same guard
                        program = None
                    if program is not None:
                        verb = str(program[0]).lower()
                        counts[verb] = counts.get(verb, 0) + 1
            else:
                key = f"@{lowered}"
                counts[key] = counts.get(key, 0) + 1
    return counts


def _test_stats(
    agent_steps: list[dict[str, Any]],
) -> tuple[int, int | None, int | None, int | None]:
    """``(n_runs, passed, failed, last_step)`` over agent-side pytest runs.

    A step is a test run when its command invokes ``pytest`` or its
    observation carries a pytest summary bar. Counts come from
    ``N passed/failed/...`` tokens in test-step observations only.
    """
    from evallab.token_flow import _step_calls

    n_runs = 0
    passed = failed = 0
    last_step: int | None = None
    for step in agent_steps:
        commands = "\n".join(text for _, text in _step_calls(step))
        obs = _observation_text(step)
        if not (_PYTEST_CMD_RE.search(commands) or _PYTEST_BAR_RE.search(obs)):
            continue
        n_runs += 1
        for amount, kind in _PYTEST_COUNT_RE.findall(obs):
            if kind.lower() == "passed":
                passed += int(amount)
            elif kind.lower() == "failed":
                failed += int(amount)
        sid = step.get("step_id")
        if isinstance(sid, int):
            last_step = sid if last_step is None else max(last_step, sid)
    if n_runs == 0:
        return 0, None, None, None
    return n_runs, passed, failed, last_step


def _error_stats(agent_steps: list[dict[str, Any]]) -> tuple[int, int]:
    """``(n_errors, n_exceptions)``: steps with error output / tracebacks."""
    n_errors = n_exceptions = 0
    for step in agent_steps:
        obs = _observation_text(step)
        if not obs:
            continue
        if _TRACEBACK_RE.search(obs):
            n_exceptions += 1
        if _ERROR_RE.search(obs):
            n_errors += 1
    return n_errors, n_exceptions


def _final_claim(agent_steps: list[dict[str, Any]]) -> bool | None:
    """Whether the final agent step carries a completion claim."""
    if not agent_steps:
        return None
    from evallab.token_flow import _step_calls

    last = agent_steps[-1]
    texts = [str(last.get("message") or "")]
    texts.extend(text for _, text in _step_calls(last))
    calls = last.get("tool_calls")
    if isinstance(calls, list):
        texts.extend(str(call.get("function_name") or "") for call in calls if isinstance(call, dict))
    return bool(_COMPLETION_RE.search("\n".join(texts)))


def _stop_and_limit(
    result: dict[str, Any],
    job_dir: Path,
    agent_steps: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    """``(stop_reason, limit_hit)`` via the shared classifier + ceilings."""
    from evallab.probe03 import ceiling_which
    from evallab.step_layers import classify_stop_reason

    agent_result = result.get("agent_result")
    metadata = agent_result.get("metadata") if isinstance(agent_result, dict) else None
    exception = result.get("exception_info")
    claim = _final_claim(agent_steps)
    reason, _detail = classify_stop_reason(
        agent_metadata=metadata if isinstance(metadata, dict) else None,
        exception_info=exception if isinstance(exception, dict) else None,
        last_task_complete=claim,
        last_prose_completion=None,
    )
    limit: str | None = None
    if reason == "trial_budget_exhausted":
        n_in = (agent_result or {}).get("n_input_tokens") if isinstance(agent_result, dict) else None
        n_out = (agent_result or {}).get("n_output_tokens") if isinstance(agent_result, dict) else None
        n_ep = metadata.get("n_episodes") if isinstance(metadata, dict) else None
        ceiling = ceiling_which(job_dir, n_in, n_out, n_ep)
        limit = ceiling.partition(":")[2] or None
    elif reason == "agent_timeout":
        limit = "agent_timeout"
    elif reason == "loop_break":
        limit = "loop_break"
    return reason, limit


def _reward(result: dict[str, Any]) -> tuple[float | None, bool]:
    """``(reward, scored)`` from the verifier rewards mapping."""
    verifier = result.get("verifier_result")
    rewards = verifier.get("rewards") if isinstance(verifier, dict) else None
    value = rewards.get("reward") if isinstance(rewards, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, False
    return float(value), True


def _diff_stats(
    trial_dir: Path, agent_steps: list[dict[str, Any]]
) -> tuple[int | None, list[str], str]:
    """``(diff_lines, files_touched, diff_source)`` for the final diff.

    Prefers the harness-recorded ``verifier/agent.diff``; otherwise falls
    back to the union of edit-step touched paths (no line count available).
    """
    diff_path = trial_dir / "verifier" / "agent.diff"
    try:
        text = diff_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    if text.strip():
        changed = 0
        files: list[str] = []
        for line in text.splitlines():
            if line.startswith("+++ "):
                path = line[4:].strip().split("\t")[0]
                if path.startswith("b/"):
                    path = path[2:]
                if path != "/dev/null" and path not in files:
                    files.append(path)
            elif line.startswith("+") and not line.startswith("+++") or line.startswith("-") and not line.startswith("---"):
                changed += 1
        return changed, files, "verifier/agent.diff"
    from evallab.token_flow import _is_edit, _touched_paths

    touched: list[str] = []
    for step in agent_steps:
        edit, _ = _is_edit(step)
        if not edit:
            continue
        for path in _touched_paths(step):
            if path not in touched:
                touched.append(path)
    return None, touched, "edit_steps"


def trial_features(job_dir: Path, trial_dir: Path) -> dict[str, Any]:
    """One deterministic feature row for a single trial directory."""
    import json as _json

    from evallab.copy_check import copy_check
    from evallab.counts import classify_counts
    from evallab.live_watch import (
        TRIAL_RESULT,
        WatchThresholds,
        _agent_steps,
        _read_steps,
        trial_signals,
    )
    from evallab.token_flow import analyze_token_flow

    job_dir, trial_dir = Path(job_dir), Path(trial_dir)
    steps, _traj = _read_steps(trial_dir)
    agent_steps = _agent_steps(steps)
    result = _read_json(trial_dir / TRIAL_RESULT)

    signals = trial_signals(
        job_dir,
        trial_dir,
        thresholds=WatchThresholds(),
        from_config=False,
        now=time.time(),
    )

    n_agent = len(agent_steps)
    verbs = _tool_verbs(agent_steps)
    n_runs, passed, failed, last_test = _test_stats(agent_steps)
    n_errors, n_exceptions = _error_stats(agent_steps)

    try:
        flow = analyze_token_flow(trial_dir, job_dir)
    except Exception:  # noqa: BLE001 -- features degrade, never crash
        flow = {}
    onset = flow.get("loop_onset") if isinstance(flow, dict) else None
    loop_onset = onset.get("step_id") if isinstance(onset, dict) else None
    loop_onset = loop_onset if isinstance(loop_onset, int) else None

    attempts = signals.get("upstream_attempts") or []
    confirmed = signals.get("upstream_confirmed") or []
    fetch_steps = [a.get("step") for a in attempts if isinstance(a.get("step"), int)]

    reward, scored = _reward(result)
    try:
        copy_flag = copy_check(trial_dir)
    except Exception:  # noqa: BLE001 -- same guard
        copy_flag = None
    taint = [copy_flag] if isinstance(copy_flag, dict) else []
    verdict = classify_counts(reward=reward, scored=scored, taint=taint)["verdict"]

    stop_reason, limit_hit = _stop_and_limit(result, job_dir, agent_steps)
    diff_lines, files_touched, diff_source = _diff_stats(trial_dir, agent_steps)

    first_edit = signals.get("first_repo_edit") or {}
    first_paths = first_edit.get("paths") if isinstance(first_edit, dict) else None

    def _mean(total: Any) -> float | None:
        if not isinstance(total, (int, float)) or n_agent == 0:
            return None
        return round(float(total) / n_agent, 3)

    return {
        "trial": trial_dir.name,
        "job": job_dir.name,
        "task_id": _task_id(result, trial_dir.name),
        "arm": _arm(job_dir.name, trial_dir.name),
        "n_steps": len(steps),
        "n_agent_calls": n_agent,
        "wall_time_s": _wall_time_s(result),
        "input_tokens": signals.get("prompt_tokens"),
        "output_tokens": signals.get("completion_tokens"),
        "mean_input_per_call": _mean(signals.get("prompt_tokens")),
        "mean_output_per_call": _mean(signals.get("completion_tokens")),
        "tools_used": sorted(verbs),
        "tool_counts_json": _json.dumps(verbs, sort_keys=True),
        "first_edit_step": first_edit.get("step") if isinstance(first_edit, dict) else None,
        "first_edit_paths": list(first_paths) if isinstance(first_paths, list) else [],
        "n_test_runs": n_runs,
        "test_runs_passed": passed,
        "test_runs_failed": failed,
        "last_test_step": last_test,
        "n_errors": n_errors,
        "n_exceptions": n_exceptions,
        "loop_onset_step": loop_onset,
        "max_identical_run": signals.get("max_identical_run"),
        "n_fetch_attempts": len(attempts),
        "first_fetch_step": min(fetch_steps) if fetch_steps else None,
        "n_fetch_confirmed": len(confirmed),
        "copy_verdict": verdict,
        "stop_reason": stop_reason,
        "limit_hit": limit_hit,
        "reward": reward,
        "final_diff_lines": diff_lines,
        "files_touched": files_touched,
        "diff_source": diff_source,
    }


def collect_features(paths: list[Path]) -> list[dict[str, Any]]:
    """Feature rows for every trial under each job dir or runs root."""
    from evallab.live_watch import discover_trials

    resolved = [p if p.is_absolute() else Path.cwd() / p for p in paths]
    rows = []
    for job_dir, trial_dir in discover_trials([p.resolve() for p in resolved]):
        try:
            rows.append(trial_features(job_dir, trial_dir))
        except Exception:  # noqa: BLE001 -- one bad trial never kills the table
            rows.append(
                {column: None for column in COLUMNS}
                | {"trial": trial_dir.name, "job": job_dir.name}
            )
    rows.sort(key=lambda row: (str(row.get("job")), str(row.get("trial"))))
    return rows


def write_features(rows: list[dict[str, Any]], out: Path) -> tuple[Path, Path]:
    """Write ``<out>.parquet`` plus a ``<out>.csv`` sibling; return both."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    out = Path(out)
    parquet_path = out.with_suffix(".parquet")
    csv_path = out.with_suffix(".csv")
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    schema = pa.schema(
        [
            pa.field("trial", pa.string()),
            pa.field("job", pa.string()),
            pa.field("task_id", pa.string()),
            pa.field("arm", pa.string()),
            pa.field("n_steps", pa.int64()),
            pa.field("n_agent_calls", pa.int64()),
            pa.field("wall_time_s", pa.float64()),
            pa.field("input_tokens", pa.int64()),
            pa.field("output_tokens", pa.int64()),
            pa.field("mean_input_per_call", pa.float64()),
            pa.field("mean_output_per_call", pa.float64()),
            pa.field("tools_used", pa.list_(pa.string())),
            pa.field("tool_counts_json", pa.string()),
            pa.field("first_edit_step", pa.int64()),
            pa.field("first_edit_paths", pa.list_(pa.string())),
            pa.field("n_test_runs", pa.int64()),
            pa.field("test_runs_passed", pa.int64()),
            pa.field("test_runs_failed", pa.int64()),
            pa.field("last_test_step", pa.int64()),
            pa.field("n_errors", pa.int64()),
            pa.field("n_exceptions", pa.int64()),
            pa.field("loop_onset_step", pa.int64()),
            pa.field("max_identical_run", pa.int64()),
            pa.field("n_fetch_attempts", pa.int64()),
            pa.field("first_fetch_step", pa.int64()),
            pa.field("n_fetch_confirmed", pa.int64()),
            pa.field("copy_verdict", pa.string()),
            pa.field("stop_reason", pa.string()),
            pa.field("limit_hit", pa.string()),
            pa.field("reward", pa.float64()),
            pa.field("final_diff_lines", pa.int64()),
            pa.field("files_touched", pa.list_(pa.string())),
            pa.field("diff_source", pa.string()),
        ]
    )
    table = pa.Table.from_pylist(
        [{column: row.get(column) for column in COLUMNS} for row in rows],
        schema=schema,
    )
    pq.write_table(table, parquet_path)

    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    column: (
                        ";".join(str(v) for v in (row.get(column) or []))
                        if isinstance(row.get(column), list)
                        else row.get(column)
                    )
                    for column in COLUMNS
                }
            )
    return parquet_path, csv_path


def _features_command(args: argparse.Namespace, root: Path, *, harbor: Any | None = None) -> int:
    del harbor
    from evallab.cli import _resolve

    rows = collect_features([_resolve(root, Path(p)) for p in args.paths])
    parquet_path, csv_path = write_features(rows, _resolve(root, Path(args.out)))
    print(f"trials: {len(rows)}")
    print(f"parquet: {parquet_path}")
    print(f"csv: {csv_path}")
    return 0


def build_features_parser(commands: argparse._SubParsersAction) -> None:
    """Register the ``evallab features`` subcommand (one self-contained block)."""
    parser = commands.add_parser(
        "features", help="One-row-per-trial deterministic feature table (parquet + csv)"
    )
    parser.add_argument("paths", nargs="+", help="Job dir(s) or runs root(s)")
    parser.add_argument("--out", required=True, help="Output path stem (.parquet + .csv siblings)")
    parser.set_defaults(func=_features_command)
