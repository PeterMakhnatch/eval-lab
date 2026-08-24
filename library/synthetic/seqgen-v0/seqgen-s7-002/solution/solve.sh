#!/bin/sh
set -eu

INPUT="/app/data/orders.jsonl"
OUTPUT="/app/output/result.jsonl"
mkdir -p /app/output

/app/bin/rp sort-by --in "$INPUT" --out /tmp/step_0.jsonl --field region --order asc
/app/bin/rp filter-ge --in /tmp/step_0.jsonl --out /tmp/step_1.jsonl --field amount --value 444
/app/bin/rp dedupe-by --in /tmp/step_1.jsonl --out /tmp/step_2.jsonl --field status
/app/bin/rp select --in /tmp/step_2.jsonl --out /tmp/step_3.jsonl --fields id status amount
/app/bin/rp sort-by --in /tmp/step_3.jsonl --out /tmp/step_4.jsonl --field id --order asc
/app/bin/rp sort-by --in /tmp/step_4.jsonl --out /tmp/step_5.jsonl --field status --order desc
/app/bin/rp write --in /tmp/step_5.jsonl --out "$OUTPUT"
