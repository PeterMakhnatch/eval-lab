#!/bin/sh
set -eu

python3 /app/runtime.py call spotify.read_gmail '{"action": "search_threads", "query": "Katie"}'
python3 /app/runtime.py call spotify.read_spotify_library '{"include_album_tracks": "true", "include_playlists": "true", "include_saved_albums": "true", "include_saved_tracks": "true", "playlist_id": ""}'
python3 /app/runtime.py call spotify.read_gmail '{"action": "read_draft", "query": "draft_katie_001"}'
python3 /app/runtime.py call spotify.write_gmail_draft '{"action": "update", "draft_id": "draft_katie_001", "subject": "Re: Songs for the road trip", "body": "Hi Katie,\n\nHere’s the updated list:\n- Sunset Drive — The Midnight\n- Green Light — Lorde\n- Dreams — Fleetwood Mac\n- Midnight City — M83\n\nLet me know what you think!\n\nBest,\nAlice"}'
python3 /app/runtime.py call spotify.write_gmail_draft '{"action": "save", "draft_id": "draft_katie_001"}'
