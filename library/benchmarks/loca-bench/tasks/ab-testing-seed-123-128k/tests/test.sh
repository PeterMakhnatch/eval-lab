#!/bin/sh
set -eu
python3 /tests/verify.py --task-dir /app/task_state --workspace /app/task_state/agent_workspace
