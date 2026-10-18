"""One text-only GEPA reflection through an authenticated Codex subscription.

User config, project instructions and tool features are disabled for every vote.
An optional real, metadata-only native seed gives fresh ephemeral forks a shared
cache-routing key and blind empty cwd, never another vote's history or output.
This is not host file-read isolation, and provider cache hits are not guaranteed.
Only a complete, single-turn text response is usable. Quota and billing remain
unknown; request limits count CLI invocations, not physical upstream requests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from evallab.execution_contracts import persist_private_bytes

#: Installed subscription CLI under test. Pinned to the Homebrew path so a
#: shadowed ``codex`` earlier on PATH cannot change the route.
CODEX_EXECUTABLE = Path("/opt/homebrew/bin/codex")
CODEX_VERSION = "codex-cli 0.162.1"

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
_SEED_POPEN = subprocess.Popen


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
    # Preserve warnings in events.jsonl, but never accept a model reroute.
    # In-turn errors are fatal except the CLI's bounded reconnect notification.
    return (
        not started
        and kind == "item.completed"
        and isinstance(item, dict)
        and item.get("type") == "error"
        and isinstance(item.get("message"), str)
        and not item["message"].startswith("model rerouted:")
    )


def _reconnect_notification(event: dict[str, Any], *, started: bool) -> bool:
    """A CLI reconnect notice is not a terminal outcome or permission to reroute."""
    message = event.get("message")
    return (
        started
        and event.get("type") == "error"
        and isinstance(message, str)
        and re.match(r"^Reconnecting\.\.\. [1-9]\d*/[1-9]\d* \(", message) is not None
        and "model rerouted:" not in message.casefold()
    )


def parse_transcript(path: Path, *, allow_preamble: bool = False) -> tuple[str, dict[str, Any]]:
    """Accept exactly one completed text-only turn; reject everything else.

    Returns ``(response, usage)`` where ``usage`` is the honest token report
    from the single ``turn.completed`` event. CLI reconnect notifications are
    retained, but only a later completed turn can yield text. Fatal error
    events, failed/missing/repeated completions, empty output, tool attempts,
    unknown shapes, and oversized transcripts still fail closed.
    Proposal callers may select the final message after a text preamble;
    that opt-in never relaxes lifecycle, error, usage or tool-call checks.
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
        if _reconnect_notification(event, started=starts > 0):
            continue
        if kind == "error":
            raise CodexTransportError(
                f"Codex reported an error: {str(event.get('message', ''))[:200]}"
            )
        if kind == "turn.failed":
            detail = event.get("error", {})
            message = detail.get("message", "") if isinstance(detail, dict) else ""
            raise CodexTransportError(f"Codex turn failed: {str(message)[:200]}")
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
    if not agent_texts or (len(agent_texts) != 1 and not allow_preamble):
        raise CodexTransportError("Codex returned no complete single-turn message")
    response = agent_texts[-1]
    if not response.strip():
        raise CodexTransportError("Codex returned an empty message")
    if len(response.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise CodexTransportError("Codex response exceeded its bound")
    if len(agent_texts) > 1:
        usage["text_selection"] = "final completed message of one text-only turn"
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
        if _reconnect_notification(event, started=started):
            continue
        if kind in {"error", "turn.failed"}:
            raise CodexTransportError("Codex reported an error or failed the turn")
        if kind == "turn.started":
            started = True
        if kind in {"item.started", "item.updated", "item.completed"}:
            item = event.get("item")
            if _startup_warning(item, kind, started):
                continue
            detail = item.get("type") if isinstance(item, dict) else None
            if detail in _TOOL_ITEM_TYPES or (detail not in {"agent_message", "reasoning"}):
                raise CodexTransportError("Codex attempted a tool call; no automatic retry")
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


def _no_symlink_directory(path: Path) -> Path:
    absolute = path.absolute()
    if any(part.is_symlink() for part in (absolute, *absolute.parents)):
        raise ValueError("Codex directories must not follow symlinks")
    return absolute.resolve()


def _require_empty_prefill_cwd(cwd: Path) -> None:
    _no_symlink_directory(cwd)
    if not cwd.is_dir() or next(cwd.iterdir(), None) is not None:
        raise ValueError("Shared Codex prefill cwd must remain an empty directory")


def _empty_seed_bytes(path: Path, *, thread_id: str, cwd: Path, model: str) -> bytes:
    """Accept only native model instructions and one root metadata record."""
    _no_symlink_directory(path)
    if not path.is_file() or path.stat().st_size > MAX_TRANSCRIPT_BYTES:
        raise CodexTransportError("Codex prefill seed is not a bounded regular file")
    raw = path.read_bytes()
    try:
        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if (
            len(records) != 1
            or records[0]["type"] != "session_meta"
            or set(records[0]) != {"timestamp", "type", "payload"}
        ):
            raise ValueError("history")
        meta = records[0]["payload"]
        allowed = {
            "creator_user_id", "creator_account_id", "session_id", "id", "timestamp",
            "cwd", "runtime_workspace_roots", "originator", "cli_version", "source",
            "model_provider", "base_instructions", "history_mode", "context_window",
        }
        instructions = meta["base_instructions"]
        window = meta.get("context_window")
        if window is not None:
            if set(window) != {"window_id"}:
                raise ValueError("context window")
            UUID(window["window_id"])
        if (
            set(meta) - allowed
            or str(UUID(thread_id)) != thread_id
            or meta["id"] != thread_id
            or meta["session_id"] != thread_id
            or meta["cwd"] != str(cwd)
            or meta["runtime_workspace_roots"] != [str(cwd)]
            or meta["model_provider"] != MODEL_PROVIDER
            or meta["cli_version"] != CODEX_VERSION.removeprefix("codex-cli ")
            or meta["history_mode"] != "legacy"
            or set(instructions) != {"text", "provenance"}
            or not isinstance(instructions["text"], str)
            or not instructions["text"].strip()
            or instructions["provenance"] != {"type": "model", "model": model}
        ):
            raise ValueError("context")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CodexTransportError("Codex prefill seed contains history or custom context") from exc
    return raw


@dataclass(frozen=True)
class _EmptySeed:
    thread_id: str
    rollout: Path
    snapshot: Path
    content: bytes

    def require_unchanged(self, *, cwd: Path, model: str) -> None:
        current = _empty_seed_bytes(self.rollout, thread_id=self.thread_id, cwd=cwd, model=model)
        _no_symlink_directory(self.snapshot)
        if current != self.content or self.snapshot.read_bytes() != self.content:
            raise CodexTransportError("Codex prefill seed changed; retained evidence is unusable")


def _require_seed_policy_read_only(home: Path) -> None:
    """Allow policy loading, but never trigger Codex's one-shot policy rewrite."""
    marker = home / ".sandbox_migration"
    if marker.is_symlink() or not marker.is_file():
        raise CodexTransportError(
            "Codex seed requires an existing native migration marker; refusing policy migration"
        )
    _no_symlink_directory(marker)


def _create_empty_seed(*, directory: Path, cwd: Path, model: str) -> _EmptySeed:
    """Materialize a genuine model-free thread; never submit a turn or user text.

    Standalone app-server lacks exec's config/rules isolation flags. Disable
    hooks and legacy notify per process/thread and reject custom instructions
    or provider routes. Native execpolicy may load, but cannot enter a strictly
    metadata-only seed; an existing migration marker prevents policy rewrites.
    """
    directory.mkdir(mode=0o700, exist_ok=False)
    requests_path = directory / "requests.jsonl"
    events_path = directory / "events.jsonl"
    log_path = directory / "client.log"
    for path in (requests_path, events_path, log_path):
        path.touch(mode=0o600, exist_ok=False)
    argv = [
        str(_executable()), "app-server", "--stdio",
        "-c", f'model_provider="{MODEL_PROVIDER}"',
        "-c", 'web_search="disabled"',
        "-c", "analytics.enabled=false", "-c", "feedback.enabled=false",
        "-c", "project_doc_max_bytes=0", "-c", "features.skip_host_skill_discovery=true",
        "-c", "notify=[]",
    ]
    for feature in DISABLED_FEATURES:
        argv.extend(("--disable", feature))
    with requests_path.open("ab") as requests, events_path.open("ab") as events, log_path.open(
        "ab"
    ) as log, selectors.DefaultSelector() as selector:
        process = _SEED_POPEN(
            argv, cwd=cwd, env=_scrub_env(dict(os.environ)), stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=log, start_new_session=True, bufsize=0,
        )
        pending = b""
        received = 0
        deadline = time.monotonic() + 60
        selector.register(process.stdout, selectors.EVENT_READ)

        def next_message() -> dict[str, Any] | None:
            nonlocal pending, received
            while b"\n" not in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise CodexTransportError("Codex model-free seed creation timed out")
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    if pending:
                        raise CodexTransportError("Codex seed server returned partial JSON")
                    return None
                received += len(chunk)
                if received > MAX_TRANSCRIPT_BYTES or log_path.stat().st_size > MAX_TRANSCRIPT_BYTES:
                    raise CodexTransportError("Codex seed server exceeded its output bound")
                pending += chunk
            line, pending = pending.split(b"\n", 1)
            try:
                message = json.loads(line)
            except (ValueError, UnicodeDecodeError) as exc:
                raise CodexTransportError("Codex seed server returned invalid JSON") from exc
            if not isinstance(message, dict):
                raise CodexTransportError("Codex seed server returned a non-object")
            # Config may contain credentials. Never retain its values or layers.
            retained = {"id": 2, "configuration_redacted": True} if message.get("id") == 2 else message
            events.write((json.dumps(retained, ensure_ascii=False) + "\n").encode())
            events.flush()
            method = message.get("method", "")
            if not isinstance(method, str) or method.startswith(
                ("turn/", "item/", "mcpServer/", "hook/", "hooks/", "codex/event/")
            ):
                raise CodexTransportError("Codex seed server attempted nonempty/tool activity")
            return message

        def rpc(identifier: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
            payload = (json.dumps({"id": identifier, "method": method, "params": params}) + "\n").encode()
            requests.write(payload)
            requests.flush()
            process.stdin.write(payload)
            process.stdin.flush()
            while (message := next_message()) is not None:
                if "id" not in message:
                    continue
                if message["id"] != identifier or "error" in message or not isinstance(message.get("result"), dict):
                    raise CodexTransportError(f"Codex model-free seed {method} failed; inspect retained logs")
                return message["result"]
            raise CodexTransportError("Codex seed server exited before completing setup")

        try:
            initialized = rpc(1, "initialize", {
                "clientInfo": {"name": "evallab-empty-prefill", "version": "1"},
                "capabilities": {"experimentalApi": True},
            })
            home = _no_symlink_directory(Path(initialized["codexHome"]))
            config = rpc(2, "config/read", {"cwd": str(cwd)})["config"]
            if (
                not isinstance(config, dict)
                or any(config.get(key) for key in (
                    "instructions", "base_instructions", "user_instructions", "developer_instructions",
                    "model_instructions_file", "model_catalog_json",
                ))
                or config.get("model_provider") != MODEL_PROVIDER
                or config.get("model_providers", {}).get(MODEL_PROVIDER)
            ):
                raise CodexTransportError("Codex seed requires native instructions and provider routing")
            features = config.get("features")
            if (
                not isinstance(features, dict)
                or any(features.get(feature) is not False for feature in DISABLED_FEATURES)
                or config.get("notify") != []
                or config.get("project_doc_max_bytes") != 0
            ):
                raise CodexTransportError("Codex seed hook/tool/instruction isolation is unknown")
            _require_seed_policy_read_only(home)
            servers = config.get("mcp_servers", {})
            if not isinstance(servers, dict):
                raise CodexTransportError("Codex seed MCP configuration is unknown")
            started = rpc(3, "thread/start", {
                "model": model, "modelProvider": MODEL_PROVIDER, "cwd": str(cwd),
                "approvalPolicy": "never", "sandbox": "read-only", "ephemeral": False,
                "historyMode": "legacy", "developerInstructions": "",
                "config": {
                    "mcp_servers": {name: {"enabled": False} for name in servers},
                    "features": {feature: False for feature in DISABLED_FEATURES},
                    "notify": [], "project_doc_max_bytes": 0, "web_search": "disabled",
                    "developer_instructions": "",
                },
            })["thread"]
            thread_id = started["id"]
            # An unused thread is lazy. Explicit ungrouped placement is the
            # supported model-free operation that persists its real metadata.
            rpc(4, "thread/section/move", {"threadId": thread_id, "sectionId": None})
            thread = rpc(5, "thread/read", {"threadId": thread_id, "includeTurns": True})["thread"]
            if (
                thread["id"] != thread_id or thread["sessionId"] != thread_id
                or thread["turns"] != [] or thread["preview"] != ""
                or thread["ephemeral"] is not False or thread.get("forkedFromId") is not None
                or thread.get("parentThreadId") is not None or thread["historyMode"] != "legacy"
            ):
                raise CodexTransportError("Codex seed is not a fresh empty root thread")
            rollout = _no_symlink_directory(Path(thread["path"]))
            _require_seed_policy_read_only(home)
            if not rollout.is_relative_to(home / "sessions"):
                raise CodexTransportError("Codex seed rollout escaped native session storage")
            content = _empty_seed_bytes(rollout, thread_id=thread_id, cwd=cwd, model=model)
            snapshot = directory / "seed-rollout.jsonl"
            persist_private_bytes(snapshot, content, secrets=())
            seed = _EmptySeed(thread_id, rollout, snapshot, content)
            process.stdin.close()
            while next_message() is not None:
                pass
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if process.returncode != 0:
                raise CodexTransportError("Codex seed server failed during shutdown")
        except (KeyError, TypeError, AttributeError, OSError, subprocess.TimeoutExpired) as exc:
            raise CodexTransportError("Codex model-free seed creation failed; inspect retained evidence") from exc
        finally:
            if not process.stdin.closed:
                process.stdin.close()
            _stop(process)
            process.stdout.close()
    seed.require_unchanged(cwd=cwd, model=model)
    _require_seed_policy_read_only(home)
    _require_empty_prefill_cwd(cwd)
    return seed


def _independent_fork_id(path: Path, *, seed: _EmptySeed, used: set[str]) -> str:
    threads = [
        event.get("thread_id")
        for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
        if (event := json.loads(line)).get("type") == "thread.started"
    ]
    if len(threads) != 1 or not isinstance(threads[0], str):
        raise CodexTransportError("Codex fork did not report exactly one fresh thread")
    thread_id = threads[0]
    try:
        if str(UUID(thread_id)) != thread_id:
            raise ValueError("noncanonical thread id")
    except ValueError as exc:
        raise CodexTransportError("Codex fork reported an invalid native thread id") from exc
    if thread_id == seed.thread_id or thread_id in used:
        raise CodexTransportError("Codex fork reused a prior lifecycle/thread identity")
    used.add(thread_id)
    return thread_id


class CodexTransport:
    """One text-only GEPA reflection turn via the Codex subscription CLI."""

    def __init__(
        self,
        *,
        repo_root: Path,
        model: str,
        timeout_seconds: int = 900,
        prefill_cwd: Path | None = None,
    ):
        self.repo_root = repo_root.resolve()
        self.model = _bare_model(model)
        self.timeout_seconds = timeout_seconds
        self.prefill_cwd = prefill_cwd
        self._prefill_seed: _EmptySeed | None = None
        self._seed_attempted = False
        self._used_thread_ids: set[str] = set()

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
        output = completed.stdout.decode(errors="replace") + completed.stderr.decode(
            errors="replace"
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
            )
        )
        if self._prefill_seed is not None:
            argv.extend(("fork", self._prefill_seed.thread_id))
        argv.append("-")
        return argv

    def request(self, prompt: str | list[dict[str, Any]], *, directory: Path) -> dict[str, Any]:
        """Run one fresh ephemeral turn; retain unique evidence, never retry.

        ``prefill_cwd`` enables fresh native ephemeral forks of one real,
        metadata-only empty seed. Only its cache-routing key and empty cwd
        repeat; seed purity and immutable bytes are checked before each launch
        and before accepting text. All prompt/output artifacts remain unique.
        """
        facts = self.preflight()
        cwd = directory / "cwd"
        if self.prefill_cwd is not None:
            directory = _no_symlink_directory(directory)
            cwd = _no_symlink_directory(self.prefill_cwd)
            if cwd.is_relative_to(directory) or directory.is_relative_to(cwd):
                raise ValueError("Codex prefill cwd and evidence directory must be disjoint")
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        cwd.mkdir(parents=True, exist_ok=self.prefill_cwd is not None, mode=0o700)
        if self.prefill_cwd is not None:
            _require_empty_prefill_cwd(cwd)
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
        if self.prefill_cwd is not None:
            if self._prefill_seed is None:
                if self._seed_attempted:
                    raise CodexTransportError("Codex seed bootstrap already failed; never retry implicitly")
                self._seed_attempted = True
                self._prefill_seed = _create_empty_seed(
                    directory=directory / "prefill-seed", cwd=cwd, model=self.model
                )
            self._prefill_seed.require_unchanged(cwd=cwd, model=self.model)
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
                        f"Codex exited with status {process.returncode}; inspect retained logs"
                    )
            finally:
                _stop(process)
        if self.prefill_cwd is not None:
            _require_empty_prefill_cwd(cwd)
        if any(
            path.stat().st_size > MAX_TRANSCRIPT_BYTES
            for path in (events_path, log_path, last_message)
        ):
            raise CodexTransportError("Codex output limit reached")
        response, usage = parse_transcript(events_path)
        if self._prefill_seed is not None:
            self._prefill_seed.require_unchanged(cwd=cwd, model=self.model)
            thread_id = _independent_fork_id(
                events_path, seed=self._prefill_seed, used=self._used_thread_ids
            )
            facts["prefill_cache"] = {
                "mode": "empty_seed_ephemeral_fork",
                "seed_thread_id": self._prefill_seed.thread_id,
                "seed_rollout_sha256": hashlib.sha256(self._prefill_seed.content).hexdigest(),
                "thread_id": thread_id,
            }
        return {
            "response": response,
            "upstream_estimated_cost_usd": None,
            "usage": {**usage, "requested_model": f"codex/{self.model}"},
            "transport": facts,
        }
