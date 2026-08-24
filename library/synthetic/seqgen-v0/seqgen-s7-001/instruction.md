# Record Processing Pipeline

Transform the newline-delimited JSON records in `/app/data/orders.jsonl` into `/app/output/result.jsonl`.

## Required transformations

1. Keep only records where `amount` is at least 111 and `day` is at least 25.
2. Group records by `status` and compute the sum of `amount` as `total_amount`. The resulting records must have fields `status` and `total_amount`, sorted ascending by `status`.
3. Sort records by `total_amount` in descending order (stable sort).
4. Sort records by `status` in descending order (stable sort).
5. Keep only records where `status` is `returned`.

## Output requirements

The output file `/app/output/result.jsonl` must contain one JSON object per line with keys sorted alphabetically in compact format (`sep=(',', ':')`) with a trailing newline.

- Input path: `/app/data/orders.jsonl`
- Output path: `/app/output/result.jsonl`
- Do not modify files under `/app/data`.
- Do not create any other file under `/app/output`.

The helper utility `/app/bin/rp` is provided in the environment (run `/app/bin/rp --help` for details), but any implementation approach is permitted.
