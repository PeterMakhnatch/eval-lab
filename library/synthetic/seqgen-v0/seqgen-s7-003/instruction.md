# Record Processing Pipeline

Transform the newline-delimited JSON records in `/app/data/orders.jsonl` into `/app/output/result.jsonl`.

## Required transformations

1. Project each record to retain only the fields: `id`, `status`, `amount`.
2. Keep only records where `amount` is at least 476.
3. Group records by `status` and compute the sum of `amount` as `total_amount`. The resulting records must have fields `status` and `total_amount`, sorted ascending by `status`.
4. Keep the 3 records with the largest `total_amount` values (stable ties).

## Output requirements

The output file `/app/output/result.jsonl` must contain one JSON object per line with keys sorted alphabetically in compact format (`sep=(',', ':')`) with a trailing newline.

- Input path: `/app/data/orders.jsonl`
- Output path: `/app/output/result.jsonl`
- Do not modify files under `/app/data`.
- Do not create any other file under `/app/output`.

The helper utility `/app/bin/rp` is provided in the environment (run `/app/bin/rp --help` for details), but any implementation approach is permitted.
