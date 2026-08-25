#!/bin/sh
set -eu
python3 /adapter/adapter.py --task-dir /app/task_state --size 8k --seed 42
export LOCA_TASK_ROOT=/app/task_state
exec "$@"
