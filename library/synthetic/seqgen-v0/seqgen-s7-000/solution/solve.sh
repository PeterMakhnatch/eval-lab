#!/bin/sh
set -eu

INPUT="/app/data/orders.jsonl"
OUTPUT="/app/output/result.jsonl"
mkdir -p /app/output

/app/bin/rp head --in "$INPUT" --out /tmp/step_0.jsonl -n 3
/app/bin/rp select --in /tmp/step_0.jsonl --out /tmp/step_1.jsonl --fields id region
/app/bin/rp dedupe-by --in /tmp/step_1.jsonl --out /tmp/step_2.jsonl --field region
/app/bin/rp filter-eq --in /tmp/step_2.jsonl --out /tmp/step_3.jsonl --field region --value west
/app/bin/rp sort-by --in /tmp/step_3.jsonl --out /tmp/step_4.jsonl --field id --order desc
/app/bin/rp sort-by --in /tmp/step_4.jsonl --out /tmp/step_5.jsonl --field id --order asc
/app/bin/rp write --in /tmp/step_5.jsonl --out "$OUTPUT"
