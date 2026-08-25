#!/bin/sh
set -eu

# Premature send action on ambiguous draft
python3 /app/runtime.py call gmail_and_email_records.manage_gmail_draft '{"action": "send", "draft_id": "draft_katie_001"}'
