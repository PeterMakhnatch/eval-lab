"""Per-trial metrics for Harbor job dirs (Trace Lab probe-02-mimo-kit).

Reads one or more Harbor 0.21 job directories and writes one JSON row per
trial with: task/trial ids, reward (missing or -1 = infra/no-score, kept
separate), turns, tool calls per turn, total tokens (if recorded), stop
flags (context overflow / step-timeout limits), Xiaomi MiMo-V2.6 tool-call
repetition stats, and a failure class from Eval Lab's deterministic
``evallab.trial_diagnosis`` when importable (else reward/exception only --
we never build a second classifier).

Usage::

    uv run --no-project --with ~/Developer/eval-lab/.worktrees/har87-readonly \\
        python metrics.py <job_dir>... --out metrics.jsonl
    python3 metrics.py <job_dir>... --out metrics.jsonl  # stdlib only,
        failure_class falls back (reason recorded per row)

Turn = one ATIF step with source "agent"/"assistant" (one model turn).
A "call" = one entry of that step's ``tool_calls`` list, canonicalized as
JSON with sorted keys and whitespace-normalized strings, exactly as the
MiMo blog canonicalizes (tool name + JSON-normalized arguments).

Terminus-2 mapping: Terminus-2 also persists ATIF ``agent/trajectory.json``.
Its tool arguments carry the terminal input as a ``keystrokes``/``command``
string, so one keystrokes/command entry = one call, and the generic
tool+arguments canonicalization covers it. Harness-recorded timing keys
(``duration``/``timeout``) are dropped from the identity.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

DEFAULT_EVALLAB_RO = Path.home() / (
    "Developer/eval-lab/.worktrees/har87-readonly/src"
)

AGENT_SOURCES = {"agent", "assistant"}

# Keys inside tool-call arguments that carry the human-meaningful command.
COMMAND_KEYS = ("command", "keystrokes", "cmd", "script", "input", "code")

# Wall-clock metadata recorded by the harness, not chosen by the model:
# dropped from call identity (see canonical_call).
TIMING_KEYS = frozenset({"duration", "duration_ms", "timeout", "timeout_ms"})

STOP_PATTERNS = {
    "context_overflow": re.compile(
        r"context (window|length|overflow)|max_?tokens.{0,20}exceed|"
        r"token limit|context.{0,20}(exceed|full)|summariz",
        re.IGNORECASE,
    ),
    "step_or_timeout": re.compile(
        r"timeout|timed out|deadline exceeded|max.{0,10}steps|"
        r"step (limit|budget|count).{0,20}exceed|too many steps|"
        r"turn budget|agent_timeout",
        re.IGNORECASE,
    ),
}
WS_RE = re.compile(r"\s+")



def _norm_str(value: str) -> str:
    return WS_RE.sub(" ", value).strip()


def _norm_json(value):
    if isinstance(value, str):
        return _norm_str(value)
    if isinstance(value, dict):
        return {key: _norm_json(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_norm_json(item) for item in value]
    return value


def canonical_call(tool_name, arguments) -> str:
    """Canonical form of one tool call (MiMo blog: sorted-keys JSON).

    Timing-only argument keys (duration/timeout, harness-recorded) are
    dropped: the same keystrokes with a different recorded duration are
    still the same call. Verified identical to full-args canonicalization
    on 20 real Terminus-2 trajectories (134 calls / 84 unique both ways).
    """
    if isinstance(arguments, dict):
        arguments = {
            key: value for key, value in arguments.items() if key not in TIMING_KEYS
        }
    payload = {"tool": (tool_name or ""), "args": _norm_json(arguments)}
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def command_string(arguments) -> str | None:
    if isinstance(arguments, str):
        text = arguments.strip()
        return text or None
    if isinstance(arguments, dict):
        for key in COMMAND_KEYS:
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def repetition_stats(turn_calls: list[list[str]]) -> dict:
    """MiMo exact within-turn repetition + flooding + across-turn repeats."""
    per_turn: list[float] = []
    flooding_turns = 0
    seen: set[str] = set()
    across_repeats = 0
    total = 0
    for calls in turn_calls:
        n = len(calls)
        total += n
        if n == 0:
            per_turn.append(0.0)
            continue
        unique = len(set(calls))
        per_turn.append((n - unique) / n)
        if n > 10:
            flooding_turns += 1
        for call in calls:
            if call in seen:
                across_repeats += 1
            else:
                seen.add(call)
    scored = [rate for calls, rate in zip(turn_calls, per_turn, strict=False) if calls]
    return {
        "per_turn_repetition": [round(rate, 4) for rate in per_turn],
        "mean_repetition": round(sum(scored) / len(scored), 4) if scored else 0.0,
        "max_repetition": round(max(scored), 4) if scored else 0.0,
        "flooding_turns": flooding_turns,
        "flooding_rate": round(flooding_turns / len(turn_calls), 4)
        if turn_calls
        else 0.0,
        "across_turn_repeat_rate": round(across_repeats / total, 4) if total else 0.0,
        "total_calls": total,
        "unique_calls": len(seen),
    }


def read_reward(trial_dir: Path):
    """Return (reward, scored, source). Missing or -1 -> not scored."""
    try:
        result = json.loads((trial_dir / "result.json").read_text())
    except (OSError, ValueError):
        result = {}
    verifier = result.get("verifier_result") or {}
    rewards = verifier.get("rewards") or {}
    reward = rewards.get("reward", "MISSING")
    source = "verifier_result"
    if reward == "MISSING":
        reward_txt = trial_dir / "verifier" / "reward.txt"
        try:
            reward = float(reward_txt.read_text().strip())
            source = "reward.txt"
        except (OSError, ValueError):
            return None, False, "missing"
    try:
        reward = float(reward)
    except (TypeError, ValueError):
        return None, False, source
    if reward == -1:
        return reward, False, source
    return reward, True, source


def read_exception_class(trial_dir: Path):
    try:
        result = json.loads((trial_dir / "result.json").read_text())
    except (OSError, ValueError):
        return None
    info = result.get("exception_info")
    if info is None:
        return None
    if isinstance(info, dict):
        return (
            info.get("className")
            or info.get("exception_type")
            or info.get("type")
            or info.get("class")
            or "exception"
        )
    return str(info)


def read_tokens(trial_dir: Path, trajectory: dict | None):
    """Total tokens if recorded, else None. Never impute."""
    if trajectory:
        metrics = trajectory.get("final_metrics") or {}
        total = metrics.get("extra", {}).get("total_tokens")
        if isinstance(total, (int, float)):
            return int(total), "trajectory.final_metrics"
        prompt = metrics.get("total_prompt_tokens")
        completion = metrics.get("total_completion_tokens")
        if isinstance(prompt, (int, float)) and isinstance(
            completion, (int, float)
        ):
            return int(prompt + completion), "trajectory.final_metrics"
    try:
        result = json.loads((trial_dir / "result.json").read_text())
    except (OSError, ValueError):
        return None, "missing"
    agent_result = result.get("agent_result") or {}
    inputs = agent_result.get("n_input_tokens")
    outputs = agent_result.get("n_output_tokens")
    if isinstance(inputs, (int, float)) and isinstance(outputs, (int, float)):
        return int(inputs + outputs), "result.json:agent_result"
    return None, "missing"


def turn_cap_hit(trial_dir: Path, trajectory: dict | None, turns: int) -> str:
    """Evidence when the agent was cut off by its configured turn cap.

    Using every allowed turn is a cap stop unless the last agent turn
    declared completion itself (Terminus-2 `mark_task_complete`).
    """
    agent = _read_json(trial_dir / "config.json").get("agent") or {}
    kwargs = agent.get("kwargs") or {}
    agent_steps = [
        step
        for step in (trajectory or {}).get("steps") or []
        if isinstance(step, dict)
        and str(step.get("source", "")).lower() in AGENT_SOURCES
    ]
    if agent_steps and any(
        isinstance(call, dict) and call.get("function_name") == "mark_task_complete"
        for call in agent_steps[-1].get("tool_calls") or []
    ):
        return ""
    for key in ("max_turns", "max_episodes"):
        cap = kwargs.get(key)
        if isinstance(cap, int) and cap > 0 and turns >= cap:
            return f"turn cap reached: {turns}/{cap} turns (agent.kwargs.{key})"
    return ""


def detect_stop(trial_dir: Path, trajectory: dict | None, turns: int = 0):
    haystacks: list[str] = []
    exc = read_exception_class(trial_dir)
    if exc:
        haystacks.append(str(exc))
    try:
        info = json.loads((trial_dir / "result.json").read_text()).get(
            "exception_info"
        )
        if info:
            haystacks.append(json.dumps(info)[:2000])
    except (OSError, ValueError):
        pass
    log = trial_dir / "trial.log"
    if log.is_file():
        try:
            with open(log, encoding="utf-8", errors="replace") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 8000))
                haystacks.append(handle.read()[-8000:])
        except OSError:
            pass
    if trajectory:
        steps = trajectory.get("steps") or []
        if steps and isinstance(steps[-1], dict):
            haystacks.append(str(steps[-1].get("message", ""))[-2000:])
    flags: dict[str, bool] = {}
    evidence = ""
    for name, pattern in STOP_PATTERNS.items():
        hit = ""
        for hay in haystacks:
            match = pattern.search(hay)
            if match:
                start = max(0, match.start() - 80)
                hit = _norm_str(hay[start : match.end() + 80])
                break
        flags[name] = bool(hit)
        if hit and not evidence:
            evidence = hit
    cap = turn_cap_hit(trial_dir, trajectory, turns)
    if cap:
        flags["step_or_timeout"] = True
        evidence = f"{cap} | {evidence}" if evidence else cap
    return {
        "context_overflow": flags["context_overflow"],
        "step_or_timeout_limit": flags["step_or_timeout"],
        "evidence": evidence[:300],
    }


def load_trajectory(trial_dir: Path):
    path = trial_dir / "agent" / "trajectory.json"
    if not path.is_file():
        return None, None
    try:
        return json.loads(path.read_text(encoding="utf-8")), str(path)
    except (OSError, ValueError):
        return None, str(path)


def try_diagnose(trial_dir: Path, evallab_src: str | None):
    """Use evallab.trial_diagnosis when importable; else fall back.

    Returns (failure_class, failure_modes, source, error). The fallback
    reports reward/exception fields only -- never a second classifier.
    """
    if evallab_src and Path(evallab_src).is_dir():
        sys.path.insert(0, evallab_src)
    try:
        from evallab.trial_diagnosis import diagnose_trial  # noqa: E402

        diagnosis = diagnose_trial(trial_dir)
        modes = [mode.mode for mode in diagnosis.modes]
        label = diagnosis.heuristic_label or (
            modes[0] if modes else diagnosis.outcome
        )
        return label, modes, "evallab.trial_diagnosis", None
    except Exception as exc:  # noqa: BLE001 -- reason is recorded
        return None, [], "reward_exception_fallback", (
            f"{type(exc).__name__}: {exc}"
        )
    finally:
        if evallab_src and sys.path and sys.path[0] == evallab_src:
            sys.path.pop(0)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_trial_dir(path: Path) -> bool:
    """Harbor trial, not job: trial config.json names the trial.

    Job dirs also carry result.json/config.json (job-level: n_total_trials,
    job_name), so "has result.json" alone misreads a job as a trial. A trial
    that crashed before writing result.json is still a trial (config.json is
    written at start) and must surface as an unscored row, not vanish.
    """
    if "trial_name" in _read_json(path / "config.json"):
        return True
    result = _read_json(path / "result.json")
    return bool(result) and "n_total_trials" not in result


def find_trials(root: Path, depth: int = 3) -> list[Path]:
    """Trial dirs at or under root (a trial, job, or dir of jobs)."""
    if is_trial_dir(root):
        return [root]
    if depth == 0:
        return []
    try:
        children = sorted(child for child in root.iterdir() if child.is_dir())
    except OSError:
        print(f"warn: cannot list {root}", file=sys.stderr)
        return []
    found: list[Path] = []
    for child in children:
        found.extend(find_trials(child, depth - 1))
    return found


def trial_rows(job_dir: Path, evallab_src: str | None) -> list[dict]:
    rows: list[dict] = []
    for trial_dir in find_trials(job_dir):
        result = _read_json(trial_dir / "result.json")
        config = _read_json(trial_dir / "config.json")
        reward, scored, reward_source = read_reward(trial_dir)
        trajectory, traj_path = load_trajectory(trial_dir)
        turn_calls: list[list[str]] = []
        turns = 0
        agent_name = ((result.get("agent_info") or {}).get("name")) or (
            (result.get("config") or {}).get("agent") or {}
        ).get("name", "unknown")
        model_info = (result.get("agent_info") or {}).get("model_info") or {}
        model_name = model_info.get("name")
        steps = 0
        if trajectory and isinstance(trajectory.get("steps"), list):
            steps = len(trajectory["steps"])
            for step in trajectory["steps"]:
                if not isinstance(step, dict):
                    continue
                if str(step.get("source", "")).lower() not in AGENT_SOURCES:
                    continue
                turns += 1
                calls = step.get("tool_calls") or []
                canon = []
                for call in calls:
                    if not isinstance(call, dict):
                        continue
                    canon.append(
                        canonical_call(
                            call.get("function_name"), call.get("arguments")
                        )
                    )
                turn_calls.append(canon)
        rep = repetition_stats(turn_calls)
        total_tokens, tokens_source = read_tokens(trial_dir, trajectory)
        failure_class, modes, fc_source, fc_error = try_diagnose(
            trial_dir, evallab_src
        )
        rows.append(
            {
                "job_dir": str(trial_dir.parent),
                "task": result.get("task_name")
                or Path(str((config.get("task") or {}).get("path", "unknown"))).name,
                "trial": result.get("trial_name")
                or config.get("trial_name", trial_dir.name),
                "trial_dir": str(trial_dir),
                "result_present": bool(result),
                "agent": agent_name or "unknown",
                "model": model_name,
                "reward": reward,
                "scored": scored,
                "infra_no_score": not scored,
                "reward_source": reward_source,
                "exception_class": read_exception_class(trial_dir),
                "turns": turns,
                "trajectory_steps": steps,
                "calls_per_turn": [len(calls) for calls in turn_calls],
                "total_calls": rep["total_calls"],
                "total_tokens": total_tokens,
                "tokens_source": tokens_source,
                "stop": detect_stop(trial_dir, trajectory, turns),
                "repetition": rep,
                "trajectory_present": trajectory is not None,
                "trajectory_path": traj_path,
                "failure_class": failure_class,
                "failure_modes": modes,
                "failure_class_source": fc_source,
                "evallab_error": fc_error,
            }
        )
    return rows


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dir", nargs="+", help="Harbor job directories")
    parser.add_argument("--out", required=True, help="output metrics.jsonl")
    parser.add_argument(
        "--evallab-src",
        default=str(DEFAULT_EVALLAB_RO),
        help="evallab src dir for trial_diagnosis ('' disables, uses fallback)",
    )
    args = parser.parse_args(argv)
    evallab_src = args.evallab_src or None
    rows: list[dict] = []
    for job in args.job_dir:
        rows.extend(trial_rows(Path(job), evallab_src))
    out = Path(args.out)
    with open(out, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote {len(rows)} trial rows -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
