"""Interchangeable model lanes for the Task QA environment agent.

``make_driver`` returns one driver per audit. A driver owns exactly one persisted
conversation and exposes the surface ``EnvAuditSession`` needs: ``thread_id``,
``invocations``, ``facts``, ``cumulative``, ``turn()`` and ``accounting()``.
Every lane is text-only: the reviewer emits JSON actions that the parent controller
executes, never native shell or file-system calls.

* ``codex``  - the existing ``CodexSession`` (ChatGPT subscription, native thread).
* ``omp``    - one saved OMP conversation (``omp -p --mode json --resume <id>``) on an
  explicit Anthropic model through the stored subscription OAuth account.
* ``openai`` - an OpenAI-compatible HTTPS chat-completions endpoint with a
  driver-maintained conversation, an explicit key *name* and a metered budget that
  reserves a worst-case charge before every request.

Rules shared by all lanes:

* No retry, no reroute, no provider or model fallback. A failed turn raises
  ``DriverError``; evidence written before the failure stays on disk.
* Unknown token counts or costs stay unknown (``None``); they are never zero.
  ``usage_complete`` on the driver turns false once any turn's usage is unknown.
* Credential values never reach facts, files, errors or URLs; only the *name* of
  the environment variable that holds an API key is recorded.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import urllib.parse
import uuid
from collections import Counter
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from evallab.execution_contracts import persist_private_bytes
from evallab.explorer import redact_text
from evallab.gepa_optimizer.codex_transport import (
    CodexSession,
    CodexTransport,
    CodexTransportError,
)
from evallab.interpretation.monitor_agent import (
    BudgetExhausted,
    InvestigationBudget,
    InvestigatorError,
    InvestigatorTransport,
    OpenAIInvestigator,
    _append_event,
    _payload,
)

LANES = ("codex", "omp", "openai")

#: Models the OMP lane may be pinned to. ``--model`` accepts fuzzy selectors, so the
#: selector must be fully qualified and the served model is verified on every turn.
OMP_MODELS = ("anthropic/claude-sonnet-5-5", "anthropic/claude-opus-5-5")
#: Resolved through ``shutil.which`` when a driver is built.
OMP_COMMAND: tuple[str, ...] = ("omp",)
OMP_SYSTEM_PROMPT = (
    "You are a careful reviewer working through a JSON-action controller.\n"
    "You have no tools and no file system. Reply exactly as the user message instructs."
)
#: Prompts up to this size travel as the positional message; larger ones use stdin so
#: they cannot hit the argv size limit. The persisted prompt echo is verified either way.
OMP_ARGV_PROMPT_BYTES = 100_000
MAX_PROMPT_BYTES = 4 * 1024 * 1024
MAX_OMP_EVENT_BYTES = 64 * 1024 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
#: Only runtime/auth-location settings reach OMP; API keys and tool/provider overrides do not.
_OMP_ENV_VARS = frozenset(
    {
        "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR",
        "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
        "OMP_PROFILE", "PI_PROFILE", "PI_CONFIG_DIR", "PI_CODING_AGENT_DIR",
    }
)
#: Session events that mean the turn was not one plain no-tool completion.
_OMP_FORBIDDEN_EVENTS = frozenset(
    {
        "tool_execution_start", "tool_execution_update", "tool_execution_end",
        "tool_stream_update", "auto_compaction_start", "auto_compaction_end",
        "auto_retry_start", "auto_retry_end",
        "retry_fallback_applied", "retry_fallback_succeeded", "ttsr_triggered",
    }
)
#: Present in the saved system prompt only if memory, context files or rules leaked in.
_OMP_FORBIDDEN_PROMPT_MARKERS = ("<memories>", "<repo-rules>", '<file path="')
_OMP_CONFIG = """\
memory:
  backend: "off"
autolearn:
  enabled: false
retry:
  enabled: false
compaction:
  enabled: false
advisor:
  enabled: false
enabledProviders: []
disabledProviders:
  - native
  - claude
  - claude-plugins
  - codex
  - gemini
  - github
  - opencode
  - cursor
  - windsurf
  - vscode
  - agents-md
  - mcp-json
  - agent-plugins
disabledExtensions:
  - "context-file:user:AGENTS.md"
  - "context-file:project:AGENTS.md"
  - "context-file:user:CLAUDE.md"
  - "context-file:project:CLAUDE.md"
  - "context-file:user:GEMINI.md"
  - "context-file:project:GEMINI.md"
"""

#: Output ceiling per OpenAI-lane request; it is also the output term of the reservation.
OPENAI_MAX_OUTPUT_TOKENS = 6000
OPENAI_MAX_REQUESTS = 200
OPENAI_MAX_TRANSPORT_SECONDS = 600.0
_OPENAI_SCHEMA: dict[str, Any] = {"type": "object"}
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

_USAGE_NAMES = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")


class DriverError(RuntimeError):
    """A lane refused or failed a turn; evidence from earlier turns survives on disk."""


class Driver(Protocol):
    """What ``EnvAuditSession`` may rely on, whichever lane backs it."""

    lane: str
    thread_id: str | None
    invocations: int
    cumulative: dict[str, int]
    facts: dict[str, Any] | None

    def turn(self, prompt: str, *, timeout_seconds: float) -> dict[str, Any]: ...

    def accounting(self) -> dict[str, Any]: ...


# -- shared helpers --------------------------------------------------------------------


def _number(value: Any, *, positive: bool = False) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and (value > 0 if positive else value >= 0)
    )


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _check_timeout(value: Any, what: str = "timeout_seconds") -> float:
    if not _number(value, positive=True):
        raise DriverError(f"{what} must be a positive finite number")
    return float(value)


def _check_prompt(prompt: Any) -> bytes:
    if not isinstance(prompt, str) or not prompt.strip():
        raise DriverError("model prompt must be a non-empty string")
    data = prompt.encode("utf-8")
    if len(data) > MAX_PROMPT_BYTES:
        raise DriverError(f"model prompt exceeds {MAX_PROMPT_BYTES} bytes; nothing is truncated")
    return data


def _new_directory(directory: Path) -> Path:
    directory = Path(directory)
    if any(part.is_symlink() for part in (directory, *directory.parents)):
        raise DriverError("driver directories must not follow symlinks")
    try:
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
    except OSError as exc:
        raise DriverError(f"cannot create driver directory: {type(exc).__name__}") from exc
    return directory


def _clean(text: str, secrets: tuple[str, ...] = (), limit: int = 600) -> str:
    """Bounded single-line diagnostic with credential values removed."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return " ".join(redact_text(text).split())[:limit]


def _write_json(path: Path, value: Any, secrets: tuple[str, ...] = ()) -> None:
    data = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str) + "\n"
    persist_private_bytes(path, data.encode(), secrets=tuple(s.encode() for s in secrets if s))


def _now() -> str:
    return datetime.now(UTC).isoformat()


# -- codex -----------------------------------------------------------------------------


class CodexDriver:
    """The existing native Codex thread, unchanged, behind the common surface."""

    lane = "codex"

    def __init__(
        self, *, model: str, directory: Path, repo_root: Path, timeout_seconds: float
    ) -> None:
        self.model = model
        try:
            transport = CodexTransport(
                repo_root=Path(repo_root),
                model=model,
                timeout_seconds=max(1, int(timeout_seconds)),
            )
            self._session = CodexSession(transport, directory=Path(directory))
        except (ValueError, OSError) as exc:
            raise DriverError(f"codex lane unavailable: {type(exc).__name__}: {exc}") from exc
        self._usage_complete = True

    @property
    def thread_id(self) -> str | None:
        return self._session.thread_id

    @property
    def invocations(self) -> int:
        return self._session.invocations

    @property
    def cumulative(self) -> dict[str, int]:
        return self._session.cumulative

    @property
    def usage_complete(self) -> bool:
        return self._usage_complete

    @property
    def facts(self) -> dict[str, Any]:
        return {
            "lane": "codex",
            "provider": "openai-codex (ChatGPT subscription)",
            "thread_id_kind": "native_codex_thread",
            "actual_model": "not reported by codex exec JSONL; pinned with -m and the "
            "subscription provider id",
            "accounting": "subscription route verified by preflight before every turn; no "
            "metered API or provider fallback. Subscription quota is consumed separately and "
            "is gated by the caller.",
            "usage_basis": "per-turn delta of Codex's cumulative thread usage; input_tokens "
            "includes cached input",
            **(self._session.facts or {}),
        }

    def turn(self, prompt: str, *, timeout_seconds: float) -> dict[str, Any]:
        _check_prompt(prompt)
        _check_timeout(timeout_seconds)
        try:
            reply = self._session.turn(prompt, timeout_seconds=timeout_seconds)
        except (CodexTransportError, OSError, subprocess.SubprocessError) as exc:
            self._usage_complete = False
            raise DriverError(f"{type(exc).__name__}: {exc}") from exc
        return {
            **reply,
            "provider": "openai-codex",
            "actual_model": None,
            "usage_known": True,
            "metered_usd": 0.0,
            "cost_known": True,
            "cost_basis": "chatgpt_subscription",
            "api_equivalent_usd": None,
        }

    def accounting(self) -> dict[str, Any]:
        return {
            "lane": "codex",
            "requested_model": f"codex/{self._session.transport.model}",
            "invocations": self.invocations,
            "usage_complete": self._usage_complete,
            "cumulative_usage": dict(self.cumulative),
            "metered_usd": 0.0,
            "cost_basis": "chatgpt_subscription",
            "api_equivalent_usd": None,
        }


# -- omp -------------------------------------------------------------------------------


class _OmpStream:
    """What one ``omp --mode json`` invocation actually printed."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.header_ids: list[str] = []
        self.assistants: list[dict[str, Any]] = []
        self.users: list[str] = []
        self.agent_ends: list[dict[str, Any]] = []


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""


def parse_omp_events(path: Path) -> _OmpStream:
    """Decode the JSON-lines stream; any undecodable line fails the turn closed."""
    if path.stat().st_size > MAX_OMP_EVENT_BYTES:
        raise DriverError("OMP event stream exceeded its bound")
    stream = _OmpStream()
    for raw in path.read_bytes().splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError as exc:
            raise DriverError("OMP emitted a line that is not JSON") from exc
        kind = event.get("type") if isinstance(event, dict) else None
        if not isinstance(kind, str):
            raise DriverError("OMP emitted an event without a type")
        stream.counts[kind] += 1
        if kind == "session" and isinstance(event.get("id"), str):
            stream.header_ids.append(event["id"])
        elif kind == "message_end" and isinstance(event.get("message"), dict):
            message = event["message"]
            if message.get("role") == "assistant":
                stream.assistants.append(message)
            elif message.get("role") == "user":
                stream.users.append(_message_text(message))
        elif kind == "agent_end":
            stream.agent_ends.append(event)
    if not stream.assistants and stream.agent_ends:
        messages = stream.agent_ends[-1].get("messages")
        if isinstance(messages, list):
            stream.assistants = [
                m for m in messages if isinstance(m, dict) and m.get("role") == "assistant"
            ]
    return stream


def _omp_usage(message: dict[str, Any]) -> tuple[dict[str, int | None], float | None]:
    """Codex-shaped usage (input includes cached) or all-``None`` when not reported."""
    unknown: dict[str, int | None] = dict.fromkeys(_USAGE_NAMES)
    raw = message.get("usage")
    if not isinstance(raw, dict):
        return unknown, None
    fresh, out = _count(raw.get("input")), _count(raw.get("output"))
    read, write = _count(raw.get("cacheRead")), _count(raw.get("cacheWrite"))
    cost = raw.get("cost")
    total = cost.get("total") if isinstance(cost, dict) else None
    api_usd = float(total) if _number(total) else None
    if None in (fresh, out, read, write):
        return unknown, api_usd
    prompt_tokens = fresh + read + write  # type: ignore[operator]
    if prompt_tokens <= 0 or out <= 0:  # type: ignore[operator]
        return unknown, api_usd
    return {
        "input_tokens": prompt_tokens,
        "cached_input_tokens": read,
        "cache_write_input_tokens": write,
        "output_tokens": out,
    }, api_usd


def _session_header_id(path: Path) -> str | None:
    """Session id from the first lines of an OMP session file (a title slot may precede it)."""
    try:
        with path.open("rb") as stream:
            for _ in range(6):
                line = stream.readline(1 << 20).strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("type") == "session":
                    value = entry.get("id")
                    return value if isinstance(value, str) and value else None
    except OSError:
        return None
    return None


def _read_subscription(timeout_seconds: float) -> dict[str, Any]:
    """Free metadata probe proving an authenticated Anthropic *subscription* account."""
    from evallab.quota import read_claude_headroom

    headroom = read_claude_headroom(now=datetime.now(UTC), timeout_seconds=timeout_seconds)
    if headroom.availability != "observed":
        raise DriverError(
            "OMP Anthropic subscription could not be verified "
            f"({headroom.reason or 'no 5h/7d subscription windows reported'}); "
            "API-key routes are refused"
        )
    weekly = headroom.windows.get("7d")
    return {
        "availability": headroom.availability,
        "weekly_used_percent": weekly.used_percent if weekly is not None else None,
        "fetched_at": headroom.fetched_at.isoformat() if headroom.fetched_at else None,
    }


class OmpDriver:
    """One saved OMP session, advanced turn by turn with ``--resume <id>``."""

    lane = "omp"

    def __init__(self, *, model: str, directory: Path, timeout_seconds: float) -> None:
        if model not in OMP_MODELS:
            raise DriverError(f"omp lane requires one of {', '.join(OMP_MODELS)}")
        self.model = model
        executable = shutil.which(OMP_COMMAND[0])
        if executable is None:
            raise DriverError("omp executable not found")
        self._executable = str(Path(executable).resolve())
        try:
            probe = subprocess.run(
                [self._executable, "--version"],
                capture_output=True, timeout=20, check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DriverError(f"omp --version failed: {type(exc).__name__}") from exc
        version = probe.stdout.decode(errors="replace").strip().splitlines()
        if probe.returncode != 0 or not version:
            raise DriverError("omp --version failed")
        self.directory = _new_directory(directory)
        self.cwd = self.directory / "cwd"
        self.cwd.mkdir(mode=0o700)
        self.session_dir = self.directory / "sessions"
        self.session_dir.mkdir(mode=0o700)
        self._config = self.directory / "omp-config.yml"
        persist_private_bytes(self._config, _OMP_CONFIG.encode())
        self.thread_id: str | None = None
        self.invocations = 0
        self.cumulative: dict[str, int] = {}
        self._session_file: Path | None = None
        self._prompt_audit: dict[str, Any] | None = None
        self._usage_complete = True
        self._api_usd: float | None = 0.0
        self._static = {
            "lane": "omp",
            "provider": "anthropic via OMP, stored subscription OAuth account",
            "requested_model": model,
            "authenticated_route": "anthropic_subscription_oauth_via_omp",
            "executable": self._executable,
            "version": version[0][:200],
            "thread_id_kind": "native_omp_session",
            "native_session": "omp --resume <session id>; session JSONL persisted under "
            "the driver's sessions/ directory",
            "text_only": {
                "tools": "disabled (--no-tools); tool events or tool-call blocks fail the turn",
                "rules_skills_extensions": "disabled (--no-rules --no-skills --no-extensions)",
                "memory_and_context_files": "disabled through the frozen discovery/runtime "
                "overlay; retained native base-prompt digests are not plaintext snapshots",
                "prewalk_title_lsp": "disabled",
                "system_prompt": "explicit minimal reviewer prompt",
                "cwd": "empty owned directory",
            },
            "usage_basis": "OMP assistant-message usage: input_tokens = input + cacheRead + "
            "cacheWrite (cached input is a subset), plus cached_input_tokens, "
            "cache_write_input_tokens and output_tokens; unknown stays null",
            "accounting": "subscription route verified by an Anthropic subscription-window "
            "probe before every turn; this lane has no metered API fallback. "
            "api_equivalent_usd is OMP's list-price estimate, not a charge.",
            "limitations": [
                "OMP does not report which credential served a turn; the subscription "
                "OAuth account is verified by the usage probe, and OMP prefers stored OAuth "
                "over environment API keys, which are also withheld from the child.",
                "thinking level is OMP's default for the model, not pinned here",
                "the served model is verified against the assistant message's "
                "provider/model fields; a missing field fails the turn",
            ],
        }

    @property
    def usage_complete(self) -> bool:
        return self._usage_complete

    @property
    def facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = dict(self._static)
        facts["session_file"] = str(self._session_file) if self._session_file else None
        facts["prompt_audit"] = self._prompt_audit
        return facts

    # -- one turn ---------------------------------------------------------------------

    def _argv(self, prompt: str, *, positional: bool) -> list[str]:
        argv = [
            self._executable, "-p", "--mode", "json", "--model", self.model,
            "--no-tools", "--no-rules", "--no-skills", "--no-extensions", "--no-title",
            "--no-prewalk", "--no-lsp", "--config", str(self._config),
            "--system-prompt", OMP_SYSTEM_PROMPT,
            "--session-dir", str(self.session_dir), "--cwd", str(self.cwd),
        ]
        if self.thread_id is not None:
            argv += ["--resume", self.thread_id]
        if positional:
            argv += ["--", prompt]
        return argv

    def _bind_session(self, stream_ids: list[str], *, required: bool) -> None:
        found: list[tuple[str, Path]] = []
        for path in sorted(self.session_dir.rglob("*.jsonl")):
            session_id = None if path.is_symlink() or not path.is_file() else _session_header_id(path)
            if session_id:
                found.append((session_id, path))
        problem = None
        if len(found) != 1:
            problem = f"expected exactly one OMP session file, found {len(found)}"
        elif self.thread_id is not None and found[0][0] != self.thread_id:
            problem = "OMP resumed a different session; the audit is not one conversation"
        elif any(sid != found[0][0] for sid in stream_ids):
            problem = "OMP stream session id differs from the saved session file"
        if problem is not None:
            if required:
                raise DriverError(problem)
            return
        self.thread_id, self._session_file = found[0]

    def _audit_system_prompt(self) -> dict[str, Any]:
        assert self._session_file is not None
        blocks: list[str] = []
        digests: list[str] = []
        entries = 0
        with self._session_file.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if '"session_init"' not in line and '"system-prompt-digest"' not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "session_init":
                    entries += 1
                    prompt = entry.get("systemPrompt")
                    blocks.extend(prompt if isinstance(prompt, list) else [prompt])
                elif (entry.get("type") == "custom"
                      and entry.get("customType") == "system-prompt-digest"
                      and isinstance(entry.get("data"), str)):
                    digests.append(entry["data"])
        text = "\n".join(block for block in blocks if isinstance(block, str))
        leaked = [marker for marker in _OMP_FORBIDDEN_PROMPT_MARKERS if marker in text]
        if leaked:
            raise DriverError(
                f"OMP system prompt carries memory/context-file content ({', '.join(leaked)}); "
                "the reviewer is not isolated"
            )
        if not text and not digests:
            raise DriverError("OMP persisted neither effective prompt text nor a native base-prompt digest")
        return {
            "session_init_entries": entries, "effective_text_observed": bool(text),
            "system_prompt_chars": len(text) if text else None,
            "system_prompt_sha256": sha256(text.encode()).hexdigest() if text else None,
            "native_base_prompt_digests": digests,
            "provided_prompt_sha256": sha256(OMP_SYSTEM_PROMPT.encode()).hexdigest(),
            "overlay_sha256": sha256(_OMP_CONFIG.encode()).hexdigest(),
            "basis": "Effective text is audited only when persisted. A native opaque digest "
            "is retained otherwise; it does not reveal or certify the full prompt plaintext.",
        }

    def _validate(self, stream: _OmpStream, prompt: str) -> dict[str, Any]:
        forbidden = sorted(set(stream.counts) & _OMP_FORBIDDEN_EVENTS)
        if forbidden:
            raise DriverError(f"OMP emitted events outside a plain no-tool turn: {forbidden}")
        if not stream.agent_ends:
            raise DriverError("OMP stream ended without agent_end")
        if stream.agent_ends[-1].get("isTerminal") is False:
            raise DriverError("OMP turn did not reach a terminal agent_end")
        if len(stream.assistants) != 1:
            raise DriverError(
                f"OMP produced {len(stream.assistants)} assistant messages; exactly one required"
            )
        if stream.users and stream.users[-1].strip() != prompt.strip():
            raise DriverError("OMP did not receive the prompt verbatim")
        message = stream.assistants[0]
        controls = message.get("requestControls")
        tools = controls.get("tools") if isinstance(controls, dict) else None
        if not isinstance(tools, dict) or any(
            tools.get(name) != [] for name in ("declared", "active", "deferred")
        ):
            raise DriverError("OMP did not prove a zero-tool request; built-in and ambient MCP tools must be absent")
        if message.get("stopReason") != "stop":
            detail = _clean(str(message.get("errorMessage") or ""), limit=300)
            raise DriverError(
                f"OMP assistant stopped with {message.get('stopReason')!r}"
                + (f": {detail}" if detail else "")
            )
        content = message.get("content")
        if isinstance(content, list) and any(
            isinstance(p, dict) and p.get("type") not in ("text", "thinking") for p in content
        ):
            raise DriverError("OMP assistant message contains a non-text block (tool attempt)")
        provider, served = message.get("provider"), message.get("model")
        if not isinstance(provider, str) or not isinstance(served, str):
            raise DriverError("OMP did not report the served provider/model; cannot verify it")
        if f"{provider}/{served}" != self.model:
            raise DriverError(f"OMP served {provider}/{served}, not the requested {self.model}")
        text = _message_text(message)
        if not text.strip():
            raise DriverError("OMP returned an empty message")
        if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise DriverError("OMP response exceeded its bound")
        return message

    def turn(self, prompt: str, *, timeout_seconds: float) -> dict[str, Any]:
        data = _check_prompt(prompt)
        timeout = _check_timeout(timeout_seconds)
        reading = _read_subscription(max(1.0, min(30.0, timeout)))
        self.invocations += 1
        try:
            return self._turn(prompt, data, timeout, reading)
        except DriverError:
            self._usage_complete = False
            raise

    def _turn(
        self, prompt: str, data: bytes, timeout: float, reading: dict[str, Any]
    ) -> dict[str, Any]:
        turn_dir = self.directory / f"turn-{self.invocations:03d}"
        turn_dir.mkdir(mode=0o700)
        persist_private_bytes(turn_dir / "prompt.txt", data)
        events_path, log_path = turn_dir / "events.jsonl", turn_dir / "client.log"
        for path in (events_path, log_path):
            path.touch(mode=0o600, exist_ok=False)
        via_stdin = len(data) > OMP_ARGV_PROMPT_BYTES
        argv = self._argv(prompt, positional=not via_stdin)
        feed: dict[str, Any] = {"input": data} if via_stdin else {"stdin": subprocess.DEVNULL}
        env = {
            k: v for k, v in os.environ.items()
            if k in _OMP_ENV_VARS or k.startswith("OMP_AUTH_BROKER_")
        }
        stream_ids: list[str] = []
        try:
            with events_path.open("wb") as out, log_path.open("wb") as err:
                try:
                    completed = subprocess.run(
                        argv, **feed, stdout=out, stderr=err, cwd=self.cwd, env=env,
                        timeout=timeout, check=False,
                    )
                except subprocess.TimeoutExpired as exc:
                    raise DriverError(f"OMP turn timed out after {timeout:.0f}s") from exc
                except OSError as exc:
                    raise DriverError(f"OMP could not run: {type(exc).__name__}") from exc
            if completed.returncode != 0:
                tail = _clean(log_path.read_text(errors="replace")[-400:], limit=300)
                raise DriverError(f"OMP exited {completed.returncode}: {tail}")
            stream = parse_omp_events(events_path)
            stream_ids = stream.header_ids
        finally:
            self._bind_session(stream_ids, required=False)
        message = self._validate(stream, prompt)
        self._bind_session(stream_ids, required=True)
        if self._prompt_audit is None:
            self._prompt_audit = self._audit_system_prompt()
        text = _message_text(message)
        usage, api_usd = _omp_usage(message)
        known = None not in usage.values()
        if known:
            for name, value in usage.items():
                self.cumulative[name] = self.cumulative.get(name, 0) + (value or 0)
        else:
            self._usage_complete = False
        self._api_usd = (
            self._api_usd + api_usd if self._api_usd is not None and api_usd is not None else None
        )
        persist_private_bytes(turn_dir / "response.txt", text.encode())
        _write_json(turn_dir / "reply.json", {
            "at": _now(), "invocation": self.invocations, "thread_id": self.thread_id,
            "model": self.model, "usage": usage, "api_equivalent_usd": api_usd,
            "auto_retries": stream.counts.get("auto_retry_start", 0),
            "prompt_echo_observed": bool(stream.users),
        })
        return {
            "response": text,
            "invocation": self.invocations,
            "thread_id": self.thread_id,
            "directory": str(turn_dir),
            "usage": usage,
            "usage_known": known,
            "cumulative_usage": dict(self.cumulative),
            "text_selection": None,
            "requested_model": self.model,
            "actual_model": f"{message['provider']}/{message['model']}",
            "provider": message["provider"],
            "metered_usd": 0.0,
            "cost_known": True,
            "cost_basis": "anthropic_subscription",
            "api_equivalent_usd": api_usd,
            "subscription_reading": reading,
            "transport": self.facts,
        }

    def accounting(self) -> dict[str, Any]:
        return {
            "lane": "omp",
            "requested_model": self.model,
            "invocations": self.invocations,
            "usage_complete": self._usage_complete,
            "cumulative_usage": dict(self.cumulative),
            "metered_usd": 0.0,
            "cost_basis": "anthropic_subscription",
            "api_equivalent_usd": self._api_usd,
        }


# -- openai-compatible -----------------------------------------------------------------


class OpenAIDriver:
    """A driver-held conversation against one OpenAI-compatible chat-completions endpoint."""

    lane = "openai"

    def __init__(
        self,
        *,
        model: str,
        directory: Path,
        timeout_seconds: float,
        max_usd: float,
        endpoint: str | None,
        api_key_env: str | None,
        input_usd_per_million: float | None,
        output_usd_per_million: float | None,
        transport: InvestigatorTransport | None = None,
    ) -> None:
        if not endpoint or not api_key_env:
            raise DriverError("openai lane requires an explicit endpoint and api_key_env name")
        if not _ENV_NAME.fullmatch(api_key_env):
            raise DriverError("api_key_env must be an environment variable NAME, not a value")
        parsed = urllib.parse.urlsplit(endpoint)
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise DriverError("endpoint must not carry credentials")
        if not _number(max_usd, positive=True):
            raise DriverError("openai lane requires an explicit positive metered max_usd")
        if not (_number(input_usd_per_million, positive=True)
                and _number(output_usd_per_million, positive=True)):
            raise DriverError("openai lane requires explicit positive input and output prices")
        key = os.environ.get(api_key_env, "")
        if not key:
            raise DriverError(f"environment variable {api_key_env} is not set")
        self._key = key
        self.model = model
        try:
            self.transport = transport or OpenAIInvestigator(
                endpoint=endpoint, model=model, api_key=key,
                timeout_seconds=min(timeout_seconds, OPENAI_MAX_TRANSPORT_SECONDS),
                response_format="json_object",
            )
        except ValueError as exc:
            raise DriverError(f"openai transport rejected its settings: {_clean(str(exc), (key,))}") from exc
        if self.transport.model != model:
            raise DriverError("transport model does not match the requested model")
        self.directory = _new_directory(directory)
        try:
            self.budget = InvestigationBudget(
                self.directory / "budget" / "ledger.jsonl",
                budget_usd=float(max_usd),
                max_calls=OPENAI_MAX_REQUESTS,
                input_usd_per_million=float(input_usd_per_million),
                output_usd_per_million=float(output_usd_per_million),
            )
        except (ValueError, OSError) as exc:
            raise DriverError(f"openai budget unavailable: {_clean(str(exc), (key,))}") from exc
        self.journal = self.directory / "journal.jsonl"
        self.thread_id: str | None = f"local-conversation:{uuid.uuid4()}"
        self.invocations = 0
        self.requests_sent = 0
        self.cumulative: dict[str, int] = {}
        self._messages: list[dict[str, str]] = []
        self._usage_complete = True
        self._cost_usd: float | None = 0.0
        self.facts: dict[str, Any] = {
            "lane": "openai",
            "provider": "openai-compatible chat completions",
            "endpoint": getattr(self.transport, "endpoint", endpoint),
            "requested_model": model,
            "api_key_env": api_key_env,
            "thread_id_kind": "local_conversation",
            "conversation": "driver-maintained message list resent in full on every request; "
            "the provider keeps no session",
            "text_only": "native tools are never offered or executed; a provider tool-call "
            "attempt, refusal or non-stop finish reason fails the turn",
            "response_format": "json_object",
            "max_output_tokens": OPENAI_MAX_OUTPUT_TOKENS,
            "budget": {
                "max_usd": float(max_usd),
                "max_requests": OPENAI_MAX_REQUESTS,
                "input_usd_per_million": float(input_usd_per_million),
                "output_usd_per_million": float(output_usd_per_million),
            },
            "accounting": "before each request a worst-case charge (full request UTF-8 "
            "bytes + 1024 framing as input tokens, plus the output ceiling, at the configured "
            "prices) is reserved in a durable ledger and never refunded; a request that would "
            "pass max_usd is refused unsent, so unknown usage cannot authorize unlimited "
            "calls. metered_usd is an estimate from provider-reported usage, not an invoice.",
            "limitations": [
                "the served model must equal the requested model when the provider reports "
                "one; a provider that omits it is recorded as unverified",
                "the full history is resent each turn, so the lifetime reservation grows "
                "with the sum of request sizes",
            ],
        }

    @property
    def usage_complete(self) -> bool:
        return self._usage_complete

    def _record_usage(self, input_tokens: int | None, output_tokens: int | None) -> bool:
        known = (
            input_tokens is not None and output_tokens is not None
            and input_tokens > 0 and output_tokens > 0
        )
        if not known:
            self._usage_complete = False
            self._cost_usd = None
            return False
        assert input_tokens is not None and output_tokens is not None
        self.cumulative["input_tokens"] = self.cumulative.get("input_tokens", 0) + input_tokens
        self.cumulative["output_tokens"] = self.cumulative.get("output_tokens", 0) + output_tokens
        if self._cost_usd is not None:
            self._cost_usd += self.budget.usage_cost(input_tokens, output_tokens)
        return True

    def turn(self, prompt: str, *, timeout_seconds: float) -> dict[str, Any]:
        data = _check_prompt(prompt)
        timeout = _check_timeout(timeout_seconds)
        secrets = (self._key,)
        self.invocations += 1
        turn_no = f"turn-{self.invocations:03d}"
        turn_dir = self.directory / turn_no
        turn_dir.mkdir(mode=0o700)
        persist_private_bytes(turn_dir / "prompt.txt", data, secrets=(self._key.encode(),))
        messages = [*self._messages, {"role": "user", "content": prompt}]
        payload = _payload(
            self.model, messages, OPENAI_MAX_OUTPUT_TOKENS, _OPENAI_SCHEMA,
            response_format="json_object",
        )
        try:
            reservation = self.budget.reserve(
                pass_id=turn_no, payload=payload, max_output_tokens=OPENAI_MAX_OUTPUT_TOKENS,
            )
        except (BudgetExhausted, ValueError, OSError) as exc:
            message = f"no request sent: {_clean(str(exc), secrets)}"
            _write_json(turn_dir / "error.json", {"at": _now(), "error": message}, secrets)
            raise DriverError(message) from None
        self.requests_sent += 1
        _append_event(self.journal, {
            "event": "started", "turn": self.invocations,
            "invocation_id": reservation.invocation_id,
            "request_sha256": sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
            "reserved_usd": reservation.reserved_usd,
        })
        if hasattr(self.transport, "timeout_seconds"):
            self.transport.timeout_seconds = min(timeout, OPENAI_MAX_TRANSPORT_SECONDS)
        try:
            completion = self.transport.complete(
                messages, max_output_tokens=OPENAI_MAX_OUTPUT_TOKENS, schema=_OPENAI_SCHEMA,
            )
        except Exception as exc:  # noqa: BLE001 - every provider failure keeps its evidence
            observed = exc.observed_usage if isinstance(exc, InvestigatorError) else None
            self.budget.finish(reservation, None, failed=True, observed_usage=observed)
            self._record_usage(*(observed or (None, None)))
            detail: dict[str, Any] = {}
            if isinstance(exc, InvestigatorError):
                detail = {
                    "http_status": exc.http_status,
                    "provider_detail": _clean(exc.provider_detail, secrets)
                    if exc.provider_detail else None,
                    "finish_reason": exc.finish_reason,
                }
            message = f"{type(exc).__name__}: {exc}"
            if detail.get("finish_reason") in {"tool_calls", "function_call"}:
                message += " (provider attempted a tool call; native tools are never executed)"
            if detail.get("http_status") is not None:
                reason = detail.get("provider_detail") or "no detail"
                message += f" (HTTP {detail['http_status']}: {reason})"
            message = _clean(message, secrets, 800)
            record = {"at": _now(), "error": message, "observed_usage": observed, **detail}
            _write_json(turn_dir / "error.json", record, secrets)
            _append_event(self.journal, {"event": "failed", "turn": self.invocations, **{
                k: v for k, v in record.items() if k != "at"}})
            raise DriverError(message) from None
        self.budget.finish(reservation, completion, failed=False)
        known = self._record_usage(completion.input_tokens, completion.output_tokens)
        _write_json(turn_dir / "completion.json", {
            "at": _now(), "request_id": completion.request_id,
            "returned_model": completion.returned_model,
            "input_tokens": completion.input_tokens, "output_tokens": completion.output_tokens,
        }, secrets)
        persist_private_bytes(turn_dir / "response.txt", completion.content.encode(),
                              secrets=(self._key.encode(),))
        _append_event(self.journal, {
            "event": "completed", "turn": self.invocations,
            "invocation_id": reservation.invocation_id, "request_id": completion.request_id,
        })
        if completion.returned_model is not None and completion.returned_model != self.model:
            raise DriverError(
                f"provider served {_clean(completion.returned_model, secrets, 120)!r}, not the "
                f"requested {self.model!r}; fallback is forbidden and the reply was discarded"
            )
        if (
            completion.input_tokens is not None
            and completion.input_tokens > reservation.input_token_bound
        ) or (
            completion.output_tokens is not None
            and completion.output_tokens > reservation.output_token_bound
        ):
            raise DriverError("provider usage exceeded the conservative reservation; budget held")
        if not completion.content.strip():
            raise DriverError("provider returned an empty message")
        if len(completion.content.encode("utf-8")) > MAX_RESPONSE_BYTES:
            raise DriverError("provider response exceeded its bound")
        self._messages = [*messages, {"role": "assistant", "content": completion.content}]
        usage = {
            "input_tokens": completion.input_tokens if known else None,
            "output_tokens": completion.output_tokens if known else None,
        }
        estimate = (
            self.budget.usage_cost(completion.input_tokens, completion.output_tokens)  # type: ignore[arg-type]
            if known else None
        )
        return {
            "response": completion.content,
            "invocation": self.invocations,
            "thread_id": self.thread_id,
            "directory": str(turn_dir),
            "usage": usage,
            "usage_known": known,
            "cumulative_usage": dict(self.cumulative),
            "text_selection": None,
            "requested_model": self.model,
            "actual_model": completion.returned_model,
            "served_model_reported": completion.returned_model is not None,
            "provider": "openai-compatible",
            "request_id": completion.request_id,
            "metered_usd": estimate,
            "cost_known": estimate is not None,
            "cost_basis": "reported_usage_at_configured_prices",
            "reserved_usd": reservation.reserved_usd,
            "api_equivalent_usd": None,
            "transport": self.facts,
        }

    def accounting(self) -> dict[str, Any]:
        _, reserved = self.budget.totals()
        return {
            "lane": "openai",
            "requested_model": self.model,
            "invocations": self.invocations,
            "requests_sent": self.requests_sent,
            "usage_complete": self._usage_complete,
            "cumulative_usage": dict(self.cumulative),
            "metered_usd": self._cost_usd,
            "metered_usd_upper_bound": reserved,
            "cost_basis": "reported_usage_at_configured_prices; upper bound is the sum of "
            "never-refunded reservations",
            "api_equivalent_usd": None,
        }


# -- factory ---------------------------------------------------------------------------


def make_driver(
    *,
    lane: str,
    model: str,
    directory: Path,
    repo_root: Path,
    timeout_seconds: float,
    max_usd: float = 0,
    endpoint: str | None = None,
    api_key_env: str | None = None,
    input_usd_per_million: float | None = None,
    output_usd_per_million: float | None = None,
) -> Driver:
    """Build the one driver for an audit; contradictory settings are refused, not ignored."""
    seconds = _check_timeout(timeout_seconds)
    if not _number(max_usd):
        raise DriverError("max_usd must be a finite non-negative number")
    metered = {
        "endpoint": endpoint, "api_key_env": api_key_env,
        "input_usd_per_million": input_usd_per_million,
        "output_usd_per_million": output_usd_per_million,
    }
    if lane in ("codex", "omp"):
        stray = [name for name, value in metered.items() if value is not None]
        if max_usd != 0 or stray:
            raise DriverError(
                f"the {lane} lane is subscription-only; refused budget/endpoint/key/price "
                f"settings: {['max_usd'] * (max_usd != 0) + stray}"
            )
        if lane == "codex":
            return CodexDriver(
                model=model, directory=directory, repo_root=repo_root, timeout_seconds=seconds
            )
        return OmpDriver(model=model, directory=directory, timeout_seconds=seconds)
    if lane == "openai":
        return OpenAIDriver(
            model=model, directory=directory, timeout_seconds=seconds, max_usd=max_usd,
            endpoint=endpoint, api_key_env=api_key_env,
            input_usd_per_million=input_usd_per_million,
            output_usd_per_million=output_usd_per_million,
        )
    raise DriverError(f"unknown lane {lane!r}; expected one of {', '.join(LANES)}")
