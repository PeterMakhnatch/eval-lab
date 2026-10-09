"""Join one trial's planes into a single timestamped timeline.

Sources (all best-effort except the trial ``result.json``):
- ``result.json`` phases (environment_setup/agent_setup/agent_execution/verifier)
- model calls from the recording tap (``calls.jsonl`` via the capture binding
  in ``lab-metadata.json``), attributed with ``model_capture`` chaining
- harness trajectory (``agent/trajectory.json`` + Harbor continuations)
- kernel/egress events (``flight/events.jsonl``) and file diffs
  (``flight/filediff.*.json``)
- verifier outputs (``verifier_result`` plus ``verifier/*.json``)

Every emitted row carries ``trial_id``, ``job_id`` and ``task``.

Robustness: inputs are read streaming (line iteration, never a whole-file
split); corrupt JSONL lines are counted, never fatal; uncoercible model
``seq`` values drop that call with a counter; embedded text/path lists are
capped with truncation flags so a 500k-event phase cannot balloon the
timeline; and every plane emits an explicit ``presence`` verdict row naming
the sources consulted, counts, and missing inputs. Observer ``status`` files
reporting ``available`` while ``flight/events.jsonl`` is missing or empty is
recorded as missing kernel evidence, and coverage stays ``incomplete``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evallab.flight.schemas import CONTROL_AGENTS, parse_ts, timeline_row

MAX_TS = datetime.max.replace(tzinfo=UTC)
PHASES = ("environment_setup", "agent_setup", "agent_execution", "verifier")

#: Embedded verifier text (test stdout/stderr) is capped at this many
#: characters; the row keeps the full length and a truncation flag.
MAX_TEXT_CHARS = 65_536

#: Embedded file-diff path lists are capped at this many entries per
#: change type; the row keeps the full count and a truncation flag.
MAX_PATHS_PER_LIST = 1_000

#: Planes that each get an explicit ``presence`` verdict row.
PRESENCE_PLANES = ("kernel", "file", "verifier", "model", "trajectory")


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _read_jsonl_counted(path: Path) -> tuple[list[dict[str, Any]], int, bool]:
    """Stream ``path`` line by line; return ``(rows, corrupt, exists)``.

    Corrupt lines (bad JSON or non-object JSON) are counted, never fatal.
    A missing file yields ``([], 0, False)``.
    """
    rows: list[dict[str, Any]] = []
    corrupt = 0
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError:
        return [], 0, False
    with handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                corrupt += 1
                continue
            if isinstance(payload, dict):
                rows.append(payload)
            else:
                corrupt += 1
    return rows, corrupt, True


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows, _, _ = _read_jsonl_counted(path)
    return rows


def _coerce_seq(value: Any) -> int | None:
    """Coerce a model-call ``seq`` to int; return ``None`` when uncoercible."""
    if isinstance(value, bool):
        return None
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _cap_text(text: str, *, limit: int = MAX_TEXT_CHARS) -> tuple[str, bool, int]:
    """Cap embedded text; return ``(text, truncated, full_chars)``."""
    if len(text) <= limit:
        return text, False, len(text)
    return text[:limit], True, len(text)


def _cap_paths(paths: list[Any]) -> tuple[list[Any], bool, int]:
    """Cap an embedded path list; return ``(paths, truncated, full_count)``."""
    if len(paths) <= MAX_PATHS_PER_LIST:
        return paths, False, len(paths)
    return paths[:MAX_PATHS_PER_LIST], True, len(paths)


def _job_id(job_dir: Path) -> str:
    result = _load_json(job_dir / "result.json")
    if result is not None and isinstance(result.get("id"), str):
        return str(result["id"])
    config = _load_json(job_dir / "config.json")
    if config is not None and isinstance(config.get("job_name"), str):
        return str(config["job_name"])
    return job_dir.name


def _capture_binding(job_dir: Path) -> tuple[Path | None, str | None]:
    """Resolve the job's model-capture binding; ``(calls_path, capture_dir)``."""
    metadata = _load_json(job_dir / "lab-metadata.json")
    binding = (metadata or {}).get("model_capture")
    if isinstance(binding, dict) and isinstance(binding.get("capture_dir"), str):
        capture_dir = str(binding["capture_dir"])
        return Path(capture_dir) / "calls.jsonl", capture_dir
    return None, None


def _capture_calls_counted(
    job_dir: Path, *, capture_dir: Path | None = None
) -> tuple[list[dict[str, Any]], int, bool, str]:
    """Load tap calls streaming; return ``(calls, corrupt, exists, source)``."""
    if capture_dir is not None:
        calls_path = capture_dir / "calls.jsonl"
        source = str(calls_path)
    else:
        calls_path, _ = _capture_binding(job_dir)
        if calls_path is None:
            return [], 0, False, "<no model_capture binding>"
        source = str(calls_path)
    calls, corrupt, exists = _read_jsonl_counted(calls_path)
    return calls, corrupt, exists, source


def _capture_calls(job_dir: Path) -> list[dict[str, Any]]:
    calls, _, _, _ = _capture_calls_counted(job_dir)
    return calls


def _calls_for_trial_counted(
    calls: list[dict[str, Any]], trials: list[Any], trial_name: str
) -> tuple[list[dict[str, Any]], int]:
    """Attribute calls; uncoercible ``seq`` values are dropped and counted."""
    from evallab.model_capture import attribute_calls

    good: list[dict[str, Any]] = []
    dropped_bad_seq = 0
    seq_of: dict[int, int] = {}
    for index, call in enumerate(calls):
        seq = _coerce_seq(call.get("seq") if isinstance(call, dict) else None)
        if seq is None:
            dropped_bad_seq += 1
            continue
        good.append(call)
        seq_of[index] = seq
    attribution = attribute_calls(good, trials)
    assigned = [
        call
        for index, call in enumerate(good)
        if attribution.assigned.get(seq_of[index]) == trial_name
    ]
    return assigned, dropped_bad_seq


def _calls_for_trial(
    calls: list[dict[str, Any]], trials: list[Any], trial_name: str
) -> list[dict[str, Any]]:
    assigned, _ = _calls_for_trial_counted(calls, trials, trial_name)
    return assigned


def _trajectory_docs_counted(
    trial_dir: Path,
) -> tuple[list[dict[str, Any]], str | None, int, bool]:
    """Load trajectory steps streaming; return ``(steps, source, corrupt, exists)``."""
    from evallab.model_capture import _atif_documents

    documents = _atif_documents(trial_dir)
    steps = [step for document in documents for step in document.get("steps", [])
             if isinstance(step, dict)]
    if documents:
        return steps, "agent/trajectory.json + continuations", 0, True
    path = trial_dir / "agent" / "trajectory.jsonl"
    rows, corrupt, exists = _read_jsonl_counted(path)
    return rows, "agent/trajectory.jsonl" if exists else None, corrupt, exists


def _trajectory_docs(trial_dir: Path) -> tuple[list[dict[str, Any]], str | None]:
    steps, source, _, _ = _trajectory_docs_counted(trial_dir)
    return steps, source


def _tool_calls_in(step: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for key in ("tool_calls", "toolCalls", "function_calls"):
        value = step.get(key)
        if isinstance(value, list):
            found.extend([c for c in value if isinstance(c, dict)])
    nested = step.get("message")
    if isinstance(nested, dict):
        found.extend(_tool_calls_in(nested))
    return found

MAX_REASONING_CHARS = 16384


def _reasoning_texts(call: dict[str, Any]) -> list[dict[str, str]]:
    """Extract provider-returned reasoning from a captured call, if any.

    Only reasoning the provider returned can be observed (response_body holds
    the full payload for non-streaming calls). Missing reasoning is not
    reconstructed; callers emit a model_reasoning row per entry.
    """
    found: list[dict[str, str]] = []

    def add(source: str, text: str) -> None:
        if isinstance(text, str) and text.strip():
            clipped = text[:MAX_REASONING_CHARS]
            entry = {"source": source, "text": clipped}
            if len(text) > MAX_REASONING_CHARS:
                entry["text_truncated"] = "true"
                entry["text_chars"] = str(len(text))
            found.append(entry)

    body = call.get("response_body")
    if isinstance(body, dict):
        choices = body.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if isinstance(message, dict):
                    reasoning = message.get("reasoning_content")
                    if isinstance(reasoning, str):
                        add("chat.reasoning_content", reasoning)
                    elif isinstance(reasoning, list):
                        for block in reasoning:
                            if isinstance(block, dict) and isinstance(block.get("text"), str):
                                add("chat.reasoning_content", block["text"])
                            elif isinstance(block, str):
                                add("chat.reasoning_content", block)
                delta = choice.get("delta")
                if isinstance(delta, dict) and isinstance(delta.get("reasoning_content"), str):
                    add("chat.delta.reasoning_content", delta["reasoning_content"])
        output = body.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                if item.get("type") in ("reasoning", "thinking") and isinstance(item.get("text"), str):
                    add(f"responses.{item['type']}", item["text"])
                for content in item.get("content") or []:
                    if isinstance(content, dict) and content.get("type") in (
                            "reasoning_text", "thinking_text") and isinstance(content.get("text"), str):
                        add(f"responses.{content['type']}", content["text"])
    return found


def build_timeline(
    trial_dir: Path, job_dir: Path, *, capture_dir: Path | None = None
) -> dict[str, Any]:
    """Build ``flight/timeline.jsonl`` for one trial; return the summary."""
    trial_dir = Path(trial_dir)
    job_dir = Path(job_dir)
    result = _load_json(trial_dir / "result.json")
    if result is None:
        raise ValueError(f"valid trial result.json required: {trial_dir}")
    trial_id = str(result.get("id") or trial_dir.name)
    job_id = _job_id(job_dir)
    task = result.get("task_name")
    task_name = str(task) if isinstance(task, str) else None
    agent = result.get("agent_info")
    agent_name = str(agent.get("name")) if isinstance(agent, dict) else None

    corrupt_lines: dict[str, int] = {}

    def note_corrupt(name: str, count: int) -> None:
        if count:
            corrupt_lines[name] = corrupt_lines.get(name, 0) + count

    rows: list[dict[str, Any]] = []

    for phase in PHASES:
        window = result.get(phase)
        if not isinstance(window, dict):
            continue
        for marker in ("started_at", "finished_at"):
            ts = window.get(marker)
            if isinstance(ts, str) and ts:
                rows.append(
                    timeline_row(
                        trial_id=trial_id,
                        job_id=job_id,
                        task=task_name,
                        ts=ts,
                        plane="phase",
                        kind=f"{phase}.{marker.removesuffix('_at')}",
                    )
                )

    from evallab.model_capture import collect_trial_evidence

    evidence = [collect_trial_evidence(candidate, job_dir)
                for candidate in job_dir.iterdir()
                if candidate.is_dir() and (candidate / "result.json").is_file()]
    calls, calls_corrupt, calls_exists, calls_source = _capture_calls_counted(
        job_dir, capture_dir=capture_dir
    )
    note_corrupt("calls.jsonl", calls_corrupt)
    assigned, dropped_bad_seq = _calls_for_trial_counted(calls, evidence, trial_dir.name)
    for call in assigned:
        call_ts = call.get("started_at") if isinstance(call.get("started_at"), str) else None
        rows.append(
            timeline_row(
                trial_id=trial_id,
                job_id=job_id,
                task=task_name,
                ts=call_ts,
                plane="model",
                kind="model_call",
                detail=call,
            )
        )
        # Reasoning the provider returned rides the same timestamp so the story
        # reads request -> reasoning -> response -> trajectory step in order.
        for reasoning in _reasoning_texts(call):
            rows.append(
                timeline_row(
                    trial_id=trial_id,
                    job_id=job_id,
                    task=task_name,
                    ts=call_ts,
                    plane="model",
                    kind="model_reasoning",
                    detail={"model_call_seq": call.get("seq"), **reasoning},
                )
            )
    if not assigned:
        if agent_name in CONTROL_AGENTS:
            verdict = "control-agent-expected-empty"
        elif not calls:
            verdict = "capture-missing"
        else:
            verdict = "no-calls-attributed"
        rows.append(
            timeline_row(
                trial_id=trial_id,
                job_id=job_id,
                task=task_name,
                ts=None,
                plane="model",
                kind="model_verdict",
                detail={"verdict": verdict, "agent": agent_name, "calls_total": len(calls)},
            )
        )
    else:
        verdict = "available"

    steps, trajectory_source, traj_corrupt, traj_exists = _trajectory_docs_counted(trial_dir)
    note_corrupt("agent/trajectory.jsonl", traj_corrupt)
    tool_call_count = 0
    for position, step in enumerate(steps):
        ts = step.get("timestamp") if isinstance(step.get("timestamp"), str) else None
        rows.append(
            timeline_row(
                trial_id=trial_id,
                job_id=job_id,
                task=task_name,
                ts=ts,
                plane="trajectory",
                kind="step",
                detail={"position": position, "raw_step": step},
            )
        )
        tool_calls = _tool_calls_in(step)
        tool_call_count += len(tool_calls)
        for call in tool_calls:
            rows.append(
                timeline_row(
                    trial_id=trial_id,
                    job_id=job_id,
                    task=task_name,
                    ts=ts,
                    plane="tool",
                    kind="tool_call",
                    detail={"step": position, "raw_call": call},
                )
            )
    if not steps and agent_name in CONTROL_AGENTS:
        rows.append(
            timeline_row(
                trial_id=trial_id,
                job_id=job_id,
                task=task_name,
                ts=None,
                plane="trajectory",
                kind="model_verdict",
                detail={"verdict": "control-agent-expected-empty", "agent": agent_name},
            )
        )

    flight_dir = trial_dir / "flight"
    events_path = flight_dir / "events.jsonl"
    events, events_corrupt, events_exists = _read_jsonl_counted(events_path)
    note_corrupt("events.jsonl", events_corrupt)
    kernel_events = 0
    network_events = 0
    for event in events:
        plane = str(event.get("plane", "kernel"))
        if plane == "kernel":
            kernel_events += 1
        elif plane == "egress":
            network_events += 1
        rows.append(
            timeline_row(
                trial_id=trial_id,
                job_id=job_id,
                task=task_name,
                ts=event.get("ts") if isinstance(event.get("ts"), str) else None,
                plane=plane,
                kind=str(event.get("kind", "unknown")),
                detail={k: v for k, v in event.items() if k not in ("ts", "plane", "kind")},
            )
        )

    filediff_paths = sorted(flight_dir.glob("filediff.*.json"))
    file_changes = {"added": 0, "removed": 0, "modified": 0}
    file_paths_truncated = False
    filediff_missing = [p.name for p in filediff_paths if _load_json(p) is None]
    for diff_path in filediff_paths:
        diff = _load_json(diff_path)
        if diff is None:
            continue
        for root, entry in diff.items():
            if root == "schema_version" or not isinstance(entry, dict):
                continue
            for change in ("added", "removed", "modified"):
                paths = entry.get(change)
                count_key = f"{change}_count"
                if not isinstance(paths, list):
                    continue
                embedded, truncated, full = _cap_paths(paths)
                file_paths_truncated = file_paths_truncated or truncated
                count = entry.get(count_key, full)
                file_changes[change] += int(count) if isinstance(count, int) else full
                rows.append(
                    timeline_row(
                        trial_id=trial_id,
                        job_id=job_id,
                        task=task_name,
                        ts=diff.get("ts") if isinstance(diff.get("ts"), str) else None,
                        plane="file",
                        kind=f"file_{change}",
                        detail={
                            "root": root,
                            "source": diff_path.name,
                            "count": count,
                            "paths": embedded,
                            "paths_truncated": truncated,
                        },
                    )
                )

    verifier_rows = 0
    verifier_missing: list[str] = []
    verifier_result = result.get("verifier_result")
    if isinstance(verifier_result, dict):
        window = result.get("verifier")
        ts = window.get("finished_at") if isinstance(window, dict) else None
        rows.append(
            timeline_row(
                trial_id=trial_id,
                job_id=job_id,
                task=task_name,
                ts=ts if isinstance(ts, str) else None,
                plane="verifier",
                kind="verdict",
                detail={"verifier_result": verifier_result},
            )
        )
        verifier_rows += 1
    else:
        verifier_missing.append("result.json#verifier_result")
    for name in ("reward.json", "checks.json", "ctrf.json"):
        payload = _load_json(trial_dir / "verifier" / name)
        if payload is not None:
            rows.append(
                timeline_row(
                    trial_id=trial_id,
                    job_id=job_id,
                    task=task_name,
                    ts=None,
                    plane="verifier",
                    kind="output",
                    detail={"file": name, "payload": payload},
                )
            )
            verifier_rows += 1
        else:
            verifier_missing.append(f"verifier/{name}")

    window = result.get("verifier") or {}
    verifier_text_truncated = False
    for name in ("test-stdout.txt", "test-stderr.txt"):
        path = trial_dir / "verifier" / name
        if path.is_file():
            try:
                raw_text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                verifier_missing.append(f"verifier/{name}:unreadable")
                continue
            text, truncated, full_chars = _cap_text(raw_text)
            verifier_text_truncated = verifier_text_truncated or truncated
            rows.append(timeline_row(
                trial_id=trial_id, job_id=job_id, task=task_name,
                ts=window.get("finished_at"), plane="verifier", kind="output",
                detail={"file": name, "text": text, "text_truncated": truncated,
                        "text_chars": full_chars}))
            verifier_rows += 1
        else:
            verifier_missing.append(f"verifier/{name}")
    for row in rows:
        row["task_id"] = result.get("task_id") or task_name
        row["enforcement"] = row["detail"].get(
            "enforcement", "none" if row["plane"] == "egress" else None
        )
        if row["ts"] is None:
            row["ts"] = result.get("finished_at")
            row["timestamp_source"] = "trial_finished_at"

    observer_status = {
        path.name: _load_json(path) for path in sorted(flight_dir.glob("status*.json"))
    }
    phase_status = {
        phase: (observer_status.get(f"status.{phase}.json") or {}).get("status")
        for phase in ("agent", "verifier")
    }
    phases_available = all(status == "available" for status in phase_status.values())

    kernel_missing: list[str] = []
    if not events_exists:
        kernel_missing.append("flight/events.jsonl")
    elif not events:
        kernel_missing.append("flight/events.jsonl:empty")
    for phase in ("agent", "verifier"):
        name = f"status.{phase}.json"
        if phase_status[phase] is None:
            kernel_missing.append(f"flight/{name}")
    if phases_available and (not events_exists or not events):
        kernel_status = "missing"
    elif not events_exists or not events:
        kernel_status = "missing" if not events_exists else "empty"
    elif not phases_available:
        kernel_status = "degraded"
    else:
        kernel_status = "available"

    if filediff_paths and not filediff_missing:
        file_status = "available"
    elif filediff_paths:
        file_status = "degraded"
    else:
        file_status = "missing"
    file_missing = ["flight/filediff.*.json"] if not filediff_paths else [
        f"flight/{name}:unparseable" for name in filediff_missing
    ]

    verifier_status = "available" if verifier_rows else "missing"

    if assigned:
        model_status = "available"
    elif agent_name in CONTROL_AGENTS:
        model_status = "expected_empty"
    elif not calls:
        model_status = "capture-missing"
    else:
        model_status = "no-calls-attributed"
    model_missing: list[str] = []
    if not calls_exists:
        model_missing.append(calls_source)
    elif not calls:
        model_missing.append(f"{calls_source}:empty")
    if dropped_bad_seq:
        model_missing.append(f"{dropped_bad_seq} call(s) dropped: uncoercible seq")

    if steps:
        trajectory_status = "available"
    elif agent_name in CONTROL_AGENTS:
        trajectory_status = "expected_empty"
    else:
        trajectory_status = "missing"
    trajectory_missing: list[str] = []
    if not steps and trajectory_status == "missing":
        trajectory_missing.append("agent/trajectory.json + continuations"
                                  if trajectory_source is None
                                  else f"{trajectory_source}:empty")

    presence: dict[str, dict[str, Any]] = {
        "kernel": {
            "sources": ["flight/events.jsonl", "flight/status.agent.json",
                        "flight/status.verifier.json"],
            "counts": {"events": len(events), "kernel": kernel_events,
                       "egress": network_events, "other": len(events) - kernel_events
                       - network_events},
            "corrupt_lines": events_corrupt,
            "missing": kernel_missing,
            "phase_status": phase_status,
            "status": kernel_status,
        },
        "file": {
            "sources": [p.name for p in filediff_paths] or ["flight/filediff.*.json"],
            "counts": {**file_changes, "files": len(filediff_paths)},
            "missing": file_missing,
            "paths_truncated": file_paths_truncated,
            "status": file_status,
        },
        "verifier": {
            "sources": ["result.json#verifier_result", "verifier/reward.json",
                        "verifier/checks.json", "verifier/ctrf.json",
                        "verifier/test-stdout.txt", "verifier/test-stderr.txt"],
            "counts": {"rows": verifier_rows},
            "missing": verifier_missing,
            "text_truncated": verifier_text_truncated,
            "status": verifier_status,
        },
        "model": {
            "sources": [calls_source],
            "counts": {"calls_total": len(calls), "attributed": len(assigned),
                       "dropped_bad_seq": dropped_bad_seq},
            "corrupt_lines": calls_corrupt,
            "missing": model_missing,
            "status": model_status,
        },
        "trajectory": {
            "sources": [trajectory_source or "agent/trajectory.json + continuations, "
                        "agent/trajectory.jsonl"],
            "counts": {"steps": len(steps), "tool_calls": tool_call_count},
            "corrupt_lines": traj_corrupt,
            "missing": trajectory_missing,
            "status": trajectory_status,
        },
    }
    for plane in PRESENCE_PLANES:
        detail = {"plane_status": presence[plane]["status"], **presence[plane]}
        rows.append(
            timeline_row(
                trial_id=trial_id,
                job_id=job_id,
                task=task_name,
                ts=None,
                plane=plane,
                kind="presence",
                detail=detail,
            )
        )
        rows[-1]["task_id"] = result.get("task_id") or task_name
        rows[-1]["enforcement"] = None
        if rows[-1]["ts"] is None:
            rows[-1]["ts"] = result.get("finished_at")
            rows[-1]["timestamp_source"] = "trial_finished_at"

    rows.sort(key=lambda r: (parse_ts(r["ts"]) or MAX_TS, r["plane"], r["kind"]))

    flight_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    flight_dir.chmod(0o700)
    with (flight_dir / "timeline.jsonl").open("w", encoding="utf-8") as handle:
        (flight_dir / "timeline.jsonl").chmod(0o600)
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    counts: dict[str, int] = {}
    for row in rows:
        key = f"{row['plane']}/{row['kind']}"
        counts[key] = counts.get(key, 0) + 1
    phases_available_all = all(
        (observer_status.get(f"status.{phase}.json") or {}).get("status") == "available"
        for phase in ("agent", "verifier")
    )
    coverage_reasons: list[str] = []
    if not phases_available_all:
        coverage_reasons.append("phase observer status not all available")
    if "status.json" in observer_status:
        coverage_reasons.append("legacy status.json present")
    if not events_exists:
        coverage_reasons.append("flight/events.jsonl missing")
    elif not events:
        coverage_reasons.append("flight/events.jsonl empty")
    if phases_available_all and (not events_exists or not events):
        coverage_reasons.append("status available but no kernel events recorded")
    observer_available = (
        phases_available_all and "status.json" not in observer_status and bool(events)
    )
    summary = {
        "trial_id": trial_id,
        "job_id": job_id,
        "task": task_name,
        "agent": agent_name,
        "rows": len(rows),
        "counts": counts,
        "model_calls": sum(1 for r in rows if r["plane"] == "model" and r["kind"] == "model_call"),
        "coverage": "prototype_available" if observer_available else "incomplete",
        "coverage_reasons": coverage_reasons,
        "observer_status": observer_status,
        "presence": {plane: presence[plane]["status"] for plane in PRESENCE_PLANES},
        "corrupt_lines": corrupt_lines,
        "dropped_bad_seq": dropped_bad_seq,
        "trajectory_source": trajectory_source,
        "timeline": str(flight_dir / "timeline.jsonl"),
    }
    (flight_dir / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary
