"""Read Reef harness-gate step records as ATIF.

Reef ran these trajectories itself; Eval Lab only reads them. One Reef gate
episode (``episode.json`` plus its sibling ``session.jsonl``) becomes one
valid ATIF document with the origin recorded as Reef. Reef ``None`` scores
and failed episodes stay unscored: missing source fields stay missing.

Format mirror (local Reef checkout, ref ``07dfa883``):

- ``reef/train/cordis_backend/backend.py`` ``RECORD_EPISODES_DIR`` ("episodes",
  line 235), ``RECORD_EPISODE_FILE`` ("episode.json", line 236),
  ``_episode_name`` (``<side>-<task index>``, a repeat adding
  ``-<repeat>``, line 271), the ``evaluate`` pairing order (candidate and
  current interleave inside each pairing, lines ~1215-1244), and
  ``_write_episode_record`` (task, score, failure, path, exit_code, stdout,
  stderr, residue, lines ~1627-1645).
- The native agent session writer (``reef/harness/runners/native/graph.py``
  near lines 285-415): ``session``, ``turn/start``, ``step/start``,
  ``request/header``, ``assistant/message`` (step, content, tool_calls,
  finish, usage), ``tool/call`` (step, call_id, name, arguments),
  ``tool/result`` (step, call_id, name, content, is_error, error),
  ``step/end``, ``turn/end`` events as JSONL.
- ``reef/core/trajectories.py`` ``make_trajectory`` semantics are mirrored
  only in the sense that references define one trajectory and a recorded
  score attaches to it; this module never imports ``reef``.

Run with ``python -m evallab.evidence.reef_gate --steps-root <root>
--out <new dir> --run-label <label> [--results <results.jsonl>]``. The
output layout is deliberately not a native Harbor job: ATIF documents under
``trajectories/`` plus a ``manifest.json`` (``evidence_kind`` historical)
that the existing offline analyzer
(``research/analysis/harness-mechanics/analyze.py``) reads directly, a
``pairs.json`` paired view, a ``summary.json`` reproduction, and an
``intake.json`` provenance record. No decision rules, sign tests, or
calibration live here: HAR-72 owns those.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
from typing import Any

#: Agent name this importer registers Reef-native traffic under. Never a
#: control name: the harness-mechanics analyzer classifies oracle/nop runs
#: as unsupported, and these are evaluated agent episodes.
REEF_AGENT_NAME = "reef-harness"

#: Newest ATIF version ``evidence/atif`` accepts; kept in sync with
#: ``evallab.dsh.DEFAULT_SCHEMA_VERSION``.
DEFAULT_SCHEMA_VERSION = "ATIF-v1.7"

#: Reef commit whose step-record format this module mirrors. Recorded in
#: every intake so a later format drift is attributable.
REEF_FORMAT_REF = "07dfa883"

#: Episode sides the gate evaluates.
EVALUATION_SIDES = ("candidate", "current")

#: Pass threshold mirroring the exp04 analysis (``04_gate_aa.py``:
#: ``(score or 0.0) >= 1.0``). A data rule for pairing, not a gate decision.
PASS_THRESHOLD = 1.0

JsonObject = dict[str, Any]


class ReefGateError(RuntimeError):
    """A Reef gate record could not be read as ATIF."""


def parse_episode_name(dirname: str) -> dict[str, Any] | None:
    """Split ``<side>-<task index>[-<repeat>]`` into its recorded parts."""
    parts = dirname.split("-")
    if len(parts) not in (2, 3):
        return None
    side, task_index, *rest = parts
    if side not in EVALUATION_SIDES:
        return None
    if not task_index.isdigit():
        return None
    repeat = rest[0] if rest else "0"
    if not repeat.isdigit():
        return None
    return {"side": side, "task_index": int(task_index), "repeat": int(repeat)}


def _arguments(raw: Any) -> JsonObject:
    """Tool arguments arrive as a JSON string; keep them structured when possible."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"value": raw}
        return dict(parsed) if isinstance(parsed, dict) else {"value": raw}
    return {"value": raw}


def _read_jsonl_events(path: Path) -> list[JsonObject]:
    """Read a session file, skipping blank lines and torn trailing writes."""
    events: list[JsonObject] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def validate_atif_document(payload: JsonObject) -> str | None:
    """Return the fallback-validation error for an ATIF document, if any."""
    from evallab.evidence.atif import _validate_fallback

    return _validate_fallback(payload)


def _assistant_tool_calls(data: JsonObject) -> list[tuple[str, str, Any]]:
    """``(call_id, function name, raw arguments)`` from an assistant message."""
    calls: list[tuple[str, str, Any]] = []
    raw_calls = data.get("tool_calls")
    if not isinstance(raw_calls, list):
        return calls
    for index, entry in enumerate(raw_calls):
        if not isinstance(entry, dict):
            continue
        call_id = entry.get("id")
        if not isinstance(call_id, str) or not call_id:
            call_id = f"call_assistant_{index}"
        function = entry.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        arguments = function.get("arguments") if isinstance(function, dict) else None
        calls.append((call_id, str(name) if name else "unknown_tool", arguments))
    return calls


def parse_reef_gate_episode(
    episode_dir: Path,
    *,
    scenario: str | None = None,
    step: int | None = None,
    run_label: str | None = None,
    proposal_id: str | None = None,
    release_id: str | None = None,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> JsonObject:
    """Convert one Reef gate episode directory to an ATIF document.

    ``episode_dir`` holds ``episode.json`` beside ``session.jsonl``. Only
    recorded fields are carried; anything the source lacks stays absent.
    Raises :class:`ReefGateError` when no trajectory can be built.
    """
    episode_dir = Path(episode_dir)
    name_parts = parse_episode_name(episode_dir.name)
    if name_parts is None:
        raise ReefGateError(f"not a gate episode directory: {episode_dir}")
    try:
        episode = json.loads((episode_dir / "episode.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReefGateError(f"cannot read episode.json under {episode_dir}: {exc}") from exc
    if not isinstance(episode, dict):
        raise ReefGateError(f"episode.json under {episode_dir} is not an object")

    session_path = episode_dir / "session.jsonl"
    events = _read_jsonl_events(session_path) if session_path.is_file() else []

    detected_model: str | None = None
    for event in events:
        if event.get("type") != "session":
            continue
        data = event.get("data")
        if not isinstance(data, dict):
            continue
        model = data.get("model")
        if isinstance(model, str) and model.strip():
            detected_model = model.strip()
            break

    atif_steps: list[JsonObject] = []
    task = episode.get("task")
    if isinstance(task, str) and task.strip():
        atif_steps.append({"step_id": 1, "source": "user", "message": task})

    # Fold native events onto their Reef step coordinate so a step that
    # calls a tool and observes its result stays one ATIF step, mirroring
    # the (turn, step) folding in ``evallab.dsh.parse_session_to_atif``.
    # ATIF requires an observation's source_call_id to name a tool call in
    # the same step, so tool calls and results must share the step.
    buckets: dict[tuple[int, int], JsonObject] = {}
    order: list[tuple[int, int]] = []
    total_input = 0
    total_output = 0
    turn_end_reason: dict[str, Any] = {}

    def bucket_for(turn: int, step_no: int) -> JsonObject:
        key = (turn, step_no)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "message_parts": [],
                "calls": {},
                "results": [],
                "usage": None,
                "assistant": False,
            }
            buckets[key] = bucket
            order.append(key)
        return bucket

    for event in events:
        kind = event.get("type")
        data = event.get("data")
        if not isinstance(data, dict):
            if kind == "turn/end":
                continue
            continue
        if kind == "turn/end":
            turn = data.get("turn")
            if isinstance(turn, int) and not isinstance(turn, bool):
                turn_end_reason[str(turn)] = data.get("reason")
            continue
        if kind not in {"assistant/message", "tool/call", "tool/result"}:
            continue
        raw_step = data.get("step")
        step_no = raw_step if isinstance(raw_step, int) and not isinstance(raw_step, bool) else 0
        turn = 1
        bucket = bucket_for(turn, step_no)
        if kind == "assistant/message":
            bucket["assistant"] = True
            content = data.get("content")
            if isinstance(content, str):
                bucket["message_parts"].append(content)
            for call_id, name, arguments in _assistant_tool_calls(data):
                bucket["calls"].setdefault(call_id, (name, arguments))
            usage = data.get("usage")
            if isinstance(usage, dict):
                bucket["usage"] = usage
        elif kind == "tool/call":
            call_id = data.get("call_id")
            call_id = call_id if isinstance(call_id, str) and call_id else None
            name = data.get("name")
            if call_id is not None:
                bucket["calls"].setdefault(
                    call_id, (str(name) if name else "unknown_tool", data.get("arguments"))
                )
        elif kind == "tool/result":
            call_id = data.get("call_id")
            content = data.get("content")
            result: JsonObject = {
                "source_call_id": call_id if isinstance(call_id, str) and call_id else "unknown",
                "content": content if isinstance(content, str) else "",
            }
            extra: JsonObject = {}
            is_error = data.get("is_error")
            if is_error is True:
                extra["is_error"] = True
            error = data.get("error")
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message")
                if isinstance(code, str) and code:
                    extra["error_code"] = code
                if isinstance(message, str) and message:
                    extra["error_message"] = message
            if extra:
                result["extra"] = extra
            bucket["results"].append(result)

    for key in sorted(order):
        bucket = buckets[key]
        message = "".join(part for part in bucket["message_parts"] if isinstance(part, str))
        calls = bucket["calls"]
        results = bucket["results"]
        if not message and not calls and not results and not bucket["assistant"]:
            continue
        atif_step: JsonObject = {
            "step_id": len(atif_steps) + 1,
            "source": "agent",
            "message": message,
        }
        if detected_model:
            atif_step["model_name"] = detected_model
        if calls:
            atif_step["tool_calls"] = [
                {
                    "tool_call_id": call_id,
                    "function_name": name,
                    "arguments": _arguments(arguments),
                }
                for call_id, (name, arguments) in sorted(calls.items())
            ]
        # An observation may only reference calls in its own step; results
        # whose call never appeared are still recorded with their own id so
        # the reference stays inspectable rather than silently dropped.
        known = set(calls)
        normalized_results = []
        for result in results:
            if result["source_call_id"] not in known:
                known.add(result["source_call_id"])
                atif_step.setdefault("tool_calls", []).append(
                    {
                        "tool_call_id": result["source_call_id"],
                        "function_name": "unknown_tool",
                        "arguments": {},
                    }
                )
            normalized_results.append(result)
        if normalized_results:
            atif_step["observation"] = {"results": normalized_results}
        usage = bucket["usage"]
        if isinstance(usage, dict):
            metrics: JsonObject = {}
            prompt = usage.get("input_tokens")
            completion = usage.get("output_tokens")
            if isinstance(prompt, int) and not isinstance(prompt, bool):
                metrics["prompt_tokens"] = prompt
                total_input += prompt
            if isinstance(completion, int) and not isinstance(completion, bool):
                metrics["completion_tokens"] = completion
                total_output += completion
            if metrics:
                atif_step["metrics"] = metrics
            atif_step["llm_call_count"] = 1
        atif_steps.append(atif_step)

    if not atif_steps:
        raise ReefGateError(f"no trajectory content under {episode_dir}")

    agent: JsonObject = {"name": REEF_AGENT_NAME, "version": "unknown"}
    if detected_model:
        agent["model_name"] = detected_model
    if scenario is not None and step is not None:
        identity = f"reef-{scenario}-step{step}-{episode_dir.name}"
    else:
        identity = f"reef-{episode_dir.name}"
    extra: JsonObject = {
        "origin": "reef",
        "raw_source": "session.jsonl",
        "transport": "reef-native-jsonl",
    }
    payload: JsonObject = {
        "schema_version": schema_version,
        "session_id": identity,
        "trajectory_id": identity,
        "agent": agent,
        "steps": atif_steps,
        "extra": extra,
    }
    reef_meta: JsonObject = {
        "side": name_parts["side"],
        "task_index": name_parts["task_index"],
        "repeat": name_parts["repeat"],
        "episode": episode_dir.name,
    }
    if scenario is not None:
        reef_meta["scenario"] = scenario
    if step is not None:
        reef_meta["step"] = step
    if run_label is not None:
        reef_meta["run"] = run_label
    if proposal_id is not None:
        reef_meta["proposal_id"] = proposal_id
    if release_id is not None:
        reef_meta["release_id"] = release_id
    for key in ("task", "path", "exit_code", "stdout", "stderr", "residue"):
        if key in episode and episode[key] is not None:
            reef_meta[key] = episode[key]
    failure = episode.get("failure", None)
    if failure is not None:
        reef_meta["failure"] = failure
    score = _finite_number(episode.get("score", None))
    # Reef None / failed episodes stay unscored: the score is carried only
    # when it is a finite number and no failure was recorded.
    if score is not None and failure is None:
        reef_meta["episode_score"] = score
    if session_path.is_file():
        reef_meta["session_file"] = "session.jsonl"
    extra["reef"] = reef_meta
    if turn_end_reason:
        extra["turn_end_reason"] = turn_end_reason
    payload["final_metrics"] = {
        "total_prompt_tokens": total_input,
        "total_completion_tokens": total_output,
        "total_steps": len(atif_steps),
    }

    error = validate_atif_document(payload)
    if error is not None:
        raise ReefGateError(f"converted episode under {episode_dir} is not valid ATIF: {error}")
    return payload


def iter_gate_episodes(steps_root: Path) -> list[dict[str, Any]]:
    """List every gate episode under a Reef step-record root.

    Yields ``{"scenario", "step", "episode_dir"}`` sorted by
    (scenario, step, episode name). ``steps_root`` is the ``steps/``
    directory holding ``<scenario>/<step>/episodes/<side>-<i>[-<r>]/``.
    """
    steps_root = Path(steps_root)
    found: list[dict[str, Any]] = []
    if not steps_root.is_dir():
        return found
    for episode_file in sorted(steps_root.glob("*/*/episodes/*/episode.json")):
        episode_dir = episode_file.parent
        try:
            step_no = int(episode_dir.parent.parent.name)
        except ValueError:
            continue
        if parse_episode_name(episode_dir.name) is None:
            continue
        found.append(
            {
                "scenario": episode_dir.parent.parent.parent.name,
                "step": step_no,
                "episode_dir": episode_dir,
            }
        )
    return found


def load_trial_results(results_path: Path | None) -> dict[int, JsonObject]:
    """Map trial number to its recorded ``results.jsonl`` row."""
    if results_path is None:
        return {}
    rows: dict[int, JsonObject] = {}
    for line in Path(results_path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict) and isinstance(row.get("trial"), int):
            rows[row["trial"]] = row
    return rows


def episode_passed(score: Any) -> bool | None:
    """Recorded pass as data: True/False, or None when unscored."""
    number = _finite_number(score)
    if number is None:
        return None
    return number >= PASS_THRESHOLD


def pair_outcome(candidate_pass: bool | None, current_pass: bool | None) -> str | None:
    """W/L/T for one pairing as data; None when either side is unscored."""
    if candidate_pass is None or current_pass is None:
        return None
    if candidate_pass == current_pass:
        return "T"
    return "W" if candidate_pass else "L"


def build_paired_view(
    episodes: list[dict[str, Any]],
    trial_results: dict[int, JsonObject] | None = None,
) -> list[dict[str, Any]]:
    """Pair candidate and current episodes by step, task, and repeat.

    Each entry carries the recorded publish outcome and the recorded
    wins/losses/ties alongside the observed pair outcomes. No decision
    rules: this is data for HAR-72, not a gate verdict.
    """
    trial_results = trial_results or {}
    by_key: dict[tuple[int, str, int, int], dict[str, dict[str, Any]]] = {}
    for record in episodes:
        key = (record["step"], str(record["scenario"]), record["task_index"], record["repeat"])
        sides = by_key.setdefault(key, {})
        sides[record["side"]] = record

    view: list[dict[str, Any]] = []
    for key in sorted(by_key):
        step_no, scenario, task_index, repeat = key
        sides = by_key[key]
        candidate = sides.get("candidate")
        current = sides.get("current")
        candidate_pass = episode_passed(candidate["score"] if candidate else None)
        current_pass = episode_passed(current["score"] if current else None)
        recorded = trial_results.get(step_no, {})
        view.append(
            {
                "step": step_no,
                "trial": step_no,
                "scenario": scenario,
                "task_index": task_index,
                "repeat": repeat,
                "task": (candidate or current or {}).get("task"),
                "candidate_episode": candidate["episode"] if candidate else None,
                "current_episode": current["episode"] if current else None,
                "candidate_pass": candidate_pass,
                "current_pass": current_pass,
                "outcome": pair_outcome(candidate_pass, current_pass),
                "published": recorded.get("published"),
                "wins": recorded.get("wins"),
                "losses": recorded.get("losses"),
                "ties": recorded.get("ties"),
            }
        )
    return view


def _sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def import_reef_gate_run(
    steps_root: Path,
    out_dir: Path,
    *,
    run_label: str,
    results_path: Path | None = None,
    reef_format_ref: str = REEF_FORMAT_REF,
) -> dict[str, Any]:
    """Import one Reef step-record root into an origin-marked evidence layout.

    ``out_dir`` must be new; existing directories are never overwritten, so
    a re-run is an explicit fresh import. Every converted document is
    ATIF-validated before anything is written, and files land atomically.
    Returns the summary dictionary also written to ``summary.json``.
    """
    steps_root = Path(steps_root)
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise ReefGateError(f"refusing to overwrite existing output: {out_dir}")
    episodes = iter_gate_episodes(steps_root)
    if not episodes:
        raise ReefGateError(f"no gate episodes under {steps_root}")
    trial_results = load_trial_results(results_path)

    documents: list[tuple[Path, JsonObject]] = []
    records: list[dict[str, Any]] = []
    for entry in episodes:
        recorded = trial_results.get(entry["step"], {})
        proposal_raw = recorded.get("proposal")
        proposal: JsonObject = proposal_raw if isinstance(proposal_raw, dict) else {}
        proposal_id_raw = proposal.get("id")
        release_id_raw = recorded.get("release_id")
        payload = parse_reef_gate_episode(
            entry["episode_dir"],
            scenario=entry["scenario"],
            step=entry["step"],
            run_label=run_label,
            proposal_id=proposal_id_raw if isinstance(proposal_id_raw, str) else None,
            release_id=release_id_raw if isinstance(release_id_raw, str) else None,
        )
        reef_meta = payload["extra"]["reef"]
        relative = Path("trajectories") / str(entry["step"]) / f"{entry['episode_dir'].name}.json"
        documents.append((relative, payload))
        try:
            episode_source = json.loads((entry["episode_dir"] / "episode.json").read_text())
        except (OSError, json.JSONDecodeError):
            episode_source = {}
        records.append(
            {
                "step": entry["step"],
                "scenario": entry["scenario"],
                "side": reef_meta["side"],
                "task_index": reef_meta["task_index"],
                "repeat": reef_meta["repeat"],
                "episode": entry["episode_dir"].name,
                "task": episode_source.get("task") if isinstance(episode_source, dict) else None,
                "score": episode_source.get("score") if isinstance(episode_source, dict) else None,
                "relative_path": relative.as_posix(),
            }
        )

    pairs = build_paired_view(records, trial_results)
    scored = [record for record in records if _finite_number(record["score"]) is not None]
    passes = sum(1 for record in scored if (record["score"] or 0.0) >= PASS_THRESHOLD)
    trials = sorted(trial_results) if trial_results else sorted({record["step"] for record in records})
    published = sum(1 for trial in trials if trial_results.get(trial, {}).get("published") is True)
    wins = sum(trial_results.get(trial, {}).get("wins") or 0 for trial in trials)
    losses = sum(trial_results.get(trial, {}).get("losses") or 0 for trial in trials)
    ties = sum(trial_results.get(trial, {}).get("ties") or 0 for trial in trials)
    observed_wins = sum(1 for pair in pairs if pair["outcome"] == "W")
    observed_losses = sum(1 for pair in pairs if pair["outcome"] == "L")
    observed_ties = sum(1 for pair in pairs if pair["outcome"] == "T")
    summary: dict[str, Any] = {
        "origin": "reef",
        "run": run_label,
        "reef_format_ref": reef_format_ref,
        "scenarios": sorted({record["scenario"] for record in records}),
        "episodes_total": len(records),
        "episodes_scored": len(scored),
        "episodes_unscored": len(records) - len(scored),
        "episode_pass": passes,
        "trials": len(trials),
        "published": published,
        "wins_total": wins,
        "losses_total": losses,
        "ties_total": ties,
        "observed_pairs": len(pairs),
        "observed_wins": observed_wins,
        "observed_losses": observed_losses,
        "observed_ties": observed_ties,
        "recorded_pair_totals_agree": (wins == observed_wins and losses == observed_losses),
    }

    manifest = {
        "evidence_kind": "historical",
        "origin": "reef",
        "run": run_label,
        "trajectories": [relative.as_posix() for relative, _ in documents],
    }
    intake = {
        "origin": "reef",
        "run": run_label,
        "reef_format_ref": reef_format_ref,
        "agent_name": REEF_AGENT_NAME,
        "schema_version": DEFAULT_SCHEMA_VERSION,
        "steps_root": str(steps_root),
        "results_path": str(results_path) if results_path is not None else None,
        "native_harbor_job": False,
        "offline_reader": "research/analysis/harness-mechanics/analyze.py --evidence-kind historical",
        "episodes_total": len(records),
    }

    staging = out_dir.parent / f".tmp_{out_dir.name}_{hashlib.sha256(run_label.encode()).hexdigest()[:8]}"
    if staging.exists():
        raise ReefGateError(f"staging directory already exists: {staging}")
    staging.mkdir(parents=True)
    try:
        for relative, payload in documents:
            target = staging / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        (staging / "pairs.json").write_text(json.dumps(pairs, indent=2) + "\n")
        (staging / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        (staging / "intake.json").write_text(json.dumps(intake, indent=2) + "\n")
        staging.rename(out_dir)
    except Exception:
        for leftover in sorted(staging.rglob("*"), reverse=True):
            if leftover.is_file() or leftover.is_symlink():
                leftover.unlink()
        for leftover in sorted(staging.rglob("*"), reverse=True):
            if leftover.is_dir():
                leftover.rmdir()
        staging.rmdir()
        raise
    return summary


def main(argv: list[str] | None = None) -> int:
    """Module CLI: ``python -m evallab.evidence.reef_gate ...``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--steps-root", type=Path, required=True, help="Reef steps/ directory")
    parser.add_argument("--out", type=Path, required=True, help="New output directory")
    parser.add_argument("--run-label", required=True, help="Label recorded as extra.reef.run")
    parser.add_argument("--results", type=Path, default=None, help="Reef results.jsonl path")
    parser.add_argument(
        "--reef-format-ref", default=REEF_FORMAT_REF, help="Reef commit the format was mirrored from"
    )
    args = parser.parse_args(argv)
    try:
        summary = import_reef_gate_run(
            args.steps_root,
            args.out,
            run_label=args.run_label,
            results_path=args.results,
            reef_format_ref=args.reef_format_ref,
        )
    except (ReefGateError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
