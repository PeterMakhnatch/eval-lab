#!/bin/sh
set -eu

INPUT="/app/data/orders.jsonl"
OUTPUT="/app/output/result.jsonl"
mkdir -p /app/output

/app/bin/rp filter-ge --in "$INPUT" --out /tmp/step_0.jsonl --field amount --value 54
/app/bin/rp sort-by --in /tmp/step_0.jsonl --out /tmp/step_1.jsonl --field day --order asc
/app/bin/rp select --in /tmp/step_1.jsonl --out /tmp/step_2.jsonl --fields region day
/app/bin/rp filter-ge --in /tmp/step_2.jsonl --out /tmp/step_3.jsonl --field day --value 27
/app/bin/rp filter-eq --in /tmp/step_3.jsonl --out /tmp/step_4.jsonl --field region --value east
/app/bin/rp write --in /tmp/step_4.jsonl --out "$OUTPUT"
