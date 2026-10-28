"""Offline TT7 native-stream metadata census; never emit message text or tool arguments."""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any

OUT = Path(__file__).resolve().parent
STATE = Path.home() / ".local/state/evallab-prod-live/trace-tools"
PRIVATE = STATE / "tt7-codex-continuation-20261028"
ROOTS = {
    "retained-tt7-generation-storage": STATE / "tt7",
    "claude-continuation-storage": STATE / "tt7-continuation-20261028",
    "codex-continuation-storage": PRIVATE,
}
MODELS = {"claude-sonnet-5-5", "claude-opus-5-5", "gpt-6-luna"}
PROVIDERS = {"anthropic", "openai-codex"}
STOPS = {"stop", "length", "toolUse", "error", "aborted"}
TOOLS = {
    "ask", "ast_edit", "ast_grep", "bash", "browser", "checkpoint", "context_notes", "debug",
    "edit", "eval", "generate_image", "glob", "grep", "learn", "lsp", "manage_skill", "memory_edit",
    "new_context", "read", "recall", "reflect", "retain", "rewind", "security_scan", "task", "todo",
    "wait", "web_search", "write",
}


def encoded(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def bounded(value: Any, allowed: set[str]) -> str:
    """Identifiers outside the public expected catalog remain opaque, never reflected."""
    if value is None:
        return "missing"
    return value if isinstance(value, str) and value in allowed else "other-sha256:" + digest(encoded(value))[:16]


def assistant_metadata(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": bounded(message.get("provider"), PROVIDERS),
        "model": bounded(message.get("model"), MODELS),
        "stop": bounded(message.get("stopReason"), STOPS),
        "tool_call_blocks": sum(isinstance(item, dict) and item.get("type") == "toolCall"
                                for item in message.get("content", [])),
    }


def event_metadata(event: Any) -> dict[str, Any] | None:
    """Deliberately never access args/input, result content/details, text or thinking."""
    if not isinstance(event, dict):
        return None
    kind = event.get("type")
    if kind in {"tool_execution_start", "tool_execution_end"}:
        result = {"event": kind, "tool": bounded(event.get("toolName"), TOOLS)}
        if kind == "tool_execution_end":
            result["isError"] = bool(event.get("isError", False))
        return result
    if kind == "message_end" and isinstance(event.get("message"), dict) and event["message"].get("role") == "assistant":
        return {"event": "assistant_end", **assistant_metadata(event["message"])}
    if kind == "agent_end":
        return {"event": "agent_end", "assistant_turns": [
            assistant_metadata(message) for message in event.get("messages", [])
            if isinstance(message, dict) and message.get("role") == "assistant"
        ]}
    return None


def audit_file(path: Path, namespace: str) -> dict[str, Any]:
    sha = hashlib.sha256()
    metadata = []
    byte_count = line_count = malformed = 0
    with path.open("rb") as handle:
        for line in handle:
            sha.update(line)
            byte_count += len(line)
            line_count += 1
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                malformed += 1
                continue
            selected = event_metadata(event)
            if selected is not None:
                metadata.append(selected)
    return {"namespace": namespace, "path": str(path), "sha256": sha.hexdigest(), "bytes": byte_count,
            "lines": line_count, "malformed_json_lines": malformed, "metadata": metadata}


def summarize(files: list[dict[str, Any]]) -> dict[str, Any]:
    roots: dict[str, Counter[str]] = {key: Counter() for key in ROOTS}
    routes: dict[str, Counter[str]] = {}
    tools: dict[str, Counter[str]] = {}
    multi_turn = missing_end = end_count_mismatch = 0
    for stream in files:
        root = roots[stream["namespace"]]
        root.update(files=1, bytes=stream["bytes"], malformed_json_lines=stream["malformed_json_lines"])
        assistant_ends = [item for item in stream["metadata"] if item["event"] == "assistant_end"]
        agent_ends = [item for item in stream["metadata"] if item["event"] == "agent_end"]
        missing_end += not agent_ends
        end_count_mismatch += bool(agent_ends and len(agent_ends[-1]["assistant_turns"]) != len(assistant_ends))
        multi_turn += len(assistant_ends) > 1
        identities = {(item["provider"], item["model"]) for item in assistant_ends}
        route = "/".join(next(iter(identities))) if len(identities) == 1 else "unknown-or-multiple-identities"
        counts = routes.setdefault(route, Counter())
        counts.update(files=1, assistant_turns=len(assistant_ends))
        for item in stream["metadata"]:
            if item["event"] in {"tool_execution_start", "tool_execution_end"}:
                name = item["tool"]
                outcomes = tools.setdefault(name, Counter())
                if item["event"] == "tool_execution_start":
                    outcomes.update(starts=1)
                    counts.update(tool_execution_starts=1)
                else:
                    outcomes.update(ends=1, successful_ends=int(not item["isError"]), error_ends=int(item["isError"]))
                    counts.update(tool_execution_ends=1)
    return {
        "schema": "tt7.native_tool_metadata_audit/v1",
        "at": datetime.now(UTC).isoformat(),
        "scope": "Only retained TT7 native generation event files; not other chats, skills, caches or profiles",
        "audit_source_sha256": digest(Path(__file__).read_bytes()),
        "retained_event_manifest_sha256": digest(encoded([
            {key: stream[key] for key in ("namespace", "path", "sha256", "bytes", "lines")}
            for stream in files
        ])),
        "streams": len(files), "assistant_turns": sum(value["assistant_turns"] for value in routes.values()),
        "multi_assistant_turn_streams": multi_turn, "missing_final_agent_end_streams": missing_end,
        "assistant_end_vs_final_agent_end_count_mismatches": end_count_mismatch,
        "malformed_json_lines": sum(value["malformed_json_lines"] for value in roots.values()),
        "roots": {key: dict(value) for key, value in roots.items()},
        "routes": {key: dict(value) for key, value in sorted(routes.items())},
        "tools": {key: dict(value) for key, value in sorted(tools.items())},
        "all_retained_tool_events_are_known_single_anomaly": (
            len(tools) == 1 and tools.get("manage_skill") == Counter(starts=1, ends=1, successful_ends=1, error_ends=0)
        ),
        "capability_boundary": {
            "zero_advertised_tools_attested": False, "explicit_tool_handler_deny_attested": False,
            "future_inference_authorized": False,
            "status": "STOPPED: flags were requested, but are not a tool-capability sandbox",
        },
        "not_claimed": ["no other-session consumption", "no unretained provider actions", "zero advertised or callable tools"],
        "arguments_message_text_thinking_or_result_content_emitted": False,
        "model_calls": 0, "quota_queries": 0, "new_side_effectful_tool_calls": 0,
    }


def write_once(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", type=Path, default=OUT / "native-metadata-audit.json")
    parser.add_argument("--private", type=Path, default=PRIVATE / "native-metadata-audit-private.json")
    args = parser.parse_args()
    if args.public.resolve().parent != OUT or args.private.resolve().parent != PRIVATE:
        raise ValueError("outputs must stay in this owned public/private attempt namespace")
    files = [audit_file(path, namespace) for namespace, root in ROOTS.items()
             for path in sorted(root.glob("**/*-events.jsonl")) if path.is_file() and not path.is_symlink()]
    report = summarize(files)
    write_once(args.private, encoded({"summary": report, "files": files}))
    write_once(args.public, encoded(report))
    print(json.dumps({key: report[key] for key in (
        "streams", "assistant_turns", "multi_assistant_turn_streams", "malformed_json_lines", "tools",
    )}, sort_keys=True))


if __name__ == "__main__":
    main()
