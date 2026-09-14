"""No-spend SFT and online-RL eligibility over native Harbor trials.

ATIF text cannot reconstruct generation-time behavior-policy probabilities.
Capture presence is reported, never qualified. Usage sums only recorded agent
metrics; null means no values were recorded, not zero tokens consumed.

Run with ``python -m evallab.training_eligibility JOB_OR_TRIAL [--json-out PATH]``.
The output path must be new; existing files and symlinks are never replaced.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evallab.tracing import (
    TraceError,
    is_control_trial,
    is_job_dir,
    is_trial_dir,
    load_trajectory,
    trajectory_path_for,
)


def _is_text_message(message: Any) -> bool:
    """Recognize plain text or ATIF text parts, not arbitrary truthy JSON."""
    return isinstance(message, str) or (
        isinstance(message, list)
        and all(
            isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
            and part.get("source") is None
            for part in message
        )
    )


def _is_tool_calls(calls: Any) -> bool:
    return isinstance(calls, list) and all(
        isinstance(call, dict)
        and isinstance(call.get("tool_call_id"), str)
        and bool(call["tool_call_id"].strip())
        and isinstance(call.get("function_name"), str)
        and bool(call["function_name"].strip())
        and isinstance(call.get("arguments"), dict)
        for call in calls
    )


def evaluate_trial(trial_dir: Path) -> dict[str, Any]:
    """Return evidence eligibility, without inferring missing actor metadata.

    Malformed trajectory containers raise TraceError. Unsupported actor output
    makes SFT ineligible; only text and structured ATIF tool calls are supported.
    Missing/empty actor output is reported as ``no_agent_steps``.
    """
    if not trial_dir.is_dir():
        raise TraceError(f"not a trial directory: {trial_dir}")
    trajectory_path = trajectory_path_for(trial_dir)
    trajectory_present = trajectory_path.is_file()
    control_trial = (trial_dir / "agent" / "oracle.txt").is_file() or is_control_trial(trial_dir)
    trajectory = load_trajectory(trajectory_path) if trajectory_present else {}
    steps = trajectory.get("steps", [])
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        raise TraceError(f"trajectory steps must be a list of objects: {trajectory_path}")
    agent_steps = [step for step in steps if step.get("source") == "agent"]
    model_names = {
        step["model_name"]
        for step in agent_steps
        if isinstance(step.get("model_name"), str) and step["model_name"].strip()
    }
    missing_model = any(
        not isinstance(step.get("model_name"), str) or not step["model_name"].strip()
        for step in agent_steps
    )
    has_text = False
    has_tool_calls = False
    invalid_output = False
    for step in agent_steps:
        message = step.get("message")
        if message is not None:
            if not _is_text_message(message):
                invalid_output = True
            elif isinstance(message, str):
                has_text |= bool(message.strip())
            else:
                has_text |= any(part["text"].strip() for part in message)
        calls = step.get("tool_calls")
        if calls is not None:
            if not _is_tool_calls(calls):
                invalid_output = True
            else:
                has_tool_calls |= bool(calls)
    reasons = []
    if control_trial:
        reasons.append("control_trial_no_model")
    if not trajectory_present:
        reasons.append("no_trajectory")
    elif not has_text and not has_tool_calls:
        reasons.append("no_agent_steps")
    if invalid_output:
        reasons.append("unsupported_agent_output")
    if missing_model:
        reasons.append("missing_model_name")
    if len(model_names) > 1:
        reasons.append("mixed_actor_identity")

    capture_present = any(
        path.is_file() for path in (trial_dir / "agent").rglob("proxy_capture.json")
    )
    usage: dict[str, int | None] = {"prompt_tokens": None, "completion_tokens": None}
    for step in agent_steps:
        metrics = step.get("metrics")
        if metrics is None:
            continue
        if not isinstance(metrics, dict):
            raise TraceError(f"agent metrics must be an object: {trajectory_path}")
        for key in usage:
            value = metrics.get(key)
            if value is not None:
                if type(value) is not int or value < 0:
                    raise TraceError(f"{key} must be a nonnegative integer: {trajectory_path}")
                usage[key] = (usage[key] or 0) + value
    reasoning_effort = agent_steps[0].get("reasoning_effort") if agent_steps else None
    return {
        "trial_dir": str(trial_dir),
        "trajectory_present": trajectory_present,
        "control_trial": control_trial,
        "sft": {"eligible": not reasons, "reasons": reasons},
        "online_rl": {
            "eligible": False,
            "capture_present": capture_present,
            "reasons": [
                "capture_present_unqualified"
                if capture_present
                else "missing_behavior_policy_probabilities"
            ],
        },
        "identity": {
            "model_name": next(iter(model_names))
            if len(model_names) == 1 and not missing_model
            else None,
            "reasoning_effort": reasoning_effort if isinstance(reasoning_effort, str) else None,
            "tool_protocol": "function_calls"
            if has_tool_calls
            else "text_only"
            if has_text
            else None,
        },
        "usage": usage,
    }


def evaluate_job(job_dir: Path) -> list[dict[str, Any]]:
    """Evaluate sorted immediate native trial directories, ignoring job artifacts."""
    if not job_dir.is_dir():
        raise TraceError(f"not a job directory: {job_dir}")
    return [evaluate_trial(child) for child in sorted(job_dir.iterdir()) if is_trial_dir(child)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Native Harbor job or trial directory")
    parser.add_argument(
        "--json-out", type=Path, help="Also write the JSON verdict array to a new file"
    )
    args = parser.parse_args(argv)
    try:
        verdicts = (
            [evaluate_trial(args.path)]
            if is_trial_dir(args.path) and not is_job_dir(args.path)
            else evaluate_job(args.path)
        )
        output = json.dumps(verdicts, indent=2) + "\n"
        if args.json_out is not None:
            with args.json_out.open("x", encoding="utf-8") as handle:
                handle.write(output)
    except (TraceError, OSError) as exc:
        parser.error(str(exc))
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
