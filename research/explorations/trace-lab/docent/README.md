# Docent export for Harbor trials

Docent's own Harbor converter (`docent==0.1.87`,
`convert_harbor_trial_to_agent_run`) accepts exactly one ATIF file per trial
and rejects `continued_trajectory_ref` / `subagent_trajectory_ref`. On HAR-90
it rejected 4 of 10 trials (0036-f, 0758-b, 0758-c, 0758-d) — precisely the
long runs that hit context summarization. An analysis built on it would
silently study only the short runs.

`export_harbor.py` emits one Docent AgentRun per trial instead:

| continuation pattern | handling |
| --- | --- |
| duplicate (0036-f cont-1) | dropped; not double counted |
| cumulative superset (0758-c cont-31) | replaces the shorter head |
| new session (0758-d cont-1) | separate transcript, in order |
| head missing (0758-b) | starts from the first continuation |
| `summarization-*` files | separate `summarization/...` transcripts |

Rejected reference fields are removed from an in-memory copy only and kept in
transcript metadata (`trace_lab_removed_refs`). Raw trial files are never
modified. Run metadata gains `metadata.trace_lab` (trial, task, reward,
exception, `n_episodes` vs mainline agent steps, assembly pattern, per-file
sha256, tokens) and, with `--tags`, the probe-03 fields (`first_failure`,
`outcome_relevant_failure`, stop reason, treatment key, grader note).
The stock converter leaves the task name null in metadata; `trace_lab.task`
fills it from probe-03.

```sh
# dry run (default): writes the payload locally, uploads nothing
uv run --no-project --python 3.12 --with docent==0.1.87 python export_harbor.py \
  <runs>/har90-mimo-* --collection "HAR-90 MiMo" \
  --tags ../probe-03-capabilities/har90/capabilities.jsonl --out /tmp/har90-payload.json

# upload (private to the key's owner): needs DOCENT_API_KEY from the keys store
keys run -- uv run --no-project --python 3.12 --with docent==0.1.87 python export_harbor.py \
  <runs>/har90-mimo-* --collection "HAR-90 MiMo" --tags ... --no-dry-run
```

Every string is secret-scanned before writing or uploading; any match aborts
with exit 3. Verified 2026-09-29 dry run on HAR-90: 10 runs, 17 transcripts,
7,896 messages, 0 skipped, 0 secret hits. Not uploaded.

Queries then filter on `metadata_json->'trace_lab'->>'assembly_pattern'`,
`...->'outcome_relevant_failure'->>'tag'`, and `...->>'treatment_key'`. Compare
pass/fail pairs only within one treatment key (the Transluce contrastive
plan pattern), never across keys.
