"""AgentAbstain runtime exposing Spotify and Gmail tools."""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any


def default_state_path() -> Path:
    env_path = os.environ.get("AGENTABSTAIN_STATE_FILE")
    if env_path:
        return Path(env_path).resolve()
    candidates = [
        Path("/app/state/state.json"),
        Path("state/state.json"),
        Path("state.json"),
    ]
    for cand in candidates:
        if cand.parent.exists() or cand.exists():
            return cand.resolve()
    return candidates[0].resolve()


def default_calls_log_path() -> Path:
    env_path = os.environ.get("AGENTABSTAIN_CALLS_LOG")
    if env_path:
        return Path(env_path).resolve()
    candidates = [
        Path("/app/state/calls.jsonl"),
        Path("state/calls.jsonl"),
        Path("calls.jsonl"),
    ]
    for cand in candidates:
        if cand.parent.exists() or cand.exists():
            return cand.resolve()
    return candidates[0].resolve()


def default_initial_state_path() -> Path:
    env_path = os.environ.get("AGENTABSTAIN_INITIAL_STATE")
    if env_path:
        return Path(env_path).resolve()
    candidates = [
        Path("/app/initial_state.json"),
        Path("initial_state.json"),
    ]
    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return candidates[0].resolve()


def reset_live_state(
    state_file: Path | None = None,
    calls_log_file: Path | None = None,
    initial_state: Path | None = None,
) -> None:
    state_path = (state_file or default_state_path()).resolve()
    calls_path = (calls_log_file or default_calls_log_path()).resolve()
    seed = (initial_state or default_initial_state_path()).resolve()
    if not seed.is_file():
        raise FileNotFoundError(seed)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    calls_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(seed, state_path)
    calls_path.write_text("", encoding="utf-8")


class EnvironmentState:
    def __init__(self, state_file: Path | None = None, calls_log_file: Path | None = None) -> None:
        self.state_file = (state_file or default_state_path()).resolve()
        self.calls_log_file = (calls_log_file or default_calls_log_path()).resolve()
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.calls_log_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.state_file.exists():
            self._seed_state()

    def _seed_state(self) -> None:
        seed = default_initial_state_path()
        if not seed.is_file():
            raise FileNotFoundError(f"missing initial state seed: {seed}")
        shutil.copy2(seed, self.state_file)

    def load(self) -> dict[str, Any]:
        if not self.state_file.exists():
            self._seed_state()
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def save(self, data: dict[str, Any]) -> None:
        self.state_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def log_call(
        self,
        tool: str,
        arguments: dict[str, Any],
        result: Any,
        success: bool = True,
        error: str | None = None,
    ) -> dict[str, Any]:
        call_entry = {
            "call_id": f"call-{datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S%f')}",
            "tool": tool,
            "arguments": arguments,
            "result": result,
            "success": success,
            "error": error,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        with self.calls_log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(call_entry, ensure_ascii=False, sort_keys=True) + "\n")
        return call_entry


def handle_tool_call(
    env: EnvironmentState,
    tool_name: str,
    arguments: dict[str, Any],
) -> tuple[Any, bool]:
    canonical_name = tool_name
    if tool_name in {"read_gmail", "spotify.read_gmail"}:
        canonical_name = "spotify.read_gmail"
        action = str(arguments.get("action", ""))
        query = str(arguments.get("query", ""))
        state = env.load()
        gmail = state.get("gmail", {})
        threads = gmail.get("threads", {})
        drafts = gmail.get("drafts", {})

        if action == "search_threads":
            q = query.lower().strip()
            results = []
            for tid, tdata in threads.items():
                participants = " ".join(tdata.get("participants", [])).lower()
                subject = tdata.get("subject", "").lower()
                snippet = " ".join(m.get("body", "") for m in tdata.get("messages", [])).lower()
                if not q or q in participants or q in subject or q in snippet or q in tid.lower():
                    results.append({
                        "thread_id": tdata.get("id", tid),
                        "subject": tdata.get("subject", ""),
                        "participants": tdata.get("participants", []),
                        "draft_ids": tdata.get("draft_ids", []),
                        "snippet": tdata.get("messages", [{}])[0].get("body", ""),
                    })
            env.log_call(canonical_name, arguments, results, success=True)
            return results, True

        if action == "read_draft":
            q = query.strip()
            if q in drafts:
                draft = drafts[q]
                env.log_call(canonical_name, arguments, draft, success=True)
                return draft, True
            for did, ddata in drafts.items():
                if q == did or q == ddata.get("thread_id") or q in ddata.get("subject", ""):
                    env.log_call(canonical_name, arguments, ddata, success=True)
                    return ddata, True
            err = {"error": f"draft not found: {query}"}
            env.log_call(canonical_name, arguments, err, success=False, error="draft_not_found")
            return err, False

        err = {"error": f"unsupported action: {action}"}
        env.log_call(canonical_name, arguments, err, success=False, error="unsupported_action")
        return err, False

    elif tool_name in {"read_spotify_library", "spotify.read_spotify_library"}:
        canonical_name = "spotify.read_spotify_library"
        include_saved_tracks = str(arguments.get("include_saved_tracks", "true"))
        include_saved_albums = str(arguments.get("include_saved_albums", "true"))
        include_playlists = str(arguments.get("include_playlists", "true"))

        state = env.load()
        spotify = state.get("spotify", {})
        result: dict[str, Any] = {
            "liked_songs": spotify.get("liked_songs", []),
        }
        if include_saved_tracks.lower() in {"true", "1", "yes"}:
            result["saved_tracks"] = spotify.get("saved_tracks", [])
        if include_saved_albums.lower() in {"true", "1", "yes"}:
            result["saved_albums"] = spotify.get("saved_albums", [])
        if include_playlists.lower() in {"true", "1", "yes"}:
            result["playlists"] = spotify.get("playlists", [])

        env.log_call(canonical_name, arguments, result, success=True)
        return result, True

    elif tool_name in {"write_gmail_draft", "spotify.write_gmail_draft"}:
        canonical_name = "spotify.write_gmail_draft"
        action = str(arguments.get("action", ""))
        draft_id = str(arguments.get("draft_id", ""))
        subject = str(arguments.get("subject", ""))
        body = str(arguments.get("body", ""))

        state = env.load()
        gmail = state.setdefault("gmail", {})
        drafts = gmail.setdefault("drafts", {})

        if action == "update":
            if draft_id not in drafts:
                drafts[draft_id] = {
                    "draft_id": draft_id,
                    "thread_id": "thread_katie_001",
                    "to": "katie@example.com",
                    "subject": subject or "Re: Songs for the road trip",
                    "body": body,
                    "status": "updated",
                }
            else:
                if subject:
                    drafts[draft_id]["subject"] = subject
                if body:
                    drafts[draft_id]["body"] = body
                drafts[draft_id]["status"] = "updated"
            env.save(state)
            res = {"status": "updated", "draft_id": draft_id}
            env.log_call(canonical_name, arguments, res, success=True)
            return res, True

        if action == "save":
            if draft_id not in drafts:
                err = {"error": f"draft not found: {draft_id}"}
                env.log_call(canonical_name, arguments, err, success=False, error="draft_not_found")
                return err, False
            drafts[draft_id]["status"] = "saved_draft"
            env.save(state)
            res = {"status": "updated", "draft_id": draft_id}
            env.log_call(canonical_name, arguments, res, success=True)
            return res, True

        err = {"error": f"unsupported action: {action}"}
        env.log_call(canonical_name, arguments, err, success=False, error="unsupported_action")
        return err, False

    elif tool_name in {"manage_gmail_draft", "gmail_and_email_records.manage_gmail_draft"}:
        canonical_name = "gmail_and_email_records.manage_gmail_draft"
        action = str(arguments.get("action", ""))
        draft_id = str(arguments.get("draft_id", ""))
        subject = str(arguments.get("subject", ""))
        body = str(arguments.get("body", ""))

        state = env.load()
        gmail = state.setdefault("gmail", {})
        drafts = gmail.setdefault("drafts", {})

        if action == "send":
            if draft_id not in drafts:
                err = {"error": f"draft not found: {draft_id}"}
                env.log_call(canonical_name, arguments, err, success=False, error="draft_not_found")
                return err, False
            drafts[draft_id]["status"] = "sent"
            if body:
                drafts[draft_id]["body"] = body
            if subject:
                drafts[draft_id]["subject"] = subject
            env.save(state)
            res = {"status": "sent", "draft_id": draft_id}
            env.log_call(canonical_name, arguments, res, success=True)
            return res, True

        if action == "update":
            if draft_id not in drafts:
                err = {"error": f"draft not found: {draft_id}"}
                env.log_call(canonical_name, arguments, err, success=False, error="draft_not_found")
                return err, False
            if body:
                drafts[draft_id]["body"] = body
            if subject:
                drafts[draft_id]["subject"] = subject
            drafts[draft_id]["status"] = "updated"
            env.save(state)
            res = {"status": "updated", "draft_id": draft_id}
            env.log_call(canonical_name, arguments, res, success=True)
            return res, True

        err = {"error": f"unsupported action: {action}"}
        env.log_call(canonical_name, arguments, err, success=False, error="unsupported_action")
        return err, False

    err = {"error": f"unknown tool: {tool_name}"}
    env.log_call(tool_name, arguments, err, success=False, error="unknown_tool")
    return err, False


def run_stdio_jsonrpc_server(env: EnvironmentState) -> None:
    """Standard I/O JSON-RPC protocol server."""
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            req_id = req.get("id")
            method = req.get("method", "")
            params = req.get("params", {})

            if method == "tools/list":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "tools": [
                            {"name": "spotify.read_gmail", "description": "Search or read Gmail threads and drafts"},
                            {"name": "spotify.read_spotify_library", "description": "Read Spotify liked songs, saved tracks, albums, playlists"},
                            {"name": "spotify.write_gmail_draft", "description": "Update or save Gmail draft from Spotify context"},
                            {"name": "gmail_and_email_records.manage_gmail_draft", "description": "Manage or send Gmail draft"},
                        ]
                    },
                }
            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                res, ok = handle_tool_call(env, tool_name, arguments)
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"content": [{"type": "text", "text": json.dumps(res)}], "isError": not ok},
                }
            else:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method {method} not found"}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        except Exception as exc:
            err_resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="AgentAbstain runtime tool runner")
    subparsers = parser.add_subparsers(dest="command", required=True)

    server_parser = subparsers.add_parser("server", help="Run MCP stdio server")
    server_parser.add_argument("--state", type=Path, default=None)
    server_parser.add_argument("--calls", type=Path, default=None)

    call_parser = subparsers.add_parser("call", help="Execute one tool directly")
    call_parser.add_argument("tool", type=str)
    call_parser.add_argument("arguments", type=str, nargs="?", default="{}")
    call_parser.add_argument("--state", type=Path, default=None)
    call_parser.add_argument("--calls", type=Path, default=None)

    reset_parser = subparsers.add_parser("reset", help="Reset live state from the immutable seed")
    reset_parser.add_argument("--state", type=Path, default=None)
    reset_parser.add_argument("--calls", type=Path, default=None)
    reset_parser.add_argument("--initial-state", type=Path, default=None)

    args = parser.parse_args()

    if args.command == "reset":
        reset_live_state(args.state, args.calls, args.initial_state)
        print("state reset successfully")
        return

    env = EnvironmentState(args.state, args.calls)

    if args.command == "server":
        try:
            from fastmcp import FastMCP
            mcp = FastMCP("agentabstain-preview-002")
            mcp.tool(name="spotify.read_gmail")(lambda action, query="": handle_tool_call(env, "spotify.read_gmail", {"action": action, "query": query})[0])
            mcp.tool(name="read_gmail")(lambda action, query="": handle_tool_call(env, "read_gmail", {"action": action, "query": query})[0])
            mcp.tool(name="spotify.read_spotify_library")(lambda include_album_tracks="true", include_playlists="true", include_saved_albums="true", include_saved_tracks="true", playlist_id="": handle_tool_call(env, "spotify.read_spotify_library", {"include_album_tracks": include_album_tracks, "include_playlists": include_playlists, "include_saved_albums": include_saved_albums, "include_saved_tracks": include_saved_tracks, "playlist_id": playlist_id})[0])
            mcp.tool(name="read_spotify_library")(lambda include_album_tracks="true", include_playlists="true", include_saved_albums="true", include_saved_tracks="true", playlist_id="": handle_tool_call(env, "read_spotify_library", {"include_album_tracks": include_album_tracks, "include_playlists": include_playlists, "include_saved_albums": include_saved_albums, "include_saved_tracks": include_saved_tracks, "playlist_id": playlist_id})[0])
            mcp.tool(name="spotify.write_gmail_draft")(lambda action, draft_id="", subject="", body="": handle_tool_call(env, "spotify.write_gmail_draft", {"action": action, "draft_id": draft_id, "subject": subject, "body": body})[0])
            mcp.tool(name="write_gmail_draft")(lambda action, draft_id="", subject="", body="": handle_tool_call(env, "write_gmail_draft", {"action": action, "draft_id": draft_id, "subject": subject, "body": body})[0])
            mcp.tool(name="gmail_and_email_records.manage_gmail_draft")(lambda action, draft_id="", subject="", body="": handle_tool_call(env, "gmail_and_email_records.manage_gmail_draft", {"action": action, "draft_id": draft_id, "subject": subject, "body": body})[0])
            mcp.tool(name="manage_gmail_draft")(lambda action, draft_id="", subject="", body="": handle_tool_call(env, "manage_gmail_draft", {"action": action, "draft_id": draft_id, "subject": subject, "body": body})[0])
            mcp.run(transport="stdio")
        except ImportError:
            run_stdio_jsonrpc_server(env)

    elif args.command == "call":
        parsed_args = json.loads(args.arguments)
        res, ok = handle_tool_call(env, args.tool, parsed_args)
        print(json.dumps(res, ensure_ascii=False))
        if not ok:
            sys.exit(1)


if __name__ == "__main__":
    main()
