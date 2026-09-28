#!/bin/bash
# HAR-85 train-search round helper (bash 3.2-safe: no mapfile, no assoc arrays).
#
# With no args: list THIS campaign's parked (queue/waiting) specs, print the
# count, the estimated $, and the exact approve line. Changes nothing.
#
# With --dispatch --ref PATH: refuse unless every listed spec is already
# approved; tick ONLY this campaign's approved IDs (key loaded into the tick
# process only, never printed); then rerun the campaign for the next round.
#
# Campaign-spec match: replayed search specs are named
#   gepa-runs-gepa-har85-mimo-train-search-<tags>
# derived from the evaluator output_dir runs/gepa-har85-mimo-train-search
# (evaluator.py:515 _campaign_path; intake.py _replay_spec_name, prefix
# "gepa-"). The prefix is unambiguous to this campaign.
set -euo pipefail
lab=/Users/petermakhnatch/Developer/eval-lab/.worktrees/har85-gepa-mimo
EXP=research/experiments/har85-mimo-gepa
CAMPAIGN="$EXP/campaign-train.json"
PREFIX='gepa-runs-gepa-har85-mimo-train-search-'

list_specs() {
  # list_specs <queue-state-dir>: prints "<spec_id> <est_cost_usd> <name>" per match.
  dir="$1"
  [ -d "$lab/$dir" ] || return 0
  uv run --no-sync python - "$lab/$dir" "$PREFIX" <<'EOF' 2>/dev/null || true
import json, sys
from pathlib import Path
d, prefix = Path(sys.argv[1]), sys.argv[2]
for f in sorted(d.glob("*.json")):
    try:
        spec = json.loads(f.read_text())
    except (OSError, ValueError):
        continue
    if isinstance(spec, dict) and str(spec.get("name", "")).startswith(prefix):
        print(spec.get("spec_id") or f.stem, spec.get("est_cost_usd", 0.0), spec.get("name"))
EOF
}

mode="list"
ref=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dispatch) mode="dispatch"; shift;;
    --ref) ref="${2:?--ref needs a path}"; shift 2;;
    *) echo "usage: $0 [--dispatch --ref PROPOSER_APPROVAL_REF]" >&2; exit 2;;
  esac
done

set --
while IFS= read -r line || [ -n "$line" ]; do
  [ -n "$line" ] || continue
  set -- "$@" "$line"
done <<EOF
$(list_specs queue/waiting)
EOF
waiting_n="$#"
waiting_est=$(printf '%s\n' "$@" | awk '{e+=$2} END {printf "%.2f", e+0}')
# Re-split "$@" into parallel streams via positional params: each line is
# "<id> <est> <name>"; IDs are first fields.
ids=""
est_total="0.00"
if [ "$waiting_n" -gt 0 ]; then
  ids=$(printf '%s\n' "$@" | awk '{print $1}')
  est_total="$waiting_est"
fi

if [ "$mode" = "list" ]; then
  echo "campaign: $CAMPAIGN"
  echo "parked specs (queue/waiting): $waiting_n, est \$$est_total"
  if [ "$waiting_n" -gt 0 ]; then
    printf '%s\n' "$@" | awk '{printf "  %s  est %s  %s\n", $1, $2, $3}'
    # shellcheck disable=SC2086
    echo "approve: for id in $ids; do uv run evallab approve \"\$id\" --actor peter; done"
  fi
  exit 0
fi

# --dispatch below: refuse-first, then tick, then rerun. Nothing runs before this point.
[ -n "$ref" ] || { echo "refusing: --dispatch needs --ref PROPOSER_APPROVAL_REF" >&2; exit 2; }
[ -f "$ref" ] || { echo "refusing: approval ref missing: $ref" >&2; exit 2; }
if [ "$waiting_n" -gt 0 ]; then
  echo "refusing: $waiting_n campaign specs still awaiting approval" >&2
  exit 2
fi
set --
while IFS= read -r line || [ -n "$line" ]; do
  [ -n "$line" ] || continue
  set -- "$@" "$line"
done <<EOF
$(list_specs queue/approved)
EOF
[ "$#" -gt 0 ] || { echo "refusing: no approved campaign specs to dispatch" >&2; exit 2; }
approved_ids=$(printf '%s\n' "$@" | awk '{print $1}')
approved_n="$#"
key=$(awk 'index($0,"ZAI_OPENAPI_API_KEY=")==1{v=substr($0,21); gsub(/^["'"'"']|["'"'"']$/,"",v); print v}' "$HOME/.omp/agent/.env")
[ -n "$key" ] || { echo "refusing: ZAI_OPENAPI_API_KEY missing" >&2; exit 2; }
cd "$lab"
args=(--max-specs "$approved_n")
# shellcheck disable=SC2086
for id in $approved_ids; do args+=(--spec-id "$id"); done
env -i HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-en_US.UTF-8}" \
  PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
  ZAI_OPENAPI_API_KEY="$key" \
  /Users/petermakhnatch/.local/bin/uv run --no-sync evallab tick "${args[@]}"
uv run --no-sync python -m evallab.gepa_optimizer run "$CAMPAIGN" --repo-root . \
  --proposer-approval-ref "$ref"
