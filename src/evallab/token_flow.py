"""Per-trial token-flow analysis: where the input-token budget goes (HAR-114).

Reads only existing artifacts under one trial directory (plus the job-level
``lab-metadata.json`` for the proxy ledger and limits) and answers, per
trial:

* the step (and call index) of the last useful edit;
* input/output tokens spent after it, absolute and as a share of the total;
* loop onset: the first step where the agent starts repeating
  near-identical commands/responses without progress;
* prompt size per call over time (series + summary);
* summarisation events: how many fired, whether each succeeded, prompt size
  before/after, and what they kept;
* the largest terminal outputs fed back (top-k with step refs);
* the stop reason.

Definitions (with limits):

* Last useful edit: the last agent step that writes a file: an edit tool
  name, an explicit write signal (``.write(``/``write_text``/``open(...,
  "w")``/``os.replace``/``shutil``), a shell ``>``/``>>``/``tee``
  redirection, or the shared :data:`evallab.traj.EDIT_COMMAND_PATTERNS`
  (the same patterns the traj outline uses for ``step_to_first_edit``,
  plus a bare-redirect supplement the shared ``\b(...)\b`` wrapper misses).
  Read-only probes (``open(path).read()`` without a write signal) and
  scratch-only writes (``/tmp/`` etc.) do not count. Persistence is
  checked best-effort: extracted target paths (redirection/``tee``/
  ``apply_patch`` headers/``open()``/``sed -i`` targets, filtered to
  plausible path tokens) are matched by basename against
  ``verifier/agent.diff``. Limits: exotic writers (``mv``/``cp`` over
  source, debuggers, unquoted ``open()`` variables) are missed; the
  basename match can false-positive on common filenames.
* Loop onset: the earliest step starting (a) a run of >=4 consecutive agent
  steps with the same whitespace/number-normalized command signature and no
  edit-like step inside, or (b) a run of >= ``LOOP_MIN_RUN`` consecutive
  agent steps with byte-identical stripped messages. (a) is the normalized
  cousin of the consecutive-command rule in
  :func:`evallab.traj._analyze_loop_suspicion`; (b) mirrors
  :func:`evallab.probe03.identical_runs` (same threshold constant) computed
  on stitched steps instead of the assembled sequence. Either alone is
  "repetition without progress" only heuristically: a long verification
  tail (e.g. repeated passing test runs after the fix) trips the detector
  even though the work is done, so consumers must read onset together with
  the last-useful-edit step.
* Call index: 1-based ordinal of the agent step among agent steps. Each
  agent step is one model call, so with a fully reconciled ledger this
  equals the proxy ``call_id``; unresolved/reserved calls have no step and
  shift nothing (they never appear in the trajectory).
* Terminal-output sizes are characters of the observation content as stored
  in the trajectory; ``est_tokens`` is ``chars // 4`` (documented
  assumption, not a measurement).
* Missing inputs are ``None`` with a ``reason`` string, never 0.

Wired into :func:`evallab.process_job._process_trial` as ``record["token_flow"]``
so every future job gets it; failures there are recorded, never raised.
"""

from __future__ import annotations

import contextlib
import json
import math
import re
from pathlib import Path
from typing import Any

TOKEN_FLOW_SCHEMA = "token_flow/v1"

#: Agent-side step sources. Terminus-2 writes ``agent``; accept ``assistant``
#: for older Harbor trajectories.
AGENT_SOURCES = frozenset({"agent", "assistant"})

#: Minimum normalized-command run length for loop onset. The shared traj
#: detector fires at 3 consecutive identical commands; onset uses 4 on the
#: normalized signature to cut single retries with one variant.
COMMAND_RUN_MIN = 4

#: Top-k terminal outputs kept per trial.
TOP_OUTPUTS_K = 5

#: ``chars // N`` token estimate for terminal output (assumption, recorded).
CHARS_PER_TOKEN = 4

_SUMMARIZATION_MARKER = "context summarization"

_REDIRECT_TARGET_RE = re.compile(r"(?:^|[;&|\n])\s*[^>&|]*?>\s*([^\s;|&\"']+)")
_TEE_TARGET_RE = re.compile(r"\btee\s+(?:-[a-z]+\s+)*([^\s;|&\"']+)")
_PATCH_FILE_RE = re.compile(
    r"^\*\*\*\s+(?:Update File|Add File|Delete File):\s*(\S+)", re.MULTILINE
)
_WS_RE = re.compile(r"\s+")
_DIGIT_RE = re.compile(r"\d+")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _norm_cmd(text: str) -> str:
    """Normalize a command for near-identical comparison."""
    collapsed = _WS_RE.sub(" ", text.strip())
    return _DIGIT_RE.sub("#", collapsed)


#: Supplement to the shared traj edit patterns. The shared
#: ``EDIT_COMMAND_PATTERNS`` is wrapped in ``\b(...)\b``, so a bare
#: redirection such as ``cat > file`` never matches (``>`` followed by a
#: space is not a word boundary). The redirect alternative below covers
#: shell ``>``/``>>`` writes the shared patterns miss; ``2>``/``>&``/``->``
#: are excluded.
_EDIT_REDIRECT_RE = re.compile(r"(?<![\-<>0-9&])>\s*[^\s;|&\"'>]+|\btee\b")

#: Write signals inside embedded scripts (heredocs, ``python -c``). The
#: shared patterns match any ``open(``, including read-only probes such as
#: ``open(path).read()``; a step counts as an edit only with one of these.
_WRITE_SIGNAL_RE = re.compile(
    r"\.write\s*\(|write_text|os\.replace|os\.rename|shutil\.\w+"
    r"|open\([^)]*['\"][wax]['\"]"
)

#: Read-only probe marker: ``python`` opening a file without a write signal
#: is inspection, not an edit.
_PYTHON_READ_RE = re.compile(r"\bpython[23]?\b.*\bopen\s*\(", re.DOTALL)

#: Quoted ``open()`` targets inside embedded scripts (heredoc writers).
_OPEN_TARGET_RE = re.compile(r"\bopen\(\s*['\"]([^'\"]+)['\"]")

#: ``path = "..."`` assignments in embedded scripts: heredoc writers bind
#: the target to a variable, then call ``open(path, "w")``.
_PATH_ASSIGN_RE = re.compile(
    r"^\s*(?:path|f|fp|filename|target|out(?:put|file)?)\s*=\s*['\"]([^'\"]+)['\"]",
    re.MULTILINE,
)
#: ``sed -i`` command segments (multiline: the script is usually a quoted
#: program with embedded newlines); the target is the last plausible path
#: once quoted scripts are stripped.
_SED_INPLACE_RE = re.compile(r"\bsed\s+-i[^;&|]*")

#: Quoted spans, stripped before ``sed -i`` target extraction so the
#: ``s/x/y/`` script is not mistaken for a path.
_QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

#: Plausible file-path token: no code punctuation, with a slash or an
#: extension. Filters heredoc-code false hits such as ``a > b`` type
#: comparisons captured as "redirection targets".
_PATH_TOKEN_RE = re.compile(r"^[^\s;|&\"'()\[\]{},:]+(\.[A-Za-z0-9]{1,5})?$")


def _looks_like_path(token: str) -> bool:
    token = token.strip().strip("'\"")
    if not token or ("/" not in token and "." not in token):
        return False
    return bool(_PATH_TOKEN_RE.match(token))


#: Write targets outside the task repo: scratch, not a repo edit.
_EPHEMERAL_PREFIXES = ("/tmp/", "/dev/", "/proc/", "/sys/", "/var/tmp/")


def _step_calls(step: dict[str, Any]) -> list[tuple[str | None, str]]:
    """``(tool_name, keystrokes)`` pairs proposed by one trajectory step."""
    out: list[tuple[str | None, str]] = []
    calls = step.get("tool_calls")
    if isinstance(calls, list):
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = call.get("function_name")
            args = call.get("arguments")
            keys = ""
            if isinstance(args, dict):
                for field in ("keystrokes", "command", "cmd", "input", "script"):
                    val = args.get(field)
                    if isinstance(val, str) and val.strip():
                        keys = val
                        break
            elif isinstance(args, str):
                keys = args
            out.append((name if isinstance(name, str) else None, keys if keys else ""))
    # Fallback: the accepted layer records what the harness actually sent.
    if not any(text for _, text in out):
        try:
            accepted = step.get("extra", {}).get("step_layers", {}).get("accepted", {})
        except AttributeError:
            accepted = {}
        if isinstance(accepted, dict):
            for call in accepted.get("calls", []) or []:
                if isinstance(call, dict) and isinstance(call.get("keystrokes"), str):
                    out.append((None, call["keystrokes"]))
    return out


def _is_ephemeral_only(paths: list[str]) -> bool:
    """True when every extracted target is scratch space, not the repo."""
    return bool(paths) and all(
        path in ("/tmp", "/dev", "/proc", "/sys") or path.startswith(_EPHEMERAL_PREFIXES)
        for path in paths
    )


def _is_edit(step: dict[str, Any]) -> tuple[bool, str]:
    """Whether a step looks like a file edit, reusing the traj detectors."""
    from evallab.traj import EDIT_COMMAND_PATTERNS, EDIT_TOOL_NAMES

    for name, keys in _step_calls(step):
        if name is not None and name.lower() in EDIT_TOOL_NAMES:
            return True, f"tool:{name}"
        if not keys:
            continue
        if _WRITE_SIGNAL_RE.search(keys):
            return True, "write-call"
        if EDIT_COMMAND_PATTERNS.search(keys) and not (
            _PYTHON_READ_RE.search(keys) and not _WRITE_SIGNAL_RE.search(keys)
        ):
            return True, "command-pattern"
        if _EDIT_REDIRECT_RE.search(keys):
            return True, "shell-redirect"
    return False, ""


def _touched_paths(step: dict[str, Any]) -> list[str]:
    """Best-effort target paths of an edit step's commands."""
    paths: list[str] = []
    for _, keys in _step_calls(step):
        if not keys:
            continue
        for match in _REDIRECT_TARGET_RE.finditer(keys):
            candidate = match.group(1).strip().strip("'\"")
            if _looks_like_path(candidate):
                paths.append(candidate)
        for match in _TEE_TARGET_RE.finditer(keys):
            candidate = match.group(1).strip().strip("'\"")
            if _looks_like_path(candidate):
                paths.append(candidate)
        for match in _PATCH_FILE_RE.finditer(keys):
            paths.append(match.group(1).strip())
        for match in _OPEN_TARGET_RE.finditer(keys):
            candidate = match.group(1).strip()
            if _looks_like_path(candidate):
                paths.append(candidate)
        for match in _PATH_ASSIGN_RE.finditer(keys):
            candidate = match.group(1).strip()
            if _looks_like_path(candidate):
                paths.append(candidate)
        # sed scripts are quoted multiline programs: strip quotes first so
        # the script body (and its `;`) cannot truncate the segment.
        unquoted = _QUOTED_SPAN_RE.sub(" ", keys)
        for segment in _SED_INPLACE_RE.finditer(unquoted):
            tokens = re.split(r"[\s;|&]+", segment.group(0))
            candidates = [token for token in tokens if _looks_like_path(token)]
            if candidates:
                paths.append(candidates[-1])
    # Drop option-like captures and duplicates, keep order.
    seen: set[str] = set()
    out = []
    for path in paths:
        if not path or path.startswith("-") or path in seen:
            continue
        seen.add(path)
        out.append(path)
    return out[:8]


def _observation_chars(step: dict[str, Any]) -> int:
    """Terminal-output characters fed back in one step's observation."""
    obs = step.get("observation")
    if obs is None:
        return 0
    if isinstance(obs, str):
        return len(obs)
    if isinstance(obs, dict):
        total = 0
        results = obs.get("results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    content = item.get("content")
                    if isinstance(content, str):
                        total += len(content)
                elif isinstance(item, str):
                    total += len(item)
        return total
    if isinstance(obs, list):
        return sum(len(item) for item in obs if isinstance(item, str))
    return 0


def _metrics(step: dict[str, Any]) -> tuple[int | None, int | None]:
    metrics = step.get("metrics")
    if not isinstance(metrics, dict):
        return None, None
    prompt = metrics.get("prompt_tokens")
    completion = metrics.get("completion_tokens")
    return (
        prompt if isinstance(prompt, int) else None,
        completion if isinstance(completion, int) else None,
    )


def _median(values: list[int]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _slope(xs: list[float], ys: list[float]) -> float | None:
    """Least-squares slope of prompt tokens per call; None if degenerate."""
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denom = sum((x - mean_x) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)) / denom


def _stitched_steps(trial_dir: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Stitched trajectory step dicts, reusing the shared step library."""
    from evallab.step_layers import discover_trajectory_parts, stitch_steps

    agent_dir = trial_dir / "agent"
    if not agent_dir.is_dir():
        return None, "no agent/ directory"
    try:
        parts = discover_trajectory_parts(agent_dir)
    except (OSError, ValueError) as exc:
        return None, f"trajectory parts undiscoverable: {type(exc).__name__}"
    docs: list[dict[str, Any]] = []
    for part in parts:
        if not part.readable:
            continue
        try:
            payload = json.loads(part.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            docs.append(payload)
    if not docs:
        return None, "no readable trajectory parts"
    steps, _stats = stitch_steps(docs)
    dict_steps = [step for step in steps if isinstance(step, dict)]
    if not dict_steps:
        return None, "stitched steps carry no step dicts"
    return dict_steps, None


def _last_useful_edit(agent_steps: list[dict[str, Any]], trial_dir: Path) -> dict[str, Any]:
    """Last edit-like agent step plus a best-effort persistence check."""
    for position in range(len(agent_steps) - 1, -1, -1):
        step = agent_steps[position]
        is_edit, kind = _is_edit(step)
        if not is_edit:
            continue
        touched = _touched_paths(step)
        if _is_ephemeral_only(touched):
            # Scratch writes (``> /tmp/...``) are not repo edits.
            continue
        step_id = step.get("step_id")
        excerpt = " | ".join(
            text.strip().split("\n")[0][:160] for _, text in _step_calls(step) if text.strip()
        )[:300]
        record: dict[str, Any] = {
            "step_id": step_id if isinstance(step_id, int) else None,
            "call_index": position + 1,
            "kind": kind,
            "command_excerpt": excerpt or None,
            "touched_paths": touched,
            "persists_in_final_diff": None,
            "persistence_reason": None,
        }
        diff_path = trial_dir / "verifier" / "agent.diff"
        try:
            diff_text = diff_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            record["persistence_reason"] = "verifier/agent.diff unreadable"
            return record
        if not diff_text.strip():
            record["persists_in_final_diff"] = False
            record["persistence_reason"] = "final diff is empty"
            return record
        if not touched:
            record["persistence_reason"] = (
                "edit detected but no target path extracted; persistence uncheckable (see limits)"
            )
            return record
        hit = any(Path(path).name and Path(path).name in diff_text for path in touched)
        record["persists_in_final_diff"] = bool(hit)
        record["persistence_reason"] = (
            "touched basename present in final diff"
            if hit
            else "no touched basename in final diff (edit reverted or elsewhere)"
        )
        return record
    return {"step_id": None, "reason": "no edit-like command in any agent step"}


def _loop_onset(agent_steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Earliest repetition-without-progress onset over stitched agent steps."""
    from evallab.probe03 import LOOP_MIN_RUN

    signatures: list[str] = []
    edit_flags: list[bool] = []
    for step in agent_steps:
        texts = [text for _, text in _step_calls(step) if text.strip()]
        signatures.append(_norm_cmd(" ".join(texts)) if texts else "")
        edit_flags.append(_is_edit(step)[0])

    cmd_onset: int | None = None
    cmd_detail: str | None = None
    run_start = 0
    for i in range(1, len(signatures) + 1):
        boundary = (
            i == len(signatures) or not signatures[i] or signatures[i] != signatures[run_start]
        )
        if boundary:
            length = i - run_start
            if (
                length >= COMMAND_RUN_MIN
                and signatures[run_start]
                and not any(edit_flags[run_start:i])
            ):
                cmd_onset = run_start
                cmd_detail = f"{length}x normalized command `{signatures[run_start][:80]}`"
                break
            run_start = i

    msg_onset: int | None = None
    msg_detail: str | None = None
    run_start = 0
    messages: list[str] = [
        message if isinstance(message, str) else ""
        for step in agent_steps
        for message in [step.get("message")]
    ]
    for i in range(1, len(messages) + 1):
        boundary = (
            i == len(messages)
            or not messages[i].strip()
            or messages[i].strip() != messages[run_start].strip()
        )
        if boundary:
            length = i - run_start
            if length >= LOOP_MIN_RUN and messages[run_start].strip():
                msg_onset = run_start
                msg_detail = f"{length}x identical message"
                break
            run_start = i

    candidates = [
        (pos, kind, detail)
        for pos, kind, detail in (
            (cmd_onset, "normalized_command_run", cmd_detail),
            (msg_onset, "identical_message_run", msg_detail),
        )
        if pos is not None
    ]
    if not candidates:
        return {"step_id": None, "reason": "no repeat run detected"}
    candidates.sort(key=lambda item: item[0])
    first, rest = candidates[0], candidates[1:]
    detector = first[1]
    detail = first[2] or ""
    if rest and rest[0][0] == first[0]:
        detector = "both"
        detail = f"{first[2]} + {rest[0][2]}"
    step = agent_steps[first[0]]
    step_id = step.get("step_id")
    return {
        "step_id": step_id if isinstance(step_id, int) else None,
        "call_index": first[0] + 1,
        "detector": detector,
        "detail": detail,
    }


def _prompt_series(
    agent_steps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    series: list[dict[str, Any]] = []
    unmetered = 0
    for position, step in enumerate(agent_steps):
        prompt, completion = _metrics(step)
        if prompt is None:
            unmetered += 1
            continue
        step_id = step.get("step_id")
        series.append(
            {
                "step_id": step_id if isinstance(step_id, int) else None,
                "call_index": position + 1,
                "prompt_tokens": prompt,
                "completion_tokens": completion,
            }
        )
    prompts = [entry["prompt_tokens"] for entry in series]
    summary: dict[str, Any] = {
        "n_calls": len(series),
        "steps_without_metrics": unmetered,
        "first": prompts[0] if prompts else None,
        "max": max(prompts) if prompts else None,
        "max_step_id": None,
        "median": _median(prompts),
        "slope_per_call": None,
        "reason": None,
    }
    if prompts:
        peak = max(prompts)
        for entry in series:
            if entry["prompt_tokens"] == peak:
                summary["max_step_id"] = entry["step_id"]
                break
        summary["slope_per_call"] = _slope(
            [float(entry["call_index"]) for entry in series],
            [float(value) for value in prompts],
        )
    else:
        summary["reason"] = "no agent step carries prompt metrics"
    return series, summary


def _summarisations(
    steps: list[dict[str, Any]], trial_dir: Path, result: dict[str, Any]
) -> dict[str, Any]:
    metadata = (result.get("agent_result") or {}).get("metadata") or {}
    count = metadata.get("summarization_count")
    metadata_count = count if isinstance(count, int) else None
    agent_dir = trial_dir / "agent"

    events: list[dict[str, Any]] = []
    for position, step in enumerate(steps):
        if step.get("source") != "system":
            continue
        message = step.get("message")
        if not isinstance(message, str) or _SUMMARIZATION_MARKER not in message:
            continue
        step_id = step.get("step_id")
        before: dict[str, Any] | None = None
        for prev in reversed(steps[:position]):
            if prev.get("source") in AGENT_SOURCES:
                prompt, _ = _metrics(prev)
                if prompt is not None:
                    prev_id = prev.get("step_id")
                    before = {
                        "step_id": prev_id if isinstance(prev_id, int) else None,
                        "prompt_tokens": prompt,
                    }
                    break
        after: dict[str, Any] | None = None
        for nxt in steps[position + 1 :]:
            if nxt.get("source") in AGENT_SOURCES:
                prompt, _ = _metrics(nxt)
                if prompt is not None:
                    nxt_id = nxt.get("step_id")
                    after = {
                        "step_id": nxt_id if isinstance(nxt_id, int) else None,
                        "prompt_tokens": prompt,
                    }
                    break
        # What the handoff kept: the follow-up user message plus any
        # summarization subagent trajectory files beside the head.
        kept_chars = 0
        kept_parts: list[str] = []
        for nxt in steps[position + 1 :]:
            if nxt.get("source") == "user" and isinstance(nxt.get("message"), str):
                kept_chars += len(nxt["message"])
                kept_parts.append("handoff_user_message")
                break
        kept_files: list[str] = []
        try:
            entries = sorted(agent_dir.iterdir(), key=lambda e: e.name)
        except OSError:
            entries = []
        index_guess = len(events) + 1
        for entry in entries:
            if entry.is_file() and entry.name.startswith(
                f"trajectory.summarization-{index_guess}-"
            ):
                kept_files.append(entry.name)
                with contextlib.suppress(OSError):
                    kept_chars += entry.stat().st_size // 4
        if kept_files:
            kept_parts.append(f"{len(kept_files)} subagent trajectories")
        success: bool | None = None
        success_reason: str | None = None
        if before is not None and after is not None:
            success = after["prompt_tokens"] < before["prompt_tokens"]
            if not success:
                success_reason = "prompt did not shrink across the handoff"
        else:
            success_reason = "prompt before/after unreadable"
        events.append(
            {
                "index": index_guess,
                "step_id": step_id if isinstance(step_id, int) else None,
                "prompt_before": before,
                "prompt_after": after,
                "prompt_delta": (
                    after["prompt_tokens"] - before["prompt_tokens"]
                    if before is not None and after is not None
                    else None
                ),
                "kept": {"chars": kept_chars, "parts": kept_parts, "files": kept_files},
                "success": success,
                "success_reason": success_reason,
            }
        )
    discrepancy = None
    if metadata_count is not None and metadata_count != len(events):
        if metadata_count > len(events):
            discrepancy = (
                f"metadata counts {metadata_count} but only {len(events)} "
                "handoff system steps found; the surplus likely failed "
                "mid-summarization (the harness increments the counter "
                "before the subagents run)"
            )
        else:
            discrepancy = (
                f"metadata counts {metadata_count} but {len(events)} handoff system steps found"
            )
    return {
        "metadata_count": metadata_count,
        "metadata_reason": (
            None
            if metadata_count is not None
            else "agent_result.metadata.summarization_count missing"
        ),
        "events": events,
        "discrepancy": discrepancy,
    }


def _top_outputs(agent_steps: list[dict[str, Any]], k: int = TOP_OUTPUTS_K) -> list[dict[str, Any]]:
    sized: list[tuple[int, int]] = []  # (chars, position)
    for position, step in enumerate(agent_steps):
        chars = _observation_chars(step)
        if chars > 0:
            sized.append((chars, position))
    sized.sort(key=lambda item: item[0], reverse=True)
    out: list[dict[str, Any]] = []
    for chars, position in sized[:k]:
        step = agent_steps[position]
        step_id = step.get("step_id")
        next_prompt: int | None = None
        for nxt in agent_steps[position + 1 :]:
            prompt, _ = _metrics(nxt)
            if prompt is not None:
                next_prompt = prompt
                break
        out.append(
            {
                "step_id": step_id if isinstance(step_id, int) else None,
                "call_index": position + 1,
                "chars": chars,
                "est_tokens": chars // CHARS_PER_TOKEN,
                "next_prompt_tokens": next_prompt,
            }
        )
    return out


def _stop(result: dict[str, Any], job_dir: Path | None) -> dict[str, Any]:
    from evallab.probe03 import EXCEPTION_STOP, ceiling_which

    agent_result = result.get("agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    metadata = agent_result.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    exc_info = result.get("exception_info")
    exc_type = exc_info.get("exception_type") if isinstance(exc_info, dict) else None
    stop_reason = metadata.get("stop_reason")
    stop_reason = stop_reason if isinstance(stop_reason, str) else None
    resolved = stop_reason or EXCEPTION_STOP.get(exc_type or "", None)
    if exc_type == "TrialBudgetExhaustedError" and job_dir is not None:
        episodes = metadata.get("n_episodes")
        with contextlib.suppress(OSError, ValueError, TypeError):
            resolved = ceiling_which(
                job_dir,
                agent_result.get("n_input_tokens"),
                agent_result.get("n_output_tokens"),
                episodes if isinstance(episodes, (int, float)) else None,
            )
    if resolved is None and exc_type is None and agent_result:
        reason = (
            "agent stopped without exception and without a harness stop "
            "label (clean finish, unlabeled by this harness version)"
        )
    elif resolved is None:
        reason = "no metadata stop_reason and unmapped exception type"
    else:
        reason = None
    return {
        "stop_reason": resolved,
        "exception_type": exc_type,
        "metadata_stop_reason": stop_reason,
        "reason": reason,
    }


def analyze_token_flow(
    trial_dir: str | Path,
    job_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Compute the token-flow record for one trial directory.

    Never raises on bad inputs: every unmeasurable block is ``None`` with a
    ``reason``. ``job_dir`` (the landed Harbor job holding the trial)
    supplies the proxy ledger limits for the stop reason; without it the
    stop block falls back to trial-local fields.
    """
    trial_path = Path(trial_dir)
    job_path = Path(job_dir) if job_dir is not None else None
    record: dict[str, Any] = {
        "schema": TOKEN_FLOW_SCHEMA,
        "trial_name": trial_path.name,
    }
    if not trial_path.is_dir():
        reason = "trial directory missing"
        record.update(
            {
                "trajectory": None,
                "trajectory_reason": reason,
                "last_useful_edit": {"step_id": None, "reason": reason},
                "tokens_after_last_edit": None,
                "tokens_after_reason": reason,
                "loop_onset": {"step_id": None, "reason": reason},
                "prompt_series": [],
                "prompt_summary": {"n_calls": 0, "reason": reason},
                "summarisations": {
                    "metadata_count": None,
                    "metadata_reason": reason,
                    "events": [],
                    "discrepancy": None,
                },
                "top_terminal_outputs": [],
                "stop": {
                    "stop_reason": None,
                    "exception_type": None,
                    "metadata_stop_reason": None,
                    "reason": reason,
                },
            }
        )
        return record

    result = _read_json(trial_path / "result.json") or {}
    steps, steps_reason = _stitched_steps(trial_path)
    if steps is None:
        reason = steps_reason or "trajectory unreadable"
        record.update(
            {
                "trajectory": None,
                "trajectory_reason": reason,
                "last_useful_edit": {"step_id": None, "reason": reason},
                "tokens_after_last_edit": None,
                "tokens_after_reason": reason,
                "loop_onset": {"step_id": None, "reason": reason},
                "prompt_series": [],
                "prompt_summary": {"n_calls": 0, "reason": reason},
                "summarisations": _summarisations([], trial_path, result),
                "top_terminal_outputs": [],
                "stop": _stop(result, job_path),
            }
        )
        return record

    agent_steps = [step for step in steps if step.get("source") in AGENT_SOURCES]
    unmetered = sum(1 for step in agent_steps if _metrics(step)[0] is None)
    record["trajectory"] = {
        "n_steps_stitched": len(steps),
        "n_agent_steps": len(agent_steps),
        "agent_steps_without_metrics": unmetered,
    }
    record["trajectory_reason"] = None

    last_edit = _last_useful_edit(agent_steps, trial_path)
    record["last_useful_edit"] = last_edit

    agent_result = result.get("agent_result")
    agent_result = agent_result if isinstance(agent_result, dict) else {}
    total_in = agent_result.get("n_input_tokens")
    total_out = agent_result.get("n_output_tokens")
    totals_source = "agent_result"
    if not isinstance(total_in, int) or not isinstance(total_out, int):
        total_in = sum(p for _, p in ((_metrics(s)) for s in agent_steps) if p)
        total_out = sum(c for _, c in ((_metrics(s)) for s in agent_steps) if c)
        totals_source = "step_metrics_sum"
    if last_edit.get("step_id") is None and "reason" in last_edit:
        # No useful edit: everything was spent without a persisting change.
        record["tokens_after_last_edit"] = {
            "input_tokens": total_in,
            "output_tokens": total_out,
            "share_input": 1.0,
            "share_output": 1.0,
            "totals_source": totals_source,
            "note": "no useful edit detected; whole run is post-edit waste",
        }
        record["tokens_after_reason"] = None
    else:
        # Locate the edit by identity of (step_id, call_index).
        edit_pos: int | None = None
        for pos, s in enumerate(agent_steps):
            sid = s.get("step_id")
            if (
                last_edit.get("step_id") is not None
                and sid == last_edit["step_id"]
                and pos + 1 == last_edit["call_index"]
            ):
                edit_pos = pos
                break
        if edit_pos is None:
            # Fall back to the last edit-like position (step ids may repeat
            # across continuation segments).
            for pos in range(len(agent_steps) - 1, -1, -1):
                if _is_edit(agent_steps[pos])[0]:
                    edit_pos = pos
                    break
        if edit_pos is None:
            record["tokens_after_last_edit"] = None
            record["tokens_after_reason"] = "edit step not relocatable"
        else:
            after_in = sum(p for p, _ in (_metrics(s) for s in agent_steps[edit_pos + 1 :]) if p)
            after_out = sum(c for _, c in (_metrics(s) for s in agent_steps[edit_pos + 1 :]) if c)
            record["tokens_after_last_edit"] = {
                "input_tokens": after_in,
                "output_tokens": after_out,
                "share_input": (
                    round(after_in / total_in, 4)
                    if isinstance(total_in, int) and total_in > 0
                    else None
                ),
                "share_output": (
                    round(after_out / total_out, 4)
                    if isinstance(total_out, int) and total_out > 0
                    else None
                ),
                "totals_source": totals_source,
                "note": None,
            }
            record["tokens_after_reason"] = None

    record["loop_onset"] = (
        _loop_onset(agent_steps)
        if agent_steps
        else {
            "step_id": None,
            "reason": "no agent steps",
        }
    )
    series, summary = _prompt_series(agent_steps)
    record["prompt_series"] = series
    record["prompt_summary"] = summary
    record["summarisations"] = _summarisations(steps, trial_path, result)
    record["top_terminal_outputs"] = _top_outputs(agent_steps)
    record["stop"] = _stop(result, job_path)
    return record


def trial_flags(token_flow: dict[str, Any] | None) -> list[str]:
    """Short flag strings for the process-job summary table."""
    if not token_flow:
        return []
    flags: list[str] = []
    edit = token_flow.get("last_useful_edit") or {}
    if edit.get("step_id") is None:
        flags.append("no_persisting_edit")
    after = token_flow.get("tokens_after_last_edit") or {}
    share = after.get("share_input")
    if isinstance(share, (int, float)) and not math.isnan(share):
        flags.append(f"post_edit_input_share:{share:.0%}")
    onset = token_flow.get("loop_onset") or {}
    if onset.get("step_id") is not None:
        flags.append(f"loop_onset:step-{onset['step_id']}:{onset.get('detector')}")
    summ = token_flow.get("summarisations") or {}
    events = summ.get("events") or []
    fired = [event for event in events if isinstance(event, dict)]
    succeeded = sum(1 for event in fired if event.get("success") is True)
    if fired:
        flags.append(f"summarised:{succeeded}/{len(fired)}-shrunk")
    if summ.get("discrepancy"):
        flags.append("summarisation_gap")
    summary = token_flow.get("prompt_summary") or {}
    if isinstance(summary.get("max"), int):
        flags.append(f"max_prompt:{summary['max']}")
    return flags


def markdown_lines(token_flow: dict[str, Any] | None) -> list[str]:
    """Short markdown lines for the per-trial run report."""
    if not token_flow:
        return []
    lines = ["- token flow (HAR-114):"]
    edit = token_flow.get("last_useful_edit") or {}
    if edit.get("step_id") is not None:
        persist = edit.get("persists_in_final_diff")
        lines.append(
            f"  - last useful edit: step `{edit['step_id']}` "
            f"(call {edit.get('call_index')}, {edit.get('kind')}; "
            f"persists in final diff: `{persist}`"
            + (f" -- {edit.get('persistence_reason')}" if edit.get("persistence_reason") else "")
            + ")"
        )
    else:
        lines.append(f"  - last useful edit: none ({edit.get('reason')})")
    after = token_flow.get("tokens_after_last_edit") or {}
    if after:
        lines.append(
            f"  - tokens after last edit: `{after.get('input_tokens')}` in / "
            f"`{after.get('output_tokens')}` out "
            f"(shares `{after.get('share_input')}` / `{after.get('share_output')}` "
            f"of {after.get('totals_source')})"
            + (f" -- {after.get('note')}" if after.get("note") else "")
        )
    onset = token_flow.get("loop_onset") or {}
    if onset.get("step_id") is not None:
        lines.append(
            f"  - loop onset: step `{onset['step_id']}` "
            f"(call {onset.get('call_index')}, {onset.get('detector')}: "
            f"{onset.get('detail')})"
        )
    else:
        lines.append(f"  - loop onset: none ({onset.get('reason')})")
    summary = token_flow.get("prompt_summary") or {}
    lines.append(
        f"  - prompt/call: first `{summary.get('first')}`, max "
        f"`{summary.get('max')}` (step `{summary.get('max_step_id')}`), "
        f"median `{summary.get('median')}`, slope "
        f"`{summary.get('slope_per_call')}` tokens/call over "
        f"`{summary.get('n_calls')}` calls"
    )
    summ = token_flow.get("summarisations") or {}
    events = summ.get("events") or []
    shrunk = sum(1 for e in events if e.get("success") is True)
    summ_line = (
        f"  - summarisations: metadata `{summ.get('metadata_count')}` / "
        f"{len(events)} handoff events found"
    )
    if shrunk:
        summ_line += f" ({shrunk} shrunk)"
    lines.append(summ_line)
    for event in events:
        before = (event.get("prompt_before") or {}).get("prompt_tokens")
        after_tok = (event.get("prompt_after") or {}).get("prompt_tokens")
        lines.append(
            f"    - #{event.get('index')} at step `{event.get('step_id')}`: "
            f"`{before}` -> `{after_tok}` "
            f"(success `{event.get('success')}`; kept "
            f"`{(event.get('kept') or {}).get('chars')}` chars)"
        )
    if summ.get("discrepancy"):
        lines.append(f"    - gap: {summ['discrepancy']}")
    tops = token_flow.get("top_terminal_outputs") or []
    if tops:
        refs = ", ".join(f"step `{t.get('step_id')}`:{t.get('chars')}ch" for t in tops[:3])
        lines.append(f"  - largest terminal outputs fed back: {refs}")
    return lines
