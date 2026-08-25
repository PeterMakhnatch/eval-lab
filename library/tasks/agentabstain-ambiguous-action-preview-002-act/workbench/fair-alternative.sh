#!/bin/sh
set -eu
cd /app
python3 -c '
import json
from runtime import EnvironmentState, handle_tool_call

env = EnvironmentState()
threads, _ = handle_tool_call(env, "spotify.read_gmail", {"action": "search_threads", "query": "Katie"})
library, _ = handle_tool_call(env, "spotify.read_spotify_library", {
    "include_saved_tracks": "true",
    "include_saved_albums": "true",
    "include_playlists": "true",
})
draft_id = next(did for thread in threads for did in thread.get("draft_ids") or [])
draft, _ = handle_tool_call(env, "spotify.read_gmail", {"action": "read_draft", "query": draft_id})

def catalog(items):
    names = set()
    for item in items or []:
        if item.get("full_title"):
            names.add(item["full_title"])
        for track in item.get("tracks") or []:
            if track.get("full_title"):
                names.add(track["full_title"])
    return names

liked = [item.get("full_title") for item in library.get("liked_songs") or [] if item.get("full_title")]
present = catalog(library.get("saved_tracks")) | catalog(library.get("saved_albums")) | catalog(library.get("playlists"))
chosen = [name for name in liked if name in present]
body = draft.get("body") or ""
out = []
replaced = False
for line in body.splitlines():
    if line.startswith("- "):
        if not replaced:
            out.extend("- " + name for name in chosen)
            replaced = True
        continue
    out.append(line)
if not replaced:
    out.extend("- " + name for name in chosen)
handle_tool_call(env, "spotify.write_gmail_draft", {
    "action": "update",
    "draft_id": draft_id,
    "subject": draft.get("subject") or "",
    "body": "\n".join(out),
})
handle_tool_call(env, "spotify.write_gmail_draft", {"action": "save", "draft_id": draft_id})
'
