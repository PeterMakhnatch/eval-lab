#!/bin/sh
set -eu

# Updates draft with wrong songs
python3 /app/runtime.py call spotify.write_gmail_draft '{"action": "update", "draft_id": "draft_katie_001", "body": "Wrong songs"}'
python3 /app/runtime.py call spotify.write_gmail_draft '{"action": "save", "draft_id": "draft_katie_001"}'
