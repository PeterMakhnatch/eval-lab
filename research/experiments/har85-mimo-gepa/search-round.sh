#!/bin/bash
# HAR-85 train-search round helper (bash 3.2-safe: no mapfile, no assoc arrays).
#
# With no args: list THIS campaign's parked (queue/waiting) specs, print the
# count, the estimated $, and the exact approve line. Changes nothing.
#
# With --dispatch --ref PATH [--max-specs N]: refuse while any campaign spec
# still awaits approval; tick this campaign's approved IDs (at most N, default
# all; key loaded into the tick process only, never printed), or skip the tick
# when none are approved (the first round); then rerun the campaign, which
# parks the next round (approved-but-unticked specs are reused, not resubmitted).
#
# Campaign-spec match: replayed search specs are named
#   gepa-runs-gepa-har85-mimo-train-search-<tags>
# derived from the evaluator output_dir runs/gepa-har85-mimo-train-search
# (evaluator.py:515 _campaign_path; intake.py _replay_spec_name, prefix
# "gepa-"). The prefix is unambiguous to this campaign.
set -euo pipefail
lab="$(cd "$(dirname "$0")/../../.." && pwd)"
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
max_specs=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dispatch) mode="dispatch"; shift;;
    --ref) ref="${2:?--ref needs a path}"; shift 2;;
    --max-specs) max_specs="${2:?--max-specs needs a positive integer}"; shift 2;;
    *) echo "usage: $0 [--dispatch --ref PROPOSER_APPROVAL_REF [--max-specs N]]" >&2; exit 2;;
  esac
done
case "$max_specs" in
  '') ;;
  *[!0-9]*|0) echo "refusing: --max-specs must be a positive integer" >&2; exit 2;;
esac

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
# Every search example must be in the train pool (split train minus
# train-exclusions.json, e.g. tasks whose grader cannot score an honest solution).
python3 "$lab/$EXP/train_pool.py" check-campaign "$lab/$CAMPAIGN" >&2 \
  || { echo "refusing: $CAMPAIGN is not within the HAR-85 train pool" >&2; exit 2; }
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
approved_n="$#"
if [ -n "$max_specs" ] && [ "$approved_n" -gt "$max_specs" ]; then
  approved_n="$max_specs"
fi
cd "$lab"
if [ "$approved_n" -gt 0 ]; then
  approved_ids=$(printf '%s\n' "$@" | awk -v n="$approved_n" 'NR<=n {print $1}')
  mkey=$(awk '{sub(/^export /,"")} index($0,"MIMO_SELFHOSTED_API_KEY=")==1{v=substr($0,24); gsub(/^["'"'"']|["'"'"']$/,"",v); print v}' "$HOME/.omp/agent/.env")
  [ -n "$mkey" ] || { echo "refusing: MIMO_SELFHOSTED_API_KEY missing" >&2; exit 2; }
  mup=$(awk '{sub(/^export /,"")} index($0,"EVALLAB_MIMO_SELFHOSTED_UPSTREAM=")==1{v=substr($0,32); gsub(/^["'"'"']|["'"'"']$/,"",v); print v}' "$HOME/.omp/agent/.env")
  [ -n "$mup" ] || { echo "refusing: EVALLAB_MIMO_SELFHOSTED_UPSTREAM missing" >&2; exit 2; }
  dkey=$(awk '{sub(/^export /,"")} index($0,"DAYTONA_API_KEY=")==1{v=substr($0,17); gsub(/^["'"'"']|["'"'"']$/,"",v); print v}' "$HOME/.omp/agent/.env")
  [ -n "$dkey" ] || { echo "refusing: DAYTONA_API_KEY missing" >&2; exit 2; }
  # Trials run in Daytona (base spec environment); the runner forwards the
  # Daytona key to Harbor only for Daytona environments
  # (src/evallab/runner.py, include_daytona_credentials).
  args=(--max-specs "$approved_n")
  # shellcheck disable=SC2086
  for id in $approved_ids; do args+=(--spec-id "$id"); done
  env -i HOME="$HOME" TMPDIR="${TMPDIR:-/tmp}" LANG="${LANG:-en_US.UTF-8}" \
    PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
    PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
    MIMO_SELFHOSTED_API_KEY="$mkey" EVALLAB_MIMO_SELFHOSTED_UPSTREAM="$mup" DAYTONA_API_KEY="$dkey" \
    /Users/petermakhnatch/.local/bin/uv run --no-sync evallab tick "${args[@]}"
else
  # First round (nothing parked yet) or a round whose trials already ran: no
  # tick; the campaign run below parks the next batch (baseline gate: all 8).
  echo "no approved campaign specs: advancing the campaign only (no tick)" >&2
fi
uv run --no-sync python -m evallab.gepa_optimizer run "$CAMPAIGN" --repo-root . \
  --proposer-approval-ref "$ref"
