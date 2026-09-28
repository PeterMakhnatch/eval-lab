#!/bin/bash
# HAR-83 hack-probe staging (redteam-v1).
#
# Reproducible sequence Peter runs AFTER merge, from the checkout dispatch
# will run from (the primary checkout: spec `task` paths are repo-relative
# `derived/task-store/...` and the queue Executor resolves them against the
# invoking checkout root — see `Executor._safe_repo_path`, queue.py:2569).
#
#   research/experiments/mimo-hack-probe/stage.sh pull    # snapshots + digest verify
#   research/experiments/mimo-hack-probe/stage.sh submit  # queue + approve loop
#   research/experiments/mimo-hack-probe/stage.sh all     # pull, then submit
#
# `TASKS="id1 id2"` limits pull/submit to a subset (e.g. a $0 nop proof).
# Pulls only the 32 cohort tasks (allow_patterns tasks/<id>/**), not the full
# datasets. Anonymous HF, pinned revisions. Nothing is approved here.
set -eu

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
COHORT="$ROOT/research/experiments/mimo-hack-probe/cohort.json"
SPECS="$ROOT/research/experiments/mimo-hack-probe/specs"
MODE="${1:-all}"

pull() {
  uv run --with huggingface_hub python - "$ROOT" --tasks "${TASKS:-}" <<'EOF'
import json, sys
from pathlib import Path
root = Path(sys.argv[1])
only = {t for t in sys.argv[sys.argv.index("--tasks") + 1].split() if t} or None
from huggingface_hub import snapshot_download
PINS = {
    "terminal": ("FineEnvs/MiMo-V2.6-RL-harbor-terminal", "fe1c2b665aae1ba7a09a270d979724d32269ae6a"),
    "code": ("FineEnvs/MiMo-V2.6-RL-harbor-code", "5746e2f0c5c61af12d7c5bf15d7efdd77d1f0785"),
}
selection = json.loads((root / "research/experiments/mimo-hack-probe/selection.json").read_text())
domains = {d: selection[d] for d in ("terminal", "code")}
for domain, rows in sorted(domains.items()):
    if only:
        rows = [r for r in rows if r["task_id"] in only]
    if not rows:
        continue
    repo_id, rev = PINS[domain]
    dest = root / "derived/task-store/hf" / f"{repo_id.replace('/', '__')}@{rev[:12]}"
    patterns = [f"tasks/{r['task_id']}/**" for r in rows] + ["manifest.json", "registry.json"]
    snapshot_download(repo_id=repo_id, repo_type="dataset", revision=rev,
                      local_dir=str(dest), allow_patterns=patterns, max_workers=8)
    for r in rows:
        task_dir = dest / "tasks" / r["task_id"]
        assert (task_dir / "task.toml").is_file(), f"missing {task_dir}"
        print(f"ok {domain}/{r['task_id']}")
print("pull complete (digests pinned at finalize + verified at dispatch)")
EOF
}
submit() {
  cd "$ROOT"
  IDS=""
  for spec in research/experiments/mimo-hack-probe/specs/mimo-hack-*.json; do
    base="$(basename "$spec" .json)"
    if [ -n "${TASKS:-}" ]; then
      match=0
      for t in $TASKS; do
        case "$base" in *"$(echo "$t" | tr '_' '-')") match=1;; esac
      done
      [ "$match" = 1 ] || continue
    fi
    line="$(uv run evallab submit "$spec" | grep '^spec_id:')"
    IDS="$IDS ${line#spec_id: }"
  done
  # shellcheck disable=SC2086
  echo "for id in$IDS; do"
  echo '  uv run evallab approve "$id" --actor peter'
  echo "done"
}
case "$MODE" in
  pull) pull ;;
  submit) submit ;;
  all) pull; submit ;;
  *) echo "usage: stage.sh [pull|submit|all]" >&2; exit 2 ;;
esac
