"""Read Reef-recorded evidence as ATIF.

Reef ran these trajectories itself; Eval Lab only reads them. Two sources:

- Gate episodes: one Reef gate episode (``episode.json`` plus its sibling
  ``session.jsonl``) becomes one valid ATIF document with the origin
  recorded as Reef. Reef ``None`` scores and failed episodes stay
  unscored: missing source fields stay missing.
- Captured traffic: one Reef report record plus the inference records its
  ``references`` name becomes one ATIF document, mirroring the semantics
  of ``reef/core/trajectories.py`` ``make_trajectory`` without importing
  ``reef``: references define one trajectory; the report score/feedback
  attach to it.

Format mirror (local Reef checkout; the mirrored files are byte-identical
at ``07dfa883`` and ``818997d7``, verified by diff; ``--reef-format-ref``
records which commit produced each imported corpus):

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
- ``reef/core/trajectories.py`` ``make_trajectory`` (line 63),
  ``exchange_messages``, ``_append_message`` and ``_tool_call``: ordered
  inference records become steps with shared prefixes marked copied, and
  the report score/feedback attach in ``extra.reef``.

Run gate intake with ``python -m evallab.evidence.reef_intake --steps-root
<root> --out <new dir> --run-label <label> [--results <results.jsonl>]``,
or traffic intake with ``--records <records.json> --record-details
<details.json> --scenario <scenario>`` in place of ``--steps-root``. The
output layout is deliberately not a native Harbor job: ATIF documents under
``trajectories/`` plus a ``manifest.json`` (``evidence_kind`` historical)
that the existing offline analyzer
(``research/analysis/harness-mechanics/analyze.py``) reads directly, a
``summary.json`` reproduction, and an ``intake.json`` provenance record.
Gate imports additionally write a ``pairs.json`` paired view. No decision
rules, sign tests, or calibration live here: HAR-72 owns those.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
from pathlib import Path
from typing import Any

#: Agent name this importer registers Reef-native traffic under. Never a
#: control name: the harness-mechanics analyzer classifies oracle/nop runs
#: as unsupported, and these are evaluated agent episodes.
REEF_AGENT_NAME = "reef-harness"

#: Newest ATIF version ``evidence/atif`` accepts; kept in sync with
#: ``evallab.dsh.DEFAULT_SCHEMA_VERSION``.
DEFAULT_SCHEMA_VERSION = "ATIF-v1.7"

#: Default Reef commit whose record format this module mirrors. Recorded per
#: corpus via ``--reef-format-ref`` (exp04 ran at 07dfa883, exp05/06 at
#: 818997d7); the mirrored files are byte-identical at both, verified by diff.
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


def load_trial_results(results_path: Path | None) -> list[JsonObject]:
    """Load recorded ``results.jsonl`` rows in file order."""
    if results_path is None:
        return []
    rows: list[JsonObject] = []
    for line_number, line in enumerate(
        Path(results_path).read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReefGateError(f"torn {results_path} line {line_number}: {exc}") from exc
        if isinstance(row, dict) and isinstance(row.get("trial"), int):
            rows.append(row)
    return rows


def match_trial_row(rows: list[JsonObject], scenario: str, step: int) -> JsonObject:
    """Join one (scenario, step) episode group to its recorded results row.

    An exact (scenario, trial) row wins; else the single row naming this
    scenario; else a scenario-less row for this trial (the exp04 shape).
    Repeated-scenario rows require an exact trial match: with several rows
    naming one scenario and none matching this trial, there is no safe
    fallback, so the join fails loudly rather than misattributing.
    """
    exact = [
        row
        for row in rows
        if row.get("trial") == step and row.get("scenario") == scenario
    ]
    if len(exact) > 1:
        raise ReefGateError(f"several results rows match scenario {scenario!r} trial {step}")
    if exact:
        return exact[0]
    named = [row for row in rows if row.get("scenario") == scenario]
    if len(named) == 1:
        return named[0]
    if len(named) > 1:
        raise ReefGateError(
            f"several results rows name scenario {scenario!r} with no exact trial-{step} row"
        )
    fallback = [
        row
        for row in rows
        if row.get("trial") == step and row.get("scenario") is None
    ]
    if len(fallback) > 1:
        raise ReefGateError(f"several scenario-less results rows match trial {step}")
    if fallback:
        return fallback[0]
    return {}


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
    trial_rows: list[JsonObject] | None = None,
) -> list[dict[str, Any]]:
    """Pair candidate and current episodes by scenario, step, task, and repeat.

    Each entry carries the recorded publish outcome and the recorded
    wins/losses/ties alongside the observed pair outcomes. No decision
    rules: this is data for HAR-72, not a gate verdict.
    """
    rows = trial_rows or []
    by_key: dict[tuple[str, int, int, int], dict[str, dict[str, Any]]] = {}
    for record in episodes:
        key = (str(record["scenario"]), record["step"], record["task_index"], record["repeat"])
        sides = by_key.setdefault(key, {})
        sides[record["side"]] = record

    view: list[dict[str, Any]] = []
    for key in sorted(by_key):
        scenario, step_no, task_index, repeat = key
        sides = by_key[key]
        candidate = sides.get("candidate")
        current = sides.get("current")
        candidate_pass = episode_passed(candidate["score"] if candidate else None)
        current_pass = episode_passed(current["score"] if current else None)
        recorded = match_trial_row(rows, scenario, step_no)
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
    trial_rows = load_trial_results(results_path)

    documents: list[tuple[Path, JsonObject]] = []
    records: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for entry in episodes:
        recorded = match_trial_row(trial_rows, entry["scenario"], entry["step"])
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
        relative = (
            Path("trajectories") / entry["scenario"] / str(entry["step"]) / f"{entry['episode_dir'].name}.json"
        )
        if relative.as_posix() in seen_paths:
            raise ReefGateError(f"two episodes map to the same output path: {relative}")
        seen_paths.add(relative.as_posix())
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
                "trial_matched": bool(recorded),
                "relative_path": relative.as_posix(),
            }
        )

    pairs = build_paired_view(records, trial_rows)
    scored = [record for record in records if _finite_number(record["score"]) is not None]
    passes = sum(1 for record in scored if (record["score"] or 0.0) >= PASS_THRESHOLD)
    matched_keys = sorted(
        {(record["scenario"], record["step"]) for record in records if record["trial_matched"]}
    )
    trial_keys = matched_keys or sorted({(record["scenario"], record["step"]) for record in records})
    matched_rows = {
        key: match_trial_row(trial_rows, key[0], key[1]) for key in matched_keys
    }
    published = sum(1 for row in matched_rows.values() if row.get("published") is True)
    wins = sum(row.get("wins") or 0 for row in matched_rows.values())
    losses = sum(row.get("losses") or 0 for row in matched_rows.values())
    ties = sum(row.get("ties") or 0 for row in matched_rows.values())
    observed_wins = sum(1 for pair in pairs if pair["outcome"] == "W")
    observed_losses = sum(1 for pair in pairs if pair["outcome"] == "L")
    observed_ties = sum(1 for pair in pairs if pair["outcome"] == "T")
    trials = len(trial_keys)
    summary: dict[str, Any] = {
        "origin": "reef",
        "run": run_label,
        "reef_format_ref": reef_format_ref,
        "scenarios": sorted({record["scenario"] for record in records}),
        "episodes_total": len(records),
        "episodes_scored": len(scored),
        "episodes_unscored": len(records) - len(scored),
        "episode_pass": passes,
        "trials": trials,
        "published": published,
        "wins_total": wins,
        "losses_total": losses,
        "ties_total": ties,
        "observed_pairs": len(pairs),
        "observed_wins": observed_wins,
        "observed_losses": observed_losses,
        "observed_ties": observed_ties,
        "recorded_pair_totals_agree": (
            wins == observed_wins and losses == observed_losses and ties == observed_ties
        ),
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

    _publish_layout(
        out_dir,
        run_label,
        documents,
        {
            "manifest.json": manifest,
            "pairs.json": pairs,
            "summary.json": summary,
            "intake.json": intake,
        },
    )
    return summary


def _publish_layout(
    out_dir: Path,
    run_label: str,
    documents: list[tuple[Path, JsonObject]],
    sidecars: dict[str, Any],
) -> None:
    """Write documents plus sidecar JSON files atomically into a new directory.

    Every target must resolve inside the fresh staging directory, and every
    write uses exclusive creation: nothing is overwritten, inside or
    outside the layout.
    """
    staging = out_dir.parent / f".tmp_{out_dir.name}_{hashlib.sha256(run_label.encode()).hexdigest()[:8]}"
    if staging.exists():
        raise ReefGateError(f"staging directory already exists: {staging}")
    staging.mkdir(parents=True)
    staging_resolved = staging.resolve()

    def _contained_write(relative: Path | str, text: str) -> None:
        target = staging / relative
        try:
            target.resolve().relative_to(staging_resolved)
        except ValueError as exc:
            raise ReefGateError(f"output path escapes the evidence layout: {relative}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with target.open("x", encoding="utf-8") as handle:
                handle.write(text)
        except FileExistsError as exc:
            raise ReefGateError(f"two episodes map to the same output path: {relative}") from exc

    try:
        for relative, payload in documents:
            _contained_write(relative, json.dumps(payload, indent=2) + "\n")
        for name, sidecar in sidecars.items():
            _contained_write(name, json.dumps(sidecar, indent=2) + "\n")
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


def _traffic_exchange_messages(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split one inference payload into request and response messages.

    Mirrors ``reef/core/trajectories.py`` ``exchange_messages`` without
    importing ``reef``: the training projection wins, then OpenAI-style
    ``choices``, then a generic output/message body.
    """
    response = payload.get("response", {}) if isinstance(payload, dict) else {}
    response = response if isinstance(response, dict) else {"content": response}
    training = response.get("training", {})
    training = training if isinstance(training, dict) else {}
    request: Any = training.get(
        "request_messages",
        payload.get("messages", payload.get("input", payload.get("prompt", [])))
        if isinstance(payload, dict)
        else [],
    )
    if isinstance(request, str):
        request = [{"role": "user", "content": request}]
    if not isinstance(request, list):
        request = []
    request = [entry for entry in request if isinstance(entry, dict)]
    system = payload.get("system", payload.get("instructions")) if isinstance(payload, dict) else None
    if system and not any(message.get("role") == "system" for message in request):
        request.insert(0, {"role": "system", "content": system})
    message = training.get("response_message")
    if isinstance(message, dict):
        return request, [message]
    choices = response.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            return request, [message]
        return request, [{"role": "assistant", "content": choices[0].get("text", "")}]
    output = response.get("output")
    if isinstance(output, list):
        return request, [entry for entry in output if isinstance(entry, dict)]
    message = response.get("message")
    if isinstance(message, dict):
        return request, [message]
    return request, [
        {
            "role": "assistant",
            "content": response.get("content", response.get("output_text", "")),
            **({} if response else {"text_available": False}),
        }
    ]


def _traffic_content(value: Any) -> str | list[dict[str, Any]]:
    """Project one provider message content into ATIF text or content parts."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if not isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    parts: list[dict[str, Any]] = []
    for part in value:
        if not isinstance(part, dict):
            parts.append({"type": "text", "text": str(part)})
            continue
        kind = part.get("type")
        if kind in ("text", "input_text", "output_text"):
            parts.append({"type": "text", "text": str(part.get("text", ""))})
        elif kind in ("image", "image_url", "input_image"):
            source = part.get("source", {})
            image_url = part.get("image_url", {})
            path = image_url.get("url") if isinstance(image_url, dict) else image_url
            if isinstance(source, dict):
                path = path or source.get("path") or source.get("url")
                if source.get("type") == "base64":
                    path = f"data:{source.get('media_type')};base64,{source.get('data')}"
            if not isinstance(path, str):
                parts.append({"type": "text", "text": json.dumps(dict(part), ensure_ascii=False)})
                continue
            media_type = (source.get("media_type") if isinstance(source, dict) else None) or (
                path[5:].split(";", 1)[0]
                if path.startswith("data:")
                else mimetypes.guess_type(path.split("?", 1)[0])[0]
            )
            if media_type not in ("image/jpeg", "image/png", "image/gif", "image/webp"):
                parts.append({"type": "text", "text": json.dumps(dict(part), ensure_ascii=False)})
            else:
                parts.append({"type": "image", "source": {"media_type": media_type, "path": path}})
        elif kind not in ("tool_use", "tool_result", "thinking", "redacted_thinking"):
            parts.append({"type": "text", "text": json.dumps(dict(part), ensure_ascii=False)})
    if all(part["type"] == "text" for part in parts):
        return "".join(part["text"] for part in parts)
    return parts


def _traffic_tool_call(call: dict[str, Any], step_id: int, index: int) -> JsonObject:
    """One provider tool call as an ATIF tool call, mirroring Reef's ``_tool_call``."""
    function = call.get("function", call)
    function = function if isinstance(function, dict) else {}
    arguments = function.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            arguments = {"raw_arguments": arguments}
    if not isinstance(arguments, dict):
        arguments = {"raw_arguments": arguments}
    return {
        "tool_call_id": str(call.get("id") or f"call-{step_id}-{index}"),
        "function_name": str(function.get("name") or "unknown"),
        "arguments": dict(arguments),
    }


def _traffic_append_message(
    steps: list[JsonObject], message: dict[str, Any], *, copied: bool
) -> None:
    """Append one provider message to ATIF steps, mirroring Reef's ``_append_message``."""
    role = message.get("role", "assistant")
    kind = message.get("type")
    content = message.get("content", "")
    blocks = content if isinstance(content, list) else []
    results = [part for part in blocks if isinstance(part, dict) and part.get("type") == "tool_result"]
    if role == "tool" or kind == "function_call_output":
        results.append(
            {
                "tool_use_id": message.get("tool_call_id", message.get("call_id")),
                "content": message.get("output", content),
            }
        )
    for result in results:
        call_id = result.get("tool_use_id")
        target = next(
            (
                step
                for step in reversed(steps)
                if any(call.get("tool_call_id") == call_id for call in step.get("tool_calls", []))
            ),
            None,
        )
        if target is not None:
            target.setdefault("observation", {"results": []})["results"].append(
                {
                    "source_call_id": call_id,
                    "content": _traffic_content(result.get("content")),
                    "extra": {"provider_result": dict(result)},
                }
            )
        else:
            steps.append(
                {
                    "step_id": len(steps) + 1,
                    "source": "user",
                    "message": _traffic_content(result.get("content")),
                    "extra": {"provider_message": dict(message)},
                }
            )
    if role == "tool" or kind == "function_call_output":
        return
    remaining = [
        part for part in blocks if not isinstance(part, dict) or part.get("type") != "tool_result"
    ]
    if results and not remaining:
        return
    source = "agent" if role == "assistant" else "system" if role in ("system", "developer") else "user"
    step: JsonObject = {
        "step_id": len(steps) + 1,
        "source": source,
        "message": _traffic_content(remaining if blocks else content),
        "extra": {"provider_message": dict(message)},
    }
    if copied and source == "agent":
        step["is_copied_context"] = True
    calls = list(message.get("tool_calls") or [])
    calls.extend(
        {"id": part.get("id"), "function": {"name": part.get("name"), "arguments": part.get("input", {})}}
        for part in blocks
        if isinstance(part, dict) and part.get("type") == "tool_use"
    )
    if kind == "function_call":
        calls.append({"id": message.get("call_id"), "function": message})
    if calls and source == "agent":
        step["tool_calls"] = [
            _traffic_tool_call(call, len(steps) + 1, index) for index, call in enumerate(calls)
        ]
    reasoning = message.get("reasoning_content") or "".join(
        str(part.get("thinking", ""))
        for part in blocks
        if isinstance(part, dict) and part.get("type") == "thinking"
    )
    if reasoning and source == "agent":
        step["reasoning_content"] = reasoning
    steps.append(step)


def _traffic_detail_records(details: JsonObject) -> dict[str, JsonObject]:
    """Validate the record-details map into ``{record id: detail body}``."""
    records: dict[str, JsonObject] = {}
    for record_id, body in details.items():
        if not isinstance(body, dict):
            raise ReefGateError(f"detail for {record_id!r} is not an object")
        if body.get("agent_record_id", record_id) != record_id:
            raise ReefGateError(f"detail key {record_id!r} disagrees with its agent_record_id")
        records[record_id] = body
    return records


def _traffic_listed_ids(export: JsonObject) -> tuple[set[str], str | None]:
    """Record ids named by a capture list export, plus its scenario when uniform."""
    listed: set[str] = set()
    scenarios: set[str] = set()
    pages = export.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict):
                continue
            scenario = page.get("scenario")
            if isinstance(scenario, str):
                scenarios.add(scenario)
            rows = page.get("records")
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and isinstance(row.get("agent_record_id"), str):
                    listed.add(row["agent_record_id"])
    else:
        rows = export.get("records")
        for row in rows if isinstance(rows, list) else []:
            if isinstance(row, dict) and isinstance(row.get("agent_record_id"), str):
                listed.add(row["agent_record_id"])
        scenario = export.get("scenario")
        if isinstance(scenario, str):
            scenarios.add(scenario)
    return listed, next(iter(scenarios)) if len(scenarios) == 1 else None


def parse_reef_traffic_trajectory(
    report_id: str,
    record_ids: list[str],
    by_id: dict[str, JsonObject],
    *,
    scenario: str | None = None,
    run_label: str | None = None,
    raw_source: str = "record-details.json",
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> JsonObject:
    """Build one ATIF document from a report and the inference records it references.

    Mirrors ``make_trajectory``: the report's references define the one
    trajectory, shared request prefixes are marked copied, and the report
    score/feedback attach in ``extra.reef``. Only recorded fields are
    carried; a missing score stays unscored.
    """
    report = by_id.get(report_id)
    if report is None:
        raise ReefGateError(f"report {report_id!r} has no detail record")
    members: list[tuple[str, JsonObject]] = []
    for record_id in record_ids:
        body = by_id.get(record_id)
        if body is None:
            raise ReefGateError(f"report {report_id!r} references {record_id!r} with no detail record")
        if body.get("request_type") != "inference":
            raise ReefGateError(f"report {report_id!r} references non-inference record {record_id!r}")
        payload = body.get("payload")
        if not isinstance(payload, dict):
            raise ReefGateError(f"inference record {record_id!r} carries no payload detail")
        members.append((record_id, payload))
    if not members:
        raise ReefGateError(f"report {report_id!r} references no inference records")

    steps: list[JsonObject] = []
    history: list[dict[str, Any]] = []
    for _record_id, payload in members:
        request, responses = _traffic_exchange_messages(payload)
        shared = 0
        for previous, current in zip(history, request, strict=False):
            if previous != current:
                break
            shared += 1
        for message in request[shared:]:
            _traffic_append_message(steps, message, copied=True)
        for message in responses:
            _traffic_append_message(steps, message, copied=False)
        history = [*request, *responses]
    if not steps:
        steps.append(
            {"step_id": 1, "source": "agent", "message": "", "extra": {"reef": {"text_available": False}}}
        )

    primary = members[-1][0]
    agent: JsonObject = {"name": REEF_AGENT_NAME, "version": "unknown"}
    model = members[-1][1].get("model")
    if isinstance(model, str) and model:
        agent["model_name"] = model
    extra: JsonObject = {
        "origin": "reef",
        "raw_source": raw_source,
        "transport": "reef-capture-records",
    }
    document: JsonObject = {
        "schema_version": schema_version,
        "session_id": members[0][0],
        "trajectory_id": primary,
        "agent": agent,
        "steps": steps,
        "extra": extra,
    }
    reef_meta: JsonObject = {"source_agent_record_id": primary, "report_id": report_id}
    if scenario is not None:
        reef_meta["scenario"] = scenario
    if run_label is not None:
        reef_meta["run"] = run_label
    score = _finite_number(report.get("score"))
    if score is not None:
        reef_meta["reward"] = score
    report_payload = report.get("payload")
    feedback: Any = None
    if isinstance(report_payload, dict):
        feedback = report_payload.get("feedback", report.get("feedback"))
    else:
        feedback = report.get("feedback")
    if isinstance(feedback, str | dict) and feedback:
        reef_meta["feedback"] = feedback
    entries = []
    for record_id, payload in members:
        body = by_id[record_id]
        entry: JsonObject = {"agent_record_id": record_id, "payload": dict(payload)}
        created = body.get("created_at")
        if isinstance(created, int | float) and not isinstance(created, bool):
            entry["created_at"] = created
        artifact = body.get("artifact_ref")
        if isinstance(artifact, dict) and artifact:
            entry["artifact_ref"] = artifact
        entries.append(entry)
    reef_meta["records"] = entries
    extra["reef"] = reef_meta

    error = validate_atif_document(document)
    if error is not None:
        raise ReefGateError(f"traffic trajectory for report {report_id!r} is not valid ATIF: {error}")
    return document


def _check_output_id(report_id: str) -> None:
    """Refuse a record id that cannot be a contained output filename."""
    if (
        not report_id
        or report_id in (".", "..")
        or report_id.startswith("/")
        or "/" in report_id
        or "\\" in report_id
        or report_id.startswith("~")
    ):
        raise ReefGateError(f"report id {report_id!r} cannot be an output filename")


def parse_reef_traffic_export(
    export: JsonObject,
    details: JsonObject,
    *,
    scenario: str | None = None,
    run_label: str | None = None,
    raw_source: str = "record-details.json",
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> list[tuple[str, JsonObject]]:
    """Build one ATIF document per report record in a capture export.

    ``export`` is the list export (``{"pages": [...]}``); ``details`` maps
    each ``agent_record_id`` to its full detail body. Inference records no
    report references are skipped, never built into a trajectory alone.
    """
    by_id = _traffic_detail_records(details)
    listed, listed_scenario = _traffic_listed_ids(export)
    scenario = scenario or listed_scenario
    reports = sorted(
        record_id for record_id, body in by_id.items() if body.get("request_type") == "report"
    )
    if not reports:
        raise ReefGateError("capture export holds no report records")
    if listed:
        missing = sorted(record_id for record_id in reports if record_id not in listed)
        if missing:
            raise ReefGateError(f"report records missing from the list export: {missing}")
    for report_id in reports:
        _check_output_id(report_id)
    documents: list[tuple[str, JsonObject]] = []
    for report_id in reports:
        body = by_id[report_id]
        payload = body.get("payload")
        references: Any = payload.get("references") if isinstance(payload, dict) else None
        if references is None:
            references = body.get("references")
        if not isinstance(references, list) or not all(isinstance(item, str) for item in references):
            raise ReefGateError(f"report {report_id!r} carries no string reference list")
        if not references:
            raise ReefGateError(f"report {report_id!r} references no inference records")
        documents.append(
            (
                report_id,
                parse_reef_traffic_trajectory(
                    report_id,
                    list(references),
                    by_id,
                    scenario=scenario,
                    run_label=run_label,
                    raw_source=raw_source,
                    schema_version=schema_version,
                ),
            )
        )
    return documents


def import_reef_traffic_run(
    records_path: Path,
    details_path: Path,
    out_dir: Path,
    *,
    run_label: str,
    scenario: str | None = None,
    reef_format_ref: str = REEF_FORMAT_REF,
) -> dict[str, Any]:
    """Import a Reef capture export into an origin-marked evidence layout.

    ``out_dir`` must be new. Returns the summary dictionary also written to
    ``summary.json``.
    """
    out_dir = Path(out_dir)
    if out_dir.exists():
        raise ReefGateError(f"refusing to overwrite existing output: {out_dir}")
    try:
        export = json.loads(Path(records_path).read_text(encoding="utf-8"))
        details = json.loads(Path(details_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReefGateError(f"cannot read capture export: {exc}") from exc
    if not isinstance(export, dict) or not isinstance(details, dict):
        raise ReefGateError("capture export files must both hold JSON objects")
    by_id = _traffic_detail_records(details)
    listed, listed_scenario = _traffic_listed_ids(export)
    scenario = scenario or listed_scenario
    trajectories = parse_reef_traffic_export(
        export, details, scenario=scenario, run_label=run_label, raw_source=Path(details_path).name
    )
    used: set[str] = set()
    for _report_id, payload in trajectories:
        for entry in payload["extra"]["reef"]["records"]:
            used.add(entry["agent_record_id"])
    inference_ids = sorted(
        record_id for record_id, body in by_id.items() if body.get("request_type") == "inference"
    )
    report_ids = sorted(
        record_id for record_id, body in by_id.items() if body.get("request_type") == "report"
    )
    scored = sum(1 for _report_id, payload in trajectories if "reward" in payload["extra"]["reef"])
    documents = [
        (Path("trajectories") / f"{report_id}.json", payload) for report_id, payload in trajectories
    ]
    summary: JsonObject = {
        "origin": "reef",
        "kind": "captured-traffic",
        "run": run_label,
        "scenario": scenario,
        "reef_format_ref": reef_format_ref,
        "trajectories_total": len(documents),
        "trajectories_scored": scored,
        "trajectories_unscored": len(documents) - scored,
        "inference_records_used": len(used),
        "inference_records_detail": len(inference_ids),
        "report_records": len(report_ids),
        "listed_records": len(listed),
        "unreferenced_inference_skipped": sorted(set(inference_ids) - used),
    }
    manifest = {
        "evidence_kind": "historical",
        "origin": "reef",
        "kind": "captured-traffic",
        "run": run_label,
        "trajectories": [relative.as_posix() for relative, _ in documents],
    }
    intake = {
        "origin": "reef",
        "kind": "captured-traffic",
        "run": run_label,
        "scenario": scenario,
        "reef_format_ref": reef_format_ref,
        "agent_name": REEF_AGENT_NAME,
        "schema_version": DEFAULT_SCHEMA_VERSION,
        "records_path": str(records_path),
        "details_path": str(details_path),
        "native_harbor_job": False,
        "offline_reader": "research/analysis/harness-mechanics/analyze.py --evidence-kind historical",
        "trajectories_total": len(documents),
    }
    _publish_layout(
        out_dir,
        run_label,
        documents,
        {"manifest.json": manifest, "summary.json": summary, "intake.json": intake},
    )
    return summary



def main(argv: list[str] | None = None) -> int:
    """Module CLI: ``python -m evallab.evidence.reef_intake ...``."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--steps-root", type=Path, default=None, help="Reef steps/ directory")
    parser.add_argument("--records", type=Path, default=None, help="Reef capture list export")
    parser.add_argument("--record-details", type=Path, default=None, help="Record-details map")
    parser.add_argument("--scenario", default=None, help="Scenario recorded as extra.reef.scenario")
    parser.add_argument("--out", type=Path, required=True, help="New output directory")
    parser.add_argument("--run-label", required=True, help="Label recorded as extra.reef.run")
    parser.add_argument("--results", type=Path, default=None, help="Reef results.jsonl path")
    parser.add_argument(
        "--reef-format-ref", default=REEF_FORMAT_REF, help="Reef commit the format was mirrored from"
    )
    args = parser.parse_args(argv)
    try:
        if args.records is not None:
            if args.record_details is None:
                parser.error("--records requires --record-details")
            if args.steps_root is not None:
                parser.error("--records and --steps-root are mutually exclusive")
            summary = import_reef_traffic_run(
                args.records,
                args.record_details,
                args.out,
                run_label=args.run_label,
                scenario=args.scenario,
                reef_format_ref=args.reef_format_ref,
            )
        else:
            if args.steps_root is None:
                parser.error("one of --steps-root or --records is required")
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
