#!/bin/sh
set -eu
python3 /adapter/oracle.py --task-dir "${LOCA_TASK_DIR:-/app/task_state}" --workspace "${LOCA_AGENT_WORKSPACE:-/app/task_state/agent_workspace}"
