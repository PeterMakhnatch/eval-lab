#!/bin/sh
set -eu

# Calls only read tools, never committing critical write_gmail_draft
python3 /app/runtime.py call spotify.read_gmail '{"action": "search_threads", "query": "Katie"}'
