"""Deterministic trial diagnosis: trajectory -> failure modes + bounded Reef feedback.

Reads a Harbor trial directory (Terminus 2 or mini-swe-agent ATIF at minimum)
and returns a typed, deterministic diagnosis: an outcome (scored reward,
unscored, or infra-failed) plus multi-label failure modes with step-level
evidence. Two renderers serve Reef's report ``feedback`` field (string or
object, ``docs/reference/http-api.rst:559-562`` @ reef ``818997d7``): a
JSON-serializable object and a bounded plain-text rendering. The text form
suits the reefine proposer, which reads each failing sample's score and
feedback verbatim (``reef/recipe/reefine/evolution.py:686-696``) inside a
fenced-as-data prompt (``:288-298``), and the object form carries the same
signal a GEPA ``Feedback`` hook (``recipes/gepa/method.py:61-65``) reflects
over. HAR-74 will post these as each Reef report's ``feedback``; HAR-74
itself is out of scope here.

Outcome semantics (never conflated):

* ``infra_failed`` -- the trial raised (``exception_info`` present). No modes.
* ``unscored`` -- no exception but no numeric reward. No modes.
* ``scored`` -- numeric reward present. Modes attach only to scored failures
  (reward < 1.0) with a featured trajectory; scored passes, controls without
  an agent trajectory, and unscored/infra trials carry zero modes, so an
  unscored or infra-failed trial is never labeled as a task failure.

Failure-mode taxonomy (``trial_diagnosis/failure_mode/v1``) reuses
``trajectory_behavior/v1`` concepts where they exist and extends only where a
mode is missing:

* ``tool_use_loop`` -- reuses the outline's ``loop_suspicion`` signal.
* ``planning_no_edit`` -- the heuristic's tool-calls-without-edit concept,
  with step-level evidence; the outline's edit pattern is OR-ed with a local
  heredoc/redirect matcher it misses.
* ``unrecovered_error`` -- the outline's ``unrecovered_at_terminal`` idea,
  recomputed locally because the outline's error taxonomy does not unwrap
  mini-swe-agent returncode envelopes (real failed shell trials report zero
  outline errors).
* ``no_tool_use`` (new) -- the heuristic taxonomy has ``setup_failure`` for
  zero agent steps but no mode for an agent that responds without ever
  calling a tool; mirrors Reef's ``never used a tool`` flag.
* ``empty_terminal_reply`` (new) -- no heuristic equivalent; mirrors Reef's
  ``turn ended on an empty reply`` flag.
* ``silent_tool_output`` (new) -- the outline counts errors but never flags
  empty successful outputs; mirrors Reef's ``ran code that printed nothing``.
  Flagged only when two or more tool steps return empty output, since a
  single empty output (``mkdir``, ``cd``) is routine harness traffic.
* ``wrong_tool_arguments`` (new) -- the error taxonomy classifies errors but
  surfaces no argument-specific mode; mirrors Reef's ``called a tool with
  wrong arguments`` flag.
* ``state_persistence_assumption`` (new) -- no heuristic equivalent; mirrors
  Reef's ``assumed state persisted between tool calls`` flag (``NameError``
  in the REPL harness; bare ``cd`` followed by a relative-path failure in a
  shell harness).
* ``unclassified_failure`` (fallback) -- a scored failure no detector
  matched; mirrors Reef's ``no flag matched`` bucket. Keeps the output
  honest instead of forcing a wrong mode.

Deliberately absent: Reef's ``had the right number in tool output, never
reported it`` has no deterministic ATIF counterpart without hidden verifier
inputs, which must never enter analysis output -- so it stays missing and is
documented here rather than guessed.

Safety rules (mirroring ``gepa_optimizer.feedback``): excerpts are
deterministic, bounded, and sanitized -- hidden ``tests/``/``solution/``
path segments are redacted, secret-like tokens are redacted, the text
rendering is truncated to ``max_chars``, and raw step content never carries
usage, timestamps, logprobs, or rewards beyond the trial's own recorded
reward. No model calls of any kind.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from evallab.labels import propose_heuristic_label
from evallab.tracing import TraceError, is_job_dir, is_trial_dir
from evallab.traj import (
    EDIT_COMMAND_PATTERNS,
    EDIT_TOOL_NAMES,
    TrajectoryError,
    TrajectoryOutline,
    outline_trajectory,
    resolve_trial_target,
)

TRIAL_DIAGNOSIS_TAXONOMY = "trial_diagnosis/failure_mode/v1"
DETECTOR_VERSION = "trial_diagnosis/v1"
PASS_THRESHOLD = 1.0
DEFAULT_MAX_CHARS = 2000
EXCERPT_CHARS = 160
TRUNCATION_MARKER = "[truncated: feedback exceeded max_chars budget]"

Outcome = Literal["scored", "unscored", "infra_failed"]

MODE_ORDER = (
    "no_tool_use",
    "tool_use_loop",
    "empty_terminal_reply",
    "silent_tool_output",
    "wrong_tool_arguments",
    "state_persistence_assumption",
    "planning_no_edit",
    "unrecovered_error",
    "unclassified_failure",
)

_SUGGESTIONS: dict[str, str] = {
    "no_tool_use": (
        "The harness never got the agent to call a tool; "
        "check the tool manifest and the first-turn prompt."
    ),
    "tool_use_loop": (
        "The agent repeats the same call; the harness should surface the repetition or cap retries."
    ),
    "empty_terminal_reply": (
        "The turn ended with no message and no tool call; "
        "the harness should require a final answer or a tool call each turn."
    ),
    "silent_tool_output": (
        "Tool calls return no observable output; "
        "the harness should echo exit status or require printed evidence."
    ),
    "wrong_tool_arguments": (
        "Tool calls fail on their arguments; "
        "the harness should validate arguments or show the tool schema."
    ),
    "state_persistence_assumption": (
        "The agent assumes state persists between tool calls; "
        "the harness should state the persistence model explicitly."
    ),
    "planning_no_edit": (
        "The agent runs commands but never edits a file; "
        "the harness should point at the files under repair."
    ),
    "unrecovered_error": (
        "The run ends on an error with no recovery; "
        "the harness should prompt for diagnosis before the turn budget ends."
    ),
    "unclassified_failure": (
        "No detector matched; read the cited terminal step before changing the harness."
    ),
}

_WS_RE = re.compile(r"\s+")
_HIDDEN_PATH_RE = re.compile(r"(^|[\s/\\>\"'(\[])(tests|solution)([/\\]|$)")
_SECRET_RES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(bearer\s+[A-Za-z0-9._~+/-]{8,}|api[_-]?key\s*[:=]\s*\S+)"),
)
_ARGUMENT_ERROR_RE = re.compile(
    r"unrecognized argument|unexpected.*argument|takes .*positional argument|"
    r"missing .*required .*argument|got an unexpected keyword|invalid (option|argument)|"
    r"^usage:",
    re.IGNORECASE,
)
_SHELL_TOOLS = frozenset(
    {
        "bash",
        "bash_command",
        "shell",
        "sh",
        "dash",
        "zsh",
        "powershell",
        "cmd",
        "run_bash",
    }
)
_NON_SHELL_TOOLS = frozenset({"execute", "exec", "wait"})
_EXPECTED_SILENT_FIRST_TOKENS = frozenset(
    {
        "cd",
        "mkdir",
        "touch",
        "export",
        "unset",
        "rm",
        "mv",
        "cp",
        "chmod",
        "echo",
        "sleep",
        "true",
    }
)
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?\w")
_REDIRECT_RE = re.compile(r"\s\d?>>?\s")
_INPLACE_EDIT_RE = re.compile(r"(^|[\s;&|])(sed\s+[^;&|]*-i|perl\s+[^;&|]*-i)\b")
_CHAIN_SPLIT_RE = re.compile(r"&&|\|\||;")
_SQL_WRITE_RE = re.compile(
    r"(^|[\s;'\"()])(UPDATE|INSERT|DELETE|CREATE|DROP|ALTER)\b", re.IGNORECASE
)
_FILE_TASK_RE = re.compile(
    r"(/[\w.\-~]+){2,}|[\w.\-]+\.(py|json|jsonl|txt|md|rst|js|ts|sh|yaml|yml|toml|"
    r"html|css|java|go|rs|cpp|c|h|sql|db|csv|xml)\b"
)


def _strip_comment_lines(command: str) -> str:
    """Drop full-line ``#`` comments; narration-as-command carries no signal."""
    kept = [line for line in command.splitlines() if not line.strip().startswith("#")]
    return "\n".join(kept)


def _segment_expected(segment: str) -> bool:
    head = segment.strip().split(maxsplit=1)[0] if segment.strip() else ""
    return head in _EXPECTED_SILENT_FIRST_TOKENS


_GIT_RESTORE_RE = re.compile(r"git\s+(checkout|restore|stash|add|rm|mv|clean|reset)\b")
_EXIT_ZERO_RE = re.compile(r"^\s*exit 0\s*$")
_EXIT_CODE_RE = re.compile(r"^exit ([1-9]\d*)\b")


def _silence_is_expected(command: str | None, tool_name: str | None = None) -> bool:
    """File writes, in-place edits, and bare mutations are silent by design.

    Chained commands are expected-silent only when every segment is; a quiet
    ``mkdir`` must not excuse a silent test run chained behind it. The
    heredoc/redirect/in-place patterns are shell constructs: Reef's REPL
    ``execute`` tool runs code where ``>`` is a comparison, so they do not
    apply there (a bare ``exit 0`` stays silence).
    """
    if not command:
        return False
    code = _strip_comment_lines(command)
    if not code.strip():
        return True
    if all(
        _segment_expected(segment) or _GIT_RESTORE_RE.match(segment.strip())
        for segment in _CHAIN_SPLIT_RE.split(code)
    ):
        return True
    if (tool_name or "").lower() == "execute":
        return False
    if _HEREDOC_RE.search(code) or _REDIRECT_RE.search(code):
        return True
    return bool(_INPLACE_EDIT_RE.search(code))


_WRITE_REDIRECT_RE = re.compile(r"(<<-?\s*['\"]?\w|\d?>>?\s*\S)")


def _is_edit_call(tool_name: str | None, command: str | None) -> bool:
    """Outline edit signal plus writes the outline pattern misses.

    The outline's edit pattern ends in a word boundary after ``>``, so shell
    redirects such as ``cat <<'EOF' > fix.py`` never match; every such write
    would otherwise read as ``planning_no_edit`` on mini-swe-agent trials.
    Script-text heuristics apply to shell tools only: an ``exec`` script body
    full of ``>`` comparisons is not a redirect. SQL writes count because a
    DB-task agent that runs UPDATE has changed persistent state.
    """
    lowered = (tool_name or "").lower()
    if lowered in EDIT_TOOL_NAMES:
        return True
    if command and EDIT_COMMAND_PATTERNS.search(command):
        return True
    if not command or lowered not in _SHELL_TOOLS:
        return False
    return bool(_WRITE_REDIRECT_RE.search(command) or _SQL_WRITE_RE.search(command))


def _task_involves_files(views: list[_StepView]) -> bool:
    """File-task evidence: without paths, 'never edited a file' is vacuous."""
    haystack = "\n".join(
        ["\n".join(view.all_commands) for view in views] + [view.message or "" for view in views]
    )
    return bool(_FILE_TASK_RE.search(haystack))


_MISSING_FILE_RE = re.compile(
    r"No such file or directory|not found|can't open|does not exist",
    re.IGNORECASE,
)
_BARE_CD_RE = re.compile(r"^\s*cd(\s+[^;&|]+)?\s*$")
_NAME_ERROR_RE = re.compile(r"NameError:\s*name\s*'(\w+)'\s*is not defined")
_TRACEBACK_RE = re.compile(r"Traceback \(most recent call last\)")
_DEFINITION_RES = (
    re.compile(r"(?m)^\s*NAME\s*=[^=]"),
    re.compile(r"(?m)^\s*(def|class)\s+NAME\b"),
    re.compile(r"(?m)^\s*for\s+NAME\s+in\b"),
    re.compile(r"(?m)\bwith\b.*\bas\s+NAME\b"),
)
_MIN_SILENT_OUTPUTS = 2
_MAX_EVIDENCE_STEPS = 3
_MIN_LOOP_RUN = 3
_MIN_MESSAGE_CHARS = 50
_AGENT_SOURCES = frozenset({"agent", "assistant"})


@dataclass(frozen=True)
class FailureMode:
    """One diagnosed failure mode with step-level evidence."""

    mode: str
    step_ids: tuple[int, ...]
    excerpt: str
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "step_ids": list(self.step_ids),
            "excerpt": self.excerpt,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class TrialDiagnosis:
    """Typed diagnosis of one Harbor trial directory."""

    trial_id: str
    trial_name: str
    task_name: str
    agent_name: str
    model_name: str
    outcome: Outcome
    reward: float | None
    exception_class: str | None
    heuristic_label: str | None
    modes: tuple[FailureMode, ...] = ()
    notices: tuple[str, ...] = ()
    taxonomy: str = TRIAL_DIAGNOSIS_TAXONOMY
    detector_version: str = DETECTOR_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "taxonomy": self.taxonomy,
            "detector_version": self.detector_version,
            "trial_id": self.trial_id,
            "trial_name": self.trial_name,
            "task_name": self.task_name,
            "agent_name": self.agent_name,
            "model_name": self.model_name,
            "outcome": self.outcome,
            "reward": self.reward,
            "exception_class": self.exception_class,
            "heuristic_label": self.heuristic_label,
            "modes": [mode.to_dict() for mode in self.modes],
            "notices": list(self.notices),
        }


@dataclass
class _StepView:
    """Minimal normalized step for detectors (ATIF today, other logs later)."""

    step_id: int
    source: str
    message: str
    tool_name: str | None
    command: str | None
    all_commands: tuple[str, ...]
    all_tools: tuple[str, ...]
    outputs: list[str]
    is_error: bool


def sanitize_excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    """Collapse, redact hidden paths and secret-like tokens, and bound text."""
    collapsed = _WS_RE.sub(" ", text or "").strip()
    collapsed = _HIDDEN_PATH_RE.sub(r"\1[hidden-path]\3", collapsed)
    for pattern in _SECRET_RES:
        collapsed = pattern.sub("[redacted]", collapsed)
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip() + "..."


def _command_string(call_args: Any) -> str | None:
    if isinstance(call_args, str):
        stripped = call_args.strip()
        return stripped or None
    if isinstance(call_args, dict):
        for key in ("command", "keystrokes", "cmd", "script", "input", "code"):
            value = call_args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _output_text(content: Any) -> str:
    """Unwrap mini-swe-agent JSON envelopes; pass Terminus terminal text through.

    A blank output with a nonzero returncode or harness exception note is not
    silence: surface the envelope status so evidence cites the failure, not an
    empty string.
    """
    text = content if isinstance(content, str) else str(content or "")
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return text
        if isinstance(payload, dict) and "output" in payload:
            output = str(payload["output"] or "")
            if output.strip():
                return output
            returncode = payload.get("returncode")
            note = str(payload.get("exception_info") or "").strip()
            if returncode not in (None, 0) or note:
                detail = note or "no output"
                return f"[returncode {returncode}: {detail}]"
            return output
    return text


def _raw_steps(trial_dir: Path) -> list[dict[str, Any]]:
    try:
        _, traj_path, _ = resolve_trial_target(trial_dir, explicit_runs_root=trial_dir)
    except (TrajectoryError, ValueError, OSError):
        return []
    if traj_path is None or not traj_path.is_file():
        return []
    try:
        data = json.loads(traj_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    steps = data.get("steps")
    if not isinstance(steps, list):
        return []
    return [step for step in steps if isinstance(step, dict)]


def _envelope_returncode(content: Any) -> int | None:
    """Return the mini-swe-agent JSON envelope returncode, if present."""
    if not isinstance(content, str):
        return None
    stripped = content.strip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("returncode"), int):
        return payload["returncode"]
    return None


def _result_is_error(result: dict[str, Any]) -> bool:
    """Mirror the outline's error signals: exit codes, envelopes, type/status."""
    extra = result.get("extra")
    if (
        isinstance(extra, dict)
        and isinstance(extra.get("exit_code"), int)
        and extra["exit_code"] != 0
    ):
        return True
    if str(result.get("type") or "").lower() in {"error", "tool_error"}:
        return True
    if str(result.get("status") or "").lower() in {"error", "failed"}:
        return True
    return _envelope_returncode(result.get("content")) not in (None, 0)


def _step_views(raw_steps: list[dict[str, Any]]) -> list[_StepView]:
    views: list[_StepView] = []
    for position, raw in enumerate(raw_steps, start=1):
        calls = raw.get("tool_calls")
        call_list = calls if isinstance(calls, list) else []
        tool_name: str | None = None
        command: str | None = None
        all_commands: list[str] = []
        all_tools: list[str] = []
        for call in call_list:
            if not isinstance(call, dict):
                continue
            name = call.get("function_name")
            if isinstance(name, str) and name.strip():
                all_tools.append(name.strip())
            if tool_name is None and isinstance(name, str) and name.strip():
                tool_name = name.strip()
            text = _command_string(call.get("arguments"))
            if text:
                all_commands.append(text)
                if command is None:
                    command = text
        outputs: list[str] = []
        observation = raw.get("observation")
        results: list[Any] = []
        if isinstance(observation, dict):
            nested = observation.get("results")
            if isinstance(nested, list):
                results.extend(nested)
        extra_results = raw.get("observation_results")
        if isinstance(extra_results, list):
            results.extend(extra_results)
        # The REPL harness prefixes tool outputs with "exit N"; a nonzero
        # exit there is the error signal (mini-swe-agent envelopes and
        # result flags are checked separately).
        step_error = bool(raw.get("is_error"))
        for result in results:
            content = result.get("content") if isinstance(result, dict) else result
            text = content if isinstance(content, str) else ""
            if isinstance(result, dict):
                outputs.append(_output_text(result.get("content")))
                step_error = step_error or _result_is_error(result)
            elif isinstance(result, str):
                outputs.append(_output_text(result))
                step_error = step_error or (_envelope_returncode(result) not in (None, 0))
            step_error = step_error or bool(_EXIT_CODE_RE.match(text.strip()))
        views.append(
            _StepView(
                step_id=position,
                source=str(raw.get("source") or "agent"),
                message=str(raw.get("message") or ""),
                tool_name=tool_name,
                command=command,
                all_commands=tuple(all_commands),
                all_tools=tuple(all_tools),
                outputs=outputs,
                is_error=step_error,
            )
        )
    return views


def _mode(name: str, step_ids: list[int], excerpt: str) -> FailureMode:
    """Build one mode with de-duplicated, ordered, bounded step evidence."""
    ordered = sorted({step for step in step_ids if isinstance(step, int)})
    return FailureMode(
        mode=name,
        step_ids=tuple(ordered[:_MAX_EVIDENCE_STEPS]) or (0,),
        excerpt=sanitize_excerpt(excerpt) if excerpt else "",
        suggestion=_SUGGESTIONS[name],
    )


def _longest_command_run(
    tool_views: list[_StepView],
) -> tuple[list[int], str]:
    """Longest consecutive run of identical (tool, command), citing the run.

    Citing the run -- not the first triple -- keeps evidence on the egregious
    repetition (for example nine identical polls, not the first three calls).
    """
    best: list[int] = []
    best_cmd = ""
    run: list[int] = []
    run_key: tuple[str, str] | None = None
    run_cmd = ""
    for view in tool_views:
        key = (view.tool_name or "", view.command or "")
        if key == run_key:
            run.append(view.step_id)
        else:
            run = [view.step_id]
            run_key = key
            run_cmd = view.command or view.tool_name or ""
        if len(run) >= _MIN_LOOP_RUN and len(run) > len(best):
            best = list(run)
            best_cmd = run_cmd
    return best, best_cmd


def _message_run(agent_views: list[_StepView]) -> list[int]:
    """Consecutive identical agent messages (the no-tool-call loop)."""
    best: list[int] = []
    run: list[int] = []
    last = ""
    for view in agent_views:
        normalized = _WS_RE.sub(" ", view.message or "").strip()
        if normalized and len(normalized) >= _MIN_MESSAGE_CHARS and normalized == last:
            run.append(view.step_id)
        else:
            run = [view.step_id]
            last = normalized
        if normalized and len(run) >= _MIN_LOOP_RUN and len(run) > len(best):
            best = list(run)
    return best


def _defined_names(tool_views: list[_StepView]) -> set[str]:
    """Names the agent's own commands define (assignment, def, class, for, with).

    Imports do not count: importing a name earlier does not prove the agent
    expected it to persist.
    """
    script = "\n".join("\n".join(view.all_commands) for view in tool_views)
    names: set[str] = set()
    for template in _DEFINITION_RES:
        concrete = re.compile(template.pattern.replace("NAME", r"(\w+)"))
        for match in concrete.finditer(script):
            names.add(match.group(1))
    return names


def _detect(outline: TrajectoryOutline, views: list[_StepView]) -> list[FailureMode]:
    agent_views = [view for view in views if view.source.lower() in _AGENT_SOURCES]
    tool_views = [view for view in agent_views if view.tool_name]
    modes: list[FailureMode] = []

    if outline.total_tool_calls == 0:
        # Multi-label like Reef's flags: a tool-less run can still end on an
        # empty reply, so record and continue instead of returning early.
        anchor = agent_views[-1] if agent_views else None
        modes.append(
            _mode(
                "no_tool_use",
                [anchor.step_id] if anchor else [],
                anchor.message if anchor and anchor.message.strip() else "no tool calls recorded",
            )
        )

    if outline.loop_suspicion.detected or _message_run(agent_views):
        run_ids, run_cmd = _longest_command_run(tool_views)
        if run_ids:
            excerpt = run_cmd or "repeated tool calls"
            modes.append(
                _mode(
                    "tool_use_loop",
                    run_ids,
                    f"{len(run_ids)}x repeated: {excerpt}"
                    if excerpt != "repeated tool calls"
                    else f"{len(run_ids)} repeated tool calls",
                )
            )
        else:
            message_ids = _message_run(agent_views)
            excerpt = ""
            if message_ids:
                first = next(view for view in agent_views if view.step_id == message_ids[0])
                excerpt = first.message
            modes.append(
                _mode(
                    "tool_use_loop",
                    message_ids,
                    f"{len(message_ids)}x identical agent replies"
                    + (f": {excerpt}" if excerpt else ""),
                )
            )

    if agent_views and not any(view.message.strip() for view in agent_views):
        # Reef parity (04_gate_aa.py): the flag is the absence of any
        # non-empty assistant reply in the episode, not the shape of the
        # terminal step. A run of tool calls with no text back is the signal.
        terminal = agent_views[-1]
        modes.append(
            _mode(
                "empty_terminal_reply",
                [terminal.step_id],
                "no non-empty agent reply in the episode",
            )
        )

    def _output_is_silent(text: str) -> bool:
        # Reef parity: the REPL harness prints a bare "exit 0" when code
        # prints nothing, so that marker is silence, not output.
        return not text.strip() or bool(_EXIT_ZERO_RE.match(text))

    def _step_is_silent(view: _StepView) -> bool:
        # Reef parity: ANY bare "exit 0" marks the step (multi-output steps
        # pair confirmations with the marker). A merely blank output only
        # counts when every output is blank: shell steps routinely trail
        # whitespace.
        if any(_EXIT_ZERO_RE.match(out) for out in view.outputs):
            return True
        return all(not out.strip() for out in view.outputs)

    silent_views = [
        view
        for view in tool_views
        if not view.is_error
        and view.outputs
        and _step_is_silent(view)
        and not _silence_is_expected(view.command, view.tool_name)
    ]
    # A single quiet shell command (mkdir, cd) is routine traffic, hence the
    # two-silence minimum -- but a stateful REPL whose only feedback is a
    # bare "exit 0" tells the agent nothing, so one suffices there.
    tool_names = {
        tool.lower() for view in tool_views for tool in view.all_tools
    } | {(view.tool_name or "").lower() for view in tool_views}
    repl_protocols = {"execute", "run_bash", "read_file"}
    silent_min = (
        1 if tool_names and tool_names <= repl_protocols else _MIN_SILENT_OUTPUTS
    )
    if len(silent_views) >= silent_min:
        silent_cmds = [
            view.command or view.tool_name or "" for view in silent_views[:_MAX_EVIDENCE_STEPS]
        ]
        modes.append(
            _mode(
                "silent_tool_output",
                [view.step_id for view in silent_views],
                f"{len(silent_views)} silent outputs, e.g. {'; '.join(cmd for cmd in silent_cmds if cmd)}",
            )
        )

    arg_ids: list[int] = []
    arg_excerpt = ""
    for view in tool_views:
        haystack = " ".join(view.outputs)
        match = _ARGUMENT_ERROR_RE.search(haystack)
        # Bare "usage:" lines also appear in --help output, so they only count
        # when the step itself failed; other argument-error phrasing is specific.
        # A traceback names the agent's own code failing, not a tool call with
        # wrong arguments, so tracebacks never count here.
        if (
            match
            and not _TRACEBACK_RE.search(haystack)
            and (view.is_error or not match.group(0).lower().startswith("usage:"))
        ):
            arg_ids.append(view.step_id)
            if not arg_excerpt:
                arg_excerpt = next(
                    (out for out in view.outputs if _ARGUMENT_ERROR_RE.search(out)),
                    haystack,
                )
    if arg_ids:
        modes.append(_mode("wrong_tool_arguments", arg_ids, arg_excerpt))

    state_ids: list[int] = []
    state_excerpt = ""
    defined_names = _defined_names(tool_views)
    repl_tools = {"execute", "run_bash"}
    for view in tool_views:
        if not view.is_error:
            continue
        for out in view.outputs:
            name_match = _NAME_ERROR_RE.search(out)
            # A bare NameError also names the bug under test (repro scripts) or
            # words inside displayed source; on shell trials only a name the
            # agent itself defined earlier signals assumed-persistent state.
            # On REPL tools every exec call is a fresh cell, so any NameError
            # in a failing exec is the state signal directly.
            repl_cell = any(
                tool.lower() in repl_tools for tool in view.all_tools
            ) or (view.tool_name or "").lower() in repl_tools
            if name_match and (repl_cell or name_match.group(1) in defined_names):
                state_ids.append(view.step_id)
                if not state_excerpt:
                    state_excerpt = out
                break
    cd_ids = [
        view.step_id
        for view in tool_views
        if any(_BARE_CD_RE.match(cmd) for cmd in view.all_commands)
    ]
    if cd_ids:
        for view in tool_views:
            if view.step_id > cd_ids[0] and any(
                _MISSING_FILE_RE.search(out) for out in view.outputs
            ):
                state_ids.extend([cd_ids[0], view.step_id])
                if not state_excerpt:
                    anchor_cmd = next(
                        (
                            "\n".join(item.all_commands)
                            for item in tool_views
                            if item.step_id == cd_ids[0]
                        ),
                        "",
                    )
                    state_excerpt = f"{anchor_cmd} then relative-path failure"
                break
    if state_ids:
        modes.append(_mode("state_persistence_assumption", state_ids, state_excerpt))

    outline_edit = outline.step_to_first_edit is not None
    local_edit = any(
        _is_edit_call(view.tool_name, cmd)
        for view in tool_views
        for cmd in view.all_commands or (None,)
    )
    # 'Never edited a file' is vacuous for MCP-app tasks with no file
    # evidence, and for REPL harnesses whose files are scratch rather than
    # the repair target, so the mode is a shell-harness mode: it needs
    # file-task signals plus a shell (or unknown) tool protocol. Known
    # non-shell tools (Reef REPL ``execute``, MCP ``exec``/``wait``) opt out.
    tool_names = {(view.tool_name or "").lower() for view in tool_views}
    shell_harness = bool(tool_names & _SHELL_TOOLS) or not (tool_names <= _NON_SHELL_TOOLS)
    if (
        not outline_edit
        and not local_edit
        and outline.total_tool_calls > 0
        and _task_involves_files(agent_views)
        and shell_harness
    ):
        first_tool = outline.step_to_first_tool
        last_tool = tool_views[-1].step_id if tool_views else None
        excerpt = ""
        if tool_views:
            excerpt = tool_views[-1].command or tool_views[-1].tool_name or ""
        ids = [step for step in (first_tool, last_tool) if isinstance(step, int)]
        modes.append(
            _mode(
                "planning_no_edit",
                ids or [tool_views[0].step_id] if tool_views else [],
                excerpt or "tool calls ran but no file edit was recorded",
            )
        )

    # The outline's error taxonomy does not unwrap mini-swe-agent returncode
    # envelopes, so failed shell trials report zero outline errors; the
    # terminal-error state is recomputed here from envelope-aware views with
    # the outline's own last-was-error semantics.
    error_ids = [view.step_id for view in agent_views if view.is_error]
    last_was_error = False
    for view in agent_views:
        if view.is_error:
            last_was_error = True
        elif view.tool_name or view.message.strip():
            last_was_error = False
    if error_ids and last_was_error:
        last_error_view = next(view for view in reversed(agent_views) if view.is_error)
        excerpt = next(
            (out for out in last_error_view.outputs if out.strip()),
            last_error_view.command or "",
        )
        modes.append(
            _mode(
                "unrecovered_error",
                error_ids[-_MAX_EVIDENCE_STEPS:],
                excerpt or "run ends on an error with no recovery",
            )
        )

    if not modes:
        terminal_id = views[-1].step_id if views else 0
        terminal_text = ""
        if views:
            terminal_text = views[-1].message or " ".join(views[-1].outputs)
        modes.append(_mode("unclassified_failure", [terminal_id], terminal_text))
    return modes


def diagnose_trial(
    trial_dir: str | Path,
    *,
    repo_root: Path | None = None,
    explicit_runs_root: str | Path | None = None,
) -> TrialDiagnosis:
    """Diagnose one Harbor trial directory deterministically."""
    trial = Path(trial_dir)
    resolved = trial.resolve()
    jail_root = Path(explicit_runs_root).resolve() if explicit_runs_root else resolved
    outline: TrajectoryOutline | None = None
    try:
        outline = outline_trajectory(resolved, repo_root=repo_root, explicit_runs_root=jail_root)
    except (TrajectoryError, ValueError, OSError):
        outline = None

    if outline is None:
        name = resolved.name
        return TrialDiagnosis(
            trial_id=name,
            trial_name=name,
            task_name="unknown",
            agent_name="unknown",
            model_name="unknown",
            outcome="infra_failed",
            reward=None,
            exception_class=None,
            heuristic_label=None,
            notices=("trial target did not resolve to a readable trial directory",),
        )

    if outline.exception_class is not None:
        return TrialDiagnosis(
            trial_id=outline.trial_id,
            trial_name=outline.trial_name,
            task_name=outline.task_name,
            agent_name=outline.agent_name,
            model_name=outline.model_name,
            outcome="infra_failed",
            reward=outline.primary_reward,
            exception_class=str(outline.exception_class),
            heuristic_label=None,
            notices=("trial raised; never labeled as a task failure",),
        )
    if outline.primary_reward is None:
        return TrialDiagnosis(
            trial_id=outline.trial_id,
            trial_name=outline.trial_name,
            task_name=outline.task_name,
            agent_name=outline.agent_name,
            model_name=outline.model_name,
            outcome="unscored",
            reward=None,
            exception_class=None,
            heuristic_label=None,
            notices=("no numeric reward recorded; never labeled as a task failure",),
        )

    notices: list[str] = []
    heuristic_label: str | None = None
    modes: tuple[FailureMode, ...] = ()
    if outline.status != "featured":
        notices.append(f"no agent trajectory ({outline.unavailable_reason}); no modes proposed")
    elif outline.primary_reward >= PASS_THRESHOLD:
        heuristic_label = propose_heuristic_label(outline).label
        notices.append("scored pass; no failure modes proposed")
    else:
        heuristic_label = propose_heuristic_label(outline).label
        views = _step_views(_raw_steps(resolved))
        if not views:
            notices.append("trajectory steps unreadable; no modes proposed")
        else:
            detected = _detect(outline, views)
            order = {name: index for index, name in enumerate(MODE_ORDER)}
            modes = tuple(sorted(detected, key=lambda item: order.get(item.mode, 99)))
    return TrialDiagnosis(
        trial_id=outline.trial_id,
        trial_name=outline.trial_name,
        task_name=outline.task_name,
        agent_name=outline.agent_name,
        model_name=outline.model_name,
        outcome="scored",
        reward=outline.primary_reward,
        exception_class=None,
        heuristic_label=heuristic_label,
        modes=modes,
        notices=tuple(notices),
    )


def diagnose_job(job_dir: str | Path) -> list[TrialDiagnosis]:
    """Diagnose every trial directory directly under a job directory, sorted."""
    job = Path(job_dir)
    if not job.is_dir():
        raise TraceError(f"not a job directory: {job}")
    trials = sorted(
        (child for child in job.iterdir() if is_trial_dir(child)),
        key=lambda item: item.name,
    )
    return [diagnose_trial(trial, explicit_runs_root=job.resolve()) for trial in trials]


def render_diagnosis_text(diagnosis: TrialDiagnosis, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Render bounded plain text for Reef's report ``feedback`` (string form).

    Signature stability: HAR-74 / ReefTraffic attaches this rendering to Reef
    reports, so keep ``(diagnosis, max_chars) -> str`` and the ``len(text) <=
    max_chars`` guarantee. ``TrialDiagnosis.to_dict()`` is the matching
    object form (JSON-serializable, same content).
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    reward = (
        f"{diagnosis.reward:.4f}" if isinstance(diagnosis.reward, float) else str(diagnosis.reward)
    )
    lines = [
        f"Trial {diagnosis.trial_name} ({diagnosis.task_name}): "
        f"outcome={diagnosis.outcome} reward={reward}"
    ]
    if diagnosis.exception_class:
        lines.append(f"Infra: {sanitize_excerpt(diagnosis.exception_class, 120)}")
    if diagnosis.modes:
        lines.append(f"Failure modes: {', '.join(mode.mode for mode in diagnosis.modes)}")
        for mode in diagnosis.modes:
            steps = ",".join(str(step) for step in mode.step_ids)
            lines.append(f'- {mode.mode} (steps {steps}): "{mode.excerpt}"')
            lines.append(f"  Harness fix: {mode.suggestion}")
    else:
        lines.append("Failure modes: none")
    for notice in diagnosis.notices:
        lines.append(f"Note: {sanitize_excerpt(notice, 200)}")
    text = "\n".join(lines).strip() + "\n"
    if len(text) <= max_chars:
        return text
    if max_chars <= len(TRUNCATION_MARKER) + 1:
        return text[:max_chars]
    return text[: max_chars - len(TRUNCATION_MARKER) - 1].rstrip() + "\n" + TRUNCATION_MARKER


def main(argv: list[str] | None = None) -> int:
    """CLI: ``python -m evallab.trial_diagnosis <trial_or_job_dir> [--json]``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path, help="Harbor trial or job directory")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of bounded text")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_CHARS,
        help="Upper bound on each text rendering",
    )
    args = parser.parse_args(argv)
    try:
        if args.max_chars <= 0:
            raise TraceError("max_chars must be a positive integer")
        if is_trial_dir(args.path) and not is_job_dir(args.path):
            diagnoses = [diagnose_trial(args.path)]
        else:
            diagnoses = diagnose_job(args.path)
        if args.json:
            output = json.dumps([item.to_dict() for item in diagnoses], indent=2) + "\n"
        else:
            output = "".join(
                render_diagnosis_text(item, args.max_chars) + "\n" for item in diagnoses
            )
    except (TraceError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(output, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
