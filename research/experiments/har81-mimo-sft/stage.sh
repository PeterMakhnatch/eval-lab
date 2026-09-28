#!/bin/bash
# HAR-81 staged paid pilot: prepare + submit Terminus-2 specs (NOTHING approved).
#
#   research/experiments/har81-mimo-sft/stage.sh cohort    # (re)write pilot-cohort.json from split.json ($0)
#   research/experiments/har81-mimo-sft/stage.sh prepare   # freeze specs under derived/prepared/ ($0)
#   research/experiments/har81-mimo-sft/stage.sh submit    # queue them; every spec waits for approval
#
# Run from the checkout that will dispatch (spec task paths are repo-relative
# snapshots under runs/.prepared-tasks). Tasks come from the HAR-82 pinned
# store; the queue re-verifies every package digest at dispatch.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
EXP="$ROOT/research/experiments/har81-mimo-sft"
STORE="${EVALLAB_TASK_STORE:-$(cd "$ROOT" && git rev-parse --path-format=absolute --git-common-dir | xargs dirname)/derived/task-store/hf}"
IDS="$ROOT/derived/har81/pilot.ids"

cohort() {
  cd "$ROOT"
  mkdir -p derived/har81
  uv run --no-sync evallab tasks catalog export-broken --backend daytona --out derived/har81/broken-daytona.json >/dev/null
  uv run --no-sync python - "$EXP" derived/har81/broken-daytona.json <<'EOF'
import hashlib, json, sys
from pathlib import Path
exp, broken_path = Path(sys.argv[1]), Path(sys.argv[2])
split = json.loads((exp / "split.json").read_text())
broken = json.loads(broken_path.read_text())
excluded = {item["task_version_digest"] for item in broken["items"]}
rank = lambda tid: hashlib.sha256(f"har81-pilot\0{tid}".encode()).hexdigest()
train = [t for t in split["tasks"] if t["split"] == "train" and t["task_version_digest"] not in excluded]
pick = []
for domain, n in (("terminal", 6), ("cyber", 10)):
    seen = set()
    for task in sorted((t for t in train if t["domain"] == domain), key=lambda t: rank(t["task_id"])):
        if task["split_group"] in seen:
            continue
        seen.add(task["split_group"])
        pick.append({k: task[k] for k in ("domain", "task_id", "split_group", "task_version_digest")})
        if len(seen) == n:
            break
out = {"schema": "har81.pilot_cohort/v1", "split_manifest_digest": split["manifest_digest"],
       "broken_export_sha256": broken["sha256"], "excluded_task_version_digests": sorted(excluded),
       "rule": "train split minus `catalog export-broken --backend daytona`; per domain rank "
               "sha256('har81-pilot\\0'+task_id); one task per split_group; terminal 6, cyber 10",
       "tasks": pick}
(exp / "pilot-cohort.json").write_text(json.dumps(out, indent=2) + "\n")
print(f"pilot cohort: {len(pick)} tasks")
EOF
}

prepare() {
  cd "$ROOT"
  uv run --no-sync python - "$EXP" "$STORE" <<'EOF'
import json, subprocess, sys
from pathlib import Path
exp, store = Path(sys.argv[1]), Path(sys.argv[2])
cohort = json.loads((exp / "pilot-cohort.json").read_text())
split = json.loads((exp / "split.json").read_text())
assert cohort["split_manifest_digest"] == split["manifest_digest"], "pilot cohort was built on another split"
sources = split["sources"]
arms = json.loads((exp / "arms.json").read_text())
for task in cohort["tasks"]:
    repo, rev = sources[task["domain"]].split("@")
    snap = store / f"{repo.replace('/', '__')}@{rev[:12]}" / "tasks" / task["task_id"]
    domain = arms["domains"][task["domain"]]
    for arm in arms["pilot_arms"]:
        name = f"har81-p-{arm['id']}-{task['task_id'].replace('_', '-')[:40]}".lower()
        out = f"derived/prepared/{name}.json"
        cmd = ["uv", "run", "--no-sync", "evallab", "tasks", "prepare", str(snap), "--name", name,
               "--agent", "terminus-2", "--model", arm["model"], "--environment", "daytona",
               "--harness-tree", str(exp / arm["harness"]),
               "--cost-limit-usd", str(arm["cost_limit_usd"]),
               "--max-input-tokens", str(arm["max_input_tokens"]),
               "--estimated-cost-usd", str(round(arm["cost_limit_usd"] + domain["sandbox_worst_usd"], 2)),
               "--output", out, "--json"]
        subprocess.run(cmd, check=True, capture_output=True)
        if domain.get("override_storage_mb"):
            spec = json.loads(Path(out).read_text())
            spec["override_storage_mb"] = domain["override_storage_mb"]
            Path(out).write_text(json.dumps(spec, indent=2) + "\n")
        print(out)
EOF
}

submit() {
  cd "$ROOT"
  mkdir -p "$(dirname "$IDS")"
  : > "$IDS"
  for spec in derived/prepared/har81-p-*.json; do
    line="$(uv run --no-sync evallab submit "$spec" | grep '^spec_id:')"
    echo "${line#spec_id: }" >> "$IDS"
  done
  echo "$(wc -l < "$IDS" | tr -d ' ') specs waiting -> $IDS"
  echo "approve + run (Daytona Tier 1 quota: terminal 3 / cyber 1 at a time):"
  echo "  for id in \$(cat $IDS); do uv run evallab approve \"\$id\" --actor peter; done"
  echo "  uv run evallab tick --parallel 1"
}

case "${1:-}" in
  cohort) cohort ;;
  prepare) prepare ;;
  submit) submit ;;
  *) echo "usage: stage.sh cohort|prepare|submit" >&2; exit 2 ;;
esac
