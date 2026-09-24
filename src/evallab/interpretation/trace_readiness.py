"""Deterministic, CPU-only readiness gate over a Harbor trial directory.

Answers one question: can the Lab interpret this trace? It inspects
``agent/trajectory.json`` (falling back to ``trajectory.json``), calls the
existing :func:`evallab.interpretation.trajectory_ir.build_trajectory_ir`
and :func:`evallab.interpretation.trajectory_quality.evaluate_trial_quality`,
and emits a small pydantic-free dict/JSON verdict.

The trial directory is only ever read, never modified. No model calls, no
docker, no network.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Literal

from evallab.evidence.atif import SUPPORTED_SCHEMA_VERSIONS
from evallab.interpretation.trajectory_ir import build_trajectory_ir
from evallab.interpretation.trajectory_quality import evaluate_trial_quality
from evallab.traj import ChainResolution, _chain_action_steps, _resolve_chain_segments

Verdict = Literal["interpretable", "degraded", "uninterpretable"]

MISSING_TRAJECTORY_REASON = "missing_trajectory_file"
ZERO_TOKEN_REASON = "zero_token_metrics"

_FATAL_QUALITY_STATUSES = frozenset({"fail", "failed", "quarantine", "quarantined"})


def _locate_atif(trial_dir: Path) -> Path | None:
    """Return the ATIF trajectory file, preferring ``agent/trajectory.json``."""
    candidate = trial_dir / "agent" / "trajectory.json"
    if candidate.is_file():
        return candidate
    fallback = trial_dir / "trajectory.json"
    if fallback.is_file():
        return fallback
    return None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _read_json_mapping(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _count_calls_and_observations(steps: list[Any]) -> tuple[int, int]:
    tool_calls = 0
    observations = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        calls = step.get("tool_calls")
        if isinstance(calls, list):
            tool_calls += sum(1 for call in calls if isinstance(call, dict))
        observation = step.get("observation")
        if isinstance(observation, dict):
            results = observation.get("results")
            if isinstance(results, list):
                observations += len(results)
    return tool_calls, observations


def _step_token_sums(steps: list[Any]) -> tuple[int, int]:
    prompt_total = 0
    completion_total = 0
    for step in steps:
        if not isinstance(step, dict):
            continue
        metrics = step.get("metrics")
        if not isinstance(metrics, dict):
            continue
        prompt = _as_int(metrics.get("prompt_tokens"))
        if prompt is not None:
            prompt_total += prompt
        completion = _as_int(metrics.get("completion_tokens"))
        if completion is not None:
            completion_total += completion
    return prompt_total, completion_total


def _stop_reason_present(raw: dict[str, Any], result: dict[str, Any]) -> bool:
    if isinstance(raw.get("stop_reason"), str) and raw["stop_reason"].strip():
        return True
    final_metrics = raw.get("final_metrics")
    if isinstance(final_metrics, dict):
        reason = final_metrics.get("stop_reason")
        if isinstance(reason, str) and reason.strip():
            return True
    steps = raw.get("steps")
    if isinstance(steps, list):
        for step in steps:
            if isinstance(step, dict):
                reason = step.get("stop_reason")
                if isinstance(reason, str) and reason.strip():
                    return True
    reason = result.get("stop_reason")
    return isinstance(reason, str) and bool(reason.strip())


def check_trace_readiness(
    trial_dir: str | Path,
    *,
    repo_root: str | Path | None = None,
) -> dict[str, Any]:
    """Check whether a Harbor trial directory holds an interpretable trace.

    Never raises on malformed input and never modifies ``trial_dir``.
    """
    trial = Path(trial_dir)
    resolved = trial.resolve()
    root = Path(repo_root) if repo_root is not None else resolved.parent

    def skeleton() -> dict[str, Any]:
        return {
            "trial_dir": str(trial),
            "atif_present": False,
            "atif_schema_version": None,
            "agent_name": None,
            "steps": 0,
            "tool_calls": 0,
            "observations": 0,
            "final_metrics_present": False,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "stop_reason_present": False,
            "quality_status": "unknown",
            "ir_built": False,
            "ir_error": None,
            "verdict": "uninterpretable",
            "reasons": [],
        }

    atif_path = _locate_atif(resolved)
    if atif_path is None:
        report = skeleton()
        try:
            quality_report, _ = evaluate_trial_quality(resolved)
            report["quality_status"] = str(quality_report.status)
        except Exception:
            pass
        try:
            build_trajectory_ir(target=resolved, repo_root=root)
            report["ir_built"] = True
        except Exception as exc:
            report["ir_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        report["reasons"] = [MISSING_TRAJECTORY_REASON]
        return report

    try:
        raw = json.loads(atif_path.read_text(encoding="utf-8"))
    except Exception as exc:
        report = skeleton()
        report["atif_present"] = True
        try:
            quality_report, _ = evaluate_trial_quality(resolved)
            report["quality_status"] = str(quality_report.status)
        except Exception:
            pass
        try:
            build_trajectory_ir(target=resolved, repo_root=root)
            report["ir_built"] = True
        except Exception as build_exc:
            report["ir_error"] = f"{type(build_exc).__name__}: {str(build_exc)[:200]}"
        report["reasons"] = [f"unparseable_trajectory_json:{type(exc).__name__}"]
        return report

    if not isinstance(raw, dict):
        report = skeleton()
        report["atif_present"] = True
        try:
            quality_report, _ = evaluate_trial_quality(resolved)
            report["quality_status"] = str(quality_report.status)
        except Exception:
            pass
        report["reasons"] = ["invalid_trajectory_shape"]
        return report

    try:
        resolution = _resolve_chain_segments(atif_path, raw, resolved)
    except Exception:
        resolution = ChainResolution(
            segments=((atif_path, raw, ""),),
            complete=False,
            stopped_ref="unresolvable",
            stop_cause="unparseable",
        )
    chain_incomplete_reason = None
    if not resolution.complete:
        ref = (resolution.stopped_ref or "unresolvable")[:120]
        chain_incomplete_reason = f"incomplete_continuation_chain:{ref}"
    terminal = resolution.segments[-1][1] if resolution.segments else raw
    # Stitched non-copied actions across the continuation chain; the terminal
    # document of a COMPLETE chain carries the cumulative native totals. An
    # incomplete chain degrades with its partial steps only: no stale partial
    # head is ever presented as the authoritative declaration.
    malformed_segments = sorted(
        segment_path.name
        for segment_path, segment_data, _ in resolution.segments
        if not isinstance(segment_data.get("steps"), list) or not segment_data.get("steps")
    )
    steps = [raw_step for _, raw_step in _chain_action_steps(resolution.segments)]
    schema_version = terminal.get("schema_version")
    if not isinstance(schema_version, str):
        schema_version = raw.get("schema_version")
    agent = terminal.get("agent")
    if not isinstance(agent, dict):
        agent = raw.get("agent")
    agent_name = agent.get("name") if isinstance(agent, dict) else None
    tool_calls, observations = _count_calls_and_observations(steps)
    final_metrics = terminal.get("final_metrics")
    final_metrics_present = isinstance(final_metrics, dict) and resolution.complete
    metrics_map = final_metrics if final_metrics_present else {}
    prompt_tokens = _as_int(metrics_map.get("total_prompt_tokens"))
    completion_tokens = _as_int(metrics_map.get("total_completion_tokens"))
    if prompt_tokens is None or completion_tokens is None:
        step_prompt, step_completion = _step_token_sums(steps)
        if prompt_tokens is None:
            prompt_tokens = step_prompt
        if completion_tokens is None:
            completion_tokens = step_completion
    result = _read_json_mapping(resolved / "result.json")

    report = skeleton()
    report["atif_present"] = True
    report["atif_schema_version"] = schema_version if isinstance(schema_version, str) else None
    report["agent_name"] = agent_name if isinstance(agent_name, str) else None
    report["steps"] = len(steps)
    report["tool_calls"] = tool_calls
    report["observations"] = observations
    report["final_metrics_present"] = final_metrics_present
    report["prompt_tokens"] = prompt_tokens
    report["completion_tokens"] = completion_tokens
    report["stop_reason_present"] = _stop_reason_present(raw, result)

    try:
        quality_report, _ = evaluate_trial_quality(resolved)
        report["quality_status"] = str(quality_report.status)
    except Exception as exc:
        report["quality_status"] = "unknown"
        report["reasons"].append(f"quality_error:{type(exc).__name__}")

    try:
        build_trajectory_ir(target=resolved, repo_root=root)
        report["ir_built"] = True
    except Exception as exc:
        report["ir_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

    reasons: list[str] = list(report["reasons"])
    if report["ir_error"] is not None:
        reasons.append(f"ir_build_failed:{str(report['ir_error']).split(':')[0]}")
    if not steps:
        return {**report, "verdict": "uninterpretable", "reasons": [*reasons, "missing_trajectory_steps"]}

    quality_status = str(report["quality_status"])
    if quality_status in _FATAL_QUALITY_STATUSES:
        reasons.append(f"quality_{quality_status}")
    if malformed_segments:
        reasons.append(f"continuation_segment_missing_steps:{','.join(malformed_segments)}")
    if chain_incomplete_reason is not None:
        reasons.append(chain_incomplete_reason)

    verdict: Verdict = "interpretable"
    if (
        report["ir_error"] is not None
        or quality_status in _FATAL_QUALITY_STATUSES
        or malformed_segments
        or chain_incomplete_reason is not None
    ):
        verdict = "degraded"
    return {**report, "verdict": verdict, "reasons": reasons}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m evallab.interpretation.trace_readiness",
        description="Deterministic readiness check: can the Lab interpret this Harbor trial trace?",
    )
    parser.add_argument("trial_dir", type=Path, help="Harbor trial directory to check")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root for path-jail resolution (defaults to the trial dir parent)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Print the readiness JSON for a trial directory."""
    args = _build_parser().parse_args(argv)
    report = check_trace_readiness(args.trial_dir, repo_root=args.repo_root)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
