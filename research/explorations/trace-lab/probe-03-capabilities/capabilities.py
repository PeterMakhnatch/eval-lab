"""HAR-91 capability tagging for Harbor trials (Trace Lab probe-03).

Usage::

    uv run --no-project python capabilities.py <job_dir>... --out-dir <dir> \\
        [--evallab-src PATH] [--treatment-key-source auto|har93:<path>]

``<job_dir>`` is a trial dir, a Harbor job dir, or a folder of jobs (trial
discovery and reward/exception reading are reused from probe-02, never
forked). Reads only; all rows land in ``--out-dir``:

- ``capabilities.jsonl`` -- one row per trial (four separate dimensions +
  evidence step refs + reconstructed-call summary),
- ``summary.md`` -- one-page summary,
- ``reading_sheet.md`` -- every trace (up to ~20) with probe-02's label
  columns plus the proposed tag / attribution / evidence steps.

$0: standard library only, no model calls, no launches, no uploads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE02 = HERE.parent / "probe-02-mimo-kit"
sys.path.insert(0, str(PROBE02))
import metrics as probe02  # noqa: E402  (reuse trial discovery/label format)

NOP_RUNS_DIR: str | None = None

# ---------------------------------------------------------------------------
# Observation -> harness_accepted rules (ids documented in README.md).
# ---------------------------------------------------------------------------

TERM_MARKERS = ("New Terminal Output:", "Current Terminal Screen:")


def harness_accepted(obs_content: str | None) -> tuple[str, str]:
    """Return (value, rule_id) with value in true/false/unknown.

    H-ACC-TERM-TRUE: the step's recorded observation carries terminal
      output, so the harness did NOT reject the turn. ``true`` means "not
      rejected"; keystrokes were sent only if the turn carried calls (a
      call-less prose turn with terminal echo is usually stale drain).
    H-ACC-PARSEERR-FALSE: a parse/validation error notice with no terminal
      echo, so the harness rejected the turn before sending anything.
    H-ACC-UNKNOWN: anything else (no observation, other harness versions).
    """
    if not obs_content:
        return "unknown", "H-ACC-UNKNOWN"
    if any(marker in obs_content for marker in TERM_MARKERS):
        return "true", "H-ACC-TERM-TRUE"
    if obs_content.startswith("Previous response had parsing errors"):
        return "false", "H-ACC-PARSEERR-FALSE"
    head = obs_content[:200]
    if "ERROR:" in head or "Missing required fields" in head:
        return "false", "H-ACC-PARSEERR-FALSE"
    return "unknown", "H-ACC-UNKNOWN"


# ---------------------------------------------------------------------------
# Message shape classification (ids documented in README.md).
# ---------------------------------------------------------------------------

_FUNCTION_RE = re.compile(r"<function=([^>\s]+)>")


def strict_terminus_ok(message: str) -> bool:
    try:
        value = json.loads(message.strip())
    except (ValueError, AttributeError):
        return False
    return isinstance(value, dict) and "commands" in value


def normalizer_command_count(normalized: str | None) -> int:
    if not normalized:
        return 0
    try:
        value = json.loads(normalized)
    except ValueError:
        return 0
    commands = value.get("commands") if isinstance(value, dict) else None
    return len(commands) if isinstance(commands, list) else 0


def classify_shape(message: str, n_calls: int) -> tuple[str, str]:
    """Return (shape, rule_id); shape in terminus_json/native_xml/prose/unparseable.

    SHAPE-XML: carries a native ``<function=...>`` opener and the normalizer
      recovers >=1 call. SHAPE-XML-BROKEN: same surface but nothing
      recovers -> unparseable (e.g. unknown functions like ``keystrokes`` /
      ``write``, or extra params like ``description``). SHAPE-JSON:
      brace-led Terminus object that strict-parses or normalizes
      (incl. the wrapper-tail shape). SHAPE-PROSE: natural language.
    """
    message = message or ""
    has_opener = "<function=" in message
    starts_brace = message.strip().startswith("{")
    if has_opener:
        if n_calls > 0:
            return "native_xml", "SHAPE-XML"
        return "unparseable", "SHAPE-XML-BROKEN"
    if starts_brace:
        if strict_terminus_ok(message) or n_calls > 0:
            return "terminus_json", "SHAPE-JSON"
        return "unparseable", "SHAPE-JSON-BROKEN"
    return "prose", "SHAPE-PROSE"


# ---------------------------------------------------------------------------
# Normalizer loading (import from --evallab-src, else documented fallback).
# ---------------------------------------------------------------------------

FALLBACK_PARAM_RE = re.compile(
    r"<parameter=(?:command|keystrokes)>(.*?)</parameter>", re.DOTALL
)


def fallback_normalize(message: str) -> str | None:
    """Documented explicit rules (FB-XML-PARAM, FB-DEFER) when the Eval Lab
    normalizer cannot be imported. Returns a Terminus JSON string or None."""
    if "<function=" not in message:
        return None
    if "commands" in message:
        return None  # FB-DEFER: wrapped whole objects need the real normalizer
    keystrokes = [match.group(1) for match in FALLBACK_PARAM_RE.finditer(message)]
    if not keystrokes:
        return None
    return json.dumps(
        {"analysis": "", "plan": "", "commands": [{"keystrokes": ks} for ks in keystrokes]}
    )


def load_normalizer(evallab_src: str | None) -> tuple[dict, object | None]:
    """Return (provenance, normalize_fn). provenance always records how the
    reconstruction was done (import path or explicit fallback)."""
    provenance: dict = {
        "evallab_src": evallab_src,
        "import_ok": False,
        "evallab_commit": None,
        "normalizer_sha256": None,
        "mode": "fallback-explicit-rules",
    }
    if evallab_src and Path(evallab_src).is_dir():
        sys.path.insert(0, evallab_src)
        try:
            import evallab.mimo_tool_calls as module  # noqa: E402
            from evallab.mimo_tool_calls import (  # noqa: E402
                normalize_mimo_tool_calls as normalize_fn,
            )

            provenance["import_ok"] = True
            provenance["mode"] = "evallab-normalizer"
            provenance["normalizer_sha256"] = hashlib.sha256(
                Path(str(module.__file__)).read_bytes()
            ).hexdigest()
            provenance["evallab_commit"] = git_commit(
                str(Path(evallab_src).parent)
            ) or git_commit(str(Path(evallab_src)))
            return provenance, normalize_fn
        except Exception as exc:  # noqa: BLE001 -- recorded, never raised
            provenance["import_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            if sys.path and sys.path[0] == evallab_src:
                sys.path.pop(0)
    else:
        provenance["import_error"] = "evallab-src missing or not a directory"
    return provenance, fallback_normalize


def git_commit(path: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", path, "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = out.stdout.strip()
    return commit if out.returncode == 0 and commit else None


# ---------------------------------------------------------------------------
# Trajectory assembly: head + cont-N in order with leading-prefix drop.
# Parent-measured semantics (README ASM-*): drop any leading run of a cont
# file identical (source+message) to the already-assembled sequence; never
# concatenate blindly, never sum tokens.
# ---------------------------------------------------------------------------

CONT_RE = re.compile(r"trajectory\.cont-(\d+)\.json$")


def same_step(left: dict, right: dict) -> bool:
    return left.get("source") == right.get("source") and str(
        left.get("message", "")
    ) == str(right.get("message", ""))


def assemble_trial(trial_dir: Path) -> tuple[dict, list[tuple[str, dict]]]:
    """Return (coverage, assembled [(docname, step), ...]).

    Patterns recorded per trial: duplicate | cumulative_superset |
    new_session | head_missing. Token totals come from the LAST document's
    final_metrics (cumulative); documents are never added together.
    """
    agent_dir = trial_dir / "agent"
    docs: list[tuple[str, dict | None, str | None]] = []  # (docname, doc, session)
    head_path = agent_dir / "trajectory.json"
    if head_path.is_file():
        try:
            doc = json.loads(head_path.read_text(encoding="utf-8"))
            docs.append(("head", doc, doc.get("session_id") if isinstance(doc, dict) else None))
        except (OSError, ValueError):
            docs.append(("head", None, None))
    cont_paths: list[tuple[int, Path]] = []
    for path in agent_dir.glob("trajectory.cont-*.json"):
        match = CONT_RE.search(path.name)
        if match:
            cont_paths.append((int(match.group(1)), path))
    for _number, path in sorted(cont_paths):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            docs.append((path.name, doc, doc.get("session_id") if isinstance(doc, dict) else None))
        except (OSError, ValueError):
            docs.append((path.name, None, None))
    summarization_files = sorted(
        path.name for path in agent_dir.glob("trajectory.summarization-*.json")
    )

    assembled: list[tuple[str, dict]] = []
    per_doc = []
    for docname, doc, session in docs:
        raw_steps = doc.get("steps") if isinstance(doc, dict) else None
        steps = [s for s in raw_steps if isinstance(s, dict)] if isinstance(raw_steps, list) else []
        dropped = 0
        while (
            dropped < len(steps)
            and dropped < len(assembled)
            and same_step(steps[dropped], assembled[dropped][1])
        ):
            dropped += 1
        for step in steps[dropped:]:
            assembled.append((docname, step))
        per_doc.append(
            {
                "doc": docname,
                "session_id": session,
                "steps": len(steps),
                "dropped_prefix": dropped,
                "added": len(steps) - dropped,
                "final_metrics": doc.get("final_metrics") if isinstance(doc, dict) else None,
            }
        )
    sessions = {entry["session_id"] for entry in per_doc if entry["session_id"]}
    head_present = any(entry["doc"] == "head" and entry["steps"] > 0 for entry in per_doc)
    cont_entries = [entry for entry in per_doc if entry["doc"] != "head"]
    agent_added = sum(
        1 for _doc, step in assembled
        if str(step.get("source", "")).lower() in probe02.AGENT_SOURCES
    )
    if not head_present and cont_entries:
        pattern: str = "head_missing"
    elif cont_entries and all(entry["added"] == 0 for entry in cont_entries):
        pattern = "duplicate"
    elif len(sessions) > 1:
        pattern = "new_session"
    elif cont_entries:
        pattern = "cumulative_superset"
    else:
        pattern = "single_head"
    notes: list[str] = []
    if pattern == "head_missing" and cont_entries:
        # ASM-HEADLESS-CUM: a lone cont that starts at the system prompt and
        # spans n_episodes is a cumulative document covering all episodes
        # (0758-b); the head FILE is missing, the episodes are not.
        widest = max(cont_entries, key=lambda entry: entry["steps"])
        starts_system = False
        first_step = next(
            (s for s in (assembled[:1] or [])), None
        )
        if first_step and str(first_step[1].get("source", "")).lower() in ("user", "system"):
            starts_system = True
        notes.append(
            f"head file missing; episodes covered {agent_added} via cumulative "
            f"{widest['doc']}"
            + (" (starts at system prompt)" if starts_system else "")
        )
    last_metrics = None
    for entry in reversed(per_doc):
        if isinstance(entry.get("final_metrics"), dict):
            last_metrics = entry["final_metrics"]
            break
    coverage = {
        "head_present": head_path.is_file(),
        "cont_files": [number for number, _path in sorted(cont_paths)],
        "summarization_files": summarization_files,
        "assembly_pattern": pattern,
        "per_doc": per_doc,
        "assembled_steps": len(assembled),
        "assembled_agent_steps": agent_added,
        "last_doc_final_metrics": last_metrics,
        "notes": notes,
    }
    return coverage, assembled


def obs_content(step: dict) -> str | None:
    observation = step.get("observation")
    if not isinstance(observation, dict):
        return None
    results = observation.get("results")
    if not isinstance(results, list) or not results:
        return None
    parts = [
        result.get("content") for result in results
        if isinstance(result, dict) and isinstance(result.get("content"), str)
    ]
    return "\n".join(parts) if parts else None


def step_ref(docname: str, step: dict) -> str:
    return f"{docname}#{step.get('step_id')}"


# ---------------------------------------------------------------------------
# Failure rules (every id documented in README.md).
# ---------------------------------------------------------------------------

LOOP_MIN_RUN = 10
COMPLETION_CLAIM_RE = re.compile(r"the (task|fix) is (verified and |confirmed )?complete", re.IGNORECASE)


def _stripped(step: dict) -> str:
    return str(step.get("message", "")).strip()


def identical_runs(agent_seq: list[tuple[str, dict]]) -> list[dict]:
    """Consecutive assembled agent steps with byte-identical stripped
    messages. Length counts steps, never step_id arithmetic, so session
    restarts cannot misfire."""
    runs: list[dict] = []
    index = 0
    total = len(agent_seq)
    while index < total:
        doc, step = agent_seq[index]
        text = _stripped(step)
        length = 1
        while (
            index + length < total
            and _stripped(agent_seq[index + length][1]) == text
        ):
            length += 1
        if length >= 10:
            runs.append(
                {
                    "start_doc": doc,
                    "start": step.get("step_id"),
                    "end_doc": agent_seq[index + length - 1][0],
                    "end": agent_seq[index + length - 1][1].get("step_id"),
                    "length": length,
                    "message": text,
                }
            )
        index += length
    return runs


ECHO_TASK_COMPLETE_RE = re.compile(r"""echo\s+['"]?task_complete\b""")


def completion_handshake(model_seq, stop_reason: str) -> dict | None:
    """HANDSHAKE: the harness asks 'Are you sure you want to mark the task
    as complete? ... include "task_complete": true in your JSON response
    again.' A MiMo native turn confirms only as a turn with NO tool call
    (prose_completion); claim + tool call executes the call and the episode
    continues. Returns None when the prompt never appears, else the first
    prompt ref, prompt count, whether the trial ended confirmed, the turns
    and per-step prompt tokens after the first prompt, and how many turns
    ran `echo task_complete` (the model trying to follow the JSON wording
    with a shell command)."""
    first = None
    prompts = 0
    for index, (_doc, step) in enumerate(model_seq):
        if CONFIRM_PROMPT_RE.search(str(obs_content(step) or "")):
            prompts += 1
            if first is None:
                first = index
    if first is None:
        return None
    after = model_seq[first + 1:]
    prompt_after = 0
    unmetered = 0
    for _doc, step in after:
        prompt, _completion = _step_tokens(step)
        if prompt is None:
            unmetered += 1
        else:
            prompt_after += prompt
    return {
        "first_prompt_ref": _ref(model_seq[first][0], model_seq[first][1].get("step_id")),
        "prompts": prompts,
        "confirmed": stop_reason == "task_complete_confirmed",
        "turns_after_first_prompt": len(after),
        "prompt_tokens_after_first_prompt": prompt_after,
        "steps_without_metrics": unmetered,
        "echo_task_complete_turns": sum(
            1 for _doc, step in model_seq
            if ECHO_TASK_COMPLETE_RE.search(str(step.get("message") or ""))
        ),
    }


def _step_tokens(step: dict) -> tuple[int | None, int | None]:
    metrics = step.get("metrics") or {}
    if not isinstance(metrics, dict):
        return None, None
    prompt, completion = metrics.get("prompt_tokens"), metrics.get("completion_tokens")
    return (
        int(prompt) if isinstance(prompt, (int, float)) else None,
        int(completion) if isinstance(completion, (int, float)) else None,
    )


# ---------------------------------------------------------------------------
# WEDGE (HAR-99): wedged terminal -- the model keeps typing commands while
# the shell is not at a prompt (pager, `>` continuation, a program reading
# stdin, a still-running foreground command), so they never run as shell
# commands. Terminal state is read from the LAST non-empty line of each
# executed turn's observation (the screen after the turn).
# ---------------------------------------------------------------------------

WEDGE_MIN_TURNS = 3
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Za-z0-9]|\r")
# user@host:path# / $ at the end of the line (arvo `agent@<uuid>:~/src/x$`,
# terminal/code `root@<uuid>:/app#`); unanchored so output without a final
# newline followed by the prompt (`.PHONY: test cleanroot@...#`) counts.
SHELL_PROMPT_RE = re.compile(r"(?:\S*@[\w.-]+:[^\n]*?[#$]|\b(?:ba|z)?sh-[\d.]+[#$])\s*$")
PAGER_LINE_RE = re.compile(
    r"^:\s*$|\(END\)|--More--|Pattern not found\s+\(press RETURN\)|"
    r"Press RETURN for more|^lines \d+-\d+"
)
CONTINUATION_LINE_RE = re.compile(r"^>(?:\s.*)?$")
# `? Question (default)`: inquirer-style CLI questionnaires (npm/dredd init).
INTERACTIVE_LINE_RE = re.compile(
    r"^(?:>>>|\.\.\.)(?:\s|$)|^\(Pdb\)|\[y/N\]|\[Y/n\]|password[^\n]*:\s*$|^\? \S",
    re.IGNORECASE,
)
INTERRUPT_KEY_RE = re.compile(r"^(?:C-[cdz\\]|\^C|q|Q|Escape|:q!?)$")
WEDGED_STATES = frozenset({"pager", "continuation", "interactive", "no_prompt"})


def terminal_state(observation: str, keystrokes: list[str]) -> tuple[str, str]:
    """(state, last line) of the terminal after a turn: `prompt`, `pager`,
    `continuation`, `interactive`, `unsubmitted` (the turn typed a command
    without Enter, so the shell sits at a prompt with a pending line: the
    R-TOOL-00 case, not a wedge), `no_prompt` (a submitted command has not
    given the prompt back: a program reading stdin or still running), or
    `unknown` (no screen). The harness's confirm question is cut first."""
    text = ANSI_RE.sub("", observation or "")
    confirm = text.find("Are you sure you want to mark the task as complete")
    if confirm >= 0:
        text = text[:confirm]
    lines = [line.rstrip() for line in text.split("\n") if line.strip()]
    if not lines:
        return "unknown", ""
    last = lines[-1]
    if SHELL_PROMPT_RE.search(last):
        return "prompt", last
    if PAGER_LINE_RE.search(last):
        return "pager", last
    if CONTINUATION_LINE_RE.match(last):
        return "continuation", last
    if INTERACTIVE_LINE_RE.search(last):
        return "interactive", last
    submitted = any(
        key.endswith("\n") or key.strip() in ("Enter", "C-m") or INTERRUPT_KEY_RE.match(key.strip())
        for key in keystrokes
    )
    if not submitted:
        return "unsubmitted", last
    return "no_prompt", last


def _landed_at_prompt(observation: str, keys: list[str]) -> bool:
    """The screen shows a shell prompt immediately followed by this turn's
    own first command, so the keystrokes reached a prompt: a slow command
    from the previous turn had finished (a4-1789 head#19-24 pip installs),
    whatever the previous screen's last line said. Line wraps are removed
    before matching."""
    first = next((key.strip().split("\n")[0] for key in keys if key.strip()), "")
    probe = first[:24]
    if len(probe) < 4:
        return False
    flat = ANSI_RE.sub("", observation or "").replace("\n", "")
    return re.search(r"@[\w.-]+:[^#$\s]*[#$] ?" + re.escape(probe), flat) is not None


def wedged_terminal(model_seq, info) -> dict:
    """WEDGE: stretches of executed turns whose keystrokes landed while the
    terminal was not at a prompt: the previous executed turn left a
    non-prompt screen AND this turn's command does not appear right after a
    shell prompt (`_landed_at_prompt`). A stretch opens on the first such
    turn (the turn before it is the trigger) and closes on the turn after
    which the prompt is back, or when a turn lands at a prompt
    (`prompt_returned`), or at run end (`until_run_end`). Rejected turns send
    nothing and neither open nor close a stretch. Reported when a stretch has
    >= WEDGE_MIN_TURNS turns; `interrupt_ref` is the first turn in it that
    sent an interrupt key (C-c, C-d, q, Escape, :q). HAR-81 uses the
    harness-recorded executed keystrokes; HAR-90 uses normalizer replay
    (what the model proposed), flagged in `keystroke_source`."""
    executed = []
    source = "none"
    for index, (doc, step) in enumerate(model_seq):
        cell = info.get((doc, step.get("step_id"))) or {}
        keys = [key for key in (cell.get("keystrokes") or []) if key]
        if cell.get("harness_accepted") != "true" or not keys:
            continue
        source = cell.get("keystroke_source") or source
        observation = _step_observation_text(doc, step, info)
        state, last = terminal_state(observation, keys)
        if state == "unknown":
            # No screen: the state the next turn lands in is unobserved,
            # so the turn neither opens nor closes a stretch.
            continue
        executed.append({
            "index": index,
            "ref": _ref(doc, step.get("step_id")),
            "keys": keys,
            "state": state,
            "last": last,
            "at_prompt": _landed_at_prompt(observation, keys),
        })
    stretches: list[dict] = []
    short = 0
    current = None
    previous = None
    for turn in executed:
        landed_wedged = (
            previous is not None
            and previous["state"] in WEDGED_STATES
            and not turn["at_prompt"]
        )
        if current is not None and not landed_wedged:
            # The prompt was back before this turn: it landed at a prompt.
            current["prompt_returned"] = True
            current["until_run_end"] = False
            stretches.append(current)
            current = None
        if landed_wedged:
            if current is None:
                current = {
                    "trigger": previous,
                    "start": turn,
                    "end": turn,
                    "turns": 0,
                    "interrupt": None,
                }
            current["turns"] += 1
            current["end"] = turn
            if current["interrupt"] is None and any(
                INTERRUPT_KEY_RE.match(key.strip()) or "\x03" in key for key in turn["keys"]
            ):
                current["interrupt"] = turn
        if current is not None and turn["state"] not in WEDGED_STATES:
            current["prompt_returned"] = turn["state"] == "prompt"
            current["until_run_end"] = False
            stretches.append(current)
            current = None
        previous = turn
    if current is not None:
        current["prompt_returned"] = False
        current["until_run_end"] = True
        stretches.append(current)
    reported = []
    for stretch in stretches:
        if stretch["turns"] < WEDGE_MIN_TURNS:
            short += 1
            continue
        prompt_sum = unmetered = 0
        for _doc, step in model_seq[stretch["start"]["index"]:stretch["end"]["index"] + 1]:
            prompt, _completion = _step_tokens(step)
            if prompt is None:
                unmetered += 1
            else:
                prompt_sum += prompt
        trigger = stretch["trigger"]
        interrupt = stretch["interrupt"]
        reported.append({
            "trigger_ref": trigger["ref"],
            "trigger_command": " | ".join(key.strip() for key in trigger["keys"])[:160],
            "cause": trigger["state"],
            "screen": trigger["last"][:120],
            "start_ref": stretch["start"]["ref"],
            "end_ref": stretch["end"]["ref"],
            "turns": stretch["turns"],
            "interrupt_ref": interrupt["ref"] if interrupt else None,
            "interrupt_keys": (
                next(key.strip() for key in interrupt["keys"]
                     if INTERRUPT_KEY_RE.match(key.strip()) or "\x03" in key)
                if interrupt else None
            ),
            "prompt_returned": stretch["prompt_returned"],
            "until_run_end": stretch["until_run_end"],
            "prompt_tokens": prompt_sum,
            "steps_without_metrics": unmetered,
        })
    return {
        "keystroke_source": source,
        "executed_turns": len(executed),
        "states": {
            state: sum(1 for turn in executed if turn["state"] == state)
            for state in sorted({turn["state"] for turn in executed})
        },
        "stretches": reported,
        "short_stretches": short,
    }


def _replay_keystrokes(normalized: str | None, message: str) -> list[str]:
    """Keystrokes the model proposed (HAR-90: no harness record): the
    normalizer's Terminus commands, else a strict Terminus JSON message."""
    for text in (normalized, message):
        if not text:
            continue
        try:
            value = json.loads(text.strip())
        except ValueError:
            continue
        commands = value.get("commands") if isinstance(value, dict) else None
        if isinstance(commands, list):
            return [
                str(command.get("keystrokes") or "")
                for command in commands if isinstance(command, dict)
            ]
    return []


def loop_token_cost(model_seq, runs, confirmation_loop, tokens_result) -> dict:
    """LOOP-COST: prompt/completion tokens the model spent inside loops,
    from per-step `metrics`. Loops = every identical run (>=10 byte-identical
    consecutive messages) plus the R-COMP-04 confirmation span; steps are
    counted once when spans overlap. Shares divide by the per-step sum over
    ALL model steps (same source, so shares are internally consistent);
    result.json `n_input_tokens` rides along because the proxy total also
    counts summarization calls the trajectory does not attribute. Steps
    without metrics (the summarization hand-off question turn, cont-N#3)
    are counted in `steps_without_metrics`, never as 0 tokens; a sum with
    no metered step at all is `null`."""
    position = {(doc, step.get("step_id")): index for index, (doc, step) in enumerate(model_seq)}

    def span(start_ref, end_ref):
        start, end = position.get(start_ref), position.get(end_ref)
        if start is None or end is None or end < start:
            return set()
        return set(range(start, end + 1))

    identical: set[int] = set()
    spans = []
    for run in runs:
        steps = span((run["start_doc"], run["start"]), (run["end_doc"], run["end"]))
        identical |= steps
        spans.append({"kind": "identical", "length": run["length"],
                      "span": [_ref(run["start_doc"], run["start"]), _ref(run["end_doc"], run["end"])]})
    confirmation: set[int] = set()
    if confirmation_loop and confirmation_loop.get("loop_span"):
        refs = []
        for ref in confirmation_loop["loop_span"]:
            doc, _, sid = ref.rpartition("#")
            refs.append((doc, int(sid) if sid.isdigit() else sid))
        confirmation = span(refs[0], refs[-1])
        spans.append({"kind": "confirmation", "length": len(confirmation),
                      "span": list(confirmation_loop["loop_span"])})

    def total(indices):
        prompt_sum = completion_sum = metered = unmetered = 0
        for index in indices:
            prompt, completion = _step_tokens(model_seq[index][1])
            if prompt is None or completion is None:
                unmetered += 1
                continue
            metered += 1
            prompt_sum += prompt
            completion_sum += completion
        if not metered:
            return None
        return {"prompt": prompt_sum, "completion": completion_sum,
                "steps": metered, "steps_without_metrics": unmetered}

    all_steps = total(range(len(model_seq)))
    in_loops = total(identical | confirmation)
    share = (
        round(in_loops["prompt"] / all_steps["prompt"], 4)
        if in_loops and all_steps and all_steps["prompt"]
        else None
    )
    return {
        "spans": spans,
        "identical": total(identical),
        "confirmation": total(confirmation),
        "in_loops": in_loops,
        "all_steps": all_steps,
        "loop_prompt_share": share,
        "result_input_tokens": tokens_result.get("input"),
    }


MIMO_EXEC_FUNCTIONS = frozenset({"exec", "exec_command", "bash"})
NATIVE_SIGNATURE_RE = re.compile(
    r"<function=(?:exec|exec_command|bash)><parameter=(?:command|keystrokes)>"
)
_PARAM_NAME_RE = re.compile(r"<parameter=([^>\s]+)>")
_ALLOWED_PARAMS = frozenset({"command", "keystrokes", "duration"})
HARNESS_STANDIN = "Technical difficulties. Please continue with the task."
CONFIRM_PROMPT_RE = re.compile(r"are you sure", re.IGNORECASE)
SETUP_ERROR_RE = re.compile(r"ERROR at setup of (\S+)")
FILE_CHANGE_RES = (
    re.compile(r"(?<![\w-])sed\s+[^|;\n]*-i\b"),
    # Shell redirect: exclude `->` arrows and fd dups (`2>`, `&>`).
    re.compile(r"(?<![->\w&])>\s*\S"),
    re.compile(r"(?<![\w-])patch\b"),
    re.compile(r"python3?\s+-\s*<<"),
    re.compile(r"(?<![->\w&])cat\s+>\s*\S"),
)
# File targets named by a write op: redirect path-likes and sed -i files.
REDIRECT_TARGET_RE = re.compile(
    r"(?<![->\w&])>\s*([~/][^\s|;&]+|[A-Za-z0-9_][\w.~-]*\.[A-Za-z0-9]{1,6})"
)
SED_TARGET_RE = re.compile(
    r"sed\s+(?:-[^\s|;&]+\s+)*(?:'[^']*'\s+|\"[^\"]*\"\s+)?([^\s|;&'\"]+)"
)
PACKAGING_PATH_RE = re.compile(
    r"setup\.(cfg|py)$|\.egg-info|requirements.*\.txt$|pyproject\.toml$|/tmp/",
    re.IGNORECASE,
)
ENV_WRESTLE_RE = re.compile(
    r"pip3?\s+install|setup\.cfg|setup\.py|\bpbr\b|stevedore|No module named",
    re.IGNORECASE,
)
GUARD_REJECT_RE = re.compile(r"anti_hack_guard:\s*REJECT\s*(\S+)")
IMPORT_ERROR_RE = re.compile(
    r"ModuleNotFoundError|No module named|ImportError|cannot import name"
)
PIP_INSTALL_RE = re.compile(
    r"pip3?\s+install\s+((?:-[^\s]+\s+)*)([A-Za-z0-9_.\-\[\]]+)"
)
# "ModuleNotFoundError: No module named 'x'" must capture x, not "No": the
# bare-exception branch only applies when "No module named" does not follow.
MISSING_MODULE_RE = re.compile(
    r"No module named ['\"]?([\w.]+)|ModuleNotFoundError:\s*(?!No module named)([\w.']+)"
)


def layer_status(step: dict) -> dict | None:
    """Recorded per-step harness verdict (HAR-81): the ``extra.step_layers``
    mapping the harness wrote at run time. Returns None when the trial
    predates recorded layers (all HAR-90 trials). When present, the recorded
    acceptance is authoritative; the observation-inferred H-ACC value is kept
    only for the agreement cross-check (recorded-vs-inferred confusion)."""
    layers = (step.get("extra") or {}) if isinstance(step.get("extra"), dict) else {}
    layers = layers.get("step_layers")
    if not isinstance(layers, dict):
        return None
    accepted = layers.get("accepted")
    if not isinstance(accepted, dict):
        return None
    # executed/observed live at the top level of step_layers, NOT inside
    # accepted; keystrokes_sent is a list of command lines.
    executed = layers.get("executed")
    keystrokes: list[str] = []
    if isinstance(executed, dict):
        sent = executed.get("keystrokes_sent")
        if isinstance(sent, list):
            keystrokes = [str(line) for line in sent]
        elif isinstance(sent, str) and sent:
            keystrokes = [sent]
    observed = layers.get("observed")
    output = ""
    if isinstance(observed, dict):
        output = str(observed.get("output") or "")
    return {
        "kind": accepted.get("kind"),
        "task_complete": accepted.get("task_complete"),
        "parse_error": accepted.get("parse_error"),
        "calls": accepted.get("calls") if isinstance(accepted.get("calls"), list) else [],
        "keystrokes_sent": keystrokes,
        "executed_keystrokes": "\n".join(keystrokes),
        "observed_output": output,
    }


def recorded_acceptance(layer: dict | None) -> str | None:
    """'true' for executed call turns and accepted prose completions,
    'false' for recorded parse errors, None when the kind is unrecognized
    (caller falls back to observation inference)."""
    if not layer:
        return None
    kind = layer.get("kind")
    if kind in ("calls", "prose_completion"):
        return "true"
    if kind == "parse_error":
        return "false"
    return None


KNOWN_NATIVE_FUNCTIONS = frozenset({"exec", "exec_command", "bash", "task_complete"})


def malformed_native_fn(message: str) -> str | None:
    """A KNOWN function whose parameter block doesn't parse: a closing
    ``</parameter>`` with no ``<parameter=...>`` opener, or no parameter
    markup at all (#526 maps ``<function=task_complete>``, so it is known,
    not unknown). Returns the function name or None."""
    match = _FUNCTION_RE.search(message)
    if not match or match.group(1) not in KNOWN_NATIVE_FUNCTIONS:
        return None
    if "<parameter=" not in message:
        return match.group(1)
    return None


def feedback_names_problem(feedback: str | None, message: str) -> bool | None:
    """Whether the harness feedback names the problem: True iff it mentions
    a function/parameter name from the message. None when no feedback was
    recorded. ('No valid JSON found in response' names nothing -> False.)"""
    if not feedback:
        return None
    names = set(_FUNCTION_RE.findall(message)) | set(_PARAM_NAME_RE.findall(message))
    if "task_complete" in message:
        names.add("task_complete")
    lowered = feedback.lower()
    return any(name.lower() in lowered for name in names if name)

def recorded_call_count(layer: dict | None) -> int:
    """Tool calls the harness recorded as proposed: accepted.calls entries
    carrying keystrokes (a bare ``task_complete`` marker is a completion
    signal, not a tool call)."""
    if not layer:
        return 0
    count = 0
    for call in layer.get("calls") or []:
        if isinstance(call, dict) and (
            "keystrokes" in call or "command" in call or "duration" in call
        ):
            count += 1
    return count


def _attach_feedback(failure: dict, cell: dict) -> None:
    """Append the recorded harness feedback to a failure note, with whether
    it names the problem. No-op when no step_layers were recorded."""
    feedback = cell.get("harness_feedback")
    if feedback:
        names = cell.get("feedback_names_problem")
        failure["note"] += (
            f"; harness feedback: '{feedback[:160]}' "
            f"(names the problem: {str(bool(names)).lower()})"
        )
        failure["harness_feedback"] = feedback
        failure["feedback_names_problem"] = bool(names)



BARE_COMMAND_RE = re.compile(
    r"<tool_call><function=command>([^<>]+)</function></tool_call>"
)


def _bare_plain_command(message: str) -> bool:
    """A bare `<function=command>command</function>` call: the exec
    operation's own name in function position, well-formed tags, no
    parameter block, single-line command bodies, no prose mixing. The
    parser maps only {exec_command, bash, exec}, so the unambiguous call
    is rejected as unmapped (2684 head#2, first contact). Promoted
    parameter names with script bodies (keystrokes+heredoc, 0036-f) and
    wrong-tool schemas (write+file_path) stay model drift."""
    stripped = message.strip()
    if "<parameter=" in stripped or "<<" in stripped:
        return False
    matches = BARE_COMMAND_RE.findall(stripped)
    if not matches:
        return False
    if not all(match.strip() and "\n" not in match for match in matches):
        return False
    remainder = BARE_COMMAND_RE.sub("", stripped).strip()
    return not remainder


def classify_rejection_cause(message: str, n_calls: int) -> str:
    """Parent-verified deterministic classifier (order matters):
    1. today's normalizer accepts -> today_normalizer_accepts
    2. no <function= -> json_invalid if '{'-led else prose_no_call
    3. known function with unparseable parameter block -> malformed_native:<fn>
    4. bare <function=command>plain command</function> -> unmapped_native_function
    5. function not in MIMO_EXEC_FUNCTIONS -> unknown_function:<name>
    6. <parameter=X> outside {command,keystrokes,duration} -> extra_param:<X>
    7. '{'-led -> hybrid_json_in_markup
    8. else other_native
    """
    if n_calls > 0:
        return "today_normalizer_accepts"
    if "<function=" not in message:
        return "json_invalid" if message.strip().startswith("{") else "prose_no_call"
    malformed = malformed_native_fn(message)
    if malformed:
        return f"malformed_native:{malformed}"
    match = _FUNCTION_RE.search(message)
    if match and match.group(1) not in MIMO_EXEC_FUNCTIONS:
        if match.group(1) == "command" and _bare_plain_command(message):
            return "unmapped_native_function:command"
        return f"unknown_function:{match.group(1)}"
    extras = sorted(
        {
            name
            for name in _PARAM_NAME_RE.findall(message)
            if name not in _ALLOWED_PARAMS
        }
    )
    if extras:
        return f"extra_param:{'+'.join(extras)}"
    if message.strip().startswith("{"):
        return "hybrid_json_in_markup"
    return "other_native"


_JSON_COMMANDS_RE = re.compile(r'"commands"\s*:\s*\[')


def _proposed_commands(message: str) -> list[str]:
    """Command texts the model proposed in one turn: Terminus JSON
    commands[].keystrokes or native <parameter=command|keystrokes>."""
    commands: list[str] = []
    match = _JSON_COMMANDS_RE.search(message)
    if match:
        try:
            start = message.index("{", 0)
            decoded = json.JSONDecoder().raw_decode(
                message[start:]
            )[0]
            raw = decoded.get("commands") if isinstance(decoded, dict) else None
            if isinstance(raw, list):
                for entry in raw:
                    if isinstance(entry, dict) and isinstance(entry.get("keystrokes"), str):
                        commands.append(entry["keystrokes"])
        except (ValueError, IndexError):
            commands = []
    if not commands:
        for name in _PARAM_TEXT_RE.findall(message):
            commands.append(name)
    return commands


_PARAM_TEXT_RE = re.compile(
    r"<parameter=(?:command|keystrokes)>(.*?)</parameter>", re.DOTALL
)


def concat_defect(message: str, content: str | None) -> bool:
    """R-TOOL-00 gate (A-CONCAT-EXEC): the newline warning fired AND the
    terminal echo shows two proposed commands fused on ONE prompt line --
    the concatenation actually executed (0036 head#2: 'snakemakecat ...').
    A warning alone (0036-e/0036-f/0758-d early steps: commands ran on
    separate prompt lines) is not a defect."""
    if not content or "should end with newline" not in content:
        return False
    commands = _proposed_commands(message)
    if len(commands) < 2:
        return False
    for first, second in zip(commands, commands[1:], strict=False):
        fused = first.rstrip("\n") + second.lstrip("\n")
        if fused[:200] in content:
            return True
    return False



def _ref(doc: str, sid) -> str:
    return f"{doc}#{sid}"


def _session_normalizer_loss(agent_seq, info, doc, sid, cell) -> bool:
    """R-TOOL-03 gate: native signature accepted >=10 turns in OTHER
    documents, then rejected >=10 turns from `sid` in THIS document after
    a summarization/new-session boundary (0758-d)."""
    if not cell.get("native_signature"):
        return False
    head_ok = sum(
        1 for d2, s2 in agent_seq
        if d2 != doc
        and info.get((d2, s2.get("step_id")), {}).get("native_signature")
        and info.get((d2, s2.get("step_id")), {}).get("harness_accepted") == "true"
    )
    same_doc_after = sum(
        1 for d2, s2 in agent_seq
        if d2 == doc and isinstance(s2.get("step_id"), int) and s2["step_id"] >= sid
        and info.get((d2, s2.get("step_id")), {}).get("native_signature")
        and info.get((d2, s2.get("step_id")), {}).get("harness_accepted") == "false"
    )
    return head_ok >= 10 and same_doc_after >= 10



def context_livelock(trial_dir: Path) -> dict | None:
    """R-CTX-01 livelock: trial.log repeats 'Context length exceeded ->
    fallback summarization' cycles that unwind to the same state (0758-c
    lines 192-437: 'Remaining messages: 364' every cycle; 0758-b 776-800).
    Returns {cycles, log_lines} or None."""
    log = trial_dir / "trial.log"
    if not log.is_file():
        return None
    try:
        lines = log.read_text(errors="replace").splitlines()
    except OSError:
        return None
    hits = [
        index + 1
        for index, line in enumerate(lines)
        if "Context length exceeded. Using fallback summarization." in line
    ]
    if len(hits) < 2:
        return None
    return {"cycles": len(hits), "log_lines": f"{hits[0]}-{hits[-1]}"}


def _runs_and_trailing(agent_seq):
    runs = identical_runs(agent_seq)
    # COMP-TRAILING-1: a loop counts as running to the timeout when at most
    # one agent step follows it (the timeout often cuts mid-utterance, or the
    # model emits one final variant, e.g. 0036-f head#173 after head#55-172).
    pos = {
        (doc, step.get("step_id")): index
        for index, (doc, step) in enumerate(agent_seq)
    }
    last_idx = len(agent_seq) - 1
    trailing = [
        run
        for run in runs
        if (run["end_doc"], run["end"]) in pos
        and last_idx - pos[(run["end_doc"], run["end"])] <= 1
    ]
    return runs, trailing


def _rejected_calls(agent_seq, info):
    """Earliest rejected turn the normalizer recovers calls for."""
    for doc, step in agent_seq:
        cell = info.get((doc, step.get("step_id")), {})
        if cell.get("harness_accepted") == "false" and cell.get("n_calls", 0) > 0:
            return (doc, step.get("step_id"), cell)
    return None


def _recovered(agent_seq, info, after_doc, after_sid) -> bool:
    """Later accepted turn exists (mechanical recovery; no NLP)."""
    seen = False
    for doc, step in agent_seq:
        if (doc, step.get("step_id")) == (after_doc, after_sid):
            seen = True
            continue
        if seen and info.get((doc, step.get("step_id")), {}).get("harness_accepted") == "true":
            return True
    return False


def task_complete_refs(agent_seq, info) -> list[str]:
    """Recorded task_complete acceptances (step_layers accepted.task_complete
    true). A non-empty list with no exception means the run closed cleanly
    from the harness's side (arvo-18737 #18 AND #19)."""
    refs = []
    for doc, step in agent_seq:
        layer = (info.get((doc, step.get("step_id")), {}) or {}).get("layer")
        if layer and layer.get("task_complete"):
            refs.append(_ref(doc, step.get("step_id")))
    return refs


def claim_regime(agent_seq) -> dict | None:
    """Generalized COMP-CLAIM-ONSET: the trailing claim regime independent of
    identical runs. L is the last claim-matching model position (required to
    be the final step or one before it); onset is the earliest claim step
    whose window to L stays >=50% claim-matching. Returns
    {start_doc, start, end_doc, end, steps} or None."""
    positions = [
        pos for pos, (_d, step) in enumerate(agent_seq)
        if COMPLETION_CLAIM_RE.search(_stripped(step))
    ]
    if not positions or positions[-1] < len(agent_seq) - 2:
        return None
    end_pos = positions[-1]
    for pos in positions:
        claims_here = sum(1 for pos2 in positions if pos2 >= pos)
        if claims_here * 2 >= end_pos - pos + 1:
            _d0, step0 = agent_seq[pos]
            _d1, step1 = agent_seq[end_pos]
            return {
                "start_doc": _d0, "start": step0.get("step_id"),
                "end_doc": _d1, "end": step1.get("step_id"),
                "steps": end_pos - pos + 1,
            }
    return None


def confirmation_loop(agent_seq, info, regime: dict | None) -> dict | None:
    """R-COMP-04 (secondary, never outcome): the confirmation_with_call loop.
    After the harness confirmation prompt ('Are you sure you want to mark the
    task as complete?'), the model replies with a completion claim PLUS a
    tool call, which the harness executes instead of confirming, repeating
    until the timeout/ceiling. Attribution is unclear: a reply containing a
    command is ambiguous between confirm and continue (README; HAR-94
    proposes a harness-side confirmation protocol). Recorded detector: a
    prose_completion step whose observation carries the prompt, with an
    all-claim model suffix to the regime end (format-code cont-1#38-#51).
    Inferred detector (no recorded layers): the first call-less accepted
    claim step in the regime plus the claim+call accepted steps that follow
    (0036-f #45, then #47 and #54-#172)."""
    if not regime:
        return None
    prompt_positions = []
    for pos, (doc, step) in enumerate(agent_seq):
        layer = (info.get((doc, step.get("step_id")), {}) or {}).get("layer")
        if not layer or layer.get("kind") != "prose_completion":
            continue
        if CONFIRM_PROMPT_RE.search(str(obs_content(step) or "")):
            prompt_positions.append(pos)
    if prompt_positions:
        # Loop start: the EARLIEST prompt-anchored prose_completion whose
        # suffix to the regime end is all claims (an earlier prompt whose
        # reply is followed by non-claim work is the first ask, not the
        # steady-state loop: format-code #35 prompts, #37 still verifies,
        # the all-claim cycle runs #38-#51).
        starts = [
            pos for pos in prompt_positions
            if all(
                COMPLETION_CLAIM_RE.search(_stripped(step))
                for _doc, step in agent_seq[pos:]
            )
        ]
        if not starts:
            return None
        start_pos = starts[0]
        _d0, step0 = agent_seq[start_pos]
        steps_in_span = sum(
            1 for _doc, _step in agent_seq[start_pos:]
        )
        return {
            "rule_id": "R-COMP-04",
            "detector": "recorded",
            "first_confirmation_ref": _ref(_d0, step0.get("step_id")),
            "loop_span": [_ref(_d0, step0.get("step_id")),
                          _ref(regime["end_doc"], regime["end"])],
            "steps_in_span": steps_in_span,
            "suffix_claim_only": True,
            "note": (
                "confirmation_with_call loop (secondary): the harness asked "
                "'Are you sure ...' and the model kept replying claim+call, "
                "which executed instead of confirming; attribution unclear "
                "(confirm vs continue ambiguous)"
            ),
        }
    # Inferred path: no recorded layers and no prompt text on disk.
    start_key = (regime["start_doc"], regime["start"])
    started = False
    first_prose = None
    call_steps = []
    for doc, step in agent_seq:
        key = (doc, step.get("step_id"))
        if key == start_key:
            started = True
        if not started:
            continue
        if not COMPLETION_CLAIM_RE.search(_stripped(step)):
            continue
        cell = info.get(key, {})
        if cell.get("harness_accepted") != "true":
            continue
        if cell.get("n_calls", 0) == 0 and first_prose is None:
            first_prose = _ref(doc, step.get("step_id"))
        elif cell.get("n_calls", 0) > 0:
            call_steps.append(_ref(doc, step.get("step_id")))
    if first_prose and call_steps:
        return {
            "rule_id": "R-COMP-04",
            "detector": "inferred",
            "first_confirmation_ref": first_prose,
            "loop_span": [call_steps[0], call_steps[-1]],
            "call_steps": len(call_steps),
            "note": (
                "confirmation_with_call loop (secondary, inferred): first "
                "call-less accepted claim reply plus claim+call accepted "
                "steps to the loop end; attribution unclear"
            ),
        }
    return None


def _walk_dicts(obj):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_dicts(value)


def budget_caps(job_dir: Path) -> dict:
    """Token/request ceilings from the job lab-metadata.json (Infra's trial
    proxy enforces them; they are NOT in trial result.json/config.json).
    Returns {max_input_tokens, max_output_tokens, max_requests,
    max_total_tokens} with None for absent keys."""
    caps = {"max_input_tokens": None, "max_output_tokens": None,
            "max_requests": None, "max_total_tokens": None}
    try:
        metadata = json.loads((job_dir / "lab-metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return caps
    for node in _walk_dicts(metadata):
        for key in caps:
            if caps[key] is None and isinstance(node.get(key), (int, float)):
                caps[key] = int(node[key])
    return caps


def ceiling_which(job_dir: Path, n_input, n_output, n_episodes) -> str:
    """Name the TrialBudgetExhausted ceiling by utilization: the dimension
    (input tokens, output tokens, requests~=episodes, total tokens) closest
    to its cap wins. Falls back to 'ceiling:trial_budget' when caps or
    counters are absent."""
    caps = budget_caps(job_dir)
    candidates = [
        ("input_tokens", n_input, caps["max_input_tokens"]),
        ("output_tokens", n_output, caps["max_output_tokens"]),
        ("requests", n_episodes, caps["max_requests"]),
    ]
    total = None
    if isinstance(n_input, (int, float)) and isinstance(n_output, (int, float)):
        total = n_input + n_output
    candidates.append(("total_tokens", total, caps["max_total_tokens"]))
    best, best_share = None, -1.0
    for name, used, cap in candidates:
        if isinstance(used, (int, float)) and isinstance(cap, (int, float)) and cap:
            share = used / cap
            if share > best_share:
                best, best_share = name, share
    if best is None:
        return "ceiling:trial_budget"
    return f"ceiling:{best}"


def _verifier_passage(trial_dir: Path) -> dict:
    """Trial's own verifier passage: ctrf test counts (with failing test
    names), test-stdout.txt fallback counts, setup-error flag, guard-reject
    reason, and whether the verifier ran no tests at all."""
    passes = total = None
    failing: list[str] = []
    stdout = ""
    try:
        ctrf = json.loads((trial_dir / "verifier" / "ctrf.json").read_text(encoding="utf-8"))
        tests = ((ctrf.get("results") or {}).get("tests")) or []
        total = len(tests)
        passes = sum(
            1 for test in tests
            if isinstance(test, dict) and test.get("status") == "passed"
        )
        failing = [
            str(test.get("name")) for test in tests
            if isinstance(test, dict) and test.get("status") != "passed"
        ][:8]
    except (OSError, ValueError):
        pass
    try:
        stdout = (trial_dir / "verifier" / "test-stdout.txt").read_text(errors="replace")
    except OSError:
        stdout = ""
    if total is None:
        match = re.search(r"(\d+) failed, (\d+) passed", stdout)
        if match:
            failed = int(match.group(1))
            passes = int(match.group(2))
            total = passes + failed
        else:
            match = re.search(r"(\d+) failed", stdout)
            if match:
                passes, total = 0, int(match.group(1))
            else:
                match = re.search(r"(\d+) passed", stdout)
                if match:
                    passes = total = int(match.group(1))
    fails = (total - passes) if (total is not None and passes is not None) else None
    guard_match = GUARD_REJECT_RE.search(stdout)
    setup_error = bool(
        SETUP_ERROR_RE.search(stdout)
        or "PackageNotFoundError" in stdout
        or "importlib.metadata" in stdout
        or IMPORT_ERROR_RE.search(stdout)
    )
    no_tests = not total and not re.search(r"(\d+) (failed|passed)", stdout)
    return {
        "passes": passes,
        "total": total,
        "fails": fails,
        "failing_tests": failing,
        "setup_error": setup_error,
        "guard_reject": guard_match.group(1) if guard_match else None,
        "no_tests": bool(no_tests),
        "stdout": stdout,
    }


PY_OPEN_WRITE_RE = re.compile(
    r"open\(\s*([^,()]+?)\s*,\s*['\"][wax]b?\+?['\"]"
)
PY_WRITE_TEXT_RE = re.compile(
    r"(?:Path\(\s*(['\"][^'\"]+['\"])\s*\)|(\b\w+))\.write_(?:text|bytes)\("
)


def _heredoc_write_targets(body: str) -> set[str]:
    """Files a heredoc script writes (`open(p, 'w')`, `Path(p).write_text`),
    with `name = '...'` / `name = Path('...')` assignments resolved in the
    same body (a4-1634 head#10: `p='vendor/peewee/peewee.py'` ...
    `open(p,'w').write(...)`). A name resolves to its LATEST assignment
    before the write, so a reused `p` writes every file it names (a4-1789
    head#28: tester.py, issue.py, trojansource.py). Unresolvable names are
    dropped."""
    assignments = [
        (match.start(), match.group(1), match.group(2))
        for match in re.finditer(
            r"\b(\w+)\s*=\s*(?:Path\(\s*)?['\"]([^'\"\n]+)['\"]", body
        )
    ]
    writes = [(m.start(), m.group(1)) for m in PY_OPEN_WRITE_RE.finditer(body)]
    writes += [(m.start(), m.group(1) or m.group(2)) for m in PY_WRITE_TEXT_RE.finditer(body)]
    targets: set[str] = set()
    for position, expression in writes:
        expression = expression.strip()
        if expression[:1] in ("'", '"'):
            targets.add(expression.strip("'\""))
            continue
        latest = [path for start, name, path in assignments
                  if name == expression and start < position]
        if latest:
            targets.add(latest[-1])
    return targets


def _task_targets(text: str) -> set[str]:
    """File paths a command text WRITES on task paths (R-PLAN-01 gate):
    redirect targets, sed -i files, and files a heredoc script writes
    (`python - <<EOF ... open(p, 'w') ... EOF`), minus /tmp, /dev/null and
    fd targets. Read-only prints (`sed -n ...p FILE`) never count; heredoc
    bodies are script content, so their `>` characters are not shell
    redirections and are stripped before redirect extraction. Empty means
    the step did not edit task files."""
    heredoc_re = re.compile(r"<<-?\s*(['\"]?)(\w+)\1\r?\n(.*?)\r?\n\2(?=\W|$)", re.DOTALL)
    targets: set[str] = set()
    for match in heredoc_re.finditer(text):
        targets |= _heredoc_write_targets(match.group(3))
    stripped = heredoc_re.sub("", text)
    for match in REDIRECT_TARGET_RE.finditer(stripped):
        target = match.group(1)
        if target and not target.startswith(("/dev/", "&")):
            targets.add(target)
    for chunk in re.finditer(r"sed\s+([^\n|;&]+)", stripped):
        body = chunk.group(1)
        if not re.search(r"(?:^|\s)-[a-zA-Z]*i", body):
            continue
        tokens = [
            token.strip("'\"")
            for token in body.split()
            if token.strip("'\"")
        ]
        if tokens:
            targets.add(tokens[-1])
    return {
        target for target in targets
        if target not in ("/tmp", "/dev/null")
        and not target.startswith("/tmp/")
        and not target.startswith("/dev/")
        and not target.startswith("&")
    }


def protected_file_writes(agent_seq, guard_reject: str | None) -> list[str]:
    """Model steps whose EXECUTED keystrokes write the file the anti-hack
    guard names (`protected_file_mutated:<path>`). Matched on a path suffix
    at a component boundary, since the model often edits relative to a `cd`
    (a2-1789 head#22: `cd /app/vendor/bandit`, then `open('bandit/__init__.py',
    'w')`). Empty when the trial has no recorded executed layer or no write."""
    if not guard_reject or ":" not in guard_reject:
        return []
    guarded = guard_reject.split(":", 1)[1].strip().lstrip("./")
    refs: list[str] = []
    for doc, step in agent_seq:
        layer = layer_status(step)
        if not layer:
            continue
        for target in _task_targets(layer["executed_keystrokes"]):
            target = target.strip().lstrip("./")
            if target and (
                target == guarded
                or guarded.endswith("/" + target)
                or target.endswith("/" + guarded)
            ):
                refs.append(_ref(doc, step.get("step_id")))
                break
    return refs


def suspect_grader_evidence(trial_dir: Path, nop_runs_dir: str | None) -> dict | None:
    """R-ENV-02 suspect-grader evidence (HAR-81): the verifier fails at
    setup/collection (session-fixture errors) rather than on the model's
    behavior, or the verifier ran no tests at all with only a guard reject
    (path b). Returns {note, setup_error_tests, total_tests,
    nop_control_confirms, nop_trial} or None. The nop cross-check resolves
    a same-task nop/qual trial under --nop-runs-dir by task_name; without
    the flag (or the trial) the verdict is 'unknown'."""
    stdout_path = trial_dir / "verifier" / "test-stdout.txt"
    if not stdout_path.is_file():
        return None
    try:
        stdout = stdout_path.read_text(errors="replace")
    except OSError:
        return None
    setup_tests = sorted(set(SETUP_ERROR_RE.findall(stdout)))
    package_broken = (
        "PackageNotFoundError" in stdout
        or "importlib.metadata" in stdout
        or "ERROR at setup of" in stdout
    )
    total_tests = None
    ctrf_path = trial_dir / "verifier" / "ctrf.json"
    try:
        ctrf = json.loads(ctrf_path.read_text(encoding="utf-8"))
        tests = ((ctrf.get("results") or {}).get("tests")) or []
        total_tests = len(tests)
    except (OSError, ValueError):
        tests = []
    nop_trial, nop_text = _resolve_nop_trial(trial_dir, nop_runs_dir)
    if setup_tests or package_broken:
        nop_confirm: str = "unknown"
        if nop_text is not None:
            nop_setup = set(SETUP_ERROR_RE.findall(nop_text))
            if setup_tests and set(setup_tests) <= nop_setup:
                nop_confirm = "true"
            elif setup_tests:
                nop_confirm = "false"
            elif IMPORT_ERROR_RE.search(nop_text):
                nop_confirm = "true"
        return {
            "note": (
                f"suspect grader: verifier/test-stdout.txt has "
                f"{len(setup_tests)} 'ERROR at setup of' "
                f"({', '.join(setup_tests) if setup_tests else 'session fixture'})"
                + (f" of {total_tests} ctrf tests" if total_tests else "")
                + "; nop control confirms the same setup errors"
                + (f" ({nop_trial})" if nop_trial else "")
                + f": {nop_confirm}"
            ),
            "setup_error_tests": setup_tests,
            "total_tests": total_tests,
            "nop_control_confirms": nop_confirm,
            "nop_trial": nop_trial,
        }
    # Path (b): the verifier ran NO tests and rejected the run at the guard
    # (1702: only `anti_hack_guard: REJECT protected_file_mutated:...`). The
    # environment never ran, so no agent behavior can explain the outcome.
    # Requires the trial's own stdout to show no tests; the nop control must
    # show import/setup failure (or be unknown) -- never fire when the nop
    # passes cleanly, and never when the trial itself ran tests (1634: 5/6
    # pass with a different nop failure stays a model outcome).
    guard_match = GUARD_REJECT_RE.search(stdout)
    no_tests = not total_tests and not re.search(r"(\d+) (failed|passed)", stdout)
    if guard_match and no_tests:
        nop_confirm = "unknown"
        if nop_text is not None:
            if IMPORT_ERROR_RE.search(nop_text) or SETUP_ERROR_RE.search(nop_text):
                nop_confirm = "true"
            else:
                nop_pass = re.search(r"(\d+) passed", nop_text)
                if nop_pass and int(nop_pass.group(1)) > 0:
                    return None
        return {
            "note": (
                f"suspect grader: verifier ran no tests; "
                f"verifier/test-stdout.txt is only '{guard_match.group(0)}' "
                f"(medium confidence); nop control shows import/setup failure"
                + (f" ({nop_trial})" if nop_trial else "")
                + f": {nop_confirm}"
            ),
            "setup_error_tests": [],
            "guard_reject": guard_match.group(1),
            "total_tests": total_tests,
            "nop_control_confirms": nop_confirm,
            "nop_trial": nop_trial,
        }
    return None


def _resolve_nop_trial(trial_dir: Path, nop_runs_dir: str | None) -> tuple:
    """Same-task nop/qual control trial under --nop-runs-dir (matched by
    task_name): (trial path str or None, verifier test-stdout text or None)."""
    if not nop_runs_dir:
        return None, None
    try:
        result = probe02._read_json(trial_dir / "result.json")
    except (OSError, ValueError):
        return None, None
    task_name = result.get("task_name")
    root = Path(nop_runs_dir)
    if not task_name or not root.is_dir():
        return None, None
    for job in sorted(root.iterdir()):
        if not job.is_dir():
            continue
        lname = job.name.lower()
        if "nop" not in lname and "qual" not in lname:
            continue
        for candidate in probe02.find_trials(job):
            try:
                cand_result = probe02._read_json(candidate / "result.json")
            except (OSError, ValueError):
                continue
            if cand_result.get("task_name") != task_name:
                continue
            nop_stdout = candidate / "verifier" / "test-stdout.txt"
            if not nop_stdout.is_file():
                continue
            try:
                return str(candidate), nop_stdout.read_text(errors="replace")
            except OSError:
                continue
    return None, None


def _instruction_text(trial_dir: Path) -> tuple[str | None, int | None]:
    """The trial's instruction.md (text, bytes), read from the prepared-task
    path (job lab-metadata.json experiment.task_path, resolved against the
    runs dir); (None, None) when it cannot be located."""
    try:
        lab = json.loads((trial_dir.parent / "lab-metadata.json").read_text(encoding="utf-8"))
        task_path = ((lab.get("experiment") or {}).get("task_path")) or ""
        # The prepared-task path is relative to the eval-lab worktree root
        # (e.g. 'runs/.prepared-tasks/...'): resolve against the ancestor
        # that contains `runs/`.
        root = trial_dir
        for _ in range(6):
            if (root / "runs").is_dir():
                break
            root = root.parent
        candidates = []
        if task_path:
            candidates.append(root / task_path / "instruction.md")
            stripped = task_path[len("runs/"):] if task_path.startswith("runs/") else task_path
            candidates.append(root / "runs" / stripped / "instruction.md")
        for candidate in candidates:
            if candidate.is_file():
                return (
                    candidate.read_text(encoding="utf-8", errors="replace"),
                    candidate.stat().st_size,
                )
    except (OSError, ValueError):
        pass
    return None, None


def completion_grader_check(trial_dir: Path, verifier_message: str) -> str | None:
    """R-COMP-03 grader_note: the verifier names a deliverable artifact the
    instruction never mentions (deliverable_not_in_instruction). Returns the
    note text or None."""
    tokens: set[str] = set()
    for quoted in re.findall(r"'([^']+)'|\"([^\"]+)\"", verifier_message or ""):
        tokens.update(part for part in quoted if part)
    tokens.update(re.findall(r"\b[\w.-]+\.(?:sh|py|json|patch|diff)\b", verifier_message or ""))
    tokens.update(
        match for match in re.findall(r"\b(PoC|poc|exploit|submit)\b", verifier_message or "")
    )
    tokens = {token for token in tokens if len(token) >= 2}
    instruction_text, instruction_size = _instruction_text(trial_dir)
    if instruction_text is None:
        return "instruction.md not located; deliverable check skipped"
    missing = [token for token in sorted(tokens) if token.lower() not in instruction_text.lower()]
    if tokens and len(missing) == len(tokens):
        return (
            f"deliverable_not_in_instruction: verifier names "
            f"{sorted(tokens)} but instruction.md ({instruction_size} B) never "
            f"mentions them"
        )
    if missing:
        return (
            f"deliverable_partially_in_instruction: instruction.md "
            f"({instruction_size} B) lacks {missing}"
        )
    return None


def _step_command_text(doc, step, info) -> str:
    """Executed keystrokes when the harness recorded them, else the raw
    message (HAR-90 raw_content messages already carry the tool calls)."""
    layer = (info.get((doc, step.get("step_id")), {}) or {}).get("layer") if info else None
    if layer and layer.get("executed_keystrokes"):
        return layer["executed_keystrokes"]


    return str(step.get("message") or "")

KEYSTROKES_JSON_RE = re.compile(r'"keystrokes"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _step_observation_text(doc, step, info) -> str:
    layer = (info.get((doc, step.get("step_id")), {}) or {}).get("layer") if info else None
    if layer and layer.get("observed_output"):
        return str(layer["observed_output"])
    results = (step.get("observation") or {}).get("results") or []
    return " ".join(str(result.get("content") or "") for result in results)


def _unsubmitted_keystroke_loop(longest, agent_seq, info) -> bool:
    """True when >=half of the identical run's turns send keystrokes with no
    trailing newline AND the observation echoes that command fused onto
    itself (tail immediately followed by head: `head -40grep -rn`), i.e. the
    command was typed but never submitted (0036 head#17-121)."""
    if not longest:
        return False
    positions = {(doc, step.get("step_id")): index for index, (doc, step) in enumerate(agent_seq)}
    start = positions.get((longest["start_doc"], longest["start"]))
    end = positions.get((longest["end_doc"], longest["end"]))
    if start is None or end is None or end < start:
        return False
    fused = 0
    for doc, step in agent_seq[start:end + 1]:
        match = KEYSTROKES_JSON_RE.search(str(step.get("message") or ""))
        if not match:
            continue
        try:
            keys = json.loads(f'"{match.group(1)}"')
        except ValueError:
            continue
        command = keys.strip()
        if keys.endswith("\n") or len(command) < 12:
            continue
        if command[-8:] + command[:8] in _step_observation_text(doc, step, info):
            fused += 1
    return fused * 2 >= (end - start + 1)



SUBMIT_CONTRACT_RE = re.compile(r"submit PoC", re.IGNORECASE)
SANITIZER_SUMMARY_RE = re.compile(
    r"SUMMARY:\s*\S*(?:Address|Memory|UndefinedBehavior)Sanitizer", re.IGNORECASE
)
REPRO_INVOKE_RE = re.compile(
    r"(?:^|(?<=\W))(?:[\w./-]*?/)?(?:run\.sh|fuzz[\w-]*)(?=\s|$)"
)
REPRO_VIEWER_RE = re.compile(
    r"(?:^|(?<=\W))(?:cat|less|more|head|tail|file|strings|ls|sed|grep|awk|wc|diff|vi|vim|nano)\b"
)
SUBMIT_INVOKE_RE = re.compile(
    r"(?:^|(?<=\W))(?:bash\s+)?(?:\./|[\w./-]*?/)?submit\.sh(?=\s|$)"
)


def _submit_invoke_ref(keystrokes: str) -> str | None:
    """Executed keystrokes invoke submit.sh with an argument
    (`bash submit.sh <poc>`). Reads/views (`cat submit.sh`) do not count."""
    if not keystrokes:
        return None
    for segment in re.split(r"[;&|\n]+", keystrokes):
        match = SUBMIT_INVOKE_RE.search(segment)
        if not match:
            continue
        if REPRO_VIEWER_RE.search(segment[:match.start()]):
            continue
        rest = segment[match.end():].strip()
        if rest and not rest.startswith("|"):
            return rest.split()[0][:80]
    return None


def _repro_attempt_ref(keystrokes: str) -> bool:
    """Executed keystrokes attempt a PoC reproduction: they invoke binary's
    run.sh or a fuzz binary with a path-like input argument (a crash file or
    corpus dir). Reads/views of the script itself (`cat run.sh`,
    `file binary/*fuzzer`) and flag-only runs (`-help=1`, no input path)
    do not count."""
    if not keystrokes:
        return False
    for segment in re.split(r"[;&|\n]+", keystrokes):
        match = REPRO_INVOKE_RE.search(segment)
        if not match:
            continue
        if REPRO_VIEWER_RE.search(segment[:match.start()]):
            continue
        for token in segment[match.end():].split():
            cleaned = token.strip("'\"")
            if (
                "/" in cleaned
                and not cleaned.startswith("-")
                and ">" not in cleaned
                and "=" not in cleaned
            ):
                return True
    return False


def contract_evidence(agent_seq, info) -> dict:
    """Submit-contract signaling for R-COMP-03 (arvo): contract_seen (an
    observation carries the submit.sh header / 'submit PoC'),
    attempted_repro (executed keystrokes run binary/run.sh or the fuzz
    binary on an input file), poc_reproduced (a sanitizer SUMMARY at or
    after the first attempt), submitted (executed keystrokes invoke
    submit.sh with an argument). Values are step refs or None."""
    seen = attempted = reproduced = submitted = None
    attempt_pos: int | None = None
    for pos, (doc, step) in enumerate(agent_seq):
        ref = _ref(doc, step.get("step_id"))
        obs = str(obs_content(step) or "")
        text = _step_command_text(doc, step, info)
        if seen is None and SUBMIT_CONTRACT_RE.search(obs):
            seen = ref
        if attempted is None and _repro_attempt_ref(text):
            attempted = ref
            attempt_pos = pos
        if (
            reproduced is None
            and SANITIZER_SUMMARY_RE.search(obs)
            and attempt_pos is not None
            and pos >= attempt_pos
        ):
            reproduced = ref
        if submitted is None and _submit_invoke_ref(text):
            submitted = ref
    return {
        "contract_seen": seen,
        "attempted_repro": attempted,
        "poc_reproduced": reproduced,
        "submitted": submitted,
    }


def _change_targets(text: str) -> set[str]:
    """File paths named by a write op: redirect targets and sed -i files,
    minus /dev/null and fd targets."""
    targets: set[str] = set()
    for match in REDIRECT_TARGET_RE.finditer(text):
        target = match.group(1)
        if target and not target.startswith(("/dev/", "&")):
            targets.add(target)
    for match in SED_TARGET_RE.finditer(text):
        target = match.group(1)
        if target and target not in ("-i",):
            targets.add(target)
    return targets


def env_wrestling_span(model_seq, info=None) -> dict | None:
    """Consecutive model steps whose commands wrestle the environment (pip
    installs, packaging/setup edits) rather than the task. Returns
    {start_doc, start, end_doc, end, steps, targets} or None."""
    match_positions = []
    for pos, (doc, step) in enumerate(model_seq):
        if ENV_WRESTLE_RE.search(_step_command_text(doc, step, info)):
            match_positions.append(pos)
    if not match_positions:
        return None
    # Longest consecutive run of matching positions.
    best_start = best_end = match_positions[0]
    run_start = match_positions[0]
    prev = match_positions[0]
    for pos in match_positions[1:] + [None]:
        if pos is not None and pos == prev + 1:
            prev = pos
            continue
        if prev - run_start > best_end - best_start:
            best_start, best_end = run_start, prev
        if pos is None:
            break
        run_start = prev = pos
    targets: set[str] = set()
    for pos in range(best_start, best_end + 1):
        doc, step = model_seq[pos]
        targets.update(_change_targets(_step_command_text(doc, step, info)))
    _d0, step0 = model_seq[best_start]
    _d1, step1 = model_seq[best_end]
    return {
        "start_doc": _d0, "start": step0.get("step_id"),
        "end_doc": _d1, "end": step1.get("step_id"),
        "steps": best_end - best_start + 1,
        "targets": sorted(targets)[:8],
    }


def progress_block(model_seq, info, metadata, trial_dir: Path) -> dict:
    """R-UNC-01 progress pointer (timeouts/ceilings with high acceptance and
    no loop): where the next hand-read should look. Last file-changing step
    (recorded keystrokes preferred, message text fallback), distinct files
    edited, longest run of identical executed keystrokes, verifier pass
    count (and any-pass), longest identical message run, prose completion
    count, summarization attempts/splits."""
    last_change = None
    edited_files: set[str] = set()
    task_files: set[str] = set()
    keystroke_runs: list[tuple[int, int]] = []
    run_start: int | None = None
    prev_keystrokes: str | None = None
    for pos, (doc, step) in enumerate(model_seq):
        layer = (info.get((doc, step.get("step_id")), {}) or {}).get("layer")
        keystrokes = (layer or {}).get("executed_keystrokes") or ""
        text = keystrokes or str(step.get("message") or "")
        if any(pattern.search(text) for pattern in FILE_CHANGE_RES):
            last_change = _ref(doc, step.get("step_id"))
            edited_files.update(_change_targets(text))
            task_files.update(_task_targets(text))
        if keystrokes and keystrokes == prev_keystrokes and run_start is not None:
            keystroke_runs[-1] = (run_start, pos)
        else:
            run_start = pos if keystrokes else None
            if keystrokes:
                keystroke_runs.append((pos, pos))
        prev_keystrokes = keystrokes if keystrokes else None
        if not keystrokes:
            run_start = None
    longest_keys = max(keystroke_runs, key=lambda run: run[1] - run[0]) if keystroke_runs else None
    passes = total = None
    try:
        ctrf = json.loads((trial_dir / "verifier" / "ctrf.json").read_text(encoding="utf-8"))
        tests = ((ctrf.get("results") or {}).get("tests")) or []
        total = len(tests)
        passes = sum(1 for test in tests if isinstance(test, dict) and test.get("status") == "passed")
    except (OSError, ValueError):
        pass
    if total is None:
        try:
            stdout = (trial_dir / "verifier" / "test-stdout.txt").read_text(errors="replace")
            match = re.search(r"(\d+) failed, (\d+) passed", stdout)
            if match:
                failed = int(match.group(1))
                passes = int(match.group(2))
                total = passes + failed
            else:
                match = re.search(r"(\d+) failed", stdout)
                if match:
                    passes, total = 0, int(match.group(1))
                else:
                    match = re.search(r"(\d+) passed", stdout)
                    if match:
                        passes = total = int(match.group(1))
        except OSError:
            pass
    runs = identical_runs(model_seq)
    longest = max(runs, key=lambda run: run["length"]) if runs else None
    coverage_files = sum(
        1 for name in ("trajectory.json",)
        if (trial_dir / "agent" / name).is_file()
    )
    if longest_keys and longest_keys[1] > longest_keys[0]:
        _kd0, _ks0 = model_seq[longest_keys[0]]
        _kd1, _ks1 = model_seq[longest_keys[1]]
        keys_run = (
            f"{_ref(_kd0, _ks0.get('step_id'))}-{_ref(_kd1, _ks1.get('step_id'))} "
            f"({longest_keys[1] - longest_keys[0] + 1})"
        )
    else:
        keys_run = None
    return {
        "last_file_changing_step": last_change,
        "distinct_files_edited": sorted(edited_files),
        "distinct_files_edited_count": len(edited_files),
        "distinct_task_files_edited": sorted(task_files),
        "distinct_task_files_edited_count": len(task_files),
        "longest_identical_keystrokes_run": keys_run,
        "verifier_passes": passes,
        "verifier_total": total,
        "verifier_any_pass": bool(passes) if passes is not None else None,
        "longest_identical_run": (
            f"{_ref(longest['start_doc'], longest['start'])}-"
            f"{_ref(longest['end_doc'], longest['end'])} ({longest['length']})"
            if longest else None
        ),
        "prose_completions": metadata.get("prose_completions"),
        "summarization_count": metadata.get("summarization_count"),
        "summarization_files": coverage_files,
    }

def compute_first_failure(agent_seq, info, grader_ev=None) -> dict | None:
    """Earliest firing among R-ENV-01, R-ENV-02 (suspect grader: the step
    whose observation surfaces the missing dependency), R-TOOL-00
    (accepted-but-concatenated), R-TOOL-01 (today_normalizer_accepts /
    unmapped command), R-TOOL-02 (model-emitted surface), R-TOOL-03
    (session-scoped normalizer loss), R-COMP-01 (prose final answer
    rejected). Includes `recovered`. Returns None when the execution is
    mechanically clean (loops are outcome-level R-REC-01, recorded
    separately) -- the hand key nulls first_failure for contract, planning
    and false-claim trials whose outcome explains the trial."""
    if not agent_seq:
        return {
            "step_id": None, "step_ref": None, "tag": "environment",
            "attribution": "unclear", "rule_id": "R-ENV-01",
            "evidence_step_refs": [], "recovered": False,
            "note": "no model turns; infra-only failure, nothing to attribute to the model",
        }
    if grader_ev:
        for doc, step in agent_seq:
            if IMPORT_ERROR_RE.search(str(obs_content(step) or "")):
                module_match = MISSING_MODULE_RE.search(str(obs_content(step) or ""))
                module = None
                if module_match:
                    module = module_match.group(1) or module_match.group(2)
                return {
                    "step_id": step.get("step_id"), "step_ref": _ref(doc, step.get("step_id")),
                    "tag": "environment", "attribution": "harness", "rule_id": "R-ENV-02",
                    "evidence_step_refs": [_ref(doc, step.get("step_id"))],
                    "recovered": False,
                    "note": (
                        f"model command surfaced the broken environment "
                        f"({module or 'missing dependency'}); suspect grader, "
                        f"nothing to attribute to the model"
                    ),
                }
    concat = next(
        (
            (index, doc, step.get("step_id"))
            for index, (doc, step) in enumerate(agent_seq)
            if info.get((doc, step.get("step_id")), {}).get("concat_warning")
        ),
        None,
    )
    reject = next(
        (
            (index, doc, step.get("step_id"))
            for index, (doc, step) in enumerate(agent_seq)
            if info.get((doc, step.get("step_id")), {}).get("harness_accepted") == "false"
        ),
        None,
    )
    choice = None
    if concat is not None and (reject is None or concat[0] <= reject[0]):
        choice = concat
    elif reject is not None:
        choice = reject
    if choice is not None:
        _pos, doc, sid = choice
        cell = info.get((doc, sid), {})
        recovered = _recovered(agent_seq, info, doc, sid)
        cause = cell.get("rejection_cause")
        if cell.get("concat_warning"):
            return {
                "step_id": sid, "step_ref": _ref(doc, sid), "tag": "tool_use",
                "attribution": "unclear", "rule_id": "R-TOOL-00",
                "evidence_step_refs": [_ref(doc, sid)], "recovered": recovered,
                "note": (
                    "Terminus JSON executed, but keystrokes lacked the protocol's "
                    "trailing newline, so commands concatenated (warning notice + "
                    "garbage terminal); attribution unclear per A-CONCAT-UNCLEAR"
                ),
            }
        if cause in ("today_normalizer_accepts", "unmapped_native_function:command"):
            if cause == "unmapped_native_function:command":
                note = (
                    "bare native <function=command> with a plain command body; "
                    "the parser maps only {exec_command, bash, exec}, so the "
                    "unambiguous call is rejected as unmapped (harness gap; "
                    "today's normalizer still declines)"
                )
            else:
                note = (
                    "rejected turn that today's normalizer accepts -- harness gap, "
                    "since fixed by mimo_tool_calls.py (#512)"
                )
            return {
                "step_id": sid, "step_ref": _ref(doc, sid), "tag": "tool_use",
                "attribution": "harness", "rule_id": "R-TOOL-01",
                "evidence_step_refs": [_ref(doc, sid)], "recovered": recovered,
                "note": note,
            }
        if cause == "prose_no_call":
            return {
                "step_id": sid, "step_ref": _ref(doc, sid), "tag": "completion",
                "attribution": "harness", "rule_id": "R-COMP-01",
                "evidence_step_refs": [_ref(doc, sid)], "recovered": recovered,
                "note": (
                    "prose final answer with no call rejected as a parse error; "
                    "Terminus has no prose completion path (A-PROSE-HARNESS)"
                ),
            }
        if cause and (
            cause.startswith("unknown_function:")
            or cause.startswith("extra_param:")
            or cause.startswith("malformed_native:")
        ):
            if cause.startswith("extra_param:") and _session_normalizer_loss(
                agent_seq, info, doc, sid, cell
            ):
                return {
                    "step_id": sid, "step_ref": _ref(doc, sid),
                    "tag": "tool_use", "attribution": "harness",
                    "rule_id": "R-TOOL-03",
                    "evidence_step_refs": [_ref(doc, sid)],
                    "recovered": recovered,
                    "note": (
                        f"same native signature accepted earlier, then rejected "
                        f"from this step (cause {cause}) after the summarization/"
                        f"new-session boundary; feedback never named the parameter"
                    ),
                }
            failure = {
                "step_id": sid, "step_ref": _ref(doc, sid), "tag": "tool_use",
                "attribution": "model", "rule_id": "R-TOOL-02",
                "evidence_step_refs": [_ref(doc, sid)], "recovered": recovered,
                "note": (
                    f"model-emitted surface the normalizer declines ({cause}); "
                    "A-NON-NATIVE-MODEL"
                ),
            }
            _attach_feedback(failure, cell)
            return failure
        return {
            "step_id": sid, "step_ref": _ref(doc, sid), "tag": "tool_use",
            "attribution": "unclear", "rule_id": "R-TOOL-01U",
            "evidence_step_refs": [_ref(doc, sid)], "recovered": recovered,
            "note": f"rejected turn, cause {cause or 'unknown'}; attribution unclear",
        }
    # No rejection and no concat warning: the execution is mechanically
    # clean. Identical loops belong to the outcome (R-REC-01 secondary), not
    # to first_failure -- return None per the hand-key convention.
    return None


def compute_outcome_failure(
    agent_seq, info, scored, reward, timed_out, runs, trailing, trial_dir,
    stop_reason="", limit_stop=False, completion_refs=None, grader_ev=None,
) -> dict:
    """The failure that explains the verifier outcome / stop: R-ENV-01,
    R-COMP-01, R-ENV-02 (setup-error evidence), R-COMP-02, R-COMP-03,
    R-TOOL-01, R-TOOL-01U, R-TOOL-02, R-TOOL-03, R-REC-01; the caller resolves
    the legacy collection-error R-ENV-02 / R-NONE-01 / R-UNC-01."""
    if not agent_seq:
        return {
            "step_id": None, "step_ref": None, "tag": "environment",
            "attribution": "unclear", "rule_id": "R-ENV-01",
            "evidence_step_refs": [],
            "note": "no model turns; infra-only failure, nothing to attribute to the model",
        }
    longest = max(runs, key=lambda run: run["length"]) if runs else None
    if scored and (reward or 0) >= 1.0 and timed_out:
        for run in trailing:
            cell = info.get((run["start_doc"], run["start"]), {})
            if cell.get("shape") == "prose" and cell.get("harness_accepted") == "false":
                return {
                    "step_id": run["start"],
                    "step_ref": _ref(run["start_doc"], run["start"]),
                    "tag": "completion", "attribution": "harness",
                    "rule_id": "R-COMP-01",
                    "evidence_step_refs": [
                        _ref(run["start_doc"], run["start"]),
                        _ref(run["end_doc"], run["end"]),
                    ],
                    "note": (
                        f"solved (reward 1.0) at or before step {run['start'] - 1}; "
                        f"prose final answer re-prompted as a parse error for steps "
                        f"{run['start']}-{run['end']} until the timeout"
                    ),
                }
    # R-ENV-02 suspect grader (setup-error evidence): the verifier fails at
    # setup/collection, so no agent behavior can explain the outcome. Fires
    # only on setup-error evidence (absent on all HAR-90 trials); the legacy
    # collection/import patterns stay resolved late from R-UNC-01 only.
    if grader_ev:
        import_sid = None
        import_ref = None
        for doc, step in agent_seq:
            if IMPORT_ERROR_RE.search(str(obs_content(step) or "")):
                import_sid = step.get("step_id")
                import_ref = _ref(doc, import_sid)
                break
        out = {
            "step_id": import_sid, "step_ref": import_ref, "tag": "environment",
            "attribution": "harness", "rule_id": "R-ENV-02",
            "evidence_step_refs": [import_ref] if import_ref else [],
            "note": grader_ev["note"],
            "setup_error_tests": grader_ev.get("setup_error_tests"),
            "verifier_total_tests": grader_ev.get("total_tests"),
            "nop_control_confirms": grader_ev.get("nop_control_confirms"),
            "nop_trial": grader_ev.get("nop_trial"),
        }
        span = env_wrestling_span(agent_seq, info)
        if span:
            targets = span["targets"]
            if not targets or all(
                PACKAGING_PATH_RE.search(target) for target in targets
            ):
                edit_claim = "no target-file edits"
            else:
                edit_claim = f"file targets {targets}"
            out["secondary"] = (
                f"environment wrestling: model steps "
                f"{_ref(span['start_doc'], span['start'])}-"
                f"{_ref(span['end_doc'], span['end'])} ({span['steps']} steps; "
                f"{edit_claim})"
            )
        guard_writes = protected_file_writes(agent_seq, grader_ev.get("guard_reject"))
        if grader_ev.get("guard_reject"):
            out["guard_mutation_steps"] = guard_writes
            out["note"] += (
                f"; the model itself wrote the guarded file at "
                f"{', '.join(guard_writes[:6])}, so the guard rejection is "
                f"earned; the nop control fails at import with no edit at all"
                if guard_writes else
                "; no executed model write to the guarded file was found"
            )
        return out
    # R-NONE-01: scored pass with no outcome-relevant failure (COMP-01
    # pass-plus-timeout handled above). Transient rejections, if any, are
    # covered by first_failure; a longest identical run stays secondary.
    if scored and (reward or 0) >= 1.0:
        out = {
            "step_id": None, "step_ref": None, "tag": "none",
            "attribution": "n/a", "rule_id": "R-NONE-01",
            "evidence_step_refs": [],
            "note": "scored pass with no outcome-relevant failure",
        }
        if longest:
            out["secondary"] = (
                f"error_recovery/model secondary: identical run "
                f"{_ref(longest['start_doc'], longest['start'])}-"
                f"{_ref(longest['end_doc'], longest['end'])}"
            )
            out["evidence_step_refs"] += [
                _ref(longest["start_doc"], longest["start"]),
                _ref(longest["end_doc"], longest["end"]),
            ]
        return out
    # R-COMP-03 gate (b): reward 0 on a submit-contract (cyber) trial with
    # contract engagement (attempted repro, reproduced crash, or seen
    # contract) -- the ceiling trials that explore without ever submitting.
    # No completion claim is required: pure exploration to budget with no
    # deliverable still fails on the submit contract. Attribution:
    # reproduced-and-not-submitted -> model; attempted-but-not-reproduced
    # -> model (understood the goal as reproduction and failed at it);
    # contract-seen-only -> model (knew the deliverable); never engaged
    # -> not this gate. Submitted-but-unscored -> unclear (mismatch).
    contract = contract_evidence(agent_seq, info)
    engaged = (
        contract["attempted_repro"] or contract["poc_reproduced"]
        or contract["contract_seen"]
    )
    claim_signal = list(completion_refs or [])
    if scored and (reward or 0) < 1.0 and engaged:
        if contract["submitted"]:
            attribution = "unclear"
            contract_note = (
                f"submit.sh invoked at {contract['submitted']} but the "
                f"verifier reports not submitted"
            )
        else:
            attribution = "model"
            if contract["poc_reproduced"]:
                contract_note = (
                    f"reproduced but never submitted (crash reproduced at "
                    f"{contract['poc_reproduced']})"
                )
            elif contract["attempted_repro"]:
                contract_note = (
                    f"attempted reproduction but never reproduced it "
                    f"(attempt at {contract['attempted_repro']}) and never submitted"
                )
            else:
                contract_note = (
                    f"knew the deliverable (contract seen at "
                    f"{contract['contract_seen']}) but never reproduced or submitted"
                )
        poc_sid = None
        if contract["poc_reproduced"]:
            for doc, step in agent_seq:
                if _ref(doc, step.get("step_id")) == contract["poc_reproduced"]:
                    poc_sid = step.get("step_id")
                    break
        engagement_refs = [
            ref for ref in (
                claim_signal
                + [contract["contract_seen"], contract["attempted_repro"],
                   contract["poc_reproduced"]]
            )
            if ref
        ]
        out = {
            "step_id": poc_sid, "step_ref": contract["poc_reproduced"],
            "tag": "completion", "attribution": attribution,
            "rule_id": "R-COMP-03",
            "evidence_step_refs": list(dict.fromkeys(engagement_refs))[:8],
            "note": (
                f"verifier score is {reward} (scored={scored}); {contract_note}"
            ),
            "contract_seen": contract["contract_seen"],
            "attempted_repro": bool(contract["attempted_repro"]),
            "attempted_repro_ref": contract["attempted_repro"],
            "poc_reproduced": contract["poc_reproduced"],
            "submitted": bool(contract["submitted"]),
        }
        if longest:
            out["secondary"] = (
                f"error_recovery/model secondary: identical run "
                f"{_ref(longest['start_doc'], longest['start'])}-"
                f"{_ref(longest['end_doc'], longest['end'])}"
            )
            out["evidence_step_refs"] += [
                _ref(longest["start_doc"], longest["start"]),
                _ref(longest["end_doc"], longest["end"]),
            ]
        return out
    # R-COMP-03 gate (c): a submit-contract verifier (verifier/result.json
    # carries `submitted`) reports nothing submitted, and the model never
    # engaged the contract (no repro attempt, contract never opened) and
    # never confirmed completion (that path is handled below). The
    # instruction does not state the deliverable, so attribution is
    # `unclear` (57589: source reading + identical grep loop to budget).
    if scored and (reward or 0) < 1.0 and not engaged and stop_reason != "task_complete_confirmed":
        try:
            submit_verdict = json.loads(
                (trial_dir / "verifier" / "result.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            submit_verdict = None
        if isinstance(submit_verdict, dict) and submit_verdict.get("submitted") is False:
            message = str(submit_verdict.get("message") or "")
            out = {
                "step_id": None, "step_ref": None,
                "tag": "completion", "attribution": "unclear",
                "rule_id": "R-COMP-03",
                "evidence_step_refs": [],
                "note": (
                    f"verifier score is {reward}; {message!r}; the model never "
                    f"engaged the submit contract (no reproduction attempt, "
                    f"submit.sh never opened)"
                ),
                "contract_seen": None,
                "attempted_repro": False,
                "attempted_repro_ref": None,
                "poc_reproduced": None,
                "submitted": False,
            }
            grader_note = completion_grader_check(trial_dir, message)
            if grader_note:
                out["grader_note"] = grader_note
            if longest:
                out["secondary"] = (
                    f"error_recovery/model secondary: identical run "
                    f"{_ref(longest['start_doc'], longest['start'])}-"
                    f"{_ref(longest['end_doc'], longest['end'])}"
                )
                out["evidence_step_refs"] += [
                    _ref(longest["start_doc"], longest["start"]),
                    _ref(longest["end_doc"], longest["end"]),
                ]
            return out

    stopped_by_limit = timed_out or limit_stop
    stop_word = "timeout" if timed_out else ("the stop" if limit_stop else "timeout")
    # A completion claim is a CC-CLAIM message OR a turn the harness recorded
    # as task_complete (a3-000240 head#35 "The implementation is complete";
    # a4-1634 head#21 "The repair is complete"): one definition for every
    # R-COMP-02 branch below.
    recorded_claims = set(completion_refs or [])

    def is_claim(doc, step) -> bool:
        return bool(
            COMPLETION_CLAIM_RE.search(_stripped(step))
            or _ref(doc, step.get("step_id")) in recorded_claims
        )

    if stopped_by_limit and not (scored and (reward or 0) >= 1.0):
        for run in trailing:
            if COMPLETION_CLAIM_RE.search(run["message"] or ""):
                run_end_pos = next(
                    i for i, (_d, _s) in enumerate(agent_seq)
                    if (_d, _s.get("step_id")) == (run["end_doc"], run["end"])
                )
                run_start_pos = next(
                    i for i, (_d, _s) in enumerate(agent_seq)
                    if (_d, _s.get("step_id")) == (run["start_doc"], run["start"])
                )
                # COMP-CLAIM-ONSET: the FIRST claim step whose window to the
                # loop end stays >=50% claim-matching (0036-f: head#44, with
                # wrap-up tool steps interleaved before the head#55-172 loop).
                claim_start = run["start"]
                claim_doc = run["start_doc"]
                window = agent_seq[: run_end_pos + 1]
                for pos in range(0, run_start_pos + 1):
                    _d, step_c = window[pos]
                    if not is_claim(_d, step_c):
                        continue
                    claims_here = sum(
                        1 for _d2, step2 in window[pos:]
                        if is_claim(_d2, step2)
                    )
                    if claims_here * 2 >= len(window) - pos:
                        claim_start = step_c.get("step_id")
                        claim_doc = _d
                        break
                out = {
                    "step_id": claim_start,
                    "step_ref": _ref(claim_doc, claim_start),
                    "tag": "completion", "attribution": "model",
                    "rule_id": "R-COMP-02",
                    "evidence_step_refs": [
                        _ref(claim_doc, claim_start),
                        _ref(run["start_doc"], run["start"]),
                        _ref(run["end_doc"], run["end"]),
                    ],
                    "note": (
                        f"false completion claim from step {claim_start} while the "
                        f"verifier score is {reward} (scored={scored}); identical "
                        f"loop {run['start']}-{run['end']} until "
                        f"{stop_reason or stop_word}; the claim, not the harness, "
                        f"is wrong"
                    ),
                }
                grader = source_text_assertion(trial_dir)
                if grader:
                    out["grader_note"] = grader
                return out
        # Branch (b): no trailing identical claim run, but a trailing claim
        # regime (format-code: alternating prose_completion/calls
        # confirmation cycle, never 10 consecutive identical messages).
        regime = claim_regime(agent_seq)
        if regime and regime["steps"] >= LOOP_MIN_RUN:
            out = {
                "step_id": regime["start"],
                "step_ref": _ref(regime["start_doc"], regime["start"]),
                "tag": "completion", "attribution": "model",
                "rule_id": "R-COMP-02",
                "evidence_step_refs": [
                    _ref(regime["start_doc"], regime["start"]),
                    _ref(regime["end_doc"], regime["end"]),
                ],
                "note": (
                    f"false completion claim from step {regime['start']} while "
                    f"the verifier score is {reward} (scored={scored}); claim "
                    f"regime of {regime['steps']} steps to "
                    f"{_ref(regime['end_doc'], regime['end'])} until "
                    f"{stop_reason or stop_word}; the claim, not the harness, "
                    f"is wrong"
                ),
            }
            grader = source_text_assertion(trial_dir)
            if grader:
                out["grader_note"] = grader
            return out
        # Branch (c): no trailing regime either, but the model DID claim
        # completion (is_claim above) and the verifier contradicts it with
        # failing tests (000434: "The fix is complete" at head#31, then a
        # verification loop to the ceiling while 1 test fails; a4-1634
        # head#21, then edits until the timeout without confirming). The
        # later work reads as checking the claim, not new progress, so the
        # claim is the outcome-relevant failure.
        claim_positions = [
            (doc, step) for doc, step in agent_seq if is_claim(doc, step)
        ]
        if claim_positions:
            claim_passage = _verifier_passage(trial_dir)
            # Mocha-style numbered failures ("  1) produces YAML ...") when
            # the stdout parser yields no ctrf/test counts (000434).
            mocha_fails = [
                name.strip()[:100] for name in re.findall(
                    r"^\s*\d+\)\s+(.+)$",
                    str(claim_passage.get("stdout") or ""),
                    flags=re.MULTILINE,
                )[:10]
            ]
            claim_fails = list(claim_passage.get("failing_tests") or [])
            if (claim_passage.get("fails") or 0) > 0 and not claim_fails:
                claim_fails = [f"{claim_passage['fails']} failing (unnamed)"]
            if not claim_fails:
                claim_fails = mocha_fails
            if claim_fails:
                claim_doc, claim_step = claim_positions[0]
                out = {
                    "step_id": claim_step.get("step_id"),
                    "step_ref": _ref(claim_doc, claim_step.get("step_id")),
                    "tag": "completion", "attribution": "model",
                    "rule_id": "R-COMP-02",
                    "evidence_step_refs": [
                        _ref(claim_doc, claim_step.get("step_id")),
                    ],
                    "note": (
                        f"false completion claim at "
                        f"{_ref(claim_doc, claim_step.get('step_id'))} while "
                        f"{len(claim_fails)} verifier test(s) fail "
                        f"({', '.join(claim_fails[:3])}); the claim, not the "
                        f"harness, is wrong"
                    ),
                }
                grader = source_text_assertion(trial_dir)
                if grader:
                    out["grader_note"] = grader
                return out

    # Confirmed task_complete with reward 0 (passes return earlier): the
    # claim is either contradicted by failing tests -> R-COMP-02 (false
    # claim: 1634 broke a pinned signature with 5/6 passing, 000434), or a
    # submit-contract / deliverable matter -> R-COMP-03 with the gate-(b)
    # attribution (contract engagement first, then instruction coverage).
    if stop_reason == "task_complete_confirmed" and (completion_refs or []):
        verifier_message = ""
        try:
            verifier_result = json.loads(
                (trial_dir / "verifier" / "result.json").read_text(encoding="utf-8")
            )
            verifier_message = str(verifier_result.get("message") or "")
        except (OSError, ValueError):
            verifier_message = ""
        passage = _verifier_passage(trial_dir)
        grader_note = completion_grader_check(trial_dir, verifier_message)
        engaged = (
            contract["attempted_repro"] or contract["poc_reproduced"]
            or contract["contract_seen"]
        )
        first_claim = completion_refs[0]
        first_sid = None
        for doc, step in agent_seq:
            if _ref(doc, step.get("step_id")) == first_claim:
                first_sid = step.get("step_id")
                break
        if engaged and not contract["submitted"]:
            if contract["poc_reproduced"]:
                attribution = "model"
                contract_note = (
                    f"reproduced but never submitted (crash reproduced at "
                    f"{contract['poc_reproduced']})"
                )
            elif contract["attempted_repro"]:
                attribution = "model"
                contract_note = (
                    f"attempted reproduction but never reproduced it "
                    f"(attempt at {contract['attempted_repro']}) and never submitted"
                )
            else:
                attribution = "model"
                contract_note = (
                    f"knew the deliverable (contract seen at "
                    f"{contract['contract_seen']}) but never reproduced or submitted"
                )
            out = {
                "step_id": first_sid, "step_ref": first_claim,
                "tag": "completion", "attribution": attribution,
                "rule_id": "R-COMP-03",
                "evidence_step_refs": list(completion_refs),
                "note": (
                    f"harness-confirmed task_complete at "
                    f"{', '.join(completion_refs)} with no exception, yet the "
                    f"verifier score is {reward} (scored={scored})"
                    + (f": {verifier_message[:160]}" if verifier_message else "")
                    + f"; {contract_note}"
                ),
                "contract_seen": contract["contract_seen"],
                "attempted_repro": bool(contract["attempted_repro"]),
                "attempted_repro_ref": contract["attempted_repro"],
                "poc_reproduced": contract["poc_reproduced"],
                "submitted": bool(contract["submitted"]),
            }
            if grader_note:
                out["grader_note"] = grader_note
            return out
        if contract["submitted"]:
            out = {
                "step_id": first_sid, "step_ref": first_claim,
                "tag": "completion", "attribution": "unclear",
                "rule_id": "R-COMP-03",
                "evidence_step_refs": list(completion_refs),
                "note": (
                    f"harness-confirmed task_complete, yet the verifier score "
                    f"is {reward}; submit.sh invoked at "
                    f"{contract['submitted']} but the verifier reports not "
                    f"submitted"
                ),
                "contract_seen": contract["contract_seen"],
                "attempted_repro": bool(contract["attempted_repro"]),
                "attempted_repro_ref": contract["attempted_repro"],
                "poc_reproduced": contract["poc_reproduced"],
                "submitted": bool(contract["submitted"]),
            }
            if grader_note:
                out["grader_note"] = grader_note
            return out
        if (passage["fails"] or 0) > 0:
            out = {
                "step_id": first_sid, "step_ref": first_claim,
                "tag": "completion", "attribution": "model",
                "rule_id": "R-COMP-02",
                "evidence_step_refs": list(completion_refs),
                "note": (
                    f"harness-confirmed task_complete at "
                    f"{', '.join(completion_refs)}, but the verifier contradicts "
                    f"the claim ({passage['passes']}/{passage['total']} tests pass"
                    + (f": failing {', '.join(passage['failing_tests'])}"
                       if passage["failing_tests"] else "")
                    + ")"
                ),
            }
            notes = [note for note in (grader_note, source_text_assertion(trial_dir)) if note]
            if notes:
                out["grader_note"] = "; ".join(notes)
            return out
        attribution = "model" if grader_note is None else "unclear"
        out = {
            "step_id": first_sid, "step_ref": first_claim,
            "tag": "completion", "attribution": attribution,
            "rule_id": "R-COMP-03",
            "evidence_step_refs": list(completion_refs),
            "note": (
                f"harness-confirmed task_complete at "
                f"{', '.join(completion_refs)} with no exception, yet the "
                f"verifier score is {reward} (scored={scored})"
                + (f": {verifier_message[:160]}" if verifier_message else "")
            ),
            "contract_seen": contract["contract_seen"],
            "attempted_repro": bool(contract["attempted_repro"]),
            "attempted_repro_ref": contract["attempted_repro"],
            "poc_reproduced": contract["poc_reproduced"],
            "submitted": bool(contract["submitted"]),
        }
        if grader_note:
            out["grader_note"] = grader_note
        return out
    # A rejected turn explains the outcome only as a rejection storm (>=10
    # rejected turns: 0036-b/0036-d/0758-b/c/d) or when nothing after it
    # was accepted. A lone recovered rejection (1048 head#39, one malformed
    # duration) is a first_failure, not the reason the run failed.
    rejected_positions = [
        index for index, (doc, step) in enumerate(agent_seq)
        if info.get((doc, step.get("step_id")), {}).get("harness_accepted") == "false"
    ]
    rejected = None
    if rejected_positions:
        first_index = rejected_positions[0]
        accepted_after = any(
            info.get((doc, step.get("step_id")), {}).get("harness_accepted") == "true"
            for doc, step in agent_seq[first_index + 1:]
        )
        if len(rejected_positions) >= 10 or not accepted_after:
            doc, step = agent_seq[first_index]
            rejected = (doc, step.get("step_id"))
    if rejected is not None:
        doc, sid = rejected
        cell = info.get((doc, sid), {})
        cause = cell.get("rejection_cause")
        # R-TOOL-03: session-scoped normalizer loss -- the same native
        # signature accepted in earlier documents is rejected here
        # (0758-d: head 56/56 accepted; cont-1 177/178 extra_param:description).
        head_ok = sum(
            1 for d2, s2 in agent_seq
            if d2 != doc
            and info.get((d2, s2.get("step_id")), {}).get("native_signature")
            and info.get((d2, s2.get("step_id")), {}).get("harness_accepted") == "true"
        )
        same_doc_after = sum(
            1 for d2, s2 in agent_seq
            if d2 == doc and isinstance(s2.get("step_id"), int) and s2["step_id"] >= sid
            and info.get((d2, s2.get("step_id")), {}).get("native_signature")
            and info.get((d2, s2.get("step_id")), {}).get("harness_accepted") == "false"
        )
        if (
            cell.get("native_signature")
            and head_ok >= 10
            and same_doc_after >= 10
            and cause
            and cause.startswith("extra_param:")
        ):
            out = {
                "step_id": sid, "step_ref": _ref(doc, sid),
                "tag": "tool_use", "attribution": "harness",
                "rule_id": "R-TOOL-03",
                "evidence_step_refs": [_ref(doc, sid)],
                "note": (
                    f"native signature accepted {head_ok}x before the "
                    f"summarization/new-session boundary, then rejected "
                    f"{same_doc_after}x from this step (cause {cause}); stock "
                    f"feedback only says 'No valid JSON found', never naming the "
                    f"parameter, so the model could not self-correct"
                ),
                "engineering_flag": (
                    "normalizer allowlist (mimo_tool_calls.py:154, args <= "
                    "{command|keystrokes, duration}) rejects the extra `description` "
                    "param after summarization handoff while the same shape was "
                    "accepted before; feedback is uninformative"
                ),
            }
            if longest:
                out["secondary"] = (
                    f"error_recovery/unclear secondary: {longest['length']} identical "
                    f"retries {_ref(longest['start_doc'], longest['start'])}-"
                    f"{_ref(longest['end_doc'], longest['end'])} (feedback never "
                    f"named the cause)"
                )
                out["evidence_step_refs"] += [
                    _ref(longest["start_doc"], longest["start"]),
                    _ref(longest["end_doc"], longest["end"]),
                ]
            return out
        if cause in ("today_normalizer_accepts", "unmapped_native_function:command"):
            if cause == "unmapped_native_function:command":
                note = (
                    "bare native <function=command> with a plain command body; "
                    "the parser maps only {exec_command, bash, exec}, so the "
                    "unambiguous call is rejected as unmapped (harness gap; "
                    "today's normalizer still declines)"
                )
            else:
                note = (
                    "rejected turn that today's normalizer accepts -- harness gap, "
                    "since fixed by mimo_tool_calls.py (#512)"
                )
            out = {
                "step_id": sid, "step_ref": _ref(doc, sid), "tag": "tool_use",
                "attribution": "harness", "rule_id": "R-TOOL-01",
                "evidence_step_refs": [_ref(doc, sid)],
                "note": note,
            }
            if longest:
                out["secondary"] = (
                    f"error_recovery/model secondary: identical run "
                    f"{_ref(longest['start_doc'], longest['start'])}-"
                    f"{_ref(longest['end_doc'], longest['end'])}"
                )
                out["evidence_step_refs"] += [
                    _ref(longest["start_doc"], longest["start"]),
                    _ref(longest["end_doc"], longest["end"]),
                ]
            return out
        if cause and (
            cause.startswith("unknown_function:")
            or cause.startswith("extra_param:")
            or cause.startswith("malformed_native:")
        ):
            out = {
                "step_id": sid, "step_ref": _ref(doc, sid), "tag": "tool_use",
                "attribution": "model", "rule_id": "R-TOOL-02",
                "evidence_step_refs": [_ref(doc, sid)],
                "note": (
                    f"model-emitted surface the normalizer declines ({cause}); "
                    "A-NON-NATIVE-MODEL"
                ),
            }
            _attach_feedback(out, cell)
            if longest:
                out["secondary"] = (
                    f"error_recovery/model secondary: identical run "
                    f"{_ref(longest['start_doc'], longest['start'])}-"
                    f"{_ref(longest['end_doc'], longest['end'])}"
                )
                out["evidence_step_refs"] += [
                    _ref(longest["start_doc"], longest["start"]),
                    _ref(longest["end_doc"], longest["end"]),
                ]
            return out
        return {
            "step_id": sid, "step_ref": _ref(doc, sid), "tag": "tool_use",
            "attribution": "unclear", "rule_id": "R-TOOL-01U",
            "evidence_step_refs": [_ref(doc, sid)],
            "note": f"rejected turn, cause {cause or 'unknown'}; attribution unclear",
        }
    # R-PLAN-01: ceiling/timeout stop, not a pass, no R-ENV/R-COMP rule
    # fired (by position: rejections return above), and ZERO executed
    # file-changing ops on task paths (/tmp, /dev/null, bare redirects and
    # heredocs-to-/tmp excluded) -- 'no task edit before budget ran out'.
    # Identical loops stay secondary R-REC-01. The outcome tag records WHAT
    # failed deterministically; the WHY (wrong localization, API break)
    # lives in `mechanism`, hand-only.
    if limit_stop and not (scored and (reward or 0) >= 1.0):
        task_edits = []
        for doc, step in agent_seq:
            text = _step_command_text(doc, step, info)
            if (
                any(pattern.search(text) for pattern in FILE_CHANGE_RES)
                and _task_targets(text)
            ):
                task_edits.append(_ref(doc, step.get("step_id")))
        if not task_edits:
            # An absence claim cites the span it scanned, so every outcome
            # tag carries step refs (HAR-91 acceptance).
            scanned = [
                _ref(agent_seq[0][0], agent_seq[0][1].get("step_id")),
                _ref(agent_seq[-1][0], agent_seq[-1][1].get("step_id")),
            ] if agent_seq else []
            out = {
                "step_id": None, "step_ref": None, "tag": "planning",
                "attribution": "model", "rule_id": "R-PLAN-01",
                "evidence_step_refs": [],
                "note": (
                    "no task edit before budget ran out (zero executed "
                    "file-changing ops on task paths"
                    + (f"; scanned {scanned[0]}-{scanned[1]})" if scanned else ")")
                ),
                "scanned_step_refs": scanned,
                "task_file_edits": 0,
            }
            if longest:
                out["secondary"] = (
                    f"error_recovery/model secondary: identical run "
                    f"{_ref(longest['start_doc'], longest['start'])}-"
                    f"{_ref(longest['end_doc'], longest['end'])}"
                )
                out["evidence_step_refs"] += [
                    _ref(longest["start_doc"], longest["start"]),
                    _ref(longest["end_doc"], longest["end"]),
                ]
            if not out["evidence_step_refs"]:
                out["evidence_step_refs"] = list(scanned)
            return out
    if runs and _unsubmitted_keystroke_loop(longest, agent_seq, info):
        # A-CONCAT-UNCLEAR applied to the loop itself (0036 head#17-121): each
        # turn sends one command WITHOUT a trailing newline, the terminal
        # echoes it fused onto the previous copy (`head -40grep -rn ...`), so
        # nothing ever executes. The model saw the fusion and did not add the
        # newline; the harness sent known-bad keystrokes verbatim.
        return {
            "step_id": longest["start"],
            "step_ref": _ref(longest["start_doc"], longest["start"]),
            "tag": "tool_use", "attribution": "unclear",
            "rule_id": "R-TOOL-00",
            "evidence_step_refs": [
                _ref(longest["start_doc"], longest["start"]),
                _ref(longest["end_doc"], longest["end"]),
            ],
            "note": (
                f"identical turns {longest['start']}-{longest['end']} each send a "
                f"command without a trailing newline; the terminal echo shows it "
                f"fused onto the previous copy, so it never executed "
                f"(A-CONCAT-UNCLEAR: model omitted the newline, harness sent it verbatim)"
            ),
        }
    if runs:
        return {
            "step_id": longest["start"],
            "step_ref": _ref(longest["start_doc"], longest["start"]),
            "tag": "error_recovery", "attribution": "model",
            "rule_id": "R-REC-01",
            "evidence_step_refs": [
                _ref(longest["start_doc"], longest["start"]),
                _ref(longest["end_doc"], longest["end"]),
            ],
            "note": (
                f"identical message repeated steps "
                f"{longest['start']}-{longest['end']} with no change after errors"
            ),
        }
    return {
        "step_id": None, "step_ref": None, "tag": "unclear",
        "attribution": "unclear", "rule_id": "R-UNC-01",
        "evidence_step_refs": [], "note": "no rule fired; needs hand reading",
    }


# The failing pytest line of a membership assertion: `>   assert "<lit>" in x`.
FAILING_IN_ASSERT_RE = re.compile(r"^>\s+assert\s+(['\"])(.+?)\1\s+in\s+(\w+)", re.MULTILINE)


def source_text_assertion(trial_dir: Path) -> str | None:
    """GRADER-SRC-ASSERT: the verifier asserts literal source strings
    rather than behavior: either `read_text()` + `'...' in source` (0036-f),
    or the failing line `assert "<literal>" in <name>` where <name> was read
    from a file (`open(p).read()` / `.read_text()`; 1634 pins
    `def atomic(self, transaction_type=None, **kwargs):`). Also records
    whether that literal appears in instruction.md (a pinned implementation
    the instruction never states). Detected from the ctrf trace; recorded
    for Data's verifier audit, NOT R-ENV-02."""
    path = trial_dir / "verifier" / "ctrf.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tests = ((data.get("results") or {}).get("tests")) or []
    for test in tests:
        if not isinstance(test, dict) or test.get("status") == "passed":
            continue
        trace = str(test.get("trace") or "") + str(test.get("message") or "")
        failing = FAILING_IN_ASSERT_RE.search(trace)
        read_from_file = bool(failing) and bool(re.search(
            rf"\b{re.escape(failing.group(3))}\s*=\s*[^\n]*"
            r"(?:\.read_text\(|open\([^\n]*\)\.read\()",
            trace,
        ))
        if not (("read_text()" in trace and " in source" in trace) or read_from_file):
            continue
        note = (
            "source_text_assertion: failing verifier test asserts literal "
            f"source strings ({test.get('name')})"
        )
        if failing:
            literal = failing.group(2)
            instruction_text, _size = _instruction_text(trial_dir)
            if instruction_text is not None:
                where = "appears in" if literal in instruction_text else "is not in"
                note += f"; asserted literal {literal[:80]!r} {where} instruction.md"
        return note + "; grader audit flag, not R-ENV-02"
    return None


def grader_collection_error(trial_dir: Path) -> str | None:
    """R-ENV-02: grader failing at collection/import (HAR-81 suspect-grader
    rule). Returns evidence text or None."""
    stdout_path = trial_dir / "verifier" / "test-stdout.txt"
    if not stdout_path.is_file():
        return None
    try:
        haystack = stdout_path.read_text(errors="replace")[:6000]
    except OSError:
        return None
    for pattern in (
        "ModuleNotFoundError",
        "ImportError",
        "ERROR collecting",
        "collection error",
        "No module named",
    ):
        if pattern in haystack:
            return f"verifier/test-stdout.txt: {pattern}"
    return None


# ---------------------------------------------------------------------------
# Treatment keys (provisional until Data's HAR-93 key exists).
# ---------------------------------------------------------------------------

KEY_FIELDS = (
    "agent_name",
    "model_name",
    "temperature",
    "top_p",
    "reasoning_effort",
    "max_tokens",
    "proactive_summarization_threshold",
    "raw_content",
    "linear_history",
)


INFRA_PIN_FIELDS = (
    "setup_key",
    "treatment_key",
    "pinned_setup_key",
    "pinned_treatment_key",
    "trial_setup_key",
    "setup_sha",
    "treatment_sha",
)


def infra_pin(job_dir: Path) -> tuple[str | None, str | None]:
    """Setup key pinned by Infra in the job lab-metadata.json (second
    priority after HAR-93). Searches the metadata for known pin fields.
    Returns (key, field) or (None, None) when no pin field is present
    (current HAR-81/HAR-90 job metadata carries none, so har93 or auto
    applies; recorded either way)."""
    try:
        metadata = json.loads((job_dir / "lab-metadata.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None
    for node in _walk_dicts(metadata):
        if not isinstance(node, dict):
            continue
        for field in INFRA_PIN_FIELDS:
            value = node.get(field)
            if isinstance(value, str) and value:
                return value, field
    return None, None


def dispatch_key(trial_dir: Path) -> dict | None:
    """Infra's pinned dispatch treatment key for the worktree the trial
    ran from: `<worktree>/derived/<lane>/treatment-key.json` (HAR-81:
    0d4b7a40 on 7de1ce6e in har81-dispatch-528, 04b4e45c on 48b787b5 in
    har81-dispatch-531; `receipt pair` verified it per job). Exactly one
    such file must exist, else None."""
    worktree = trial_dir.parent.parent.parent
    candidates = sorted((worktree / "derived").glob("*/treatment-key.json"))
    if len(candidates) != 1:
        return None
    try:
        data = json.loads(candidates[0].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("key") or not isinstance(data.get("fields"), dict):
        return None
    return {"key": data["key"], "fields": data["fields"], "path": str(candidates[0])}


def display_name(row: dict) -> str:
    """Full-length trial display: job-dir basename plus the trial suffix.
    HAR-90 trial names already start with the job name (display equals the
    historic short form); HAR-81 trial names are truncated to 32 chars, so
    the job basename restores the full name
    (har81-p-d-candidate-1789-security-appsec__Nhf2HdJ)."""
    trial_base = Path(row["trial_dir"]).name
    job_base = Path(row["job_dir"]).name
    if trial_base.startswith(job_base):
        return trial_base.split("__")[0]
    suffix = trial_base.split("__")[-1] if "__" in trial_base else trial_base
    return f"{job_base}__{suffix}"


def provisional_key(trial_dir: Path, result: dict, config: dict) -> tuple[str, dict]:
    agent = config.get("agent") or {}
    kwargs = agent.get("kwargs") or {}
    llm_kwargs = kwargs.get("llm_call_kwargs") or {}
    extra_body = llm_kwargs.get("extra_body") or {}
    traj_config = kwargs.get("trajectory_config") or {}
    model_info = (result.get("agent_info") or {}).get("model_info") or {}
    metadata = (result.get("agent_result") or {}).get("metadata") or {}
    # The parser/normalizer version is NOT in config.json. Where metadata
    # exposes parser-era signals (prose_completions appears only on runs
    # with the newer parser), record them as key evidence; the TRUE key
    # waits for Data's HAR-93.
    normalizer_evidence = (
        "prose_completions_meta"
        if isinstance(metadata.get("prose_completions"), int)
        else "none"
    )
    fields = {
        "agent_name": agent.get("name") or (result.get("agent_info") or {}).get("name"),
        "model_name": agent.get("model_name") or model_info.get("name"),
        "temperature": kwargs.get("temperature"),
        "top_p": llm_kwargs.get("top_p"),
        "reasoning_effort": extra_body.get("reasoning_effort"),
        "max_tokens": llm_kwargs.get("max_tokens"),
        "proactive_summarization_threshold": kwargs.get("proactive_summarization_threshold"),
        "raw_content": traj_config.get("raw_content"),
        "linear_history": traj_config.get("linear_history"),
        "normalizer_evidence": normalizer_evidence,
    }
    if fields["temperature"] is not None or fields["top_p"] is not None:
        sampling = f"temp{fields['temperature']}-topp{fields['top_p']}"
    else:
        sampling = f"re-{fields['reasoning_effort']}"
    key = (
        f"{fields['agent_name']}|{fields['model_name']}|{sampling}"
        f"|mt{fields['max_tokens']}|psum{fields['proactive_summarization_threshold']}"
        f"|raw{fields['raw_content']}+linear{fields['linear_history']}"
        f"|normevidence:{normalizer_evidence}"
    )
    return key, fields


def load_har93_map(path: str) -> dict[str, str]:
    """Shape-tolerant HAR-93 override: {trial: key}, {"trials": {...}}, or
    [{trial/trial_name, key/treatment_key}]. Unknown shapes -> {} (caller
    falls back to auto per trial and says so)."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    mapping: dict[str, str] = {}
    if isinstance(raw, dict):
        candidates = raw.get("trials") if isinstance(raw.get("trials"), dict) else raw
        if isinstance(candidates, dict):
            for trial, key in candidates.items():
                if isinstance(key, str):
                    mapping[str(trial)] = key
                elif isinstance(key, dict):
                    for sub in ("key", "treatment_key", "treatment"):
                        if isinstance(key.get(sub), str):
                            mapping[str(trial)] = key[sub]
                            break
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            trial = entry.get("trial") or entry.get("trial_name") or entry.get("id")
            key = entry.get("key") or entry.get("treatment_key") or entry.get("treatment")
            if isinstance(trial, str) and isinstance(key, str):
                mapping[trial] = key


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_har93_treatment(path: str) -> dict:
    """HAR-93 (#519, 792d459d) trial_treatment.parquet -> per-trial dict
    {trial_name: {setup_key, complete, unknown_fields}}. Read-only."""
    parquet = Path(path)
    if not parquet.is_file():
        return {"_error": f"not a file: {path}"}
    try:
        import pyarrow.parquet as pq  # via `uv run --no-project --with pyarrow`
    except ImportError as exc:
        return {"_error": f"pyarrow unavailable ({exc}); rerun with --with pyarrow"}
    table = pq.read_table(parquet)
    rows = table.to_pylist()
    out: dict[str, dict] = {"_sha256": _sha256_file(parquet), "_path": str(parquet)}
    for row in rows:
        trial = row.get("trial_name")
        if not trial:
            continue
        out[str(trial)] = {
            "setup_key": row.get("setup_key") or row.get("treatment_key") or row.get("key"),
            "complete": row.get("complete"),
            "unknown_fields": row.get("unknown_fields"),
            "treatment_key": row.get("treatment_key"),
            "row": {key: value for key, value in row.items()},
        }
    return out


def load_har93_capture(path: str) -> dict:
    """HAR-93 trial_capture.parquet -> {trial_name: capture fields}."""
    parquet = Path(path)
    if not parquet.is_file():
        return {"_error": f"not a file: {path}"}
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        return {"_error": f"pyarrow unavailable ({exc}); rerun with --with pyarrow"}
    rows = pq.read_table(parquet).to_pylist()
    fields = (
        "continuations_missing",
        "proxy_input_tokens",
        "trajectory_input_tokens",
        "input_tokens_unattributed",
    )
    out: dict[str, dict] = {"_sha256": _sha256_file(parquet), "_path": str(parquet)}
    for row in rows:
        trial = row.get("trial_name")
        if not trial:
            continue
        out[str(trial)] = {field: row.get(field) for field in fields}
    return out


# ---------------------------------------------------------------------------
# Per-trial analysis.
# ---------------------------------------------------------------------------

EXCEPTION_STOP = {
    "AgentTimeoutError": "agent_timeout",
    "RateLimitError": "trial_budget_exhausted",
    "AuthenticationError": "model_auth_error",
}


def analyze_trial(
    trial_dir: Path,
    provenance: dict,
    normalize_fn: object | None,
    har93_treatment: dict | None,
    har93_capture: dict | None,
    har93_source: str | None,
) -> dict:
    result = probe02._read_json(trial_dir / "result.json")
    config = probe02._read_json(trial_dir / "config.json")
    reward, scored, reward_source = probe02.read_reward(trial_dir)
    exception = result.get("exception_info")
    if isinstance(exception, dict):
        exc_type = (
            exception.get("className")
            or exception.get("exception_type")
            or exception.get("type")
        )
        exc_message = str(exception.get("exception_message") or "")[:300]
    elif exception is None:
        exc_type, exc_message = None, ""
    else:
        exc_type, exc_message = str(exception)[:80], str(exception)[:300]

    agent_result = result.get("agent_result") or {}
    metadata = agent_result.get("metadata") or {}
    n_episodes = metadata.get("n_episodes")
    summarization_count = metadata.get("summarization_count")
    timed_out = exc_type == "AgentTimeoutError"
    stop_reason = EXCEPTION_STOP.get(exc_type or "", "unknown")
    if exc_type == "TrialBudgetExhaustedError":
        stop_reason = ceiling_which(
            trial_dir.parent,
            agent_result.get("n_input_tokens"),
            agent_result.get("n_output_tokens"),
            metadata.get("n_episodes"),
        )
    limit_stop = stop_reason == "agent_timeout" or stop_reason.startswith("ceiling")

    coverage, assembled = assemble_trial(trial_dir)
    agent_seq = [
        (doc, step)
        for doc, step in assembled
        if str(step.get("source", "")).lower() in probe02.AGENT_SOURCES
    ]

    info: dict[tuple[str, int], dict] = {}
    shape_counts: dict[str, int] = {}
    acc_counts = {"true": 0, "false": 0, "unknown": 0}
    prov_counts = {"recorded": 0, "inferred": 0, "reconstructed": 0}
    agreement = {"agree_accept": 0, "agree_reject": 0, "disagree": 0,
                 "inferred_unknown": 0}
    calls_reconstructed = 0
    newline_warn_accepted = 0
    rejection_causes: dict[str, dict] = {}
    standins: dict[str, dict] = {}
    model_seq: list[tuple[str, dict]] = []
    native_functions: dict[str, int] = {}
    for doc, step in agent_seq:
        message = step.get("message") if isinstance(step.get("message"), str) else ""
        is_standin = message.strip() == HARNESS_STANDIN
        layer = layer_status(step)
        try:
            normalized = normalize_fn(message) if normalize_fn else None
        except Exception:  # noqa: BLE001 -- one bad message must not kill the trial
            normalized = None
        replay_calls = normalizer_command_count(normalized)
        content = obs_content(step)
        inferred, infer_rule = harness_accepted(content)
        recorded = recorded_acceptance(layer)
        if layer is not None and recorded is not None:
            # Recorded layers are authoritative for acceptance; the
            # observation-inferred value feeds the agreement cross-check.
            accepted, acc_rule = recorded, f"RECORDED:{layer.get('kind')}"
            provenance = "recorded"
            n_calls = recorded_call_count(layer)
            if inferred == "unknown":
                agreement["inferred_unknown"] += 1
            elif inferred == accepted:
                agreement["agree_accept" if accepted == "true" else "agree_reject"] += 1
            else:
                agreement["disagree"] += 1
        elif inferred != "unknown":
            accepted, acc_rule = inferred, infer_rule
            provenance = "inferred"
            n_calls = replay_calls
        else:
            accepted, acc_rule = "unknown", infer_rule
            provenance = "reconstructed"
            n_calls = replay_calls
        prov_counts[provenance] += 1
        shape, _shape_rule = classify_shape(message, n_calls)
        cause = None
        if is_standin:
            cause = "harness_standin"
        elif accepted == "false":
            cause = classify_rejection_cause(message, n_calls)
        feedback = (layer or {}).get("parse_error")
        if not isinstance(feedback, str) or not feedback.strip():
            feedback = None
        info[(doc, step.get("step_id"))] = {
            "shape": shape,
            "n_calls": n_calls,
            "harness_accepted": accepted,
            "acc_rule": acc_rule,
            "acceptance_provenance": provenance,
            "layer": layer,
            "rejection_cause": cause,
            "harness_feedback": feedback,
            "feedback_names_problem": feedback_names_problem(feedback, message),
            "harness_standin": is_standin,
            "native_signature": NATIVE_SIGNATURE_RE.search(message) is not None,
            "concat_warning": concat_defect(message, content),
            "keystrokes": (
                layer["keystrokes_sent"] if layer is not None
                else _replay_keystrokes(normalized, message)
            ),
            "keystroke_source": "recorded" if layer is not None else "replay",
        }
        if cause:
            bucket = rejection_causes.setdefault(doc, {})
            entry = bucket.setdefault(cause, {"count": 0, "first_step": step.get("step_id")})
            entry["count"] += 1
        if is_standin:
            # STANDIN: Harbor's stand-in reply, not model output -- counted
            # separately, excluded from model-behavior counts and loops.
            record = standins.setdefault(
                doc, {"count": 0, "first": step.get("step_id"), "last": step.get("step_id")}
            )
            record["count"] += 1
            record["last"] = step.get("step_id")
            continue
        model_seq.append((doc, step))
        shape_counts[shape] = shape_counts.get(shape, 0) + 1
        acc_counts[accepted] = acc_counts.get(accepted, 0) + 1
        calls_reconstructed += n_calls
        for name in _FUNCTION_RE.findall(message):
            native_functions[name] = native_functions.get(name, 0) + 1
        if info[(doc, step.get("step_id"))]["concat_warning"]:
            newline_warn_accepted += 1

    last_metrics = coverage["last_doc_final_metrics"] or {}
    tokens_traj = {
        "prompt": last_metrics.get("total_prompt_tokens"),
        "completion": last_metrics.get("total_completion_tokens"),
        "cached": last_metrics.get("total_cached_tokens"),
    }
    tokens_result = {
        "input": agent_result.get("n_input_tokens"),
        "output": agent_result.get("n_output_tokens"),
    }
    tokens_match = (
        tokens_traj["prompt"] == tokens_result["input"]
        and tokens_traj["completion"] == tokens_result["output"]
        if isinstance(tokens_traj["prompt"], int)
        and isinstance(tokens_result["input"], int)
        else None
    )

    runs, trailing = _runs_and_trailing(model_seq)
    completion_refs = task_complete_refs(model_seq, info)
    if exc_type is None and completion_refs:
        # Natural completion: no exception and the harness recorded a
        # task_complete acceptance (arvo-18737 #18 AND #19).
        stop_reason = "task_complete_confirmed"
    grader_ev = suspect_grader_evidence(trial_dir, NOP_RUNS_DIR)
    first = compute_first_failure(model_seq, info, grader_ev)
    outcome = compute_outcome_failure(
        model_seq, info, scored, reward, timed_out, runs, trailing, trial_dir,
        stop_reason=stop_reason, limit_stop=limit_stop,
        completion_refs=completion_refs, grader_ev=grader_ev,
    )
    livelock = context_livelock(trial_dir)
    # Research-Harbor ruling (HAR-91 05:11, verified 05:43): a summarization
    # livelock is the OUTCOME failure (context/harness) -- steps after the
    # first cycle ran under degraded context, so a rejection loop inside them
    # (0758-b/c R-TOOL-01) stays first_failure. Only a prose-completion
    # outcome (R-COMP-01) or an infra/grader outcome (R-ENV-*) is kept.
    if livelock and outcome["rule_id"] not in ("R-COMP-01", "R-ENV-01", "R-ENV-02"):
        boundary = (
            _ref(trailing[0]["start_doc"], trailing[0]["start"])
            if trailing
            else None
        )
        outcome = {
            "step_id": None,
            "step_ref": boundary,
            "tag": "context", "attribution": "harness",
            "rule_id": "R-CTX-01",
            "evidence_step_refs": [boundary] if boundary else [],
            "note": (
                f"context livelock: trial.log lines {livelock['log_lines']} repeat "
                f"'Context length exceeded -> full summary failed -> short summary "
                f"succeeded -> Even fallback chat failed' ({livelock['cycles']} "
                f"attempts per metadata), each unwinding to the same state; steps "
                f"after the first cycle were produced under degraded context and "
                f"are not attributed to the model (incl. harness stand-ins)"
            ),
            "secondary": (
                f"pre-livelock failure kept as first_failure: "
                f"{(first or {}).get('rule_id')} at {(first or {}).get('step_ref')}"
            ),
            "engineering_flag": (
                "old config (max_tokens 8192) summarization livelock: full summary "
                "and fallback chat fail, short summary succeeds but context "
                "immediately overflows again; loop of summarizations never "
                "terminates until timeout"
            ),
        }
    if outcome["rule_id"] == "R-UNC-01":
        grader_hit = grader_collection_error(trial_dir)
        if grader_hit:
            outcome = {
                "step_id": None, "step_ref": None, "tag": "environment",
                "attribution": "harness", "rule_id": "R-ENV-02",
                "evidence_step_refs": [], "note": f"suspect grader: {grader_hit}",
            }
        elif scored and (reward or 0) >= 1.0:
            outcome = {
                "step_id": None, "step_ref": None, "tag": "none",
                "attribution": "n/a", "rule_id": "R-NONE-01",
                "evidence_step_refs": [], "note": "passed cleanly",
            }
    # Secondary patterns never replace the outcome rule.
    secondary = []
    if outcome["rule_id"] == "R-COMP-02":
        bad = [
            f"{doc}#{step.get('step_id')}" for doc, step in agent_seq
            if info.get((doc, step.get("step_id")), {}).get("harness_accepted") == "false"
            and info.get((doc, step.get("step_id")), {}).get("n_calls", 0) == 0
        ][:6]
        if bad:
            secondary.append(
                "transient non-native rejections (SHAPE-XML-BROKEN, nothing to "
                f"recover) at {','.join(bad)}; R-TOOL-02 covers the first of these"
            )
    if (
        coverage["summarization_files"] or coverage["cont_files"]
    ) and outcome["rule_id"] != "R-CTX-01":
        secondary.append(
            "context markers present (see coverage/stop) but not the outcome rule"
        )
    # R-COMP-04 confirmation_with_call (secondary, never outcome): computed
    # for every trial; attached as a secondary string (existing `secondary`
    # slot when free, else `secondary_comp04`), and always as the row-level
    # `confirmation_loop` field.
    regime = claim_regime(model_seq)
    loop = confirmation_loop(model_seq, info, regime)
    if loop:
        if outcome["rule_id"] == "R-COMP-02":
            outcome["confirmation_loop"] = loop

        loop_str = (
            f"confirmation_with_call loop ({loop['detector']}): first "
            f"confirmation {loop['first_confirmation_ref']}, span "
            f"{loop['loop_span'][0]}-{loop['loop_span'][1]}; "
            f"attribution unclear (confirm vs continue ambiguous)"
        )
        if outcome.get("secondary"):
            outcome["secondary_comp04"] = loop_str
        else:
            outcome["secondary"] = loop_str
        secondary.append(loop_str)
    # R-UNC-01 progress pointer on timeouts/ceilings.
    if outcome["rule_id"] == "R-UNC-01" and limit_stop:
        outcome["progress"] = progress_block(model_seq, info, metadata, trial_dir)
        last_edit = outcome["progress"].get("last_file_changing_step")
        if last_edit and not outcome["evidence_step_refs"]:
            outcome["evidence_step_refs"] = [last_edit]
    if (first or {}).get("rule_id") == "R-UNC-01" and limit_stop and "progress" not in outcome:
        first["progress"] = progress_block(model_seq, info, metadata, trial_dir)
    for failure in [f for f in (first, outcome) if f]:
        if failure.get("rule_id") in ("R-COMP-01", "R-COMP-02"):
            refs = failure["evidence_step_refs"]
            loop_prompt = loop_completion = 0
            if len(refs) >= 2:
                try:
                    start_doc = refs[0].split("#")[0]
                    start_n = int(refs[0].split("#")[1])
                    end_doc = refs[-1].split("#")[0]
                    end_n = int(refs[-1].split("#")[1])
                    for doc, step in agent_seq:
                        sid = step.get("step_id")
                        if (
                            doc == start_doc == end_doc
                            and isinstance(sid, int)
                            and start_n <= sid <= end_n
                        ):
                            step_metrics = step.get("metrics") or {}
                            if isinstance(step_metrics, dict):
                                prompt = step_metrics.get("prompt_tokens")
                                completion = step_metrics.get("completion_tokens")
                                if isinstance(prompt, (int, float)):
                                    loop_prompt += int(prompt)
                                if isinstance(completion, (int, float)):
                                    loop_completion += int(completion)
                    failure["loop_token_cost"] = {
                        "prompt": loop_prompt,
                        "completion": loop_completion,
                    }
                except (ValueError, IndexError):
                    pass

    loop_cost = loop_token_cost(model_seq, runs, loop, tokens_result)
    handshake = completion_handshake(model_seq, stop_reason)
    wedge = wedged_terminal(model_seq, info)
    # R-REC-02 (secondary, never an outcome): a wedged stretch the model
    # never interrupted. The harness sent exactly the model's keystrokes;
    # getting the shell back (C-c, q) is the model's recovery to make.
    unrecovered = [s for s in wedge["stretches"] if not s["interrupt_ref"]]
    if unrecovered:
        outcome["secondary_wedge"] = "error_recovery/model secondary (R-REC-02 wedged terminal): " + "; ".join(
            f"{s['cause']} after {s['trigger_ref']} (`{s['trigger_command'][:60]}`), "
            f"{s['turns']} turns {s['start_ref']}-{s['end_ref']} with no interrupt, "
            + ("until run end" if s["until_run_end"] else "prompt back after the command finished"
               if s["prompt_returned"] else "ended without a prompt")
            for s in unrecovered
        )

    trial_name = (
        result.get("trial_name") or config.get("trial_name") or trial_dir.name
    )
    task_name = result.get("task_name") or "unknown"
    key_auto, key_fields = provisional_key(trial_dir, result, config)
    har93_row = None
    if har93_treatment and trial_name in har93_treatment:
        har93_row = har93_treatment[trial_name]
    pin_key, pin_field = infra_pin(trial_dir.parent)
    dispatch = dispatch_key(trial_dir)
    # The pinned dispatch key is the treatment Research-Harbor rules on
    # (HAR-81 09:27 equivalence) and covers every trial of its worktree;
    # HAR-93 covers wave A only and keys per domain, so it would split
    # attempt 1 from attempts 2-4. HAR-93's row stays attached as a
    # cross-check wherever it exists.
    if dispatch:
        treatment_key = dispatch["key"]
        key_source = f"infra-dispatch:{Path(dispatch['path']).parent.name}/treatment-key.json"
    elif har93_row:
        treatment_key = har93_row.get("setup_key") or key_auto
        key_source = "har93"
    elif pin_key:
        treatment_key, key_source = pin_key, f"infra-pin:{pin_field}"
    elif har93_treatment:
        treatment_key, key_source = key_auto, "auto-fallback (absent from har93 table)"
    else:
        treatment_key, key_source = key_auto, "auto-provisional"

    agent_ids = [step.get("step_id") for _doc, step in agent_seq]
    covered = {"min": None, "max": None, "count": len(agent_ids)}
    if agent_ids and all(isinstance(i, int) for i in agent_ids):
        covered = {"min": min(agent_ids), "max": max(agent_ids), "count": len(agent_ids)}
    tech_tail = 0
    for _doc, step in reversed(agent_seq):
        if str(step.get("message", "")).strip() == (
            "Technical difficulties. Please continue with the task."
        ):
            tech_tail += 1
        else:
            break
    system_handoffs = [
        f"{doc}#{step.get('step_id')}"
        for doc, step in assembled
        if str(step.get("source", "")).lower() == "system"
    ]
    passage = _verifier_passage(trial_dir)
    pass_caveats: list[str] | None = None
    if outcome["rule_id"] == "R-NONE-01":
        pass_caveats = []
        pip_pkgs: dict[str, str] = {}
        for doc, step in model_seq:
            text = _step_command_text(doc, step, info)
            for match in PIP_INSTALL_RE.finditer(text):
                pkg = match.group(2).strip().strip("'\"").lower()
                if pkg and not pkg.startswith("-"):
                    pip_pkgs.setdefault(pkg.split("[")[0], _ref(doc, step.get("step_id")))
        # A passing trial has no suspect-grader evidence, so resolve the
        # same-task nop control directly (2684: pip install stevedore at
        # head#33; the nop fails on the missing stevedore module).
        _nop_trial, nop_stdout = _resolve_nop_trial(trial_dir, NOP_RUNS_DIR)
        if nop_stdout:
            missing = {
                (group[0] or group[1]).strip("'\"").lower()
                for group in MISSING_MODULE_RE.findall(nop_stdout)
                if (group[0] or group[1])
            }
            for pkg in sorted(pip_pkgs):
                if pkg in missing or pkg.replace("-", "_") in {
                    module.replace("-", "_") for module in missing
                }:
                    pass_caveats.append(
                        f"env_remediated: pip install {pkg} at {pip_pkgs[pkg]} "
                        f"(network) while nop fails on missing {pkg}"
                    )
        nparse = sum(
            1 for doc, step in model_seq
            if info.get((doc, step.get("step_id")), {}).get("harness_accepted") == "false"
        )
        if nparse >= 2:
            pass_caveats.append(f"parse_errors: {nparse}")
        # A pass whose tokens went mostly to loops (a2-001520: 75 identical
        # completion claims after the fix, 91%) is a poor SFT sample as-is.
        share = loop_cost.get("loop_prompt_share")
        if share is not None and share >= 0.25:
            spans = ", ".join(
                f"{span['span'][0]}-{span['span'][1]}" for span in loop_cost["spans"]
            )
            pass_caveats.append(f"loop_heavy: {share:.0%} of prompt tokens in loops ({spans})")
    row: dict = {
        "trial": trial_name,
        "trial_dir": str(trial_dir),
        "job_dir": str(trial_dir.parent),
        "task": task_name,
        "no_signal": not agent_seq,
        "verifier": {
            "scored": scored, "reward": reward, "source": reward_source,
            "guard_reject": passage["guard_reject"],
        },
        "stop": {
            "reason": stop_reason,
            "exception_type": exc_type,
            "exception_message": exc_message,
            "natural_completion": stop_reason == "task_complete_confirmed",
            "task_complete_refs": completion_refs,
            "summarization_markers": {
                "summarization_files": len(coverage["summarization_files"]),
                "summarization_count": summarization_count,
                "cont_files": len(coverage["cont_files"]),
                "tech_difficulties_tail": tech_tail,
                "system_handoffs": system_handoffs,
            },
        },
        "coverage": {
            **coverage,
            "agent_turns_read": len(agent_seq),
            "model_turns_excluding_standins": len(model_seq),
            "n_episodes_result": n_episodes,
            "steps_covered": covered,
            "steps_vs_episodes": (
                f"{covered['count']}/{n_episodes}"
                if isinstance(n_episodes, int)
                else f"{covered['count']}/unknown"
            ),
            "tokens_last_doc_cumulative": tokens_traj,
            "tokens_result_totals": tokens_result,
            "tokens_cumulative_match": tokens_match,
            **_capture_fields(har93_capture, trial_name),
        },
        "coverage_notes": [
            *(coverage.get("notes") or []),
            *(_capture_fields(har93_capture, trial_name).get("notes") or []),
        ],
        "first_failure": first,
        "outcome_relevant_failure": outcome,
        "confirmation_loop": loop,
        "loop_cost": loop_cost,
        "completion_handshake": handshake,
        "wedge": wedge,
        "reconstruction": {
            "steps_processed": len(model_seq),
            "calls_reconstructed": calls_reconstructed,
            "by_shape": shape_counts,
            "harness_accepted": acc_counts,
            "accepted_with_newline_warnings": newline_warn_accepted,
            "native_functions": native_functions,
            "rejection_causes": rejection_causes,
            "harness_standins": standins,
            "acceptance_provenance": prov_counts,
            "acceptance_agreement": agreement,
        },
        "secondary_patterns": secondary,
        "treatment": {
            "key": treatment_key,
            "key_source": key_source,
            "fields": key_fields,
            "har93": har93_row,
            "dispatch": dispatch,
        },
    }
    if pass_caveats is not None:
        row["pass_caveats"] = pass_caveats
    return row


def _capture_fields(har93_capture: dict | None, trial_name: str) -> dict:
    if not har93_capture or trial_name not in har93_capture:
        return {}
    row = har93_capture[trial_name]
    proxy = row.get("proxy_input_tokens")
    traj = row.get("trajectory_input_tokens")
    share = (
        round(traj / proxy, 4)
        if isinstance(proxy, (int, float)) and isinstance(traj, (int, float)) and proxy
        else None
    )
    fields = {
        "har93_capture": {
            **row,
            "token_attributed_share": share,
        },
    }
    missing = row.get("continuations_missing")
    if missing:
        # Reconciliation: HAR-93's "continuations missing" verbatim. The
        # ledger's cont-N numbering counts split attempts, so missing numbers
        # need not correspond to trajectory.cont-N.json files on disk -- see
        # coverage/cont_files for which episodes were actually read.
        fields["notes"] = [
            f"har93_capture lists continuations {missing[0]}-{missing[-1]} "
            f"({len(missing)}) as missing (Data's table, verbatim)"
        ]
    return fields


# ---------------------------------------------------------------------------
# Outputs.
# ---------------------------------------------------------------------------

LABEL_COLUMNS = (
    "| trial | what the agent tried | where it went wrong | "
    "failure label (free text) | is the task at fault (y/n/unsure) | "
    "evidence line | proposed tag | attribution | evidence steps |"
)


def write_jsonl(rows: list[dict], path: Path) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

EQUIV_IGNORE_COLUMNS = frozenset({
    "job_name", "trial_name", "trial_dir",
    "complete", "unknown_fields",
    "repository_commit", "produced_at", "field_sources",
})


def commit_in_set(commit, commit_set) -> bool:
    """A parquet repository_commit is in the declared set when it matches a
    member by prefix in either direction (full SHAs and short prefixes both
    work)."""
    if not isinstance(commit, str):
        return False
    return any(
        commit.startswith(prefix) or prefix.startswith(commit)
        for prefix in commit_set
        if isinstance(prefix, str) and prefix
    )


def rows_equivalent(row_a, row_b, commit_set) -> tuple[bool, str]:
    """Same-task HAR-93 rows are equivalent iff every treatment column
    matches except repository_commit/produced_at/field_sources, AND both
    commits are in the declared set. Returns (equivalent, reason)."""
    if not commit_set:
        return False, "no declared commit set"
    left, right = (row_a or {}), (row_b or {})
    if not left or not right:
        return False, "missing HAR-93 row (not key_source=har93)"
    for key in set(left) | set(right):
        if key in EQUIV_IGNORE_COLUMNS:
            continue
        if left.get(key) != right.get(key):
            return False, f"column {key} differs"
    commit_a, commit_b = left.get("repository_commit"), right.get("repository_commit")
    if not (commit_in_set(commit_a, commit_set) and commit_in_set(commit_b, commit_set)):
        return False, f"commits {commit_a}/{commit_b} not in declared set"
    short_a = str(commit_a)[:12]
    short_b = str(commit_b)[:12]
    return True, f"all treatment columns match; commits {short_a}/{short_b} in declared set"


def treatment_equivalent(treat_a: dict, treat_b: dict, commit_set) -> tuple[bool, str]:
    """Declared equivalence between two rows' treatments. Infra dispatch
    keys (HAR-81 treatment-key.json) are equivalent iff every field matches
    except eval_lab_commit and both commits are in the declared set;
    otherwise the HAR-93 row comparison applies."""
    dispatch_a, dispatch_b = treat_a.get("dispatch"), treat_b.get("dispatch")
    if dispatch_a and dispatch_b:
        if not commit_set:
            return False, "no declared commit set"
        fields_a = dict(dispatch_a.get("fields") or {})
        fields_b = dict(dispatch_b.get("fields") or {})
        commit_a = fields_a.pop("eval_lab_commit", None)
        commit_b = fields_b.pop("eval_lab_commit", None)
        differing = sorted(k for k in set(fields_a) | set(fields_b) if fields_a.get(k) != fields_b.get(k))
        if differing:
            return False, f"dispatch fields {differing} differ"
        if not (commit_in_set(commit_a, commit_set) and commit_in_set(commit_b, commit_set)):
            return False, f"commits {commit_a}/{commit_b} not in declared set"
        return True, (
            f"dispatch keys differ only in eval_lab_commit; commits "
            f"{str(commit_a)[:12]}/{str(commit_b)[:12]} in declared set"
        )
    return rows_equivalent(
        (treat_a.get("har93") or {}).get("row"),
        (treat_b.get("har93") or {}).get("row"),
        commit_set,
    )


EVALAB_REPO = Path("/Users/petermakhnatch/Developer/eval-lab")


def resolve_equivalence(commit_arg, source) -> dict | None:
    """--commit-equivalent prefixes + --equivalence-source string ->
    {commits, resolved, source}. Full SHAs via git rev-parse in the eval-lab
    repo (best effort; prefixes still match by prefix either way)."""
    if not commit_arg:
        return None
    commits = [part.strip() for part in str(commit_arg).split(",") if part.strip()]
    if not commits:
        return None
    resolved = []
    for prefix in commits:
        full = prefix
        try:
            proc = subprocess.run(
                ["git", "-C", str(EVALAB_REPO), "rev-parse", prefix],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                full = proc.stdout.strip()
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        resolved.append(full)
    return {"commits": commits, "resolved": resolved, "source": source}


def learnability_groups(rows: list[dict], equiv: dict | None = None) -> list[dict]:
    """Cluster trials by (task, treatment key). Excluded from attempts:
    trials with zero model turns (no_signal, R-ENV-01), trials whose
    outcome is a suspect grader (R-ENV-02: the verifier fails at setup, so
    a correct fix still scores 0), and declared tainted passes
    (`pass_tainted`: the reward stays 1.0 in the record, but the pass is
    no learning evidence); all three are listed separately. Status:
    `learnable` = 2+ scored attempts with a pass and a fail; `all-pass` /
    `all-fail` = 2+ scored attempts with one outcome; else `unknown`
    (`grader-suspect` / `pass-tainted` / `no-signal` when every trial was
    excluded).
    Declared key equivalence (equiv={commits, source}) merges same-task
    keys per `treatment_equivalent`, with the merge recorded. Never merged
    otherwise."""
    equiv = equiv or {}
    commit_set = equiv.get("commits") or []
    source = equiv.get("source")
    by_task: dict[str, list[dict]] = {}
    for row in rows:
        by_task.setdefault(row["task"], []).append(row)
    out = []
    for task in sorted(by_task):
        members = by_task[task]
        parent = list(range(len(members)))

        def find(i, _parent=parent):
            while _parent[i] != i:
                _parent[i] = _parent[_parent[i]]
                i = _parent[i]
            return i

        def union(i, j, _parent=parent):
            found_i, found_j = find(i), find(j)
            if found_i != found_j:
                _parent[found_i] = found_j

        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                if members[i]["treatment"]["key"] == members[j]["treatment"]["key"]:
                    union(i, j)
                elif commit_set:
                    equivalent, _reason = treatment_equivalent(
                        members[i]["treatment"], members[j]["treatment"], commit_set
                    )
                    if equivalent:
                        union(i, j)
        clusters: dict[int, list[dict]] = {}
        for i, member in enumerate(members):
            clusters.setdefault(find(i), []).append(member)
        for cluster in clusters.values():
            no_signal = [m["trial"] for m in cluster if m.get("no_signal")]
            grader_suspect = [
                m["trial"] for m in cluster
                if not m.get("no_signal")
                and m["outcome_relevant_failure"]["rule_id"] == "R-ENV-02"
            ]
            tainted = [
                m["trial"] for m in cluster
                if not m.get("no_signal") and m.get("pass_tainted")
            ]
            signal = [
                m for m in cluster
                if not m.get("no_signal")
                and m["outcome_relevant_failure"]["rule_id"] != "R-ENV-02"
                and not m.get("pass_tainted")
            ]
            attempts = len(signal)
            scored_rows = [m for m in signal if m["verifier"]["scored"]]
            passes = [m for m in scored_rows if (m["verifier"]["reward"] or 0) >= 1.0]
            fails = [m for m in scored_rows if (m["verifier"]["reward"] or 0) < 1.0]
            scored_n = len(scored_rows)
            if attempts == 0:
                learnable: bool | None = None
                if grader_suspect:
                    status = "grader-suspect"
                elif tainted:
                    status = "pass-tainted"
                else:
                    status = "no-signal"
            elif attempts <= 1 or scored_n == 0:
                learnable = None
                status = "unknown"
            elif passes and fails:
                learnable, status = True, "learnable"
            elif passes:
                learnable, status = False, "all-pass"
            else:
                learnable, status = False, "all-fail"
            keys = sorted({m["treatment"]["key"] for m in cluster})
            merged = len(keys) > 1
            merge_reason: str | None = None
            if merged:
                pair_reason = None
                for i in range(len(cluster)):
                    for j in range(i + 1, len(cluster)):
                        if cluster[i]["treatment"]["key"] == cluster[j]["treatment"]["key"]:
                            continue
                        equivalent, reason = treatment_equivalent(
                            cluster[i]["treatment"], cluster[j]["treatment"], commit_set
                        )
                        if equivalent:
                            pair_reason = reason
                            break
                    if pair_reason:
                        break
                merge_reason = (
                    f"declared equivalence ({pair_reason}; "
                    f"set={','.join(commit_set)}, source={source})"
                    if pair_reason
                    else f"same task, distinct keys ({source})"
                )
            out.append(
                {
                    "task": task,
                    "treatment_keys": keys,
                    "keys_merged": merged,
                    "merge_reason": merge_reason,
                    "merge_source": source if merged else None,
                    "attempts": attempts,
                    "scored": scored_n,
                    "passes": len(passes),
                    "fails": len(fails),
                    "pass_rate_scored": (len(passes) / scored_n) if scored_n else None,
                    "learnable": learnable,
                    "status": status,
                    "trials": [m["trial"] for m in signal],
                    "no_signal_trials": no_signal,
                    "grader_suspect_trials": grader_suspect,
                    "tainted_trials": tainted,
                }
            )
    return out


def _names(rows, tag, attribution) -> str:
    return ", ".join(
        f"`{display_name(r)}`"
        for r in rows
        if (
            r["outcome_relevant_failure"]["tag"],
            r["outcome_relevant_failure"]["attribution"],
        )
        == (tag, attribution)
    )


def load_hand_key(path: str) -> dict:
    """Parent-owned hand answer key (read-only): {job: row}."""
    keyed: dict[str, dict] = {}
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if row.get("job"):
                    keyed[row["job"]] = row
    except (OSError, ValueError) as exc:
        return {"_error": f"hand key unreadable ({exc})"}
    return keyed


def validate_against_hand(rows: list[dict], hand: dict) -> dict:
    """Join tagger rows to the hand key on job-dir basename. Agreement on
    (a) outcome tag, (b) outcome attribution, (c) stop reason, (d) outcome
    rule id. Disagreements carry the hand `mechanism`. Never tunes rules;
    residual disagreements are reported honestly."""
    dims = ("tag", "attribution", "stop", "rule")
    counts = {dim: {"agree": 0, "total": 0} for dim in dims}
    disagreements: list[dict] = []
    compared = 0
    for row in rows:
        job = Path(row["job_dir"]).name
        expected = hand.get(job)
        if not expected or not isinstance(expected, dict):
            continue
        rel = row["outcome_relevant_failure"]
        got = {
            "tag": rel["tag"],
            "attribution": rel["attribution"],
            "stop": row["stop"]["reason"],
            "rule": rel["rule_id"],
        }
        want = {
            "tag": (expected.get("outcome") or {}).get("tag"),
            "attribution": (expected.get("outcome") or {}).get("attribution"),
            "stop": expected.get("stop"),
            "rule": (expected.get("outcome") or {}).get("rule"),
        }
        compared += 1
        agree = {}
        for dim in dims:
            match = got[dim] == want[dim]
            agree[dim] = bool(match)
            counts[dim]["total"] += 1
            if match:
                counts[dim]["agree"] += 1
        if not all(agree.values()):
            disagreements.append({
                "job": job,
                "tagger": got,
                "hand": want,
                "agree": agree,
                "mechanism": expected.get("mechanism"),
            })
    return {
        "compared": compared,
        "counts": counts,
        "disagreements": disagreements,
    }


WEDGE_HAND_CAUSES = {
    "pager": "pager",
    "continuation": "continuation",
    "interactive_prompt": "interactive",
    "stdin_read": "no_prompt",
    "running_process": "no_prompt",
    "other": "no_prompt",
}


def _wedge_ref_key(ref: str | None) -> tuple[int, int] | None:
    """`head#12` / `cont-1#12` / `trajectory.cont-1.json#12` -> (file, step)."""
    match = re.match(r"^(?:head|trajectory\.json|(?:trajectory\.)?cont-(\d+)(?:\.json)?)#(\d+)$", ref or "")
    if not match:
        return None
    return (int(match.group(1) or 0), int(match.group(2)))


def _merge_hand_stretches(stretches: list[dict]) -> list[dict]:
    """A blind reader may split one wedge at an empty wait turn or a file
    boundary (same trigger, or the first piece ends with neither the prompt
    back nor the run over). The detector skips turns that send nothing, so
    such pieces are joined before scoring."""
    merged: list[dict] = []
    for stretch in sorted(stretches, key=lambda s: _wedge_ref_key(s.get("start")) or (0, 0)):
        last = merged[-1] if merged else None
        if last and (
            last.get("trigger_ref") == stretch.get("trigger_ref")
            or (not last.get("prompt_returned") and not last.get("until_run_end"))
        ):
            last.update({
                "end": stretch.get("end"),
                "turns": (last.get("turns") or 0) + (stretch.get("turns") or 0),
                "prompt_returned": stretch.get("prompt_returned"),
                "until_run_end": stretch.get("until_run_end"),
                "interrupt_ref": last.get("interrupt_ref") or stretch.get("interrupt_ref"),
                "pieces": last.get("pieces", 1) + 1,
            })
        else:
            merged.append(dict(stretch))
    return merged


def validate_wedge(rows: list[dict], hand: dict) -> dict:
    """Score WEDGE against a blind hand key (`job` -> {stretches: [...]}).
    Trial level: does the detector report a stretch iff the hand key does.
    Stretch level (hand stretches, merged, each matched to an overlapping
    detector stretch): start exact / within 3 turns, end exact / within 1,
    interrupt ref, prompt returned, cause (hand cause mapped onto the
    detector's states)."""
    dims = ("start_exact", "start_within_3", "end_exact", "end_within_1",
            "interrupt", "prompt_returned", "cause")
    counts = {dim: 0 for dim in dims}
    trials = {"agree": 0, "total": 0, "hand_positive": 0, "detector_positive": 0}
    disagreements: list[dict] = []
    stretches_out: list[dict] = []
    for row in rows:
        job = Path(row["job_dir"]).name
        expected = hand.get(job)
        if not isinstance(expected, dict):
            continue
        detected = (row.get("wedge") or {}).get("stretches") or []
        wanted = _merge_hand_stretches(
            [s for s in expected.get("stretches") or [] if isinstance(s, dict)]
        )
        trials["total"] += 1
        trials["hand_positive"] += bool(wanted)
        trials["detector_positive"] += bool(detected)
        if bool(wanted) == bool(detected):
            trials["agree"] += 1
        else:
            disagreements.append({
                "job": job,
                "detector": [f"{s['start_ref']}-{s['end_ref']} ({s['turns']})" for s in detected],
                "hand": [f"{s.get('start')}-{s.get('end')} ({s.get('turns')})" for s in wanted],
                "hand_notes": expected.get("notes"),
            })
        for want in wanted:
            start, end = _wedge_ref_key(want.get("start")), _wedge_ref_key(want.get("end"))
            got = next((
                s for s in detected
                if start and end
                and _wedge_ref_key(s["start_ref"]) <= end
                and _wedge_ref_key(s["end_ref"]) >= start
            ), None)
            entry = {"job": job, "hand": f"{want.get('start')}-{want.get('end')}", "matched": got is not None}
            if got is not None:
                got_start, got_end = _wedge_ref_key(got["start_ref"]), _wedge_ref_key(got["end_ref"])
                agree = {
                    "start_exact": got_start == start,
                    "start_within_3": got_start[0] == start[0] and abs(got_start[1] - start[1]) <= 3,
                    "end_exact": got_end == end,
                    "end_within_1": got_end[0] == end[0] and abs(got_end[1] - end[1]) <= 1,
                    "interrupt": _wedge_ref_key(got["interrupt_ref"]) == _wedge_ref_key(want.get("interrupt_ref")),
                    "prompt_returned": got["prompt_returned"] == bool(want.get("prompt_returned")),
                    "cause": got["cause"] == WEDGE_HAND_CAUSES.get(want.get("cause"), want.get("cause")),
                }
                for dim in dims:
                    counts[dim] += agree[dim]
                entry.update({"detector": f"{got['start_ref']}-{got['end_ref']}", "agree": agree})
            stretches_out.append(entry)
    return {
        "trials": trials,
        "trial_disagreements": disagreements,
        "hand_stretches": len(stretches_out),
        "matched": sum(1 for entry in stretches_out if entry["matched"]),
        "counts": counts,
        "stretches": stretches_out,
    }

def write_summary(
    rows: list[dict],
    groups: list[dict],
    provenance: dict,
    inputs: list[str],
    argv: str,
    path: Path,
    skipped: list[str] | None = None,
    validation: dict | None = None,
    wedge_validation: dict | None = None,
) -> None:
    from collections import Counter

    tag_att = Counter(
        (r["outcome_relevant_failure"]["tag"], r["outcome_relevant_failure"]["attribution"])
        for r in rows
    )
    grader_notes = sorted(
        {(display_name(r), r["outcome_relevant_failure"].get("grader_note"))
         for r in rows if r["outcome_relevant_failure"].get("grader_note")}
    )
    first_grader_notes = sorted(
        {(display_name(r), (r["first_failure"] or {}).get("grader_note"))
         for r in rows if (r["first_failure"] or {}).get("grader_note")}
    )
    engineering = sorted(
        (display_name(r), r["outcome_relevant_failure"].get("engineering_flag"))
        for r in rows if r["outcome_relevant_failure"].get("engineering_flag")
    )
    no_signal_rows = [r for r in rows if r.get("no_signal")]
    standin_rows = [
        r for r in rows if (r["reconstruction"].get("harness_standins") or {})
    ]
    livelock_rows = [
        r for r in rows if r["outcome_relevant_failure"]["rule_id"] == "R-CTX-01"
    ]
    key_sources = {r["treatment"]["key_source"] for r in rows}
    provisional = any("auto" in source for source in key_sources)
    recorded_rows = [
        r for r in rows
        if (r["reconstruction"].get("acceptance_provenance") or {}).get("recorded")
    ]
    progress_rows = [
        r for r in rows
        if r["outcome_relevant_failure"].get("progress")
        or (r["first_failure"] or {}).get("progress")
    ]
    lines = [
        "# probe-03 capabilities summary",
        "",
        f"_Inputs: {', '.join(inputs)}. Invocation: `{argv}`._",
        "",
        "## Tag x attribution (outcome-relevant failure)",
        "",
        "| tag | attribution | trials |",
        "| --- | --- | --- |",
    ]
    for (tag, attribution), count in sorted(tag_att.items()):
        lines.append(f"| {tag} | {attribution} | {count} ({_names(rows, tag, attribution)}) |")
    lines += [
        "",
        "## Per-trial: first failure vs outcome-relevant failure",
        "",
        "| trial | outcome | stop reason | first failure (recovered) | "
        "outcome-relevant failure | rule | evidence |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        first = row["first_failure"]
        rel = row["outcome_relevant_failure"]
        outcome_txt = (
            f"{row['verifier']['reward']} (scored"
            + (", pass_tainted)" if row.get("pass_tainted") else ")")
            if row["verifier"]["scored"]
            else "unscored"
        )
        evidence = ",".join(rel["evidence_step_refs"]) or "--"
        name = display_name(row) + (" (no signal)" if row.get("no_signal") else "")
        if first:
            first_txt = (
                f"{first['step_ref'] or '--'} {first['tag']}/{first['attribution']} "
                f"({first['rule_id']}, recovered={str(first.get('recovered')).lower()})"
            )
        else:
            first_txt = "none (clean execution)"
        rel_txt = f"{rel['step_ref'] or '--'} {rel['tag']}/{rel['attribution']}"
        lines.append(
            f"| `{name}` | {outcome_txt} | {row['stop']['reason']} | "
            f"{first_txt} | {rel_txt} | {rel['rule_id']} | {evidence} |"
        )
        for extra in (rel.get("secondary"), rel.get("secondary_comp04"),
                      rel.get("secondary_wedge"), rel.get("engineering_flag"),
                      rel.get("grader_note")):
            if extra:
                lines.append(
                    f"| | | | | | note: | {extra} |"
                )
        for caveat in row.get("pass_caveats") or []:
            lines.append(f"| | | | | | caveat: | {caveat} |")
        if row.get("pass_tainted"):
            lines.append(
                f"| | | | | | pass_tainted: | "
                f"{row['pass_tainted']['source']} |"
            )
    lines.append("")
    if skipped:
        lines += ["## Skipped (still running)", ""]
        for entry in skipped:
            lines.append(f"- `{entry}`")
        lines.append("")
    if no_signal_rows:
        lines += [
            "## No capability signal",
            "",
            "Zero model turns (R-ENV-01); excluded from learnability attempts:",
            "",
        ]
        for row in no_signal_rows:
            lines.append(
                f"- `{display_name(row)}`: "
                f"{row['stop']['exception_type'] or row['stop']['reason']}"
            )
        lines.append("")
    if provisional:
        auto_rows = [
            display_name(r) for r in rows if "auto" in r["treatment"]["key_source"]
        ]
        lines += [
            "## Learnability: NOT COMPUTED",
            "",
            f"{len(auto_rows)} trial(s) carry only a config-derived key "
            f"({', '.join(auto_rows[:6])}{', ...' if len(auto_rows) > 6 else ''}). "
            "config.json does not record the parser/normalizer version, so "
            "their treatment identity is unverified and no learnability is "
            "computed; none is ever computed across keys. Key clusters below "
            "are shown only as evidence of splitting:",
            "",
        ]
    elif all(group["attempts"] <= 1 for group in groups):
        lines += [
            "## Learnability: unknown (one attempt per key so far)",
            "",
            "Every (task, treatment-key) cluster has a single attempt, so no "
            "within-key comparison is possible. No claim is ever made across "
            "keys:",
            "",
        ]
    else:
        lines += [
            "## Learnability",
            "",
            "Per (task, treatment-key) cluster, excluding no-signal, "
            "suspect-grader (R-ENV-02) and declared pass_tainted trials:",
            "",
        ]
    lines += [
        "| task | treatment key(s) | attempts | scored | passes | status | excluded |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for group in groups:
        keys = ", ".join(
            key[:24] + "…" if len(key) > 25 else key
            for key in group["treatment_keys"]
        )
        excluded = [
            *(f"{trial} (no signal)" for trial in group.get("no_signal_trials") or []),
            *(f"{trial} (R-ENV-02)" for trial in group.get("grader_suspect_trials") or []),
            *(f"{trial} (pass_tainted)" for trial in group.get("tainted_trials") or []),
        ]
        lines.append(
            f"| `{group['task']}` | `{keys}` | "
            f"{group['attempts']} | {group['scored']} | {group['passes']} | "
            f"{group['status']} | {', '.join(excluded) or '--'} |"
        )
        if group.get("keys_merged"):
            lines.append(f"| | merged: {group['merge_reason']} | | | | | |")
    lines.append("")
    costed = [r for r in rows if (r.get("loop_cost") or {}).get("all_steps")]
    if costed:
        uncosted = [display_name(r) for r in rows if r not in costed]
        unmetered = sum(r["loop_cost"]["all_steps"]["steps_without_metrics"] for r in costed)
        total_prompt = sum(r["loop_cost"]["all_steps"]["prompt"] for r in costed)
        in_loops = sum((r["loop_cost"]["in_loops"] or {}).get("prompt", 0) for r in costed)
        identical = sum((r["loop_cost"]["identical"] or {}).get("prompt", 0) for r in costed)
        confirmation = sum((r["loop_cost"]["confirmation"] or {}).get("prompt", 0) for r in costed)
        proxy_input = sum(r["loop_cost"]["result_input_tokens"] or 0 for r in costed)
        lines += [
            "## Loop cost (LOOP-COST)",
            "",
            f"Per-step prompt tokens over {len(costed)} trials: {total_prompt:,} total; "
            f"{in_loops:,} ({in_loops / total_prompt:.1%}) inside loops "
            f"(identical runs >=10: {identical:,}; confirmation spans: "
            f"{confirmation:,}; overlapping steps counted once). Proxy input "
            f"(result.json, includes summarization calls): {proxy_input:,}. "
            f"{unmetered} step(s) carry no metrics (summarization hand-off "
            f"turns) and are uncounted, not 0."
            + (f" No metered steps at all: {', '.join(uncosted)}." if uncosted else ""),
            "",
            "| trial | reward | loop prompt tokens | share | spans |",
            "| --- | --- | --- | --- | --- |",
        ]
        for row in sorted(
            costed, key=lambda r: -((r["loop_cost"]["in_loops"] or {}).get("prompt", 0))
        ):
            cost = row["loop_cost"]
            if not (cost["in_loops"] or {}).get("prompt"):
                continue
            spans = "; ".join(
                f"{span['kind']} {span['span'][0]}-{span['span'][1]} ({span['length']})"
                for span in cost["spans"]
            )
            lines.append(
                f"| `{display_name(row)}` | {row['verifier']['reward']} | "
                f"{cost['in_loops']['prompt']:,} | {cost['loop_prompt_share']:.0%} | {spans} |"
            )
    lines.append("")
    shaken = [r for r in rows if r.get("completion_handshake")]
    if shaken:
        unconfirmed = [r for r in shaken if not r["completion_handshake"]["confirmed"]]
        after = sum(r["completion_handshake"]["prompt_tokens_after_first_prompt"] for r in unconfirmed)
        all_prompt = sum(
            (r.get("loop_cost") or {}).get("all_steps", {}).get("prompt", 0)
            if (r.get("loop_cost") or {}).get("all_steps") else 0
            for r in rows
        )
        echo_rows = [r for r in shaken if r["completion_handshake"]["echo_task_complete_turns"]]
        lines += [
            "## Completion handshake (HANDSHAKE)",
            "",
            f"{len(shaken)} of {len(rows)} trials reached the harness prompt 'Are you "
            f"sure you want to mark the task as complete? ... include \"task_complete\": "
            f"true in your JSON response again.'; {len(shaken) - len(unconfirmed)} "
            f"confirmed, {len(unconfirmed)} never did and spent {after:,} per-step "
            f"prompt tokens after the first prompt"
            + (f" ({after / all_prompt:.1%} of all per-step prompt tokens)" if all_prompt else "")
            + ". The model writes native tool calls, not JSON; a native turn "
            "confirms only when it carries no tool call. "
            + (
                f"{len(echo_rows)} trial(s) ran `echo task_complete` as a shell "
                f"command ({', '.join(f'{display_name(r)} {r['completion_handshake']['echo_task_complete_turns']}x' for r in echo_rows)})."
                if echo_rows else ""
            ),
            "",
            "| trial | reward | first prompt | prompts | confirmed | turns after | prompt tokens after |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row in sorted(
            shaken, key=lambda r: -r["completion_handshake"]["prompt_tokens_after_first_prompt"]
        ):
            hand = row["completion_handshake"]
            lines.append(
                f"| `{display_name(row)}` | {row['verifier']['reward']} | "
                f"{hand['first_prompt_ref']} | {hand['prompts']} | "
                f"{'yes' if hand['confirmed'] else 'no'} | {hand['turns_after_first_prompt']} | "
                f"{hand['prompt_tokens_after_first_prompt']:,} |"
            )
        lines.append("")
    wedged = [
        (row, stretch)
        for row in rows
        for stretch in (row.get("wedge") or {}).get("stretches") or []
    ]
    if wedged:
        all_prompt = sum(
            (r.get("loop_cost") or {}).get("all_steps", {}).get("prompt", 0)
            if (r.get("loop_cost") or {}).get("all_steps") else 0
            for r in rows
        )
        wedge_prompt = sum(stretch["prompt_tokens"] for _row, stretch in wedged)
        interrupted = [s for _r, s in wedged if s["interrupt_ref"]]
        to_end = [s for _r, s in wedged if s["until_run_end"]]
        sources = sorted({(r.get("wedge") or {}).get("keystroke_source") for r, _s in wedged})
        lines += [
            "## Wedged terminals (WEDGE, measurement only)",
            "",
            f"{len(wedged)} stretch(es) in {len({id(r) for r, _s in wedged})} trial(s) of "
            f">= {WEDGE_MIN_TURNS} executed turns whose keystrokes landed while the shell "
            f"was not at a prompt (pager, `>` continuation, a program reading stdin or "
            f"still running). {len(interrupted)} sent an interrupt key; {len(to_end)} "
            f"lasted until the run ended. {wedge_prompt:,} per-step prompt tokens inside "
            "the stretches"
            + (f" ({wedge_prompt / all_prompt:.1%} of all)" if all_prompt else "")
            + f". Keystroke source: {', '.join(s for s in sources if s)}.",
            "",
        ]
        if wedge_validation:
            trials = wedge_validation["trials"]
            counts = wedge_validation["counts"]
            matched = wedge_validation["matched"]
            lines += [
                f"Blind key `{wedge_validation['wedge_key']}`: trials {trials['agree']}/"
                f"{trials['total']} (hand {trials['hand_positive']} wedged, detector "
                f"{trials['detector_positive']}); hand stretches matched {matched}/"
                f"{wedge_validation['hand_stretches']}; of matched: start exact "
                f"{counts['start_exact']}, within 3 {counts['start_within_3']}; end exact "
                f"{counts['end_exact']}, within 1 {counts['end_within_1']}; interrupt "
                f"{counts['interrupt']}; prompt back {counts['prompt_returned']}; cause "
                f"{counts['cause']}. Trial disagreements: "
                + (", ".join(f"`{d['job']}`" for d in wedge_validation["trial_disagreements"]) or "none")
                + ".",
                "",
            ]
        lines += [
            "| trial | reward | outcome rule | cause | trigger | stretch | turns | "
            "interrupt | prompt back | prompt tokens |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for row, stretch in sorted(wedged, key=lambda item: -item[1]["prompt_tokens"]):
            lines.append(
                f"| `{display_name(row)}` | {row['verifier']['reward']} | "
                f"{row['outcome_relevant_failure']['rule_id']} | {stretch['cause']} | "
                f"{stretch['trigger_ref']} | {stretch['start_ref']}-{stretch['end_ref']} | "
                f"{stretch['turns']} | "
                f"{stretch['interrupt_ref'] or 'none'}"
                f"{' (' + stretch['interrupt_keys'] + ')' if stretch['interrupt_keys'] else ''} | "
                f"{'yes' if stretch['prompt_returned'] else ('run end' if stretch['until_run_end'] else 'no')} | "
                f"{stretch['prompt_tokens']:,} |"
            )
        lines.append("")
    if grader_notes or first_grader_notes:
        lines.append("## Grader notes (for Data's verifier audit; not R-ENV-02)")
        lines.append("")
        for trial, note in grader_notes:
            lines.append(f"- `{trial}` (outcome): {note}")
        for trial, note in first_grader_notes:
            lines.append(f"- `{trial}` (first): {note}")
        lines.append("")
    if engineering:
        lines.append("## Engineering findings")
        lines.append("")
        for trial, flag in engineering:
            lines.append(f"- `{trial}`: {flag}")
        lines.append("")
    if progress_rows:
        lines += ["## Progress pointers (R-UNC-01; where the next hand-read looks)", ""]
        for row in progress_rows:
            for which in ("outcome_relevant_failure", "first_failure"):
                progress = (row[which] or {}).get("progress")
                if not progress:
                    continue
                lines.append(
                    f"- `{display_name(row)}` ({which} "
                    f"{row[which]['rule_id']}): last file-changing step "
                    f"{progress['last_file_changing_step'] or '--'}; verifier "
                    f"{progress['verifier_passes']}/{progress['verifier_total']} "
                    f"passed; longest identical run "
                    f"{progress['longest_identical_run'] or '--'}; "
                    f"prose_completions={progress['prose_completions']}; "
                    f"summarizations={progress['summarization_count']}"
                )
        lines.append("")
    if recorded_rows:
        lines += [
            "## Acceptance: recorded vs inferred",
            "",
            "Steps with harness-recorded step_layers use the recorded verdict; "
            "the observation-inferred (H-ACC) value is kept only as a "
            "cross-check:",
            "",
            "| trial | recorded steps | agree accept | agree reject | disagree | "
            "inferred unknown |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for row in recorded_rows:
            prov = row["reconstruction"]["acceptance_provenance"]
            agree = row["reconstruction"]["acceptance_agreement"]
            lines.append(
                f"| `{display_name(row)}` | {prov['recorded']} | "
                f"{agree['agree_accept']} | {agree['agree_reject']} | "
                f"{agree['disagree']} | {agree['inferred_unknown']} |"
            )
        lines.append("")
    else:
        lines += [
            "## Acceptance: recorded vs inferred",
            "",
            "No recorded step_layers in this batch; acceptance is "
            "observation-inferred (H-ACC) with normalizer replay for the "
            "proposed calls.",
            "",
        ]
    unattributed = [
        (display_name(r), (r["coverage"].get("har93_capture") or {}))
        for r in rows
        if ((r["coverage"].get("har93_capture") or {}).get("input_tokens_unattributed"))
    ]
    if unattributed:
        detail = "; ".join(
            f"{name}: conts {cap.get('continuations_missing') or '--'} missing, "
            f"{cap.get('input_tokens_unattributed')} unattributed -> "
            f"{cap.get('token_attributed_share')} share"
            for name, cap in unattributed
        )
        lines += [
            "## Capture coverage",
            "",
            "Episodes by count AND token-attributed share are both shown: a "
            "step count matching n_episodes is NOT full coverage when tokens "
            f"are unattributed ({detail}).",
            "",
        ]
    else:
        lines += [
            "## Capture coverage",
            "",
            "Episodes by count AND token-attributed share are both shown; no "
            "trial in this batch has unattributed input tokens.",
            "",
        ]
    lines += [
        "| trial | assembly | head | cont files | summ files | steps vs "
        "episodes | conts missing | token share (traj/proxy) | unattributed "
        "input | standins | notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        coverage = row["coverage"]
        cap = coverage.get("har93_capture") or {}
        standins = row["reconstruction"].get("harness_standins") or {}
        standin_total = sum(entry.get("count", 0) for entry in standins.values())
        notes = "; ".join(row.get("coverage_notes") or [])
        lines.append(
            f"| `{display_name(row)}` | {coverage['assembly_pattern']} | "
            f"{coverage['head_present']} | {coverage['cont_files'] or '--'} | "
            f"{len(coverage['summarization_files'])} | "
            f"{coverage['steps_vs_episodes']} | "
            f"{len(cap.get('continuations_missing') or []) or '--'} | "
            f"{cap.get('token_attributed_share', '--')} | "
            f"{cap.get('input_tokens_unattributed', '--')} | "
            f"{standin_total or '--'} | {notes or '--'} |"
        )
    lines += [
        "",
        "## Provenance",
        "",
        f"- evallab-src: `{provenance.get('evallab_src')}`",
        f"- evallab commit: `{provenance.get('evallab_commit')}`",
        f"- normalizer sha256 (`mimo_tool_calls.py`): "
        f"`{provenance.get('normalizer_sha256')}`",
        f"- normalizer mode: `{provenance.get('mode')}`",
        f"- HAR-93 treatment parquet sha256: "
        f"`{provenance.get('har93_treatment_sha256')}`",
        f"- HAR-93 capture parquet sha256: "
        f"`{provenance.get('har93_capture_sha256')}`",
    ]
    if provenance.get("import_error"):
        lines.append(f"- import note: `{provenance['import_error']}`")
    if provenance.get("nop_runs_dir"):
        lines.append(f"- nop runs dir: `{provenance['nop_runs_dir']}`")
    lines += [
        "",
        "## Limits",
        "",
        "- Replaying old messages through today's normalizer recovers what the "
        "model PROPOSED; it is never what the harness accepted/executed then. "
        "Acceptance comes only from that step's recorded observation, or from "
        "recorded step_layers where the harness wrote them.",
        "- Anything absent is null/`unknown`, never 0.",
        "- Treatment keys: Infra's pinned dispatch key "
        "(`<worktree>/derived/<lane>/treatment-key.json`) first, then Data's "
        "HAR-93 trial_treatment.parquet setup_key (sha256 recorded above; "
        "attached as a cross-check wherever it exists), then a lab-metadata "
        "pin; config-derived keys are only a fallback.",
    ]
    if provisional:
        lines += [
            "- No learnability is computed while any trial has only a "
            "config-derived key; no claim is ever made across keys.",
        ]
    else:
        lines += [
            "- A (task, key) cluster is `learnable` only with 2+ scored "
            "attempts and mixed outcomes; `all-pass`/`all-fail` need 2+ scored "
            "attempts; otherwise `unknown`. No claim is ever made across keys "
            "except a declared, recorded equivalence.",
        ]
    if standin_rows:
        bits = []
        for row in standin_rows:
            for doc, entry in sorted(
                (row["reconstruction"].get("harness_standins") or {}).items()
            ):
                bits.append(
                    f"{display_name(row)} {doc} "
                    f"#{entry['first']}-{entry['last']} ({entry['count']})"
                )
        lines += [
            "- 'Technical difficulties. Please continue with the task.' agent "
            "messages are Harbor stand-ins, not model output; they are excluded "
            f"from model-behavior counts and loops ({'; '.join(bits)}) and cited "
            "as the overflow/end-of-run pattern.",
        ]
    if livelock_rows:
        names = ", ".join(display_name(r) for r in livelock_rows)
        lines += [
            "- R-CTX-01 livelock trials "
            f"({names}): steps after the first context-exceeded cycle were "
            "produced under degraded summarization context and are not "
            "attributed to the model; trial.log is the authority.",
        ]
    if validation:
        lines += ["", "## Validation vs hand key", ""]
        for entry in validation.get("sets") or []:
            set_counts = entry.get("counts") or {}
            lines.append(
                f"- `{entry['hand_key']}` ({entry.get('compared', 0)} trials): "
                + ", ".join(
                    f"{dim} {(set_counts.get(dim) or {}).get('agree', 0)}/"
                    f"{(set_counts.get(dim) or {}).get('total', 0)}"
                    for dim in ("tag", "attribution", "stop", "rule")
                )
            )
        if validation.get("sets"):
            lines += ["", "Combined:", ""]
        counts = validation.get("counts") or {}
        for dim in ("tag", "attribution", "stop", "rule"):
            entry = counts.get(dim) or {}
            lines.append(
                f"- {dim}: {entry.get('agree', 0)}/{entry.get('total', 0)} agree"
            )
        disagreements = validation.get("disagreements") or []
        if disagreements:
            lines += ["", "Disagreements (tagger vs hand):", ""]
            for entry in disagreements:
                got, want = entry["tagger"], entry["hand"]
                lines.append(
                    f"- `{entry['job']}`: tagger "
                    f"{got['tag']}/{got['attribution']} {got['stop']} "
                    f"{got['rule']} vs hand {want['tag']}/{want['attribution']} "
                    f"{want['stop']} {want['rule']}; hand mechanism: "
                    f"{entry.get('mechanism') or '--'}"
                )
        else:
            lines += ["", "No disagreements.", ""]
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")



def first_evidence(first: dict | None) -> str:
    if not first:
        return "first=none (clean execution)"
    return (
        f"first={first['tag']}/{first['attribution']} ({first['rule_id']}, "
        f"recovered={str(first.get('recovered')).lower()})"
    )


def probe02_evidence(row: dict) -> str:
    coverage = row["coverage"]
    rel = row["outcome_relevant_failure"]
    first = row["first_failure"]
    return (
        f"outcome={rel['tag']}/{rel['attribution']} ({rel['rule_id']}); "
        f"{first_evidence(first)}; "
        f"stop={row['stop']['reason']}; reward={row['verifier']['reward']} "
        f"(scored={row['verifier']['scored']}); turns={coverage['agent_turns_read']} "
        f"acc={row['reconstruction']['harness_accepted']}; "
        f"trace={row['trial_dir']}"
    )


def sheet_name(row: dict) -> str:
    """Reading-sheet trial name: the recorded trial name where it already
    starts with the job name (HAR-90, byte-stable), else the full job-based
    display name (HAR-81 truncated trial names)."""
    trial_base = Path(row["trial_dir"]).name
    job_base = Path(row["job_dir"]).name
    if trial_base.startswith(job_base):
        return row["trial"]
    return display_name(row)


SHEET_SIZE = 20


def sheet_selection(rows: list[dict], picks: list[str] | None = None) -> list[dict]:
    """Traces for the hand-labeling sheet. With `picks` (job-directory
    names, e.g. har81-l-d-a4-format-code-task-000240) the sheet is exactly
    those trials in that order; an unknown pick is an error, never skipped.
    Otherwise ~SHEET_SIZE traces balanced across outcome (tag, attribution)
    strata: round-robin over strata, distinct tasks first within each."""
    if picks:
        by_job = {Path(row["trial_dir"]).parent.name: row for row in rows}
        missing = [pick for pick in picks if pick not in by_job]
        if missing:
            raise SystemExit(f"--sheet-picks: no trial row for {missing}")
        return [by_job[pick] for pick in picks]
    strata: dict[tuple[str, str], list[dict]] = {}
    for row in sorted(rows, key=sheet_name):
        rel = row["outcome_relevant_failure"]
        strata.setdefault((rel["tag"], rel["attribution"]), []).append(row)
    for key, members in strata.items():
        seen: set[str] = set()
        fresh, repeat = [], []
        for row in members:
            (repeat if row["task"] in seen else fresh).append(row)
            seen.add(row["task"])
        strata[key] = fresh + repeat
    ordered: list[dict] = []
    depth = 0
    while len(ordered) < SHEET_SIZE and any(len(m) > depth for m in strata.values()):
        for key in sorted(strata):
            if depth < len(strata[key]) and len(ordered) < SHEET_SIZE:
                ordered.append(strata[key][depth])
        depth += 1
    return ordered


def write_reading_sheet(
    rows: list[dict], metrics_path: str, path: Path, picks: list[str] | None = None
) -> None:
    ordered = sheet_selection(rows, picks)
    selection = (
        f"{len(ordered)} hand-picked traces (--sheet-picks)" if picks else
        f"{len(ordered)} traces balanced across outcome tag x attribution "
        f"(round-robin, distinct tasks first)"
    )
    lines = [
        "# Reading sheet (hand labeling, HAR-91 proposals)",
        "",
        f"_Source: {metrics_path}; {selection}. Open each trace, read it end "
        "to end, then fill one table row and CONFIRM or CORRECT the proposed "
        "tag (the last three columns are proposals, not labels)._",
        "",
        "## Traces to read",
        "",
    ]
    for row in ordered:
        first = row["first_failure"]
        rel = row["outcome_relevant_failure"]
        lines += [
            f"### {rel['tag']}: `{sheet_name(row)}`",
            "",
            f"- task: `{row['task']}`",
            f"- reward: {row['verifier']['reward']} (scored={row['verifier']['scored']})",
            f"- stop: {row['stop']['reason']} ({row['stop']['exception_type']})",
            f"- turns read: {row['coverage']['agent_turns_read']}, "
            f"calls reconstructed: {row['reconstruction']['calls_reconstructed']}, "
            f"tokens: {row['coverage']['tokens_result_totals']}",
            (
                f"- first failure: {first['tag']}/{first['attribution']} "
                f"({first['rule_id']}, step {first['step_ref']}, "
                f"recovered={str(first.get('recovered')).lower()})"
                if first else
                "- first failure: none (clean execution)"
            ),
            f"- outcome-relevant: {rel['tag']}/{rel['attribution']} "
            f"({rel['rule_id']}, step {rel['step_ref']})",
            f"- trace: `{row['trial_dir']}`",
            "",
        ]
    lines += [
        "## Labeling table",
        "",
        LABEL_COLUMNS,
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in ordered:
        first = row["first_failure"]
        rel = row["outcome_relevant_failure"]
        first_steps = ",".join((first or {}).get("evidence_step_refs") or []) or "--"
        rel_steps = ",".join(rel["evidence_step_refs"]) or "--"
        first_cell = (
            f"first {first['tag']}/{first['attribution']} @ {first_steps} "
            f"({first['rule_id']})"
            if first else "first none (clean execution)"
        )
        # probe-02's six columns, then probe-03's three proposal columns;
        # the first/outcome summary joins probe-02's evidence line.
        lines.append(
            f"| `{sheet_name(row)}` |  |  |  |  | {probe02_evidence(row)}; "
            f"{first_cell}; outcome {rel['tag']}/{rel['attribution']} "
            f"@ {rel_steps} ({rel['rule_id']}) | {rel['tag']} | "
            f"{rel['attribution']} | {rel_steps} |"
        )
    lines += [
        "",
        "## After labeling",
        "",
        "- Where your hand tag differs from the proposal, cite the step that "
        "changed your mind.",
        "- If 2+ rows blame the task (not the agent), flag the task for the "
        "env audit; if rows show the same agent loop twice, note the loop "
        "shape for the harness fix.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv=None) -> int:
    global NOP_RUNS_DIR
    import glob as glob_module

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "job_dir", nargs="+",
        help="trial, job, or jobs folder (shell globs accepted)",
    )
    parser.add_argument("--out-dir", required=True, help="output directory")
    parser.add_argument(
        "--evallab-src",
        default="/Users/petermakhnatch/Developer/eval-lab/.worktrees/har76-main-proof/src",
        help="evallab src dir owning normalize_mimo_tool_calls",
    )
    parser.add_argument(
        "--treatment-key-source",
        default="auto",
        help="auto or har93:<path> pluggable override (shape tolerant)",
    )
    parser.add_argument(
        "--nop-runs-dir",
        default=None,
        help="runs tree holding nop/qual control trials for the R-ENV-02 "
        "nop cross-check (optional; without it nop_control_confirms=unknown)",
    )
    parser.add_argument(
        "--commit-equivalent",
        default=None,
        help="comma-separated eval-lab commits (full SHAs or prefixes) "
        "declared as ONE treatment for learnability grouping",
    )
    parser.add_argument(
        "--equivalence-source",
        default=None,
        help="provenance string for the declared equivalence "
        "(e.g. 'HAR-81 RH 09:27')",
    )
    parser.add_argument(
        "--pass-tainted",
        nargs="+",
        default=None,
        help="job-dir names whose scored pass a coordinator declared "
        "tainted (reward stays 1.0 in the record; excluded from "
        "learnability attempts). Requires --taint-source; an unknown job "
        "or a non-pass is an error",
    )
    parser.add_argument(
        "--taint-source",
        default=None,
        help="provenance string for --pass-tainted (e.g. 'HAR-81 RH 10:32Z')",
    )
    parser.add_argument(
        "--hand-key",
        nargs="+",
        default=None,
        help="parent-owned hand answer key JSONL file(s) (read-only); join "
        "on job name for the 'Validation vs hand key' summary section plus "
        "validation.json. Each file is scored as its own set (in-sample vs "
        "held-out), then combined. Never tunes rules.",
    )
    parser.add_argument(
        "--wedge-key",
        default=None,
        help="blind WEDGE hand key JSONL (job, stretches[]; read-only): "
        "scores the wedged-terminal measurement per trial and per stretch "
        "into wedge_validation.json and the WEDGE summary section",
    )
    parser.add_argument(
        "--sheet-picks",
        default=None,
        help="text file of job-directory names (one per line, # comments) "
        "for a hand-picked reading sheet; default is ~20 traces balanced "
        "across outcome tag x attribution",
    )
    args = parser.parse_args(argv)
    NOP_RUNS_DIR = args.nop_runs_dir

    provenance, normalize_fn = load_normalizer(args.evallab_src or None)
    har93_source: str | None = None
    har93_treatment: dict | None = None
    har93_capture: dict | None = None
    if args.treatment_key_source != "auto":
        if args.treatment_key_source.startswith("har93:"):
            har93_source = args.treatment_key_source[len("har93:"):]
            har93_treatment = load_har93_treatment(har93_source)
            if "_error" in har93_treatment:
                print(f"warn: {har93_treatment['_error']}", file=sys.stderr)
                har93_treatment = None
            else:
                capture_path = Path(har93_source).parent / "trial_capture.parquet"
                if capture_path.is_file():
                    har93_capture = load_har93_capture(str(capture_path))
                    if "_error" in har93_capture:
                        print(f"warn: {har93_capture['_error']}", file=sys.stderr)
                        har93_capture = None
        else:
            print(
                f"warn: --treatment-key-source must be auto or har93:<path>; "
                f"got {args.treatment_key_source!r}, using auto",
                file=sys.stderr,
            )

    rows: list[dict] = []
    skipped: list[str] = []
    expanded: list[str] = []
    for job in args.job_dir:
        hits = sorted(glob_module.glob(job))
        expanded.extend(hits if hits else [job])
    for job in expanded:
        if ".transient" in str(job):
            skipped.append(f"{job} (transient attempts)")
            continue
        for trial_dir in probe02.find_trials(Path(job)):
            try:
                trial_result = json.loads(
                    (trial_dir / "result.json").read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                skipped.append(f"{trial_dir} (no result.json)")
                print(f"skipped: no result.json {trial_dir}", file=sys.stderr)
                continue
            if not trial_result.get("finished_at"):
                skipped.append(f"{trial_dir} (still running: no finished_at)")
                print(f"skipped: running {trial_dir}", file=sys.stderr)
                continue
            rows.append(
                analyze_trial(
                    trial_dir,
                    provenance,
                    normalize_fn,
                    har93_treatment,
                    har93_capture,
                    har93_source,
                )
            )
    rows.sort(key=lambda row: row["trial"])
    provenance["har93_treatment_sha256"] = (
        har93_treatment.get("_sha256") if har93_treatment else None
    )
    provenance["har93_capture_sha256"] = (
        har93_capture.get("_sha256") if har93_capture else None
    )
    if NOP_RUNS_DIR:
        provenance["nop_runs_dir"] = NOP_RUNS_DIR
    equiv = resolve_equivalence(args.commit_equivalent, args.equivalence_source)
    if equiv:
        provenance["commit_equivalent"] = equiv["commits"]
        provenance["commit_equivalent_resolved"] = equiv["resolved"]
        provenance["equivalence_source"] = equiv["source"]
    if args.pass_tainted:
        if not args.taint_source:
            parser.error("--pass-tainted requires --taint-source")
        by_job = {Path(row["job_dir"]).name: row for row in rows}
        for job in args.pass_tainted:
            row = by_job.get(job)
            if row is None:
                parser.error(f"--pass-tainted: unknown job {job!r}")
            if not (row["verifier"]["scored"] and (row["verifier"]["reward"] or 0) >= 1.0):
                parser.error(f"--pass-tainted: {job!r} is not a scored pass")
            row["pass_tainted"] = {"source": args.taint_source}
        provenance["pass_tainted"] = sorted(args.pass_tainted)
        provenance["taint_source"] = args.taint_source
    for row in rows:
        row["provenance"] = provenance

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "capabilities.jsonl"
    summary_path = out_dir / "summary.md"
    sheet_path = out_dir / "reading_sheet.md"
    write_jsonl(rows, jsonl_path)
    groups = learnability_groups(rows, equiv)
    validation = None
    if args.hand_key:
        hands = [(key_path, load_hand_key(key_path)) for key_path in args.hand_key]
        for key_path, hand in hands:
            if "_error" in hand:
                print(f"warn: {key_path}: {hand['_error']}", file=sys.stderr)
        hands = [(key_path, hand) for key_path, hand in hands if "_error" not in hand]
        if hands:
            merged: dict[str, dict] = {}
            for key_path, hand in hands:
                for job in sorted(set(hand) & set(merged)):
                    print(f"warn: {job} keyed in more than one hand key; "
                          f"{key_path} wins", file=sys.stderr)
                merged.update(hand)
            validation = validate_against_hand(rows, merged)
            validation["hand_key"] = [key_path for key_path, _hand in hands]
            if len(hands) > 1:
                validation["sets"] = [
                    {"hand_key": key_path, **validate_against_hand(rows, hand)}
                    for key_path, hand in hands
                ]
            with open(out_dir / "validation.json", "w", encoding="utf-8") as handle:
                json.dump(validation, handle, ensure_ascii=False, indent=2)
            print(f"wrote validation -> {out_dir / 'validation.json'}")
    wedge_validation = None
    if args.wedge_key:
        wedge_hand = load_hand_key(args.wedge_key)
        if "_error" in wedge_hand:
            print(f"warn: {args.wedge_key}: {wedge_hand['_error']}", file=sys.stderr)
        else:
            wedge_validation = {"wedge_key": args.wedge_key, **validate_wedge(rows, wedge_hand)}
            with open(out_dir / "wedge_validation.json", "w", encoding="utf-8") as handle:
                json.dump(wedge_validation, handle, ensure_ascii=False, indent=2)
            print(f"wrote wedge validation -> {out_dir / 'wedge_validation.json'}")
    argv_str = " ".join(["capabilities.py", *(argv if argv is not None else sys.argv[1:])])
    write_summary(rows, groups, provenance, expanded, argv_str, summary_path,
                  skipped, validation, wedge_validation)
    picks = None
    if args.sheet_picks:
        picks = [
            line.split("#", 1)[0].strip()
            for line in Path(args.sheet_picks).read_text(encoding="utf-8").splitlines()
        ]
        picks = [pick for pick in picks if pick]
    write_reading_sheet(rows, str(jsonl_path), sheet_path, picks)
    print(f"wrote {len(rows)} trial rows -> {jsonl_path}")
    print(f"wrote summary -> {summary_path}")
    print(f"wrote reading sheet -> {sheet_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
