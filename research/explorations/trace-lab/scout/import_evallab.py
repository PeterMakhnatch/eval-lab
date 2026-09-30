#!/usr/bin/env python3
"""Load Harbor runs into an Inspect Scout transcripts DB from Eval Lab records.

One reusable script for old ``raw_content`` runs (HAR-81: tool calls live
in ``extra.step_layers``) and new default-recorded runs (HAR-104: stock
``bash_command`` / ``mark_task_complete`` tool calls in one
``trajectory.json``). Each trial becomes one transcript with real tool
calls and Eval Lab metadata (task name, reward, verdict, stop reason,
tokens, trial id) -- never session IDs or ``task unknown``.

Usage::

    uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 -- \\
        python import_evallab.py <trial dirs or runs roots>... \\
        --db <transcripts dir> [--staging <dir>] [--overwrite]

Each input may be a trial directory (``result.json`` +
``agent/trajectory.json``), a job directory, or a runs root; trial
discovery reuses probe-02's ``find_trials`` (never forked). Metadata comes
from Eval Lab's ``build_run_report`` (the same code behind ``evallab
report run --json``); tool calls come from Eval Lab's
``effective_tool_calls`` (native calls win, recorded ``step_layers``
synthesized otherwise). Steps are stitched head + cont-N with probe-03's
``assemble_trial`` prefix-drop, so continuations stay one transcript and
step refs (``head#12``) match the rule scanners.

$0: local files only; no model calls, no uploads, no trial launches.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRACE_LAB = HERE.parent
for _p in (str(HERE), str(TRACE_LAB / "probe-03-capabilities")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
import capabilities as cap  # noqa: E402  (head+cont assembly, step refs)

try:
    from evallab.interpretation.run_report import build_run_report
except Exception:  # noqa: BLE001 -- caller reports; fallback uses result.json
    build_run_report = None  # type: ignore[assignment]

try:
    from evallab.step_layers import effective_tool_calls
except Exception:  # noqa: BLE001
    effective_tool_calls = None  # type: ignore[assignment]

import rules  # noqa: E402  (normalizer defaults; shares cap import)


def find_trials(inputs: list[Path]) -> list[Path]:
    """Expand trial dirs / job dirs / runs roots to trial directories."""
    sys.path.insert(0, str(TRACE_LAB / "probe-02-mimo-kit"))
    try:
        import metrics as probe02  # noqa: E402

        trials: list[Path] = []
        seen: set[str] = set()
        for raw in inputs:
            path = Path(raw).resolve()
            if not path.exists():
                print(f"warn: input missing: {raw}", file=sys.stderr)
                continue
            if (path / "result.json").is_file() and (path / "agent").is_dir():
                key = str(path)
                if key not in seen:
                    seen.add(key)
                    trials.append(path)
                continue
            for trial_dir in probe02.find_trials(path):
                key = str(trial_dir.resolve())
                if key not in seen:
                    seen.add(key)
                    trials.append(trial_dir.resolve())
        return sorted(trials)
    finally:
        sys.path.pop(0)


def _report_metadata(trial_dir: Path) -> dict:
    """Eval Lab metadata for one trial (report run JSON when available)."""
    if build_run_report is not None:
        try:
            report = build_run_report(trial_dir)
            identity = report.get("identity") or {}
            outcome = report.get("outcome") or {}
            tokens = report.get("tokens") or {}
            cost = report.get("cost") or {}
            return {
                "task": identity.get("task"),
                "trial_name": identity.get("trial_name") or trial_dir.name,
                "trial_id": identity.get("trial_id"),
                "job": identity.get("job") or trial_dir.parent.name,
                "agent": identity.get("agent"),
                "model": identity.get("model"),
                "session_id": identity.get("session_id"),
                "verdict": outcome.get("verdict"),
                "reward": outcome.get("reward"),
                "rewards": outcome.get("rewards"),
                "stop_reason": outcome.get("stop_reason"),
                "stop_detail": outcome.get("stop_detail"),
                "input_tokens": tokens.get("input"),
                "output_tokens": tokens.get("output"),
                "total_tokens": tokens.get("total"),
                "llm_steps": tokens.get("llm_steps"),
                "cost_usd": (cost or {}).get("total_usd"),
                "source": "evallab:build_run_report",
            }
        except Exception as exc:  # noqa: BLE001 -- fall back to result.json
            print(f"warn: build_run_report failed for {trial_dir}: {exc}", file=sys.stderr)
    try:
        result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"{trial_dir}: no readable result.json ({exc})") from exc
    agent_result = result.get("agent_result") or {}
    metadata = agent_result.get("metadata") or {}
    verifier = result.get("verifier_result") or {}
    rewards = verifier.get("rewards") or {}
    reward = rewards.get("reward")
    exception = result.get("exception_info") or {}
    stop_reason = metadata.get("stop_reason")
    if exception.get("exception_type") == "TrialBudgetExhaustedError":
        stop_reason = stop_reason or "trial_budget_exhausted"
    verdict = "not_scored" if reward is None else ("passed" if reward >= 1.0 else "failed")
    return {
        "task": result.get("task_name"),
        "trial_name": result.get("trial_name") or trial_dir.name,
        "trial_id": result.get("id"),
        "job": trial_dir.parent.name,
        "agent": None,
        "model": (result.get("config") or {}).get("agent", {}).get("model_name"),
        "session_id": None,
        "verdict": verdict,
        "reward": reward,
        "rewards": rewards,
        "stop_reason": stop_reason,
        "stop_detail": exception.get("exception_message") or "",
        "input_tokens": agent_result.get("n_input_tokens"),
        "output_tokens": agent_result.get("n_output_tokens"),
        "total_tokens": None,
        "llm_steps": None,
        "cost_usd": agent_result.get("cost_usd"),
        "source": "evallab:result.json",
    }


def _staged_tool_calls(step: dict) -> tuple[list[dict] | None, str]:
    """(tool_calls or None, source) using Eval Lab's read path."""
    native = step.get("tool_calls")
    if isinstance(native, list) and [c for c in native if isinstance(c, dict)]:
        return native, "native"
    if effective_tool_calls is not None:
        try:
            calls = effective_tool_calls(step)
        except Exception:  # noqa: BLE001 -- one bad step must not kill import
            calls = []
        if calls:
            return calls, "recorded_or_replay"
        return None, "none"
    extra = step.get("extra") if isinstance(step.get("extra"), dict) else {}
    layers = extra.get("step_layers") if isinstance(extra, dict) else None
    if isinstance(layers, dict):
        accepted = layers.get("accepted") if isinstance(layers.get("accepted"), dict) else {}
        if accepted.get("kind") in ("calls", "prose_completion"):
            calls = []
            for position, call in enumerate(accepted.get("calls") or []):
                if not isinstance(call, dict):
                    continue
                call_id = f"call_{step.get('step_id')}_{position + 1}"
                if call.get("task_complete") is True:
                    calls.append({
                        "tool_call_id": call_id,
                        "function_name": "mark_task_complete",
                        "arguments": {},
                    })
                elif isinstance(call.get("keystrokes"), str):
                    calls.append({
                        "tool_call_id": call_id,
                        "function_name": "bash_command",
                        "arguments": {
                            "keystrokes": call["keystrokes"],
                            "duration": call.get("duration_sec"),
                        },
                    })
            if calls:
                return calls, "recorded_or_replay"
    return None, "none"


def _exception_type(trial_dir: Path, meta: dict) -> str | None:
    try:
        result = json.loads((trial_dir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    exception = result.get("exception_info") or {}
    exc_type = exception.get("exception_type") or exception.get("className")
    if exc_type:
        return str(exc_type)
    stop = str(meta.get("stop_reason") or "")
    if stop.startswith("ceiling"):
        return "TrialBudgetExhaustedError"
    if stop == "agent_timeout":
        return "AgentTimeoutError"
    return None


def _limit_of(stop_reason: str | None, stop_detail: str | None = None) -> str | None:
    if stop_detail and "binding ceiling:" in stop_detail:
        tail = stop_detail.split("binding ceiling:", 1)[1].strip().split()[0]
        return tail.strip("(),.")
    if not stop_reason:
        return None
    if stop_reason.startswith("ceiling:"):
        return stop_reason.split(":", 1)[1]
    if stop_reason in ("agent_timeout", "trial_budget_exhausted"):
        return "timeout" if stop_reason == "agent_timeout" else "budget"
    return None


def _task_set_of(job: str | None) -> str | None:
    if not job:
        return None
    head = job.split("-")[0]
    return head if head.startswith("har") else job


def build_staged_trajectory(trial_dir: Path, staging_root: Path) -> Path:
    """Write the stitched stock-shaped trajectory; return its path."""
    coverage, assembled = cap.assemble_trial(trial_dir)
    agent = trial_dir / "agent"
    head_doc: dict | None = None
    for name in ["trajectory.json"] + [
        f"trajectory.cont-{n}.json" for n in coverage["cont_files"]
    ]:
        path = agent / name
        if path.is_file():
            try:
                head_doc = json.loads(path.read_text(encoding="utf-8"))
                break
            except ValueError:
                continue
    if head_doc is None:
        raise SystemExit(f"{trial_dir}: no readable trajectory document")

    steps: list[dict] = []
    for index, (docname, raw) in enumerate(assembled, start=1):
        step = copy.deepcopy(raw)
        ref = cap.step_ref(docname, raw)
        for result in ((step.get("observation") or {}).get("results") or []):
            if isinstance(result, dict):
                result.pop("subagent_trajectory_ref", None)
        calls, source = _staged_tool_calls(step)
        if calls:
            step["tool_calls"] = calls
        else:
            step.pop("tool_calls", None)
        step["step_id"] = index
        extra = step.get("extra") if isinstance(step.get("extra"), dict) else {}
        step["extra"] = {
            **extra,
            "trace_lab": {
                "ref": ref,
                "source_file": "trajectory.json" if docname == "head" else docname,
                "orig_step_id": raw.get("step_id"),
                "tool_calls_source": source,
            },
        }
        steps.append(step)

    trajectory = {
        key: copy.deepcopy(value)
        for key, value in head_doc.items()
        if key not in ("steps", "continued_trajectory_ref", "final_metrics")
    }
    trajectory["steps"] = steps
    if coverage.get("last_doc_final_metrics"):
        trajectory["final_metrics"] = coverage["last_doc_final_metrics"]
    trajectory["extra"] = {
        **(trajectory.get("extra") if isinstance(trajectory.get("extra"), dict) else {}),
        "trace_lab": {
            "staged_by": "trace-lab/scout/import_evallab.py",
            "assembly_pattern": coverage["assembly_pattern"],
            "orig_trial_dir": str(trial_dir),
        },
    }

    out_dir = staging_root / trial_dir.parent.name / trial_dir.name / "agent"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "trajectory.json"
    out_path.write_text(json.dumps(trajectory, indent=1) + "\n", encoding="utf-8")
    return out_path


async def _insert(db_dir: Path, staged: list[tuple[Path, Path, dict]]) -> int:
    from inspect_scout._transcript.database.factory import transcripts_db
    from inspect_scout.sources._atif.client import import_trajectory_model
    from inspect_scout.sources._atif.transcripts import _create_transcript

    model = import_trajectory_model()
    transcripts = []
    for trial_dir, staged_path, meta in staged:
        try:
            trajectory = model.model_validate_json(staged_path.read_bytes())
        except Exception as exc:  # noqa: BLE001
            print(f"warn: skipping invalid staged file {staged_path}: {exc}", file=sys.stderr)
            continue
        transcript = _create_transcript(trajectory, source_uri=str(staged_path))
        reward = meta.get("reward")
        verdict = meta.get("verdict")
        stop_reason = meta.get("stop_reason")
        success: bool | None = None
        if reward is not None:
            try:
                success = float(reward) >= 1.0
            except (TypeError, ValueError):
                success = None
        elif verdict in ("passed", "failed", "partial"):
            success = verdict == "passed"
        merged_metadata = {
            **(transcript.metadata or {}),
            "task": meta.get("task"),
            "trial": meta.get("trial_name"),
            "trial_id": meta.get("trial_id"),
            "job": meta.get("job"),
            "verdict": verdict,
            "reward": reward,
            "rewards": meta.get("rewards"),
            "stop_reason": stop_reason,
            "stop_detail": meta.get("stop_detail"),
            "input_tokens": meta.get("input_tokens"),
            "output_tokens": meta.get("output_tokens"),
            "llm_steps": meta.get("llm_steps"),
            "cost_usd": meta.get("cost_usd"),
            "trial_dir": str(trial_dir),
            "evallab_source": meta.get("source"),
        }
        transcript = transcript.model_copy(
            update={
                "task_set": _task_set_of(meta.get("job")),
                "task_id": meta.get("task"),
                "score": reward,
                "success": success,
                "error": _exception_type(trial_dir, meta),
                "limit": _limit_of(stop_reason, meta.get("stop_detail")),
                "metadata": merged_metadata,
            }
        )
        transcripts.append(transcript)

    db_dir.mkdir(parents=True, exist_ok=True)
    async with transcripts_db(str(db_dir)) as db:
        await db.insert(transcripts)
    return len(transcripts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("inputs", nargs="+", help="trial dirs, job dirs, or runs roots")
    parser.add_argument("--db", required=True, help="Scout transcripts directory to write")
    parser.add_argument(
        "--staging",
        default=None,
        help="staged stock-shaped trajectories dir (default: <db>_staging)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="re-stage and re-insert even when the staged file exists",
    )
    args = parser.parse_args(argv)

    db_dir = Path(args.db)
    staging_root = Path(args.staging) if args.staging else Path(str(db_dir) + "_staging")

    trials = find_trials([Path(p) for p in args.inputs])
    if not trials:
        raise SystemExit("no trials found under inputs")
    print(f"resolved {len(trials)} trial dirs", flush=True)

    staged: list[tuple[Path, Path, dict]] = []
    for trial_dir in trials:
        meta = _report_metadata(trial_dir)
        staged_path = staging_root / trial_dir.parent.name / trial_dir.name / "agent" / "trajectory.json"
        if staged_path.is_file() and not args.overwrite:
            pass
        else:
            staged_path = build_staged_trajectory(trial_dir, staging_root)
        staged.append((trial_dir, staged_path, meta))

    count = asyncio.run(_insert(db_dir, staged))
    with_tool_calls = sum(
        1 for _, path, _ in staged
        if '"tool_calls"' in path.read_text(encoding="utf-8")[:2000000]
    )
    print(f"inserted {count} transcripts into {db_dir} ({with_tool_calls} staged files carry tool_calls)")
    _ = rules.default_evallab_src()  # warm normalizer default for scanners
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
