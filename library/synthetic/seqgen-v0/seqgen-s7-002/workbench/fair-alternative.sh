#!/bin/sh
set -eu
ROOT="${SEQGEN_APP_ROOT:-/app}"
export SEQGEN_APP_ROOT="$ROOT"
mkdir -p "$ROOT/output"
python - <<'PY'
import json
import os
from pathlib import Path

root = Path(os.environ["SEQGEN_APP_ROOT"])
rows = [
    json.loads(line)
    for line in (root / "data/orders.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]
sequence = json.loads('[{"args":{"field":"region","order":"asc"},"op":"sort_by"},{"args":{"field":"amount","value":444},"op":"filter_ge"},{"args":{"field":"status"},"op":"dedupe_by"},{"args":{"fields":["id","status","amount"]},"op":"select"},{"args":{"field":"id","order":"asc"},"op":"sort_by"},{"args":{"field":"status","order":"desc"},"op":"sort_by"}]')
for step in sequence:
    op, args = step["op"], step["args"]
    if op == "filter_eq":
        rows = [dict(row) for row in rows if str(row.get(args["field"])) == str(args["value"])]
    elif op == "filter_ge":
        rows = [dict(row) for row in rows if int(row.get(args["field"], 0)) >= int(args["value"])]
    elif op == "select":
        rows = [{key: row[key] for key in args["fields"] if key in row} for row in rows]
    elif op == "sort_by":
        rows = sorted(
            [dict(row) for row in rows],
            key=lambda row: row.get(args["field"]),
            reverse=args.get("order", "asc").lower() == "desc",
        )
    elif op == "dedupe_by":
        seen, kept = set(), []
        for row in rows:
            value = row.get(args["field"])
            if value not in seen:
                seen.add(value)
                kept.append(dict(row))
        rows = kept
    elif op == "head":
        rows = [dict(row) for row in rows[: int(args["n"])]]
    elif op == "group_sum":
        sums = {}
        for row in rows:
            group = str(row.get(args["group_field"]))
            sums[group] = sums.get(group, 0) + int(row.get(args["value_field"], 0))
        total_key = f'total_{args["value_field"]}'
        rows = [
            {args["group_field"]: group, total_key: sums[group]}
            for group in sorted(sums)
        ]
    else:
        raise ValueError(f"unsupported operation: {op}")

output = root / "output/result.jsonl"
output.write_text(
    "".join(
        json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        for row in rows
    ),
    encoding="utf-8",
)
PY
