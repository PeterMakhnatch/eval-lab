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

## Recurring hosted-free own-run readings

`evallab.readers.docent_watch` applies the frozen TT-3 reward-hacking rubric with
`openai/gpt-5.4-mini` to attested own DEV actor runs. This is operational triage,
not a claim that TT-7 selected a universal reader. Goodfire and external datasets
are not admitted to this recurring model job. The read-only source projection
uses the private trace store's own-source and cross-key admission gate.

```sh
keys run -- uv run --with docent==0.1.88 python -m evallab.readers.docent_watch
scripts/ops/launchd/install-docent-watch.sh --load
```

The installer snapshots the complete committed repository and pinned SDK outside
every worktree. The ten-minute job uses `keys run --`; neither the plist nor its
logs contains credentials. At most one whole run is approved per tick, with one
active plan. Results append to the existing
`~/.local/state/evallab-prod-live/trace-tools/results-v1.jsonl`; spend/usage is
appended to `~/.local/state/evallab-failure-library/spend.jsonl`. The job's
`docent-watch-last.json` distinguishes blocked, quarantined, pending, collected,
and idle states.

Before submission and again before model approval, the service must report a
finite positive remaining **free** allowance and the exact native hosted model
must have `uses_byok=false`. BYOK, OpenRouter, unknown allowance, and exhausted
allowance never fall back to paid generation. This service remains closed if an
account change makes that same model a BYOK route.

Only literal actor-context segments enter the model request: no collection
references can render source, actor model, outcome, or label metadata. Every
message, tool call, observation, and reasoning field is preserved with original
step markers. The canonical recursive-JSON secret gate runs before transmission.
No input is truncated. An input above the conservative UTF-8-byte context bound
is quarantined, not called or classified as clean. That bound is not evidence of
an actual tokenizer overflow. No automatic schema or provider-refusal retries
are made.

Submission creates a manual-approval plan. A durable intent and deterministic
plan name permit reconciliation after a lost POST response without duplicating
model requests. Approval also records its intent before the POST. Ambiguous
submission/approval remains blocked rather than being retried blindly. New
admission seals, changed payloads, or failed privacy checks prevent approval of
an existing unapproved plan. Valid negative judgments, provider errors, and
invalid/nonexistent evidence steps remain distinct; the latter two abstain.

Verification includes an actual authenticated exhausted-allowance smoke with
zero approvals/results, plus regression scenarios for new DB arrivals, held
cross-key admission, payload blinding, lost submission/approval responses,
duplicate prevention, and output/citation boundaries. A successful future model
reading still depends on available hosted allowance; quota errors do not prove
model capability.
