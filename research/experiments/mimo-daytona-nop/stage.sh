#!/bin/bash
# HAR-88 cloud nop qualification: pull the cohort tasks and submit the specs.
#
# Run from the checkout that will dispatch: spec `task` paths are repo-relative
# and the queue Executor resolves them against the invoking checkout root
# (`Executor._safe_repo_path`). Nothing is approved here: Daytona is a
# non-Docker environment, so every spec waits for `evallab approve`.
#
#   research/experiments/mimo-daytona-nop/stage.sh pull           # pinned tasks
#   research/experiments/mimo-daytona-nop/stage.sh submit [a|b]   # queue specs
#   research/experiments/mimo-daytona-nop/stage.sh all            # pull + submit a b
#
# Batches follow the Daytona org quota (10 vCPU / 10 GiB / 30 GiB disk):
#   a = terminal (64) + music (1): 1 vCPU / 2 GiB / <=10 GiB each -> tick --parallel 3
#   b = code (32) + cyber (16):    2 vCPU / 8 GiB each            -> tick --parallel 1
# Submitted spec IDs go to derived/har88/staged-<batch>.ids for the approve loop.

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
MODE="${1:-all}"

pull() {
  uv run --with huggingface_hub python - "$ROOT" <<'EOF'
import json, sys
from pathlib import Path
from huggingface_hub import snapshot_download
root = Path(sys.argv[1])
cohort = json.loads((root / "research/experiments/mimo-daytona-nop/cohort.json").read_text())
pins = cohort["pins"]
by_domain = {}
for row in cohort["cohort"]:
    by_domain.setdefault(row["domain"], []).append(row)
for domain, rows in sorted(by_domain.items()):
    repo_id, rev = pins[domain].split("@")
    dest = root / Path(rows[0]["task"]).parent.parent
    patterns = [f"tasks/{r['task_id']}/**" for r in rows] + ["manifest.json", "registry.json"]
    snapshot_download(repo_id=repo_id, repo_type="dataset", revision=rev,
                      local_dir=str(dest), allow_patterns=patterns, max_workers=8)
    for r in rows:
        assert (root / r["task"] / "task.toml").is_file(), f"missing {r['task']}"
    print(f"ok {domain}: {len(rows)} tasks")
print("pull complete (package digests are verified again at dispatch)")
EOF
}

submit() {
  batch="$1"
  case "$batch" in
    a) globs="mimo-qual-t-*.json mimo-qual-m-*.json"; parallel=3 ;;
    b) globs="mimo-qual-c-*.json mimo-qual-y-*.json"; parallel=1 ;;
    *) echo "unknown batch $batch" >&2; exit 2 ;;
  esac
  cd "$ROOT"
  mkdir -p derived/har88
  ids="derived/har88/staged-$batch.ids"
  : > "$ids"
  for glob in $globs; do
    for spec in research/experiments/mimo-daytona-nop/specs/$glob; do
      line="$(uv run evallab submit "$spec" | grep '^spec_id:')"
      echo "${line#spec_id: }" >> "$ids"
    done
  done
  echo "batch $batch: $(wc -l < "$ids" | tr -d ' ') specs waiting -> $ids"
  echo "approve + run:"
  echo "  for id in \$(cat $ids); do uv run evallab approve \"\$id\" --actor peter; done"
  echo "  uv run evallab tick --parallel $parallel"
}

case "$MODE" in
  pull) pull ;;
  submit) shift; [ "$#" -gt 0 ] || set -- a b; for b in "$@"; do submit "$b"; done ;;
  all) pull; submit a; submit b ;;
  *) echo "usage: stage.sh [pull|submit [a|b]|all]" >&2; exit 2 ;;
esac
