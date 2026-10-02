#!/usr/bin/env bash
# OVN G5 operator round (HAR-126): the whole paired eval in ONE LoRA-server session.
#
#   run_g5.sh --specs-dir DIR --adapter REL --candidate-usd USD --actor TEXT \
#             --modal-app-day-limit-usd USD [--round-cap-usd USD] [options]
#
# Order (failures abort, except a last-wave gate failure finalizes at 10;
#   1. preflight: clean checkout, spec --check, key file,
#      `capture smoke --model` available (no free-port probe: each segment
#      binds its own OS-assigned port; see step 8)
#   2. `evallab modal billing-reconcile --for <UTC day>`
#   3. `evallab spend check --candidate-usd USD --cap-usd CAP --since <UTC day>`
#   4. plan position waves by spec name from cohort.json
#   5. (--dry-run stops here; nothing submitted) submit every spec, record ids
#   6. ONE deploy of the LoRA app (the single cold start), wait for /health
#   7. warm smokes for BOTH model names through secret proxy -> capture -> Modal
#      (retries, never a redeploy); abort unless both pass
#   8. capture server (own OS-assigned port, endpoint read from capture.json
#      after the bind; explicit --port fails before any tick when taken),
#      telemetry sampler, watchdog that stops NEW launches (never the app or a
#      live trial) once the app's billed cost today reaches
#      --modal-app-day-limit-usd (HAR-156: money gates stop launches only)
#   9. tick position waves serially (all first arms, then seconds, then thirds),
#      approving each wave just before its tick, at one pinned --parallel;
#      between waves re-check /health; a wave advances only when every spec of
#      the previous wave has a result.json; stop on 3+ infra failures or any refusal;
#      with --round-cap-usd, stop when this round's own spend (Modal app since
#      deploy + its Daytona trials) plus a projected average wave would pass the cap
#  10. capture link per job, freeze the capture file (sha256/bytes/lines),
#      reconcile billing, write round-manifest.json
#
# The lab's drain teardown only stops evallab-mimo-v26-9b
# (modal_ops.MODAL_APP_NAME), never the LoRA app, so this script stops the LoRA
# app itself on every exit path (EXIT trap).
#
# Lessons built in from G2/G5 (2026-10-01): a redeploy restarts a live container
# (no redeploy after step 6); a warm check that is not enforced lets a tick run
# against a cold server; fixed capture ports collide across concurrent segments
# (HAR-145: every call lands in one file), so each segment binds its own
# OS-assigned port and reads the endpoint from capture.json after the bind.
set -euo pipefail

usage() { sed -n '2,30p' "$0"; exit 2; }

SPECS_DIR="" ADAPTER="" ADAPTER_NAME="har129" CANDIDATE_USD="" CAP_USD="35"
PARALLEL="20" LABEL="g5" ACTOR="" MODAL_APP_DAY_LIMIT_USD="" GEPA_CANDIDATE="" GEPA_SHA256=""
PORT="" DRY_RUN=0 ROUND_CAP_USD="" MODAL_RATE_USD_PER_H="2.8149" RESUME_NAMES="" PRIOR_SPEND_USD="0"
while [ $# -gt 0 ]; do
  case "$1" in
    --specs-dir) SPECS_DIR=$2; shift 2 ;;
    --adapter) ADAPTER=$2; shift 2 ;;
    --adapter-name) ADAPTER_NAME=$2; shift 2 ;;
    --candidate-usd) CANDIDATE_USD=$2; shift 2 ;;
    --cap-usd) CAP_USD=$2; shift 2 ;;
    --parallel) PARALLEL=$2; shift 2 ;;
    --label) LABEL=$2; shift 2 ;;
    --actor) ACTOR=$2; shift 2 ;;
    --modal-app-day-limit-usd) MODAL_APP_DAY_LIMIT_USD=$2; shift 2 ;;
    --modal-rate-usd-per-h) MODAL_RATE_USD_PER_H=$2; shift 2 ;;
    --gepa-candidate) GEPA_CANDIDATE=$2; shift 2 ;;
    --gepa-sha256) GEPA_SHA256=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --round-cap-usd) ROUND_CAP_USD=$2; shift 2 ;;
    --resume-names) RESUME_NAMES=$2; shift 2 ;;
    --prior-spend-usd) PRIOR_SPEND_USD=$2; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done
[ -n "$SPECS_DIR" ] && [ -n "$ADAPTER" ] && [ -n "$CANDIDATE_USD" ] && [ -n "$ACTOR" ] && [ -n "$MODAL_APP_DAY_LIMIT_USD" ] || usage

REPO=$(git rev-parse --show-toplevel)
cd "$REPO"
EVALLAB="$REPO/.venv/bin/evallab"
PY="$REPO/.venv/bin/python"
MODAL=(uv run --project tools/modal-mimo-serve --locked modal)
TREE=research/experiments/har126-lf2/harness-lf2
TREE_DIGEST=sha256:f18091f344b075230bf99744fb92dd75c1e9ebe67f5cb15027a0d6ce791456be
KEY_FILE="$HOME/.local/state/evallab-har116/key"
APP=$(awk -F'"' '/^APP_NAME *=/{print $2}' tools/modal-mimo-serve/serve_lora.py)
BASE_SELECTOR="selfhosted/XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B"
DAY=$(date -u +%F)
OUT="$HOME/Developer/eval-lab-results/$DAY/$LABEL-round"
[ "$DRY_RUN" = 1 ] && OUT="$OUT-dryrun-$(date -u +%H%M%S)"
CAPDIR="$HOME/Developer/eval-lab-results/har126-capture/$LABEL"
LOG="$OUT/round.log"
export EVALLAB_DERIVED_ROOT="$REPO/derived/parquet"
mkdir -p "$OUT" "$CAPDIR"

log() { echo "$(date -u +%FT%TZ) $*" | tee -a "$LOG"; }
die() { log "ABORT: $*"; exit 3; }
manifest() { # manifest KEY JSON-VALUE: merge one key into round-manifest.json
  "$PY" - "$OUT/round-manifest.json" "$1" "$2" <<'EOF'
import json, sys, pathlib
p = pathlib.Path(sys.argv[1])
doc = json.loads(p.read_text()) if p.exists() else {}
doc[sys.argv[2]] = json.loads(sys.argv[3])
p.write_text(json.dumps(doc, indent=2) + "\n")
EOF
}

# ---- 1. preflight ---------------------------------------------------------
[ ! -e "$OUT/round-manifest.json" ] || { echo "round $LABEL already ran today ($OUT); use a fresh --label" >&2; exit 3; }
log "round $LABEL: app $APP adapter $ADAPTER name $ADAPTER_NAME parallel $PARALLEL"
[ -n "$APP" ] || die "APP_NAME not found in serve_lora.py"
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "checkout has tracked changes; launch from a clean main checkout"
COMMIT=$(git rev-parse HEAD)
manifest checkout "{\"commit\": \"$COMMIT\", \"repo\": \"$REPO\", \"app\": \"$APP\", \"adapter\": \"$ADAPTER\", \"adapter_name\": \"$ADAPTER_NAME\", \"parallel\": $PARALLEL, \"cap_usd\": $CAP_USD, \"candidate_usd\": $CANDIDATE_USD}"
[ -r "$KEY_FILE" ] || die "self-hosted key file $KEY_FILE missing"
CHECK_ARGS=(--check "$SPECS_DIR" --tree "$TREE" --tree-digest "$TREE_DIGEST")
[ -n "$GEPA_CANDIDATE" ] && CHECK_ARGS+=(--gepa-candidate "$GEPA_CANDIDATE" --gepa-sha256 "$GEPA_SHA256")
"$PY" research/experiments/ovn-sft-v0/make_g5_specs.py "${CHECK_ARGS[@]}" >"$OUT/spec-check.txt" 2>&1 || true
cat "$OUT/spec-check.txt" >>"$LOG"
grep -q '^check ok' "$OUT/spec-check.txt" || die "spec --check failed (see $OUT/spec-check.txt)"
# Every spec's agent/model must resolve to a profile and pass its pin, exactly
# as dispatch will check it (g5r wave 1: the tuned arm had no profile).
"$PY" - "$SPECS_DIR" "$REPO" >"$OUT/profile-check.txt" 2>&1 <<'EOF' || die "profile preflight failed (see $OUT/profile-check.txt)"
import json, sys
from collections import Counter
from pathlib import Path
from evallab.profiles import validate_model_pin
from evallab.runner import RunRequest, profile_for_request
specs, repo = Path(sys.argv[1]), Path(sys.argv[2])
resolved, failures = Counter(), []
for path in sorted(specs.glob("ovn-g5-*.json")):
    spec = json.loads(path.read_text())
    try:
        request = RunRequest(task=repo, agent=spec["agent"], model=spec.get("model"), name=spec["name"], jobs_dir=repo / "runs")
        profile = profile_for_request(request)
        validate_model_pin(profile, spec.get("model"))
        resolved[(spec.get("model"), profile.profile_id)] += 1
    except ValueError as exc:
        failures.append(f"{path.name}: {exc}")
for (model, profile_id), count in sorted(resolved.items()):
    print(f"{count} specs: {model} -> {profile_id}")
print("\n".join(failures))
sys.exit(1 if failures else 0)
EOF
cat "$OUT/profile-check.txt" >>"$LOG"
SMOKE_HELP=$("$EVALLAB" capture smoke --help 2>/dev/null || true)
case "$SMOKE_HELP" in *--model*) ;; *) die "evallab capture smoke has no --model; the adapter leg cannot be smoked" ;; esac

# ---- 2-3. billing reconcile + spend check -------------------------------------
"$EVALLAB" modal billing-reconcile --for "$DAY" >"$OUT/billing-reconcile-before.txt" 2>&1 || die "billing reconcile failed"
"$EVALLAB" spend check --candidate-usd "$CANDIDATE_USD" --cap-usd "$CAP_USD" --since "${DAY}T00:00:00Z" --json \
  >"$OUT/spend-check.json" 2>/dev/null || true
"$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get('allowed') else 1)" "$OUT/spend-check.json" \
  || die "spend check refused: $(cat "$OUT/spend-check.json")"
log "spend check allowed (candidate \$$CANDIDATE_USD, cap \$$CAP_USD)"

# ---- 4. wave plan (by name), then submit -------------------------------------------
"$PY" - "$SPECS_DIR" "$OUT" "$RESUME_NAMES" <<'EOF' | tee -a "$LOG" || die "wave plan failed"
import json, sys, pathlib
specs, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
cohort = json.loads((specs / "cohort.json").read_text())["cohort"]
files = {p.stem for p in specs.glob("ovn-g5-*.json")}
# --resume-names: run only the listed specs; every cohort spec left out must
# already have a result.json, so each task's arm order is still preserved.
keep = None
if sys.argv[3]:
    keep = {line.strip() for line in open(sys.argv[3]) if line.strip()}
    unknown = keep - files
    if unknown:
        sys.exit(f"--resume-names lists specs not in {specs}: {sorted(unknown)}")
waves = {}
for entry in cohort:
    short = entry["task_id"].removeprefix("format-code-task-")
    for position, arm in enumerate(entry["order"], start=1):
        name = f"ovn-g5-{short}-{arm}"
        if name not in files:
            sys.exit(f"cohort names {name} but {specs} has no {name}.json")
        if keep is not None and name not in keep:
            if not list(pathlib.Path("runs", name).glob("*/result.json")):
                sys.exit(f"--resume-names leaves out {name}, which has no result.json")
            continue
        waves.setdefault(position, []).append(name)
if keep is None and sum(len(v) for v in waves.values()) != len(files):
    sys.exit("spec files and cohort waves disagree")
for position, names in sorted(waves.items()):
    (out / f"wave-{position}.names").write_text("\n".join(names) + "\n")
    print(f"wave {position}: {len(names)} specs ({', '.join(names[:3])}, ...)")
EOF
WAVE_NAMES=$(ls "$OUT"/wave-*.names | sort)
[ -n "$WAVE_NAMES" ] || die "no position waves planned"
for names in $WAVE_NAMES; do
  while read -r name; do
    [ ! -e "runs/$name" ] || die "runs/$name already exists; G5 job names must be fresh"
  done <"$names"
done

if [ "$DRY_RUN" = 1 ]; then
  log "dry run: preflight, reconcile, spend check and wave plan passed; nothing submitted, deployed or ticked"
  exit 0
fi

: >"$OUT/ids.txt"
for names in $WAVE_NAMES; do
  wave="${names%.names}.txt"
  : >"$wave"
  while read -r name; do
    id=$("$EVALLAB" submit "$SPECS_DIR/$name.json" </dev/null 2>/dev/null | "$PY" -c "import re,sys; m=re.search(r'01M[0-9A-Z]{23}', sys.stdin.read()); print(m.group(0) if m else '')" || true)
    [ -n "$id" ] || die "submit failed for $name"
    echo "$name $id" | tee -a "$OUT/ids.txt" >>"$wave"
  done <"$names"
done
WAVES=$(ls "$OUT"/wave-*.txt | sort)
log "submitted $(awk 'END{print NR}' "$OUT/ids.txt") specs into $(echo "$WAVES" | awk 'END{print NR}') position waves"

# ---- teardown on every exit path -----------------------------------------------
CAPTURE_PID="" SAMPLER_PID="" WATCHDOG_PID=""
teardown() {
  status=$?
  for pid in "$WATCHDOG_PID" "$SAMPLER_PID" "$CAPTURE_PID"; do
    [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true
  done
  "${MODAL[@]}" app stop --yes "$APP" >>"$LOG" 2>&1 || log "app stop returned nonzero"
  state="unknown"
  for _ in $(seq 1 24); do
    state=$("${MODAL[@]}" app list --json 2>/dev/null | "$PY" -c "import json,sys; print(next((a.get('state') for a in json.load(sys.stdin) if a.get('description')==sys.argv[1]), 'absent'))" "$APP" 2>/dev/null || echo unknown)
    [ "$state" = "stopped" ] || [ "$state" = "absent" ] && break
    sleep 5
  done
  manifest teardown "{\"at\": \"$(date -u +%FT%TZ)\", \"app\": \"$APP\", \"state\": \"$state\", \"exit_status\": $status}"
  log "teardown: $APP $state (exit $status)"
}
trap teardown EXIT

# ---- 6. the one cold start --------------------------------------------------------
KEY="$(<"$KEY_FILE")"
app_cost() { # today's billed Modal cost for $APP alone
  "${MODAL[@]}" billing report --for today --json 2>/dev/null \
    | "$PY" -c "import json,sys; print(sum(float(r['cost']) for r in json.load(sys.stdin) if r.get('description')==sys.argv[1]))" "$APP"
}
MODAL_BASELINE=$(app_cost || echo 0)
"$PY" -c "import sys; sys.exit(0 if float(sys.argv[1]) < float(sys.argv[2]) else 1)" "$MODAL_BASELINE" "$MODAL_APP_DAY_LIMIT_USD" \
  || die "$APP already billed \$$MODAL_BASELINE today, at or over the \$$MODAL_APP_DAY_LIMIT_USD app-day limit"
DEPLOYED_EPOCH=$(date +%s)
log "deploy $APP (cold start)"
# COLUMNS keeps Modal's rich output from wrapping the endpoint URL across lines.
DEPLOY_OUT=$(COLUMNS=1000 EVALLAB_MIMO_LORA_ADAPTER="$ADAPTER" EVALLAB_MIMO_LORA_NAME="$ADAPTER_NAME" \
  "${MODAL[@]}" deploy tools/modal-mimo-serve/serve_lora.py 2>&1) || die "deploy failed: $DEPLOY_OUT"
echo "$DEPLOY_OUT" >"$OUT/deploy.txt"
URL=$(awk 'match($0, /https:\/\/[A-Za-z0-9.-]+\.modal\.direct/) {print substr($0, RSTART, RLENGTH); exit}' <<<"$DEPLOY_OUT")
[ -n "$URL" ] || die "no modal.direct URL in deploy output (see $OUT/deploy.txt)"
manifest endpoint "{\"url\": \"$URL\", \"deployed_at\": \"$(date -u +%FT%TZ)\"}"
for _ in $(seq 1 60); do
  [ "$(curl -s -o /dev/null -w '%{http_code}' -m 20 "$URL/health")" = 200 ] && break
  sleep 10
done
[ "$(curl -s -o /dev/null -w '%{http_code}' -m 20 "$URL/health")" = 200 ] || die "$URL/health never returned 200"
log "health 200 at $URL"

# ---- 7. warm smokes, both model names, full chain -----------------------------------
warm() {
  for selector in "$BASE_SELECTOR" "$BASE_SELECTOR:$ADAPTER_NAME"; do
    leg=${selector##*:}; [ "$leg" = "$selector" ] && leg=base
    ok=0
    for attempt in 1 2 3; do
      if MIMO_SELFHOSTED_API_KEY="$KEY" "$EVALLAB" capture smoke --upstream "$URL" --model "$selector" \
          --out "$OUT/smoke-$1-$leg" >"$OUT/smoke-$1-$leg.txt" 2>&1 \
        && "$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get('status')==200 and d.get('model')==sys.argv[2].removeprefix('selfhosted/') else 1)" \
          "$OUT/smoke-$1-$leg/smoke.json" "$selector"; then ok=1; break; fi
      log "warm smoke $leg attempt $attempt failed (needs status 200 and echoed model ${selector#selfhosted/})"
      sleep 60
    done
    [ "$ok" = 1 ] || return 1
    log "warm smoke $leg ok: $("$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); print(d['status'], d['model'], d['route_token'])" "$OUT/smoke-$1-$leg/smoke.json")"
  done
}
warm initial || die "warm smoke failed; nothing approved or ticked"

# ---- 8. capture, telemetry, spend watchdog -------------------------------------------
# Each segment binds its own OS-assigned free port by default (empty --port);
# an explicit --port is passed through and fails here, before any tick, when
# taken. The endpoint is read from capture.json only after the bind succeeds
# (never a pre-bind port probe), and the runner records the capture directory
# in each job's lab-metadata.json and auto-links the job to this file.
[ ! -s "$CAPDIR/calls.jsonl" ] || die "capture file $CAPDIR/calls.jsonl already has records; use a fresh --label"
rm -f "$CAPDIR/capture.json" "$CAPDIR/provenance.json"
CAPTURE_ARGS=(--upstream "$URL" --out "$CAPDIR")
[ -n "$PORT" ] && CAPTURE_ARGS+=(--port "$PORT")
"$EVALLAB" capture serve "${CAPTURE_ARGS[@]}" >>"$OUT/capture.log" 2>&1 &
CAPTURE_PID=$!
CAPTURE_ENDPOINT=""
for _ in $(seq 1 20); do
  kill -0 "$CAPTURE_PID" 2>/dev/null || die "capture server (pid $CAPTURE_PID) exited; see $OUT/capture.log"
  if [ -s "$CAPDIR/capture.json" ]; then
    CAPTURE_ENDPOINT=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1])).get('endpoint',''))" "$CAPDIR/capture.json" 2>/dev/null || true)
    if [ -n "$CAPTURE_ENDPOINT" ] && [ "$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$CAPTURE_ENDPOINT/healthz")" = 200 ]; then
      break
    fi
    CAPTURE_ENDPOINT=""
  fi
  sleep 1
done
[ -n "$CAPTURE_ENDPOINT" ] || die "capture server (pid $CAPTURE_PID) published no healthy endpoint; see $OUT/capture.log"
"$EVALLAB" telemetry sample --out "$OUT/telemetry.jsonl" --metrics-url "$URL/metrics" --runs-dir runs \
  --queue-dir queue --modal-app "$APP" --interval 15.0 >>"$OUT/telemetry.log" 2>&1 &
SAMPLER_PID=$!
# (app_cost and MODAL_BASELINE were taken before the deploy, so the cold start counts.)
# HAR-156: the watchdog never stops the app mid-wave (that would end live trials
# by our money gate, not by the model or the task). It drops a marker; the wave
# loop launches nothing after it, the in-flight wave finishes, and the EXIT
# trap stops the app.
STOP_MARKER="$OUT/stop-launches"
(
  while sleep 180; do
    cost=$(app_cost) || continue
    if "$PY" -c "import sys; sys.exit(0 if float(sys.argv[1]) >= float(sys.argv[2]) else 1)" "$cost" "$MODAL_APP_DAY_LIMIT_USD"; then
      echo "$(date -u +%FT%TZ) WATCHDOG: $APP billed \$$cost today, app-day limit \$$MODAL_APP_DAY_LIMIT_USD; no further launches (in-flight trials finish)" >>"$LOG"
      echo "$cost" >"$STOP_MARKER"
      exit 0
    fi
  done
) &
WATCHDOG_PID=$!
log "capture pid $CAPTURE_PID at $CAPTURE_ENDPOINT (out $CAPDIR), sampler pid $SAMPLER_PID, watchdog pid $WATCHDOG_PID ($APP app-day limit \$$MODAL_APP_DAY_LIMIT_USD; billed \$$MODAL_BASELINE before deploy)"

# ---- 9. tick position waves serially, approving each wave just before its tick ----------
# (the lab's drain teardown never targets the LoRA app, so nothing stops it between waves)
TOTAL_WAVES=$(echo "$WAVES" | awk 'END{print NR}')
WAVES_DONE=0
PREV_SPENT=0
ROUND_STATUS=0
for wave in $WAVES; do
  position=$(basename "$wave" .txt)
  if [ -e "$STOP_MARKER" ]; then
    manifest gate_failure "{\"wave\": \"$position\", \"reason\": \"app-day limit reached; launches stopped\", \"billed_usd\": $(cat "$STOP_MARKER")}"
    log "$position: not launched: app-day limit reached (in-flight trials were allowed to finish)"
    ROUND_STATUS=3
    break
  fi
  if [ "$(curl -s -o /dev/null -w '%{http_code}' -m 20 "$URL/health")" != 200 ]; then
    warm "$position" || die "server unhealthy before $position and warm smoke failed"
  fi
  args=()
  count=0
  while read -r name id; do
    "$EVALLAB" approve "$id" --actor "$ACTOR" </dev/null >/dev/null 2>&1 || die "approve failed for $name"
    args+=(--spec-id "$id")
    count=$((count + 1))
  done <"$wave"
  started=$(date -u +%FT%TZ)
  log "$position: tick $count specs at parallel $PARALLEL"
  tick_status=0
  EVALLAB_MIMO_SELFHOSTED_UPSTREAM="$CAPTURE_ENDPOINT" EVALLAB_MODEL_CAPTURE=1 EVALLAB_MODEL_CAPTURE_DIR="$CAPDIR" MIMO_SELFHOSTED_API_KEY="$KEY" \
    "$EVALLAB" tick --parallel "$PARALLEL" "${args[@]}" >>"$OUT/$position-tick.log" 2>&1 || tick_status=$?
  finished=$(date -u +%FT%TZ)
  # Advance only when every spec of this wave has a result.json written during
  # this wave (stale results do not count), fewer than 3 infra failures (no
  # verifier reward and an exception outside AGENT_STOP_EXCEPTIONS) and no
  # refusal: PREREG's serial-per-task order. A nonzero tick also stops the round.
  advance=0
  "$PY" research/experiments/ovn-sft-v0/g5_wave_outcome.py "$wave" "$started" --specs-dir "$SPECS_DIR" \
    --capture "$CAPDIR/calls.jsonl" >"$OUT/$position-outcome.json" || advance=$?
  verdict=$(cat "$OUT/$position-outcome.json")
  manifest "$position" "{\"started\": \"$started\", \"finished\": \"$finished\", \"tick_status\": $tick_status, \"outcome\": $verdict}"
  log "$position done (tick exit $tick_status): $verdict"
  if [ "$tick_status" != 0 ] || [ "$advance" != 0 ]; then
    last_wave=false
    [ "$((WAVES_DONE + 1))" = "$TOTAL_WAVES" ] && last_wave=true
    manifest gate_failure "{\"wave\": \"$position\", \"tick_status\": $tick_status, \"outcome_status\": $advance, \"last_wave\": $last_wave}"
    log "GATE FAILED: $position (tick exit $tick_status, outcome exit $advance): non-terminal spec, 3+ infra failures, refusal, or unexpected model; last_wave=$last_wave"
    [ "$last_wave" = true ] || die "$position: failed gate; later waves not ticked"
    ROUND_STATUS=3
    log "$position: last-wave gate failed; preserving capture and billing in step 10 before exiting $ROUND_STATUS"
    break
  fi
  WAVES_DONE=$((WAVES_DONE + 1))
  # Before the next wave: reconcile, then refuse if spend so far (--prior-spend-usd
  # from earlier segments of the same round + this run's own spend) plus the cost
  # of the LAST completed wave would pass --round-cap-usd.
  # This run's spend: Modal = max(billed delta on $APP since before deploy, wall
  # hours since deploy x MODAL_RATE_USD_PER_H) because billing lags and
  # max_containers=1; Daytona = this run's trial lifetimes x the rate card.
  # (`spend check --since` sums ALL lab spend in the window, other lanes included.)
  if [ -n "$ROUND_CAP_USD" ] && [ "$WAVES_DONE" -lt "$TOTAL_WAVES" ]; then
    "$EVALLAB" modal billing-reconcile --for "$DAY" >"$OUT/billing-reconcile-$position.txt" 2>&1 || log "billing reconcile after $position failed"
    billed=$(app_cost || echo 0)
    "$PY" - "$OUT/ids.txt" "$DEPLOYED_EPOCH" "$billed" "$MODAL_BASELINE" "$MODAL_RATE_USD_PER_H" "$PREV_SPENT" "$PRIOR_SPEND_USD" "$ROUND_CAP_USD" \
      >"$OUT/budget-after-$position.json" <<'EOF' || die "round spend plus the last wave's cost would pass the \$$ROUND_CAP_USD round cap ($(cat "$OUT/budget-after-$position.json")); later waves not ticked"
import glob, json, sys, time
from datetime import datetime
ids, deployed, billed, baseline, rate, prev, prior, cap = sys.argv[1:9]
modal = max(float(billed) - float(baseline), (time.time() - float(deployed)) / 3600 * float(rate))
daytona_seconds = 0.0
for line in open(ids):
    for path in glob.glob(f"runs/{line.split()[0]}/*/result.json"):
        result = json.load(open(path))
        if result.get("started_at") and result.get("finished_at"):
            start = datetime.fromisoformat(result["started_at"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(result["finished_at"].replace("Z", "+00:00"))
            daytona_seconds += (end - start).total_seconds()
daytona = daytona_seconds / 3600 * 0.23094
spent = modal + daytona
last_wave = spent - float(prev)
total = float(prior) + spent
out = {"modal_usd": modal, "daytona_usd": daytona, "this_run_usd": spent, "prior_segments_usd": float(prior),
       "round_total_usd": total, "last_wave_usd": last_wave, "round_cap_usd": float(cap),
       "allowed": total + last_wave <= float(cap)}
print(json.dumps(out))
sys.exit(0 if out["allowed"] else 1)
EOF
    PREV_SPENT=$("$PY" -c "import json,sys; print(json.load(open(sys.argv[1]))['this_run_usd'])" "$OUT/budget-after-$position.json")
    log "$position budget: $(cat "$OUT/budget-after-$position.json")"
  fi
done

# ---- 10. link, freeze, reconcile ------------------------------------------------------
# The runner already auto-linked each job to this file at completion (route
# token); this per-job pass is the backstop that seals the final bytes after
# the server stops.
kill -TERM "$CAPTURE_PID" 2>/dev/null || true; wait "$CAPTURE_PID" 2>/dev/null || true; CAPTURE_PID=""
kill -TERM "$SAMPLER_PID" 2>/dev/null || true; SAMPLER_PID=""
while read -r name id; do
  [ -d "runs/$name" ] || continue
  "$EVALLAB" capture link "$CAPDIR" "$REPO/runs/$name" --json </dev/null >>"$OUT/capture-links.jsonl" 2>>"$OUT/capture-links.err" \
    || log "capture link failed for $name"
done <"$OUT/ids.txt"
chmod a-w "$CAPDIR/calls.jsonl"
manifest capture "{\"file\": \"$CAPDIR/calls.jsonl\", \"sha256\": \"$(shasum -a 256 "$CAPDIR/calls.jsonl" | awk '{print $1}')\", \"bytes\": $(wc -c <"$CAPDIR/calls.jsonl"), \"lines\": $(awk 'END{print NR}' "$CAPDIR/calls.jsonl")}"
"$EVALLAB" modal billing-reconcile --for "$DAY" >"$OUT/billing-reconcile-after.txt" 2>&1 || log "billing reconcile after the round failed"
"$EVALLAB" spend day --date "$DAY" >"$OUT/spend-day-after.txt" 2>&1 || true
if [ "$ROUND_STATUS" = 0 ]; then
  log "round $LABEL complete; manifest $OUT/round-manifest.json"
else
  log "round $LABEL finalized after failed last-wave gate (exit $ROUND_STATUS); manifest $OUT/round-manifest.json"
fi
exit "$ROUND_STATUS"
