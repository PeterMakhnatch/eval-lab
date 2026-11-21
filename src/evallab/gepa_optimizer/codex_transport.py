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

CODEX_EXECUTABLE = Path("/opt/homebrew/bin/codex")
CODEX_VERSION = "codex-cli 0.162.1"

MODEL_PROVIDER = "openai"

MAX_TRANSCRIPT_BYTES = 4 * 1024 * 1024

MAX_RESPONSE_BYTES = 1024 * 1024

_CHILD_ENV_VARS = frozenset(
    {"PATH", "HOME", "CODEX_HOME", "TMPDIR", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR"}
)

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

USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

_RUN = subprocess.run
_POPEN = subprocess.Popen


class CodexTransportError(RuntimeError):
    pass


def _scrub_env(source: dict[str, str]) -> dict[str, str]:
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


def _probe_timeout(deadline: float | None, limit: float) -> float:
    if deadline is None:
        return limit
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CodexTransportError("Codex turn deadline expired during preflight")
    return min(limit, remaining)


def _run_probe(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    return _RUN(
        argv,
        capture_output=True,
        check=False,
        timeout=timeout,
        env=_scrub_env(dict(os.environ)),
    )


def _startup_warning(item: Any, kind: str, started: bool) -> bool:
    return (
        not started
        and kind == "item.completed"
        and isinstance(item, dict)
        and item.get("type") == "error"
        and isinstance(item.get("message"), str)
        and not item["message"].startswith("model rerouted:")
    )


def _reconnect_notification(event: dict[str, Any], *, started: bool) -> bool:
    message = event.get("message")
    return (
        started
        and event.get("type") == "error"
        and isinstance(message, str)
        and re.match(r"^Reconnecting\.\.\. [1-9]\d*/[1-9]\d* \(", message) is not None
        and "model rerouted:" not in message.casefold()
    )


def parse_transcript(path: Path, *, allow_preamble: bool = False) -> tuple[str, dict[str, Any]]:
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
            usage = {name: payload[name] for name in USAGE_FIELDS if name in payload}
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


def _execute(
    argv: list[str],
    *,
    cwd: Path,
    prompt_file: Path,
    events_path: Path,
    log_path: Path,
    timeout_seconds: float,
) -> None:
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
            deadline = time.monotonic() + timeout_seconds
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


class CodexTransport:

    def __init__(self, *, repo_root: Path, model: str, timeout_seconds: int = 900):
        self.repo_root = repo_root.resolve()
        self.model = _bare_model(model)
        self.timeout_seconds = timeout_seconds

    def _version_digest(
        self, executable: Path, *, deadline: float | None = None,
    ) -> tuple[str, str]:
        completed = _run_probe([str(executable), "--version"], timeout=_probe_timeout(deadline, 20))
        if completed.returncode != 0:
            raise CodexTransportError("Codex version probe failed")
        version = completed.stdout.decode(errors="replace").strip()
        if version != CODEX_VERSION:
            raise CodexTransportError(f"Codex subscription transport requires {CODEX_VERSION}")
        with executable.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        return version, "sha256:" + digest

    def preflight(self, *, deadline: float | None = None) -> dict[str, Any]:
        executable = _executable()
        version, digest = self._version_digest(executable, deadline=deadline)
        completed = _run_probe(
            [str(executable), "login", "status"], timeout=_probe_timeout(deadline, 30),
        )
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

    def _argv(
        self,
        *,
        cwd: Path,
        last_message: Path,
        persist: bool = False,
        resume_thread_id: str | None = None,
    ) -> list[str]:
        if resume_thread_id is not None and not persist:
            raise ValueError("a resumed Codex thread must stay persisted")
        executable = _executable()
        argv = [str(executable), "exec"]
        if resume_thread_id is not None:
            argv.append("resume")
        argv.append("--json")
        if resume_thread_id is None:
            argv.extend(("--sandbox", "read-only"))
        argv.append("--skip-git-repo-check")
        if not persist:
            argv.append("--ephemeral")
        argv.extend(
            (
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
            )
        )
        if resume_thread_id is not None:
            argv.extend(("-c", 'sandbox_mode="read-only"'))
        for feature in DISABLED_FEATURES:
            argv.extend(("--disable", feature))
        if resume_thread_id is None:
            argv.extend(("-C", str(cwd)))
        argv.extend(("-o", str(last_message)))
        if resume_thread_id is not None:
            argv.append(resume_thread_id)
        argv.append("-")
        return argv

    def request(self, prompt: str | list[dict[str, Any]], *, directory: Path) -> dict[str, Any]:
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
        _execute(
            argv,
            cwd=cwd,
            prompt_file=prompt_file,
            events_path=events_path,
            log_path=log_path,
            timeout_seconds=self.timeout_seconds,
        )
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
