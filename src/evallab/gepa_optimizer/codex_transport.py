"""One text-only GEPA reflection through an authenticated Codex subscription.

User config, project instructions and tool features are disabled; the CLI runs
ephemerally in an empty read-only workspace. This is not host file-read isolation.
Only a complete, single-turn text response is usable. Quota and billing remain
unknown; request limits count CLI invocations, not physical upstream requests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from evallab.execution_contracts import persist_private_bytes

#: Installed subscription CLI under test. Pinned to the Homebrew path so a
#: shadowed ``codex`` earlier on PATH cannot change the route.
CODEX_EXECUTABLE = Path("/opt/homebrew/bin/codex")
CODEX_VERSION = "codex-cli 0.154.0"

#: ChatGPT-subscription provider id (``OPENAI_PROVIDER_ID`` in
#: ``codex-rs/model-provider-info``). Pinned so a configured default can
#: never route the turn to an API-key or local provider.
MODEL_PROVIDER = "openai"

#: ``codex exec`` JSONL is small; bound it like the OpenCode transcript.
MAX_TRANSCRIPT_BYTES = 4 * 1024 * 1024

#: A GEPA reflection answer is short instructions text; anything larger is
#: not a usable single-turn proposal.
MAX_RESPONSE_BYTES = 1024 * 1024

# Only runtime/auth-location settings reach the child, never API credentials,
# provider overrides, proxy settings, inherited tool config or telemetry secrets.
_CHILD_ENV_VARS = frozenset(
    {"PATH", "HOME", "CODEX_HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
)

#: Stable ``--disable`` set. Every key below was observed in
#: ``codex features list`` as ``stable`` + default-on, and each maps to
#: ``-c features.<key>=false``. Shell/collab removals are additionally
#: confirmed by tool-registration gates in
#: ``codex-rs/core/src/tools/spec_plan.rs`` (``Feature::ShellTool``,
#: ``Feature::UnifiedExec``, ``Feature::Collab``) and the hook runtime gate
#: on ``Feature::CodexHooks`` (``codex-rs/core/src/session/mod.rs``).
DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "unified_exec_tty",
    "multi_agent",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "apps",
    "plugins",
    "plugin_sharing",
    "remote_plugin",
    "hooks",
    "view_image",
    "image_generation",
    "sleep_tool",
    "tool_suggest",
    "skill_search",
    "skill_mcp_dependency_install",
)

#: ``ThreadItem`` detail types that are never part of a usable text-only
#: turn. ``agent_message`` is the response; ``reasoning`` summaries are
#: ignored. Everything else (including ``todo_list`` plan tracking and any
#: unknown future item) fails the turn closed.
_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "collab_tool_call",
        "web_search",
        "todo_list",
        "error",
    }
)

#: ``codex exec --json`` envelope types (``codex-rs/exec/src/exec_events.rs``).
#: Anything outside this set fails closed so a future event kind can never
#: smuggle tool activity past the parser.
_KNOWN_EVENT_TYPES = frozenset(
    {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "item.started",
        "item.updated",
        "item.completed",
        "error",
    }
)

# Probe points so deterministic tests can stub the CLI/Keychain boundary
# without touching stdlib subprocess globally.
_RUN = subprocess.run
_POPEN = subprocess.Popen


class CodexTransportError(RuntimeError):
    """Retained transport failure; callers must not automatically retry."""


def _scrub_env(source: dict[str, str]) -> dict[str, str]:
    """Drop auth/provider overrides; never add key material."""
    return {key: value for key, value in source.items() if key in _CHILD_ENV_VARS}


def _bare_model(model: str) -> str:
    if not isinstance(model, str) or not re.fullmatch(r"codex/[A-Za-z0-9][A-Za-z0-9._-]*", model):
        raise ValueError("Codex transport requires an explicit codex/<model> selector")
    return model.removeprefix("codex/")


def _executable() -> Path:
    path = CODEX_EXECUTABLE.resolve()
    if not path.is_file():
        raise CodexTransportError("Installed Codex executable is unavailable")
    return path


def _run_probe(argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[bytes]:
    return _RUN(
        argv,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=_scrub_env(dict(os.environ)),
    )


def _startup_warning(item: Any, kind: str, started: bool) -> bool:
    # Codex serializes nonterminal config/warning notifications as ErrorItem.
    # Fatal errors are top-level error/turn.failed events. Preserve warnings in
    # events.jsonl, but never accept a model-reroute notification or turn error.
    return (
        not started
        and kind == "item.completed"
        and isinstance(item, dict)
        and item.get("type") == "error"
        and isinstance(item.get("message"), str)
        and not item["message"].startswith("model rerouted:")
    )


def parse_transcript(path: Path) -> tuple[str, dict[str, Any]]:
    """Accept exactly one completed text-only turn; reject everything else.

    Returns ``(response, usage)`` where ``usage`` is the honest token report
    from the single ``turn.completed`` event. Raises
    :class:`CodexTransportError` on error events, failed turns, missing or
    repeated completions, empty output, any tool item (including mere
    attempts that never completed), unknown event/item shapes, or oversize
    transcripts.
    """
    if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        raise CodexTransportError("Codex transcript exceeded its bound")
    completions = 0
    starts = 0
    agent_texts: list[str] = []
    usage: dict[str, Any] | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise CodexTransportError("Invalid Codex event") from exc
        if not isinstance(event, dict):
            raise CodexTransportError("Invalid Codex event")
        kind = event.get("type")
        if kind not in _KNOWN_EVENT_TYPES:
            raise CodexTransportError(f"Unknown Codex event type: {kind!r}")
        if kind == "turn.started":
            starts += 1
            if starts > 1 or completions:
                raise CodexTransportError("Codex returned more than one turn")
        if completions:
            raise CodexTransportError("Codex emitted output after completing its turn")
        if kind == "error":
            raise CodexTransportError(
                f"Codex reported an error: {str(event.get('message', ''))[:200]}"
            )
        if kind == "turn.failed":
            detail = event.get("error", {})
            message = detail.get("message", "") if isinstance(detail, dict) else ""
            raise CodexTransportError(
                f"Codex turn failed: {str(message)[:200]}"
            )
        if kind == "turn.completed":
            completions += 1
            if completions > 1:
                raise CodexTransportError("Codex returned more than one completion")
            payload = event.get("usage")
            if not isinstance(payload, dict):
                raise CodexTransportError("Codex completion is missing usage")
            usage = {
                name: payload[name]
                for name in (
                    "input_tokens",
                    "cached_input_tokens",
                    "cache_write_input_tokens",
                    "output_tokens",
                    "reasoning_output_tokens",
                )
                if name in payload
            }
            for value in usage.values():
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise CodexTransportError("Codex usage report is invalid")
            continue
        if kind in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if _startup_warning(item, kind, starts > 0):
                continue
            if not isinstance(item, dict):
                raise CodexTransportError("Invalid Codex thread item")
            detail = item.get("type")
            if detail in _TOOL_ITEM_TYPES:
                raise CodexTransportError(
                    f"Codex attempted a tool call ({detail}); no text extracted"
                )
            if detail == "agent_message":
                if kind == "item.completed":
                    text = item.get("text")
                    if not isinstance(text, str):
                        raise CodexTransportError("Codex agent message is invalid")
                    agent_texts.append(text)
            elif detail == "reasoning":
                continue
            else:
                raise CodexTransportError(f"Unknown Codex item type: {detail!r}")
    if starts != 1 or completions != 1 or usage is None:
        raise CodexTransportError("Codex returned no complete single turn")
    if len(agent_texts) != 1:
        raise CodexTransportError("Codex returned no complete single-turn message")
    response = agent_texts[0]
    if not response.strip():
        raise CodexTransportError("Codex returned an empty message")
    if len(response.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise CodexTransportError("Codex response exceeded its bound")
    return response, usage


def _poll_events(
    path: Path, offset: int, pending: bytes, started: bool = False
) -> tuple[int, bytes, bool]:
    """Fail fast on tool/error output while the child is still writing.

    Consumes only complete JSONL records; a trailing partial line (possibly
    split mid-UTF-8-sequence) stays buffered. Reasoning and agent-message
    traffic passes; anything else raises.
    """
    if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        raise CodexTransportError("Codex output limit reached")
    with path.open("rb") as stream:
        stream.seek(offset)
        data = stream.read(MAX_TRANSCRIPT_BYTES - offset + 1)
    if not data:
        return offset, pending, started
    offset += len(data)
    if offset > MAX_TRANSCRIPT_BYTES:
        raise CodexTransportError("Codex output limit reached")
    lines = (pending + data).split(b"\n")
    for line in lines[:-1]:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexTransportError("Invalid Codex event") from exc
        if not isinstance(event, dict):
            raise CodexTransportError("Invalid Codex event")
        kind = event.get("type")
        if kind not in _KNOWN_EVENT_TYPES:
            raise CodexTransportError(f"Unknown Codex event type: {kind!r}")
        if kind in {"error", "turn.failed"}:
            raise CodexTransportError("Codex reported an error or failed the turn")
        if kind == "turn.started":
            started = True
        if kind in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if _startup_warning(item, kind, started):
                continue
            detail = item.get("type") if isinstance(item, dict) else None
            if detail in _TOOL_ITEM_TYPES or (
                detail not in {"agent_message", "reasoning"}
            ):
                raise CodexTransportError(
                    "Codex attempted a tool call; no automatic retry"
                )
    return offset, lines[-1], started


def _stop(process: Any) -> None:
    if process.poll() is None:
        with suppress(ProcessLookupError, OSError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError, OSError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


class CodexTransport:
    """One text-only GEPA reflection turn via the Codex subscription CLI."""

    def __init__(self, *, repo_root: Path, model: str, timeout_seconds: int = 900):
        self.repo_root = repo_root.resolve()
        self.model = _bare_model(model)
        self.timeout_seconds = timeout_seconds

    def _version_digest(self, executable: Path) -> tuple[str, str]:
        completed = _run_probe([str(executable), "--version"], timeout=20)
        if completed.returncode != 0:
            raise CodexTransportError("Codex version probe failed")
        version = completed.stdout.decode(errors="replace").strip()
        if version != CODEX_VERSION:
            raise CodexTransportError(f"Codex subscription transport requires {CODEX_VERSION}")
        with executable.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return version, "sha256:" + digest

    def preflight(self) -> dict[str, Any]:
        """Verify the ChatGPT-subscription route; return stable JSON-safe facts."""
        executable = _executable()
        version, digest = self._version_digest(executable)
        completed = _run_probe([str(executable), "login", "status"], timeout=30)
        output = (
            completed.stdout.decode(errors="replace")
            + completed.stderr.decode(errors="replace")
        )
        if completed.returncode != 0 or "ChatGPT" not in output:
            raise CodexTransportError(
                "Codex ChatGPT subscription login is required; "
                "API-key auth and logged-out state are refused"
            )
        return {
            "transport": "codex",
            "model": f"codex/{self.model}",
            "authenticated_route": "chatgpt_subscription",
            "model_provider": MODEL_PROVIDER,
            "executable": str(executable),
            "executable_sha256": digest,
            "version": version,
            "text_only": {
                "sandbox": "read-only",
                "ignore_user_config": True,
                "ignore_rules": True,
                "ephemeral": True,
                "disabled_features": list(DISABLED_FEATURES),
                "web_search": "disabled",
                "analytics": "disabled",
                "feedback": "disabled",
                "file_patch_tool": "no_cli_disable_flag;_blocked_by_read_only_sandbox_and_parser",
            },
        }

    def _argv(self, *, cwd: Path, last_message: Path) -> list[str]:
        executable = _executable()
        argv = [
            str(executable),
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "-m",
            self.model,
            "-c",
            f'model_provider="{MODEL_PROVIDER}"',
            "-c",
            'web_search="disabled"',
            "-c",
            "analytics.enabled=false",
            "-c",
            "feedback.enabled=false",
            "-c",
            "project_doc_max_bytes=0",
            "-c",
            "features.skip_host_skill_discovery=true",
        ]
        for feature in DISABLED_FEATURES:
            argv.extend(("--disable", feature))
        argv.extend(
            (
                "-C",
                str(cwd),
                "-o",
                str(last_message),
                "-",
            )
        )
        return argv

    def request(self, prompt: str | list[dict[str, Any]], *, directory: Path) -> dict[str, Any]:
        """Run exactly one completed text-only turn; never retry or fall back."""
        facts = self.preflight()
        if any(part.is_symlink() for part in (directory, *directory.parents)):
            raise ValueError("Proposer directories must not follow symlinks")
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        cwd = directory / "cwd"
        cwd.mkdir(mode=0o700)
        text = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)
        if not text.strip():
            raise ValueError("Codex proposer prompt must not be empty")
        prompt_file = directory / "prompt.txt"
        persist_private_bytes(prompt_file, text.encode(), secrets=())
        events_path = directory / "events.jsonl"
        log_path = directory / "client.log"
        last_message = directory / "last_message.txt"
        events_path.touch(mode=0o600, exist_ok=False)
        log_path.touch(mode=0o600, exist_ok=False)
        last_message.touch(mode=0o600, exist_ok=False)
        argv = self._argv(cwd=cwd, last_message=last_message)
        env = _scrub_env(dict(os.environ))
        with (
            events_path.open("ab") as stdout,
            log_path.open("ab") as stderr,
            prompt_file.open("rb") as stdin,
        ):
            process = _POPEN(
                argv,
                env=env,
                cwd=cwd,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
            try:
                event_offset, pending_event = 0, b""
                turn_started = False
                deadline = time.monotonic() + self.timeout_seconds
                while process.poll() is None:
                    event_offset, pending_event, turn_started = _poll_events(
                        events_path, event_offset, pending_event, turn_started
                    )
                    if log_path.stat().st_size > MAX_TRANSCRIPT_BYTES:
                        raise CodexTransportError("Codex output limit reached")
                    if time.monotonic() >= deadline:
                        raise CodexTransportError("Codex timed out; no automatic retry")
                    time.sleep(0.1)
                if process.returncode != 0:
                    raise CodexTransportError(
                        f"Codex exited with status {process.returncode}; "
                        "inspect retained logs"
                    )
            finally:
                _stop(process)
        if any(
            path.stat().st_size > MAX_TRANSCRIPT_BYTES
            for path in (events_path, log_path, last_message)
        ):
            raise CodexTransportError("Codex output limit reached")
        response, usage = parse_transcript(events_path)
        return {
            "response": response,
            "upstream_estimated_cost_usd": None,
            "usage": {**usage, "requested_model": f"codex/{self.model}"},
            "transport": facts,
        }
