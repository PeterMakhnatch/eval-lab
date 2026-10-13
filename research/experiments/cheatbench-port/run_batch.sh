#!/usr/bin/env bash
# CheatBench-port batch runner: readiness gate -> submit/approve -> model-call
# capture -> per-family tick with file-access capture -> live monitor ->
# capture link -> postrun. Replaces dispatch.sh (deleted).
#
# Usage: research/experiments/cheatbench-port/run_batch.sh [--dry-run] <specs-dir> "<actor + approval quote>" [parallel]
#        research/experiments/cheatbench-port/run_batch.sh [--dry-run] --resume <batch_id> [parallel]
# Run from the worktree root. Paid: only use with a budget Peter granted for
# this experiment. --dry-run prints every command without executing it.
# --resume continues a batch whose specs are already submitted and approved
# (e.g. after a queue/STOP fence): no readiness, submit or approve; it ticks
# only groups with specs still approved, under a fresh capture dir. Fix the
# cause and run `uv run evallab resume` yourself first; it refuses while
# queue/STOP exists.
#
# Env knobs: CB_JUDGE (paid judge route for readiness + postrun; default EMPTY
#   = no paid judge: the cheat judge is Sonnet 5.5 on Peter's subscription via
#   fxtr's `launch.py batch`, see RUNBOOK). CB_NO_PROBE=1 skips the paid
#   1-token provider probe (on by default: run_batch is already a paid
#   action, so an empty provider balance aborts BEFORE dispatch).
#   CB_SPEND_CAP_USD: batch agent-spend cap; the monitor writes queue/STOP at
#   it (running trials finish under their own per-trial caps).
#   CB_ONLY: tick only specs whose job name contains this text (e.g. `-q36or-`);
#   the others stay approved for a later --resume. Use it to keep one model
#   going while another's provider is overloaded.
#
# Live stop: each spec's name ends in -on or -dry (make_specs.py --stop-mode);
# its group ticks with EVALLAB_WATCH_STOP=on or dry-run. Specs without the
# suffix tick in Eval Lab's default (dry-run).
set -euo pipefail

DRY=0
RESUME=""
if [ "${1:-}" = "--dry-run" ]; then
  DRY=1
  shift
fi
if [ "${1:-}" = "--resume" ]; then
  RESUME=${2:?usage: run_batch.sh [--dry-run] --resume <batch_id> [parallel]}
  shift 2
  spec_dir=""
  export CB_ACTOR="(resume: specs already approved)"
  parallel=${1:-2}
else
  spec_dir=${1:?usage: run_batch.sh [--dry-run] <specs-dir> "<actor + approval quote>" [parallel]}
  export CB_ACTOR=${2:?usage: run_batch.sh [--dry-run] <specs-dir> "<actor + approval quote>" [parallel]}
  parallel=${3:-2}
fi
CB_JUDGE=${CB_JUDGE:-}
EXP=research/experiments/cheatbench-port
TINKER_UP=https://tinker.thinkingmachines.dev
ZAI_UP=https://api.z.ai
OPENROUTER_UP=https://openrouter.ai
SPEND_ARGS=""
if [ -n "${CB_SPEND_CAP_USD:-}" ]; then SPEND_ARGS="--spend-cap-usd $CB_SPEND_CAP_USD"; fi
if [ -z "${CB_NO_PROBE:-}" ]; then
  PROBE_ARGS="--probe-providers"
  READINESS_KEYS="keys run --"
else
  PROBE_ARGS=""
  READINESS_KEYS=""
fi

if [ -n "$RESUME" ]; then
  batch_id=$RESUME
  CAP_SUFFIX="-resume-$(date -u +%H%M%S)"
else
  batch_date=$(date -u +%F)
  slug=$(basename "$spec_dir" | tr -c '[:alnum:]-' '-' | tr -s '-')
  slug=${slug%-}; slug=${slug#-}
  slug=$(printf '%.40s' "$slug")
  batch_id="$batch_date-$slug"
  CAP_SUFFIX=""
fi
batch_dir="runs/_batches/$batch_id"
run() {
  echo "+ $*"
  if [ "$DRY" = "0" ]; then
    "$@"
  fi
}

echo "batch: $batch_id"
run mkdir -p "$batch_dir/captures" "$batch_dir/groups" "$batch_dir/monitor"

# 1. Group specs by (family, provider, stop mode) for per-group ticks, cheapest
# family first (a spend-cap STOP then cuts the most expensive family, not all).
if [ -n "$RESUME" ]; then
  [ -f "$batch_dir/groups/order.txt" ] || { echo "no $batch_dir/groups/order.txt; nothing to resume" >&2; exit 2; }
  echo "resume: reusing $batch_dir/groups (no readiness, submit or approve)"
elif [ "$DRY" = "0" ]; then
  echo "grouping specs in $spec_dir ..."
  PYTHONPATH="$EXP" uv run --no-sync python - "$spec_dir" "$batch_dir" <<'PYEOF'
import json, sys
from pathlib import Path
from make_specs import stop_mode_of
spec_dir, batch = Path(sys.argv[1]), Path(sys.argv[2])
FAMS = ("prime_factorization", "subset_sum", "openmath_agent", "geoguessr", "synthetic_puzzles")
PROV = {"zai": "zai", "tinker": "tinker", "openrouter-metered": "openrouter"}
groups = {}
models, fams, names = set(), set(), []
for spec in sorted(spec_dir.glob("*.json")):
    s = json.loads(spec.read_text())
    model = s["model"]
    task = s.get("task", "")
    fam = next((f for f in FAMS if f in task), None)
    if fam is None:
        raise SystemExit(f"{spec}: cannot tell family from task {task!r}")
    prov = PROV.get(model.split("/", 1)[0])
    if prov is None:
        raise SystemExit(f"{spec}: unknown provider in model {model!r}")
    name = s.get("name", spec.stem)
    mode = stop_mode_of(name) or "dry-run"
    groups.setdefault((fam, prov, mode), []).append(spec.name)
    models.add(model); fams.add(fam); names.append(name)
(batch / "spec_files.txt").write_text("\n".join(sorted(p.name for p in spec_dir.glob("*.json"))) + "\n")
order = sorted(groups, key=lambda g: (FAMS.index(g[0]), g[1], g[2] != "on"))
for key in order:
    (batch / "groups" / (".".join(key) + ".txt")).write_text("\n".join(groups[key]) + "\n")
(batch / "groups" / "order.txt").write_text("\n".join(".".join(k) for k in order) + "\n")
(batch / "models.txt").write_text("\n".join(sorted(models)) + "\n")
(batch / "families.txt").write_text("\n".join(sorted(fams)) + "\n")
(batch / "job_names.txt").write_text("\n".join(sorted(names)) + "\n")
print(f"{len(groups)} groups, {sum(len(r) for r in groups.values())} specs: " + ", ".join(".".join(k) for k in order))
PYEOF
else
  echo "+ uv run --no-sync python - $spec_dir $batch_dir <<'PYEOF' (spec grouping by family/provider/stop mode)"
fi
FAMS=""; MODELS=""
[ -f "$batch_dir/families.txt" ] && FAMS=$(cat "$batch_dir/families.txt")
[ -f "$batch_dir/models.txt" ] && MODELS=$(tr '\n' ' ' < "$batch_dir/models.txt")
if [ "$DRY" = "1" ] && [ -z "$RESUME" ]; then
  # Dry-run reads nothing but the spec files (free, local) for faithful echoes.
  FAMS=$(grep -ho 'cheatbench/[a-z_]*' "$spec_dir"/*.json 2>/dev/null | cut -d/ -f2 | sort -u | tr '\n' ' ')
  MODELS=$(grep -ho '"model": *"[^"]*"' "$spec_dir"/*.json 2>/dev/null | cut -d'"' -f4 | sort -u | tr '\n' ' ')
fi
[ -n "$FAMS" ] || FAMS="subset_sum prime_factorization openmath_agent geoguessr synthetic_puzzles"
PROVS=""
case "$MODELS" in *zai/*) PROVS="$PROVS zai";; esac
case "$MODELS" in *tinker/*) PROVS="$PROVS tinker";; esac
case "$MODELS" in *openrouter-metered/*) PROVS="$PROVS openrouter";; esac

# 2. Readiness gate (aborts the batch on any fail). Probes providers by
# default (paid 1-token calls; run_batch is already paid); CB_JUDGE empty
# means no judge-fitness check and no judging downstream.
readiness_out="$batch_dir/readiness-receipt.json"
JUDGE_ARGS=""
if [ -n "$CB_JUDGE" ]; then JUDGE_ARGS="--judge $CB_JUDGE"; fi
if [ -n "$RESUME" ]; then
  # The gate already passed for this batch; its queue check would now fail on
  # the batch's own approved specs. A fence still in place means the cause has
  # not been dealt with.
  if [ -f queue/STOP ]; then
    echo "queue/STOP is present: fix the cause, then 'uv run evallab resume', then rerun --resume" >&2
    exit 2
  fi
elif [ "$DRY" = "0" ]; then
  # shellcheck disable=SC2086
  $READINESS_KEYS uv run --no-sync python "$EXP/readiness.py" \
    --families $FAMS --models $MODELS $JUDGE_ARGS $PROBE_ARGS \
    --out "$readiness_out"
else
  echo "+ $READINESS_KEYS uv run --no-sync python $EXP/readiness.py --families $FAMS --models $MODELS $JUDGE_ARGS $PROBE_ARGS --out $readiness_out"
fi

# 3. Submit + approve, 8-wide (submit/approve each read provider quota state,
# ~1 min apiece; the queue serialises its own event log with flock).
ids_file="$batch_dir/spec_ids.txt"
submit_map="$batch_dir/submit_map.txt"
if [ -n "$RESUME" ]; then
  echo "resume: specs already submitted and approved ($batch_dir/groups/*.ids)"
elif [ "$DRY" = "0" ]; then
  uv run --no-sync python - "$spec_dir" "$submit_map" <<'PYEOF'
import subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
spec_dir, out = Path(sys.argv[1]), Path(sys.argv[2])
def submit(p):
    r = subprocess.run(["uv", "run", "--no-sync", "evallab", "submit", str(p)],
                       capture_output=True, text=True)
    sid = next((l.split("spec_id:")[1].strip().split()[0]
                for l in r.stdout.splitlines() if l.startswith("spec_id:")), "?")
    return f"{p.name} {sid}"
with ThreadPoolExecutor(max_workers=8) as ex:
    rows = sorted(ex.map(submit, sorted(spec_dir.glob("*.json"))))
out.write_text("\n".join(rows) + "\n")
print(f"submitted {sum(1 for r in rows if not r.endswith(' ?'))} specs")
PYEOF
  cut -d' ' -f2 "$submit_map" > "$ids_file"
  CB_ACTOR="$CB_ACTOR" uv run --no-sync python - "$ids_file" <<'PYEOF'
import os, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ids = [l.strip().split()[-1] for l in Path(sys.argv[1]).read_text().splitlines() if l.strip()]
def approve(sid):
    r = subprocess.run(["uv", "run", "--no-sync", "evallab", "approve", sid,
                        "--actor", os.environ["CB_ACTOR"]],
                       capture_output=True, text=True)
    return any(l.startswith("authorized:") for l in r.stdout.splitlines())
with ThreadPoolExecutor(max_workers=8) as ex:
    print(f"authorized {sum(ex.map(approve, ids))}")
PYEOF
  uv run --no-sync python - "$spec_dir" "$submit_map" "$batch_dir" <<'PYEOF'
import sys
from pathlib import Path
spec_dir = Path(sys.argv[1]); batch = Path(sys.argv[3])
id_of = dict(l.strip().split() for l in Path(sys.argv[2]).read_text().splitlines()
             if l.strip())
for g in (batch / "groups").glob("*.txt"):
    if g.name == "order.txt":
        continue
    names = [l.strip() for l in g.read_text().splitlines() if l.strip()]
    (batch / "groups" / (g.stem + ".ids")).write_text(
        "\n".join(id_of.get(n, "?") for n in names) + "\n")
PYEOF
else
  echo "+ uv run --no-sync python (8-wide submit) $spec_dir -> $submit_map"
  echo "+ cut -d' ' -f2 $submit_map > $ids_file"
  echo "+ CB_ACTOR=\"\$CB_ACTOR\" uv run --no-sync python (8-wide approve) $ids_file"
  echo "+ uv run --no-sync python (spec-id mapping) -> $batch_dir/groups/*.ids"
fi

# 4. Model-call capture: one recording proxy per provider in use.
declare -a CAP_PIDS=()
start_capture() { # <provider> <upstream>
  prov=$1 up=$2
  out="$batch_dir/captures/$prov$CAP_SUFFIX"
  run mkdir -p "$out"
  if [ "$DRY" = "1" ]; then
    echo "+ uv run --no-sync evallab capture serve --upstream $up --out $out --bind 127.0.0.1 &"
    echo "+ ENDPOINT=\$(python -c \"import json; print(json.load(open('$out/capture.json'))['endpoint'])\")"
    return
  fi
  uv run --no-sync evallab capture serve --upstream "$up" --out "$out" --bind 127.0.0.1 &
  CAP_PIDS+=($!)
  for _ in $(seq 1 150); do
    [ -f "$out/capture.json" ] && break
    sleep 0.2
  done
  ENDPOINT=$(python3 -c "import json; print(json.load(open('$out/capture.json'))['endpoint'])")
  echo "$prov capture: $ENDPOINT"
  case "$prov" in
    tinker) export EVALLAB_TINKER_UPSTREAM="$ENDPOINT" ;;
    openrouter) export EVALLAB_OPENROUTER_UPSTREAM="$ENDPOINT" ;;
    *) export EVALLAB_ZAI_OPENAPI_UPSTREAM="$ENDPOINT" ;;
  esac
  export EVALLAB_MODEL_CAPTURE=1
  eval "export EVALLAB_MODEL_CAPTURE_DIR_$prov=\"$out\""
}
for prov in $PROVS; do
  case "$prov" in
    tinker) start_capture tinker "$TINKER_UP" ;;
    openrouter) start_capture openrouter "$OPENROUTER_UP" ;;
    *) start_capture zai "$ZAI_UP" ;;
  esac
done

# 5. Live cheat/infra monitor for the whole batch (background).
MON_PID=""
if [ -f "$EXP/monitor.py" ]; then
  if [ "$DRY" = "1" ]; then
    echo "+ uv run --no-sync python $EXP/monitor.py --runs-dir runs --out $batch_dir/monitor --stop-on-infra --jobs-file $batch_dir/job_names.txt $SPEND_ARGS &"
  else
    # shellcheck disable=SC2086
    uv run --no-sync python "$EXP/monitor.py" --runs-dir runs --out "$batch_dir/monitor" --stop-on-infra --jobs-file "$batch_dir/job_names.txt" $SPEND_ARGS &
    MON_PID=$!
  fi
else
  echo "WARN: $EXP/monitor.py missing (LiveMonitor pending); batch runs unmonitored" >&2
fi

# 6. Per (family, provider) tick with file-access capture env. tick can
# return with specs still queued, so re-tick while any group id is still
# approved (bounded). Waiting specs stop the batch: re-approval is a human
# decision, never automatic. queue/STOP (monitor --stop-on-infra) ends
# ticking and falls through to cleanup/postrun.
WAITING_MSG=""
tick_group() { # <family> <provider> <stop mode> <ids-file>
  fam=$1 prov=$2 mode=$3 ids=$4
  # shellcheck disable=SC2086
  eval "$(PYTHONPATH="$EXP" uv run --no-sync python -c "import capture_config, shlex; print(' '.join(f'export {k}={shlex.quote(v)}' for k, v in capture_config.file_access_env(\"$fam\").items()))")"
  eval "export EVALLAB_MODEL_CAPTURE_DIR=\"\$EVALLAB_MODEL_CAPTURE_DIR_$prov\""
  export EVALLAB_WATCH_STOP="$mode"
  tick_args=""; approved_left=""; group_ids=""
  # groups/<base>.txt (job names) and .ids (spec ids) are line-aligned.
  while read -r name s; do
    [ -n "$s" ] || continue
    case "$name" in *"${CB_ONLY:-}"*) ;; *) continue ;; esac
    group_ids="$group_ids $s"
    tick_args="$tick_args --spec-id $s"
    if ls queue/approved/*-"$s".json >/dev/null 2>&1; then
      approved_left="$approved_left $s"
    fi
  done < <(paste -d' ' "${ids%.ids}.txt" "$ids")
  if [ -z "$approved_left" ]; then
    echo "skip $fam/$prov live-stop=$mode: no spec still approved"
    return 0
  fi
  echo "tick $fam/$prov live-stop=$mode"
  round=0
  while [ "$round" -lt 20 ]; do
    round=$((round + 1))
    if [ -f queue/STOP ]; then
      echo "queue/STOP present; stop ticking $fam/$prov, continue to cleanup" >&2
      break
    fi
    # Full tick output (stdout+stderr) goes to the group's log; the console
    # shows only the dispatch lines.
    # shellcheck disable=SC2086
    keys run -- uv run --no-sync evallab tick $tick_args --parallel "$parallel" --no-smoke-gate 2>&1 \
      | tee -a "$batch_dir/tick-$fam.$prov.$mode.log" \
      | grep -E '^(dispatching|failed|completed|reused|dispatched|quarantined)' || true
    waiting=""; approved_left=""
    for sid in $group_ids; do
      if ls queue/waiting/*-"$sid".json >/dev/null 2>&1; then
        waiting="$waiting $sid"
      elif ls queue/approved/*-"$sid".json >/dev/null 2>&1; then
        approved_left="$approved_left $sid"
      fi
    done
    if [ -n "$waiting" ]; then
      code="unknown"
      for sid in $waiting; do
        latest=$(ls -t queue/reasons/"$sid"-*.json 2>/dev/null | head -1)
        if [ -n "$latest" ]; then
          code=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('code', 'unknown'))" "$latest")
        fi
        WAITING_MSG="STOP: spec $sid is waiting ($code); re-approval is a human decision, never automatic"
      done
      echo "$WAITING_MSG" >&2
      break
    fi
    if [ -z "$approved_left" ]; then break; fi
  done
  if [ -z "$WAITING_MSG" ] && [ -n "$approved_left" ]; then
    echo "WARN: still approved after 20 re-ticks:$approved_left; continue to cleanup" >&2
  fi
}
if [ "$DRY" = "1" ]; then
  echo "+ eval \$(uv run --no-sync python -c 'import capture_config, shlex; ...file_access_env...')"
  echo "+ keys run -- uv run --no-sync evallab tick --spec-id <ids of group> --parallel $parallel --no-smoke-gate   # per family/provider/stop-mode group in groups/order.txt, EVALLAB_WATCH_STOP=<on|dry-run>, re-tick while approved (max 20), stop on waiting/STOP"
else
  while IFS= read -r base; do         # <family>.<provider>.<stop mode>
    [ -n "$base" ] || continue
    [ -n "$WAITING_MSG" ] && break
    if [ -f queue/STOP ]; then
      echo "queue/STOP present; stop ticking, continue to cleanup" >&2
      break
    fi
    ids="$batch_dir/groups/$base.ids"
    [ -e "$ids" ] || continue
    fam=${base%%.*} rest=${base#*.}
    prov=${rest%%.*} mode=${rest#*.}
    tick_group "$fam" "$prov" "$mode" "$ids"
  done < "$batch_dir/groups/order.txt"
fi

# 7. Stop the monitor, then the capture proxies (SIGTERM so the close path runs).
if [ -n "$MON_PID" ]; then
  run kill -TERM "$MON_PID" || true
  run wait "$MON_PID" || true
fi
if [ "$DRY" = "1" ]; then
  echo "+ kill -TERM <capture pids>  # SIGTERM so provenance.json is written"
else
  for p in ${CAP_PIDS[@]:-}; do kill -TERM "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
fi

# 8. Attribute captured calls to trials (backstop; the runner also auto-links).
# Batch jobs = runs whose lab-metadata experiment.spec_id is one of ours. Each
# job links only to the capture dir it ran under (lab-metadata
# model_capture.capture_dir): a resumed batch has several, and linking a job
# to a capture that never saw its calls would replace its link with an empty one.
if [ "$DRY" = "1" ]; then
  echo "+ uv run --no-sync evallab capture link <job's capture dir> runs/<each-batch-job>"
else
  uv run --no-sync python - "$batch_dir" <<'PYEOF'
import json, sys
from pathlib import Path
batch = Path(sys.argv[1])
want = set()
for f in (batch / "groups").glob("*.ids"):
    want.update(l.strip() for l in f.read_text().splitlines() if l.strip())
names, links = [], []
for lab in sorted(Path("runs").glob("cb-*/lab-metadata.json")):
    try:
        meta = json.loads(lab.read_text())
    except ValueError:
        continue
    if ((meta.get("experiment") or {}).get("spec_id")) in want:
        names.append(lab.parent.name)
        links.append(f"{lab.parent.name} {(meta.get('model_capture') or {}).get('capture_dir') or ''}")
(batch / "jobs.txt").write_text("".join(n + "\n" for n in names))
(batch / "job_captures.txt").write_text("".join(n + "\n" for n in links))
PYEOF
  while read -r job cap; do
    [ -n "$job" ] || continue
    if [ -z "$cap" ] || [ ! -d "$cap" ]; then
      echo "capture link: $job ran without a capture dir; not linked" >&2
      continue
    fi
    # Same layout as the runner's auto-link: the job's own derived/ dir.
    uv run --no-sync evallab capture link --derived-root "runs/$job/derived/parquet" \
      "$cap" "runs/$job" || true
  done < "$batch_dir/job_captures.txt"
fi

# 9. Postrun: detect, optional judge, analyze, spend, export.
# A waiting stop aborts here: cleanup (steps 7-8) already ran.
if [ -n "$WAITING_MSG" ]; then
  echo "$WAITING_MSG" >&2
  echo "batch $batch_id STOPPED before postrun; re-approval is a human decision" >&2
  exit 1
fi
if [ -n "$CB_JUDGE" ]; then
  run uv run --no-sync python "$EXP/postrun.py" --batch "$batch_id" --judge "$CB_JUDGE"
else
  run uv run --no-sync python "$EXP/postrun.py" --batch "$batch_id"
fi
echo "batch $batch_id done -> $batch_dir"
