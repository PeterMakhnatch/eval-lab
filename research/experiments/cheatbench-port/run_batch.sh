#!/usr/bin/env bash
# CheatBench-port batch runner: readiness gate -> submit/approve -> model-call
# capture -> per-family tick with file-access capture -> live monitor ->
# capture link -> postrun. Replaces dispatch.sh (deleted).
#
# Usage: research/experiments/cheatbench-port/run_batch.sh [--dry-run] <specs-dir> "<actor + approval quote>" [parallel]
# Run from the worktree root. Paid: only use with a budget Peter granted for
# this experiment. --dry-run prints every command without executing it.
#
# Env knobs: CB_JUDGE (judge route for readiness + postrun; default EMPTY =
#   no judging — Z.ai balance is empty). CB_NO_PROBE=1 skips the paid
#   1-token provider probe (on by default: run_batch is already a paid
#   action, so an empty provider balance aborts BEFORE dispatch).
set -euo pipefail

DRY=0
if [ "${1:-}" = "--dry-run" ]; then
  DRY=1
  shift
fi
spec_dir=${1:?usage: run_batch.sh [--dry-run] <specs-dir> "<actor + approval quote>" [parallel]}
export CB_ACTOR=${2:?usage: run_batch.sh [--dry-run] <specs-dir> "<actor + approval quote>" [parallel]}
parallel=${3:-2}
CB_JUDGE=${CB_JUDGE:-}
EXP=research/experiments/cheatbench-port
TINKER_UP=https://tinker.thinkingmachines.dev
ZAI_UP=https://api.z.ai
if [ -z "${CB_NO_PROBE:-}" ]; then
  PROBE_ARGS="--probe-providers"
  READINESS_KEYS="keys run --"
else
  PROBE_ARGS=""
  READINESS_KEYS=""
fi

batch_date=$(date -u +%F)
slug=$(basename "$spec_dir" | tr -c '[:alnum:]-' '-' | tr -s '-')
slug=${slug%-}; slug=${slug#-}
slug=$(printf '%.40s' "$slug")
batch_id="$batch_date-$slug"
batch_dir="runs/_batches/$batch_id"
run() {
  echo "+ $*"
  if [ "$DRY" = "0" ]; then
    "$@"
  fi
}

echo "batch: $batch_id"
run mkdir -p "$batch_dir/captures" "$batch_dir/groups" "$batch_dir/monitor"

# 1. Group specs by (family, provider) for per-family ticks.
echo "grouping specs in $spec_dir ..."
if [ "$DRY" = "0" ]; then
  uv run --no-sync python - "$spec_dir" "$batch_dir" <<'PYEOF'
import json, sys
from pathlib import Path
spec_dir, batch = Path(sys.argv[1]), Path(sys.argv[2])
groups = {}
models, fams, names = set(), set(), []
for spec in sorted(spec_dir.glob("*.json")):
    s = json.loads(spec.read_text())
    model = s["model"]
    task = s.get("task", "")
    fam = next((f for f in ("subset_sum", "prime_factorization", "openmath_agent") if f in task), None)
    if fam is None:
        raise SystemExit(f"{spec}: cannot tell family from task {task!r}")
    prov = model.split("/", 1)[0]
    if prov not in ("zai", "tinker"):
        raise SystemExit(f"{spec}: unknown provider in model {model!r}")
    groups.setdefault((fam, prov), []).append((spec.name, s.get("spec_id") or s.get("name", spec.stem)))
    models.add(model); fams.add(fam); names.append(s.get("name", spec.stem))
(batch / "spec_files.txt").write_text("\n".join(sorted(p.name for p in spec_dir.glob("*.json"))) + "\n")
for (fam, prov), rows in groups.items():
    (batch / "groups" / f"{fam}.{prov}.txt").write_text("\n".join(n for n, _ in rows) + "\n")
(batch / "models.txt").write_text("\n".join(sorted(models)) + "\n")
(batch / "families.txt").write_text("\n".join(sorted(fams)) + "\n")
(batch / "job_names.txt").write_text("\n".join(sorted(names)) + "\n")
print(f"{len(groups)} groups, {sum(len(r) for r in groups.values())} specs")
PYEOF
else
  echo "+ uv run --no-sync python - $spec_dir $batch_dir <<'PYEOF' (spec grouping)"
fi
FAMS=""; MODELS=""
[ -f "$batch_dir/families.txt" ] && FAMS=$(cat "$batch_dir/families.txt")
[ -f "$batch_dir/models.txt" ] && MODELS=$(tr '\n' ' ' < "$batch_dir/models.txt")
if [ "$DRY" = "1" ]; then
  # Dry-run reads nothing but the spec files (free, local) for faithful echoes.
  FAMS=$(grep -ho 'cheatbench/[a-z_]*' "$spec_dir"/*.json 2>/dev/null | cut -d/ -f2 | sort -u | tr '\n' ' ')
  MODELS=$(grep -ho '"model": *"[^"]*"' "$spec_dir"/*.json 2>/dev/null | cut -d'"' -f4 | sort -u | tr '\n' ' ')
fi
[ -n "$FAMS" ] || FAMS="subset_sum prime_factorization openmath_agent"
PROVS=""
case "$MODELS" in *zai/*) PROVS="$PROVS zai";; esac
case "$MODELS" in *tinker/*) PROVS="$PROVS tinker";; esac

# 2. Readiness gate (aborts the batch on any fail). Probes providers by
# default (paid 1-token calls; run_batch is already paid); CB_JUDGE empty
# means no judge-fitness check and no judging downstream.
readiness_out="$batch_dir/readiness-receipt.json"
JUDGE_ARGS=""
if [ -n "$CB_JUDGE" ]; then JUDGE_ARGS="--judge $CB_JUDGE"; fi
if [ "$DRY" = "0" ]; then
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
if [ "$DRY" = "0" ]; then
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
  out="$batch_dir/captures/$prov"
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
  if [ "$prov" = "tinker" ]; then export EVALLAB_TINKER_UPSTREAM="$ENDPOINT"
  else export EVALLAB_ZAI_OPENAPI_UPSTREAM="$ENDPOINT"; fi
  export EVALLAB_MODEL_CAPTURE=1
  eval "export EVALLAB_MODEL_CAPTURE_DIR_$prov=\"$out\""
}
for prov in $PROVS; do
  if [ "$prov" = "tinker" ]; then start_capture tinker "$TINKER_UP"; else start_capture zai "$ZAI_UP"; fi
done

# 5. Live cheat/infra monitor for the whole batch (background).
MON_PID=""
if [ -f "$EXP/monitor.py" ]; then
  if [ "$DRY" = "1" ]; then
    echo "+ uv run --no-sync python $EXP/monitor.py --runs-dir runs --out $batch_dir/monitor --stop-on-infra --jobs-file $batch_dir/job_names.txt &"
  else
    uv run --no-sync python "$EXP/monitor.py" --runs-dir runs --out "$batch_dir/monitor" --stop-on-infra --jobs-file "$batch_dir/job_names.txt" &
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
tick_group() { # <family> <provider> <ids-file>
  fam=$1 prov=$2 ids=$3
  # shellcheck disable=SC2086
  eval "$(PYTHONPATH="$EXP" uv run --no-sync python -c "import capture_config, shlex; print(' '.join(f'export {k}={shlex.quote(v)}' for k, v in capture_config.file_access_env(\"$fam\").items()))")"
  if [ "$prov" = "tinker" ]; then export EVALLAB_MODEL_CAPTURE_DIR="$EVALLAB_MODEL_CAPTURE_DIR_tinker"
  else export EVALLAB_MODEL_CAPTURE_DIR="$EVALLAB_MODEL_CAPTURE_DIR_zai"; fi
  tick_args=""
  while IFS= read -r s; do
    [ -n "$s" ] && tick_args="$tick_args --spec-id $s"
  done < "$ids"
  round=0
  while [ "$round" -lt 20 ]; do
    round=$((round + 1))
    if [ -f queue/STOP ]; then
      echo "queue/STOP present; stop ticking $fam/$prov, continue to cleanup" >&2
      break
    fi
    # shellcheck disable=SC2086
    keys run -- uv run --no-sync evallab tick $tick_args --parallel "$parallel" --no-smoke-gate 2>/dev/null \
      | grep -E '^(dispatching|failed|completed|reused|dispatched|quarantined)' || true
    waiting=""; approved_left=""
    while IFS= read -r sid; do
      [ -n "$sid" ] || continue
      if ls queue/waiting/*-"$sid".json >/dev/null 2>&1; then
        waiting="$waiting $sid"
      elif ls queue/approved/*-"$sid".json >/dev/null 2>&1; then
        approved_left="$approved_left $sid"
      fi
    done < "$ids"
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
  echo "+ keys run -- uv run --no-sync evallab tick --spec-id <ids of group> --parallel $parallel --no-smoke-gate   # per family/provider group, re-tick while approved (max 20), stop on waiting/STOP"
else
  for ids in "$batch_dir"/groups/*.ids; do
    [ -e "$ids" ] || continue
    [ -n "$WAITING_MSG" ] && break
    base=$(basename "$ids" .ids)      # <family>.<provider>
    fam=${base%.*} prov=${base#*.}
    tick_group "$fam" "$prov" "$ids"
  done
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
# Batch jobs = runs whose lab-metadata experiment.spec_id is one of ours.
if [ "$DRY" = "1" ]; then
  echo "+ uv run --no-sync evallab capture link $batch_dir/captures/<prov> runs/<each-batch-job>"
else
  uv run --no-sync python - "$batch_dir" <<'PYEOF' > "$batch_dir/jobs.txt"
import json, sys
from pathlib import Path
batch = Path(sys.argv[1])
want = set()
for f in (batch / "groups").glob("*.ids"):
    want.update(l.strip() for l in f.read_text().splitlines() if l.strip())
for lab in sorted(Path("runs").glob("cb-*/lab-metadata.json")):
    try:
        meta = json.loads(lab.read_text())
    except ValueError:
        continue
    if ((meta.get("experiment") or {}).get("spec_id")) in want:
        print(lab.parent.name)
PYEOF
  for cap in "$batch_dir"/captures/*/; do
    [ -d "$cap" ] || continue
    while IFS= read -r job; do
      [ -n "$job" ] || continue
      uv run --no-sync evallab capture link "$cap" "runs/$job" || true
    done < "$batch_dir/jobs.txt"
  done
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
