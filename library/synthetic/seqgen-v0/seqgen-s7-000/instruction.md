# Record Processing Pipeline

Transform the newline-delimited JSON records in `/app/data/orders.jsonl` into `/app/output/result.jsonl`.

## Required transformations

1. Keep the first 3 records.
2. Project each record to retain only the fields: `id`, `region`.
3. Deduplicate records by `region`, retaining only the first record seen for each distinct value.
4. Keep only records where `region` is `west`.
5. Sort records by `id` in descending order (stable sort).
6. Sort records by `id` in ascending order (stable sort).

## Output requirements

The output file `/app/output/result.jsonl` must contain one JSON object per line with keys sorted alphabetically in compact format (`sep=(',', ':')`) with a trailing newline.

- Input path: `/app/data/orders.jsonl`
- Output path: `/app/output/result.jsonl`
- Do not modify files under `/app/data`.
- Do not create any other file under `/app/output`.

The helper utility `/app/bin/rp` is provided in the environment (run `/app/bin/rp --help` for details), but any implementation approach is permitted.
