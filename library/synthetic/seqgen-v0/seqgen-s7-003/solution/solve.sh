#!/bin/sh
set -eu

INPUT="/app/data/orders.jsonl"
OUTPUT="/app/output/result.jsonl"
mkdir -p /app/output

/app/bin/rp select --in "$INPUT" --out /tmp/step_0.jsonl --fields id status amount
/app/bin/rp filter-ge --in /tmp/step_0.jsonl --out /tmp/step_1.jsonl --field amount --value 476
/app/bin/rp group-sum --in /tmp/step_1.jsonl --out /tmp/step_2.jsonl --group-field status --value-field amount
/app/bin/rp sort-by --in /tmp/step_2.jsonl --out /tmp/step_3.jsonl --field total_amount --order desc
/app/bin/rp head --in /tmp/step_3.jsonl --out /tmp/step_4.jsonl -n 3
/app/bin/rp write --in /tmp/step_4.jsonl --out "$OUTPUT"
