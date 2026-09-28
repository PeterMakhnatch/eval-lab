#!/bin/bash
# HAR-85 DSPy arm: STAGED paid launcher. Does nothing without recorded approval.
# Phase 1 (gepa):   dspy.GEPA over the RLM action instructions, train split only.
# Phase 2 (heldout): final PAIRED winner-vs-base eval on held-out ids, once.
# dspy.GEPA runs OUTSIDE the Lab queue (direct `harbor run` per metric call);
# the spend gate HERE (+ per-trial cost_limit_usd) is the control, not
# `evallab approve` (no queue spec exists to approve; see dspy/APPROVAL.md).
set -euo pipefail

PHASE="${1:-}"
usage() {
  cat >&2 <<'EOF'
usage:
  run-after-approval.sh --phase gepa --approval-file <signed.json> --cap-usd <cap> [--train-tasks a,b] [--val-tasks c] [--max-metric-calls N]
  run-after-approval.sh --phase heldout --approval-file <signed.json> --cap-usd <cap> --winner <policy.json> [--attempts K]
Without a phase + bound approval file this script runs nothing (exit 2).
EOF
  exit 2
}
[ "$PHASE" = "--phase" ] || usage
PHASE_NAME="${2:-}"; shift 2 || usage

APPROVAL_FILE=""; TRAIN_TASKS=""; VAL_TASKS=""; MAX_METRIC_CALLS=""
WINNER=""; ATTEMPTS=3; CAP_USD=""
while [ $# -gt 0 ]; do
  case "$1" in
    --approval-file) APPROVAL_FILE="${2:-}"; shift 2;;
    --train-tasks) TRAIN_TASKS="${2:-}"; shift 2;;
    --val-tasks) VAL_TASKS="${2:-}"; shift 2;;
    --max-metric-calls) MAX_METRIC_CALLS="${2:-}"; shift 2;;
    --winner) WINNER="${2:-}"; shift 2;;
    --attempts) ATTEMPTS="${2:-}"; shift 2;;
    --cap-usd) CAP_USD="${2:-}"; shift 2;;
    *) echo "refusing: unknown flag $1" >&2; usage;;
  esac
done

# Resolve the worktree root that owns this script (not $PWD).
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SPLIT="$REPO/research/experiments/har85-mimo-gepa/split.provisional.json"
DSPY_DIR="$SCRIPT_DIR"

[ "$PHASE_NAME" = "gepa" ] || [ "$PHASE_NAME" = "heldout" ] || { echo "refusing: --phase must be gepa|heldout" >&2; exit 2; }
[ -n "$APPROVAL_FILE" ] || { echo "refusing: --approval-file is required (see dspy/APPROVAL.md)" >&2; exit 2; }
[ -f "$APPROVAL_FILE" ] || { echo "refusing: approval file not found: $APPROVAL_FILE" >&2; exit 2; }
[ -n "$CAP_USD" ] || { echo "refusing: --cap-usd is required (bound into the approval)" >&2; exit 2; }
[ -f "$SPLIT" ] || { echo "refusing: $SPLIT missing" >&2; exit 2; }
# Task containers run in Daytona (bounded lifecycle; binding field harbor_env).
# The key is read from OMP's env file into this process only, never printed.
DAYTONA_API_KEY="$(awk '{sub(/^export /,"")} index($0,"DAYTONA_API_KEY=")==1{v=substr($0,17); gsub(/^["'"'"']|["'"'"']$/,"",v); print v}' "$HOME/.omp/agent/.env")"
[ -n "$DAYTONA_API_KEY" ] || { echo "refusing: DAYTONA_API_KEY missing from ~/.omp/agent/.env" >&2; exit 2; }
export DAYTONA_API_KEY
command -v uv >/dev/null || { echo "refusing: uv is not on PATH" >&2; exit 2; }

VENV="$REPO/runs/.harbor-dspy"
if [ ! -x "$VENV/bin/harbor" ] || ! "$VENV/bin/python" -c 'import daytona' 2>/dev/null; then
  echo "setup: lane venv at $VENV (harbor 0.21.0 + daytona 0.210.0 + dspy 3.3.1, untracked)" >&2
  [ -x "$VENV/bin/python" ] || uv venv "$VENV" --python 3.12
  uv pip install --python "$VENV/bin/python" \
    "harbor[dspy,daytona]==0.21.0" "daytona==0.210.0" "dspy==3.3.1" "litellm==1.101.0" "numpy==2.5.2" pytest
fi

# Stage byte-identical task copies (gitignored) and verify digests.
TASKS_ROOT="$REPO/runs/har85-gepa-mimo/tasks"
PINNED_SRC="/Users/petermakhnatch/Developer/.sources/mimo/terminal@fe1c2b66/tasks"
mkdir -p "$TASKS_ROOT"
stage_task() {
  id="$1"
  if [ ! -f "$TASKS_ROOT/$id/task.toml" ]; then
    cp -a "$PINNED_SRC/$id" "$TASKS_ROOT/$id"
    chmod -R u+w "$TASKS_ROOT/$id"
  fi
  diff -r "$PINNED_SRC/$id" "$TASKS_ROOT/$id" >/dev/null \
    || { echo "refusing: staged copy drifted for $id" >&2; exit 2; }
}

GEPA_PY=("$VENV/bin/python" "$DSPY_DIR/gepa_mimo.py")

if [ "$PHASE_NAME" = "gepa" ]; then
  [ -n "$TRAIN_TASKS" ] || { echo "refusing: --train-tasks is required for phase gepa" >&2; exit 2; }
  [ -n "$MAX_METRIC_CALLS" ] || MAX_METRIC_CALLS=36
  for id in $(echo "$TRAIN_TASKS,$VAL_TASKS" | tr ',' ' '); do [ -n "$id" ] && stage_task "$id"; done
  OUT="$REPO/runs/har85-gepa-mimo/phase1"
  echo "phase1: GEPA train=[$TRAIN_TASKS] val=[$VAL_TASKS] metric-calls=$MAX_METRIC_CALLS cap=$CAP_USD out=$OUT" >&2
  exec env PYTHONPATH="$REPO/src" "${GEPA_PY[@]}" \
    --split "$SPLIT" --tasks-root "$TASKS_ROOT" --repo-root "$REPO" \
    --harbor-bin "$VENV/bin/harbor" --jobs-dir "$REPO/runs/har85-gepa-mimo/jobs" \
    --job-tag har85-gepa-mimo-phase1 --base-policy stock --harbor-env daytona \
    --train-tasks "$TRAIN_TASKS" ${VAL_TASKS:+--val-tasks "$VAL_TASKS"} \
    --max-metric-calls "$MAX_METRIC_CALLS" --approval-file "$APPROVAL_FILE" \
    --cap-usd "$CAP_USD" \
    --out "$OUT"
fi
# Phase 2: final paired held-out eval, once. Winner vs base, same trial path.
[ -n "$WINNER" ] || { echo "refusing: --winner <policy.json> is required for phase heldout" >&2; exit 2; }
[ -f "$WINNER" ] || { echo "refusing: winner policy not found: $WINNER" >&2; exit 2; }
env PYTHONPATH="$REPO/src" "${GEPA_PY[@]}" \
  --phase heldout --verify-only --split "$SPLIT" --tasks-root "$TASKS_ROOT" \
  --repo-root "$REPO" --harbor-bin "$VENV/bin/harbor" --jobs-dir "$REPO/runs/har85-gepa-mimo/jobs" \
  --job-tag har85-heldout-verify --harbor-env daytona \
  --winner "$WINNER" --attempts "$ATTEMPTS" \
  --approval-file "$APPROVAL_FILE" --cap-usd "$CAP_USD" --out "$REPO/runs/har85-gepa-mimo/phase2" \
  || { echo "refusing: phase-2 approval verification failed" >&2; exit 2; }
echo "phase2: approval verified; staging held-out trials" >&2
HELDOUT_IDS="$("$VENV/bin/python" -c "import json;print(' '.join(json.load(open('$SPLIT'))['heldout_task_ids']))")"
OUT2="$REPO/runs/har85-gepa-mimo/phase2"
mkdir -p "$OUT2"
# Credential the student route actually uses: the Z.ai coding-plan key from
# the owner's OpenCode auth store (subscription quota, not metered API
# spend). materialize_zai_secret_file fails closed when absent.
SECRET_DIR="$(mktemp -d /private/tmp/har85-heldout-secret-XXXXXX)"
SECRET_FILE="$SECRET_DIR/secret"
PYTHONPATH="$REPO/src" "$VENV/bin/python" -c \
  "import sys; from evallab.execution_contracts import materialize_zai_secret_file; materialize_zai_secret_file(sys.argv[1])" \
  "$SECRET_FILE" || { echo "refusing: coding-plan credential unavailable" >&2; exit 2; }
trap 'rm -rf "$SECRET_DIR"' EXIT
task_workdir() {
  "$VENV/bin/python" -c "import sys,tomllib;print(tomllib.load(open(sys.argv[1],'rb')).get('environment',{}).get('workdir','/'))" "$1/task.toml"
}
# Same bounded-Daytona flags as phase 1 (single source: gepa_mimo.harbor_env_args).
ENV_ARGS="$(PYTHONPATH="$REPO/src:$DSPY_DIR" "$VENV/bin/python" -c 'import gepa_mimo; print(" ".join(gepa_mimo.harbor_env_args("daytona")))')"
for id in $HELDOUT_IDS; do
  stage_task "$id"
  for arm in winner base; do
    case "$arm" in
      winner) POLICY="$WINNER"; MODEL_TAG="winner";;
      base) POLICY="stock"; MODEL_TAG="base";;
    esac
    echo "phase2: task=$id arm=$arm attempts=$ATTEMPTS" >&2
    env -i HOME="$HOME" TMPDIR="${TMPDIR:-/private/tmp}" LANG="${LANG:-en_US.UTF-8}" \
      PATH="$VENV/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
      PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
      "PYTHONPATH=$REPO/src" EVALLAB_ZAI_SECRET_FILE="$SECRET_FILE" DAYTONA_API_KEY="$DAYTONA_API_KEY" \
    "$VENV/bin/harbor" run --path "$TASKS_ROOT/$id" \
      --agent evallab.harbor_rlm:LabRlmAgent $ENV_ARGS \
      --model zai-coding-plan/glm-5.3-flash \
      --ak "policy=$POLICY" --ak cost_limit_usd=1.0 \
      --ak "working_dir=$(task_workdir "$TASKS_ROOT/$id")" \
      --job-name "har85-heldout-$MODEL_TAG-$(echo "$id" | cut -c1-24)" \
      --jobs-dir "$OUT2/jobs" --n-attempts "$ATTEMPTS" --n-concurrent 1 -y
  done
done
echo "phase2: done under $OUT2/jobs (16 tasks x 2 arms x $ATTEMPTS attempts)" >&2
