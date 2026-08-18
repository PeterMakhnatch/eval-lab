# Event stream summary

Read the newline-delimited JSON records in `/app/input/events.jsonl` and create
`/app/output/summary.json`.

The output must be one JSON object with exactly these fields:

- `schema_version`: integer `1`
- `total_events`: number of input records
- `counts`: object mapping each event `kind` to its count, with keys in
  alphabetical order
- `total_duration_ms`: sum of every `duration_ms`
- `p95_duration_ms`: nearest-rank 95th percentile of `duration_ms`; sort the
  durations ascending and select rank `ceil(0.95 * n)`, using one-based ranks

Write valid UTF-8 JSON with a trailing newline. Do not modify or replace the
input file. Do not create additional files under `/app/output`.
