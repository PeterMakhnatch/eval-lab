#!/bin/bash
# Rebuild data/transcripts_raw with exactly the 54 manifest trials.
# Usage: ./import_raw54.sh  (answers the `1 = add` import prompt per trial)
cd "$(dirname "$0")" || exit 1
DATA=~/Developer/eval-lab/derived/trace-lab/scout/data
rm -rf "$DATA/transcripts_raw"
mkdir -p "$DATA/transcripts_raw"
UV="uv run --no-project --python 3.12 --with inspect-scout==0.5.3 --with harbor==0.21.0"
n=0
uv run --no-project --python 3.12 python raw_trials.py | tail -n +2 | while IFS= read -r trialdir; do
  echo "1" | $UV scout import atif -T "$DATA/transcripts_raw" -P path="$trialdir" >/dev/null 2>&1
  n=$((n + 1))
  if [ $((n % 10)) -eq 0 ]; then echo "progress: $n"; fi
done
echo done
