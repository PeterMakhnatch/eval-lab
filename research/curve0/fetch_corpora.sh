#!/usr/bin/env bash
# Curve 0 - fetch pinned public trace corpora. Read-only; no model calls.
#
# Corpora are NOT committed to the repo (size + no need to redistribute).
# Reproducibility comes from the pins below plus the per-file sha256 digests
# recorded in research/curve0/results/kstar_validation.json.
#
# Usage: bash research/curve0/fetch_corpora.sh
set -euo pipefail

CACHE="$(dirname "$0")/.cache"
SWEBENCH_SHA="1faa91cade0562ba62b66c1c99e71f7b72d96f13"   # SWE-bench/experiments
TAUBENCH_SHA="59a200c6d575d595120f1cb70fea53cef0632f6b"   # sierra-research/tau-bench
S3="https://swe-bench-submissions.s3.amazonaws.com/verified/20240402_sweagent_gpt4/trajs"

mkdir -p "$CACHE/swebench/trajs" "$CACHE/taubench"

# --- SWE-bench Verified, SWE-agent gpt-4 submission (MIT) --------------------
curl -sSL --max-time 60 -o "$CACHE/swebench/results.json" \
  "https://raw.githubusercontent.com/SWE-bench/experiments/${SWEBENCH_SHA}/evaluation/verified/20240402_sweagent_gpt4/results/results.json"

# Deterministic sample: sorted(resolved)[:30] + sorted(generated - resolved)[:30]
python3 - "$CACHE/swebench" <<'PY'
import json, sys, pathlib
d = pathlib.Path(sys.argv[1])
r = json.loads((d / "results.json").read_text())
resolved = sorted(r["resolved"])
unresolved = sorted(set(r["generated"]) - set(r["resolved"]))
ids = resolved[:30] + unresolved[:30]
(d / "sample_ids.txt").write_text("\n".join(ids) + "\n")
(d / "sample_manifest.json").write_text(json.dumps({
    "selection": "deterministic: sorted(resolved)[:30] + sorted(generated-resolved)[:30]",
    "resolved": resolved[:30], "unresolved": unresolved[:30],
}, indent=1) + "\n")
print(f"selected {len(ids)} instances")
PY

while read -r id; do
  [ -s "$CACHE/swebench/trajs/$id.traj" ] && continue
  curl -sSf --max-time 45 -o "$CACHE/swebench/trajs/$id.traj" "$S3/$id.traj" \
    || { echo "MISS $id"; rm -f "$CACHE/swebench/trajs/$id.traj"; }
done < "$CACHE/swebench/sample_ids.txt"

# --- tau-bench historical trajectories (MIT) --------------------------------
curl -sSL --max-time 180 -o "$CACHE/taubench/gpt-4o-airline.json" \
  "https://raw.githubusercontent.com/sierra-research/tau-bench/${TAUBENCH_SHA}/historical_trajectories/gpt-4o-airline.json"

echo "== digests =="
shasum -a 256 "$CACHE/swebench/results.json" "$CACHE/taubench/gpt-4o-airline.json"
echo "trajs fetched: $(ls -1 "$CACHE/swebench/trajs" | wc -l)"

# Expected digests at the pins above (verified 2026-08-27):
#   results.json          26971f1a55d711fd7c5f585443ce6945ff612f7ced94b3bca70268d2d030f65a
#   gpt-4o-airline.json   e9e6c0297660c537f83d4fd9c476ce7a9a86ecd2784874b7bfc13be598e37bfa
