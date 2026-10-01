#!/usr/bin/env bash
# OVN G5 operator round (HAR-126): the whole paired eval in ONE LoRA-server session.
#
#   run_g5.sh --specs-dir DIR --adapter REL --candidate-usd USD --actor TEXT [options]
#
# Order (each step aborts the round on failure; nothing billable starts before 6):
#   1. preflight: clean checkout, spec --check, free capture port, key file,
#      `capture smoke --model` available
#   2. `evallab modal billing-reconcile --for <UTC day>`
#   3. `evallab spend check --candidate-usd USD --cap-usd CAP --since <UTC day>`
#   4. plan position waves by spec name from cohort.json
#   5. (--dry-run stops here; nothing submitted) submit every spec, record ids
#   6. ONE deploy of the LoRA app (the single cold start), wait for /health
#   7. warm smokes for BOTH model names through secret proxy -> capture -> Modal
#      (retries, never a redeploy); abort unless both pass
#   8. capture server (PID-checked owner of the port), telemetry sampler,
#      per-app Modal spend watchdog
#   9. tick position waves serially (all first arms, then seconds, then thirds),
#      approving each wave just before its tick, at one pinned --parallel;
#      between waves re-check /health; a wave advances only when every spec of
#      the previous wave has a result.json; stop on 3+ infra failures or any refusal
#  10. capture link per job, freeze the capture file (sha256/bytes/lines),
#      reconcile billing, write round-manifest.json
#
# The lab's drain teardown only stops evallab-mimo-v26-9b
# (modal_ops.MODAL_APP_NAME), never the LoRA app, so this script stops the LoRA
# app itself on every exit path (EXIT trap).
#
# Lessons built in from G2 (2026-10-01): a redeploy restarts a live container
# (no redeploy after step 6); a warm check that is not enforced lets a tick run
# against a cold server; a capture server that outlives its round keeps the port
# and silently swallows the next round's calls.
set -euo pipefail

usage() { sed -n '2,30p' "$0"; exit 2; }

SPECS_DIR="" ADAPTER="" ADAPTER_NAME="har129" CANDIDATE_USD="" CAP_USD="35"
PARALLEL="20" LABEL="g5" ACTOR="" MODAL_LIMIT_USD="" GEPA_CANDIDATE="" GEPA_SHA256=""
PORT="8472" DRY_RUN=0
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
    --modal-limit-usd) MODAL_LIMIT_USD=$2; shift 2 ;;
    --gepa-candidate) GEPA_CANDIDATE=$2; shift 2 ;;
    --gepa-sha256) GEPA_SHA256=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) echo "unknown argument: $1" >&2; usage ;;
  esac
done
[ -n "$SPECS_DIR" ] && [ -n "$ADAPTER" ] && [ -n "$CANDIDATE_USD" ] && [ -n "$ACTOR" ] || usage
[ -n "$MODAL_LIMIT_USD" ] || MODAL_LIMIT_USD=$CANDIDATE_USD

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
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then die "capture port $PORT already has a listener"; fi
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
"$PY" - "$SPECS_DIR" "$OUT" <<'EOF' | tee -a "$LOG" || die "wave plan failed"
import json, sys, pathlib
specs, out = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
cohort = json.loads((specs / "cohort.json").read_text())["cohort"]
files = {p.stem for p in specs.glob("ovn-g5-*.json")}
waves = {}
for entry in cohort:
    short = entry["task_id"].removeprefix("format-code-task-")
    for position, arm in enumerate(entry["order"], start=1):
        name = f"ovn-g5-{short}-{arm}"
        if name not in files:
            sys.exit(f"cohort names {name} but {specs} has no {name}.json")
        waves.setdefault(position, []).append(name)
if sum(len(v) for v in waves.values()) != len(files):
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
log "deploy $APP (cold start)"
DEPLOY_OUT=$(EVALLAB_MIMO_LORA_ADAPTER="$ADAPTER" EVALLAB_MIMO_LORA_NAME="$ADAPTER_NAME" \
  "${MODAL[@]}" deploy tools/modal-mimo-serve/serve_lora.py 2>&1) || die "deploy failed: $DEPLOY_OUT"
URL=$(awk 'match($0, /https:\/\/[A-Za-z0-9.-]+\.modal\.direct/) {print substr($0, RSTART, RLENGTH); exit}' <<<"$DEPLOY_OUT")
[ -n "$URL" ] || die "no modal.direct URL in deploy output"
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
[ ! -s "$CAPDIR/calls.jsonl" ] || die "capture file $CAPDIR/calls.jsonl already has records; use a fresh --label"
"$EVALLAB" capture serve --upstream "$URL" --out "$CAPDIR" --port "$PORT" >>"$OUT/capture.log" 2>&1 &
CAPTURE_PID=$!
for _ in $(seq 1 20); do
  [ "$(lsof -t -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)" = "$CAPTURE_PID" ] && break
  sleep 1
done
[ "$(lsof -t -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null)" = "$CAPTURE_PID" ] || die "capture server (pid $CAPTURE_PID) does not own port $PORT"
"$EVALLAB" telemetry sample --out "$OUT/telemetry.jsonl" --metrics-url "$URL/metrics" --runs-dir runs \
  --queue-dir queue --modal-app "$APP" --interval 15.0 >>"$OUT/telemetry.log" 2>&1 &
SAMPLER_PID=$!
app_cost() {
  "${MODAL[@]}" billing report --for today --json 2>/dev/null \
    | "$PY" -c "import json,sys; print(sum(float(r['cost']) for r in json.load(sys.stdin) if r.get('description')==sys.argv[1]))" "$APP"
}
MODAL_BASELINE=$(app_cost || echo 0)
(
  while sleep 180; do
    cost=$(app_cost) || continue
    if "$PY" -c "import sys; sys.exit(0 if float(sys.argv[1]) - float(sys.argv[2]) >= float(sys.argv[3]) else 1)" "$cost" "$MODAL_BASELINE" "$MODAL_LIMIT_USD"; then
      echo "$(date -u +%FT%TZ) WATCHDOG: $APP billed \$$cost (baseline \$$MODAL_BASELINE, limit \$$MODAL_LIMIT_USD); stopping" >>"$LOG"
      "${MODAL[@]}" app stop --yes "$APP" >>"$LOG" 2>&1
      exit 0
    fi
  done
) &
WATCHDOG_PID=$!
log "capture pid $CAPTURE_PID on :$PORT, sampler pid $SAMPLER_PID, watchdog pid $WATCHDOG_PID (Modal limit \$$MODAL_LIMIT_USD over baseline \$$MODAL_BASELINE)"

# ---- 9. tick position waves serially, approving each wave just before its tick ----------
# (the lab's drain teardown never targets the LoRA app, so nothing stops it between waves)
for wave in $WAVES; do
  position=$(basename "$wave" .txt)
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
  EVALLAB_MIMO_SELFHOSTED_UPSTREAM="http://127.0.0.1:$PORT" EVALLAB_MODEL_CAPTURE=1 MIMO_SELFHOSTED_API_KEY="$KEY" \
    "$EVALLAB" tick --parallel "$PARALLEL" "${args[@]}" >>"$OUT/$position-tick.log" 2>&1 || tick_status=$?
  finished=$(date -u +%FT%TZ)
  # Advance only when every spec of this wave has a result.json written during
  # this wave (stale results do not count), fewer than 3 infra failures (no
  # verifier reward and an exception outside AGENT_STOP_EXCEPTIONS) and no
  # refusal: PREREG's serial-per-task order. A nonzero tick also stops the round.
  advance=0
  "$PY" research/experiments/ovn-sft-v0/g5_wave_outcome.py "$wave" "$started" >"$OUT/$position-outcome.json" || advance=$?
  verdict=$(cat "$OUT/$position-outcome.json")
  manifest "$position" "{\"started\": \"$started\", \"finished\": \"$finished\", \"tick_status\": $tick_status, \"outcome\": $verdict}"
  log "$position done (tick exit $tick_status): $verdict"
  [ "$tick_status" = 0 ] || die "$position: tick exited $tick_status; later waves not ticked"
  [ "$advance" = 0 ] || die "$position: not every spec is terminal, or 3+ infra failures, or a dispatch refusal; later waves not ticked"
done

# ---- 10. link, freeze, reconcile ------------------------------------------------------
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
log "round $LABEL complete; manifest $OUT/round-manifest.json"
