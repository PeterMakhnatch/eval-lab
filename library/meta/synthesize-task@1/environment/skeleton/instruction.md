# {{TASK_TITLE}}

Read the records in `/app/input/data.json` and generate `/app/output/summary.json`.

The output must be a valid JSON object with the following fields:
- `schema_version`: integer `1`
- `total_records`: total count of records processed
- `status`: string `"ok"`

Write valid UTF-8 JSON with a trailing newline. Do not modify the input file.
