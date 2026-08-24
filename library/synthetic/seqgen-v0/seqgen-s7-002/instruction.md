# Record Processing Pipeline

Transform the newline-delimited JSON records in `/app/data/orders.jsonl` into `/app/output/result.jsonl`.

## Required transformations

1. Keep only records where `amount` is at least 54.
2. Sort records by `day` in ascending order (stable sort).
3. Project each record to retain only the fields: `region`, `day`.
4. Keep only records where `day` is at least 27 and `region` is `east`.

## Output requirements

The output file `/app/output/result.jsonl` must contain one JSON object per line with keys sorted alphabetically in compact format (`sep=(',', ':')`) with a trailing newline.

- Input path: `/app/data/orders.jsonl`
- Output path: `/app/output/result.jsonl`
- Do not modify files under `/app/data`.
- Do not create any other file under `/app/output`.

The helper utility `/app/bin/rp` is provided in the environment (run `/app/bin/rp --help` for details), but any implementation approach is permitted.
