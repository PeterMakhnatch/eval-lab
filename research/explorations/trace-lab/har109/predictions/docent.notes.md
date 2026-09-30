# Docent predictions (HAR-109 item 5) — notes

Collection: `HAR-104 Python exploration (private)` →
https://docent.transluce.org/dashboard/b418ff53-5244-4297-8157-4450dcb1f1d7
Analysis plan: `har109-har104-python-analysis`, plan
1edc1991-946e-48fb-a866-d563584fc52b in that collection (5 steps: markdown,
DQL list, per-run reading 5632eddc-2f00-416a-8dc1-40d294cec105, 2 DQL
aggregates, synthesis reading 7129310f-51f5-42c7-a0ed-2b9bf38dce8a).
Per-run outputs: `reading_results.output.{attribution, task_fair, approach,
first_failure_window, fairness_evidence, attribution_evidence}`; synthesis
text at `reading_results.output.output.text` of the synthesis result.

## Exact commands (cwd = har109-analysis-stack worktree, all prefixed `keys run --` for DOCENT_API_KEY)

1. Freeze check: `sha256sum research/explorations/trace-lab/docent/har109_scoring.md`
   → `07612c79…cb3d2`, matches HAR109_PLAN.md. No prompt/schema change.
2. Blind metadata precompute (project python has evallab; docent env does not):
   `uv run --project . python -c "…build_run_report + binding_ceiling + task_domain…"`
   → `/tmp/har104-metadata.json` (10 trials × {stop_reason, verdict, domain}).
3. Dry run: `uv run --no-project --python 3.12 --with docent==0.1.87 python
   research/explorations/trace-lab/docent/export_har104_upload.py
   <10 har104-d-* job dirs> --metadata-json /tmp/har104-metadata.json
   --dry-run --out /tmp/har104-payload.json`
   → `10 runs, 19 transcripts, 2409 messages, 0 skipped`; `secret scan: 0 hits`.
4. Upload (2026-09-30T04:37:57Z–04:38:02Z): same with `--no-dry-run`.
   Collaborators before AND after = owner only
   (`p.makhnatch@gmail.com`, admin, single entry). Private confirmed.
5. Analysis, exactly per HAR109_PLAN.md (04:38:28Z–04:39:07Z, auto-approve ON):
   `uv run --no-project --python 3.12 --with docent==0.1.87 python
   research/explorations/trace-lab/docent/har109_docent_analysis.py
   --collection b418ff53-5244-4297-8157-4450dcb1f1d7`
   → per-run reading completed 10/10, zero errors; aggregate 6 rows (all
   `attribution=model`); synthesis completed (5 modes, 4+2+2+1+1 = 10 runs).
6. Result fetch: DQL on `reading_results`/`reading_result_links`/`agent_runs`
   for output + `input_tokens`/`output_tokens`/`error`; `list_reading_plans`
   + `get_reading_plan` for the plan URL. Raw dumps in /tmp
   (har109-docent-perrun.json, har109-docent-synth-text.md) — not committed.

No fixes to `docent/` were needed; the plan script ran unmodified.

## Cost line

Model: `openai/gpt-5.6-luna` (hosted, `uses_byok=false`, 1.05M context).
Per-run reading: 1,173,953 in / 17,221 out. Synthesis: 16,897 in / 1,144 out.
Total: **1,190,850 in / 18,365 out, $0**. No price, free/paid flag, quota, or
billing signal exists anywhere (`get_reading_models` entries, plan steps,
`reading_results` columns = input/output tokens only, no cost column; zero
quota messages in SDK output) — same free-hosted conditions as 2026-09-29
(44 runs / 2.35M tokens free). Under the $2 budget either way.

## Mapping (readings → schema)

- `attribution`: all `model` (unanimous; matches the HAR-81 prior that judges
  blame the model on every failure — parent should sample, not trust).
- `task_verdict`: `task_fair=true` → `sound` for all 10. `broken` reserved for
  `task_fair=false` (never emitted); `suspect`/`too_hard`/`too_easy` have no
  reading notion and are never emitted.
- `pass_suspect`: `false` for the 4 passed runs (attribution=model means "fix
  earned it" per the prompt rubric + approach cites passing suites); `null`
  for the 6 failed runs (no passing reward exists to suspect).
- `upstream_fetch`: `true` only where the reading reports it — 000226
  (downloaded Waitress wheel), 000927 (downloaded Soup Sieve 1.9.1), 002407
  (repeated package-download/source-inspection); `null` elsewhere (absence of
  mention ≠ tool claim of absence).
- Everything else `null`: the tool gives first-failure *windows* (widths
  2–5, mean ~3.5), never exact steps, and the scoring map says deterministic
  rules outrank readings; loops/stop-reason/completion booleans have no
  reading notion (stop_reason input was blind Eval Lab metadata, not a
  finding). Synthesis modes (`passed_clean` 4, `false_completion_claim` 2,
  `unreproduced_before_edit` 2, `incomplete_integration` 1,
  `verification_stall_after_tool_failure` 1) are recorded here only — no
  schema field holds them.

## Tool gaps / rough edges (Eval Lab gap-list candidates)

- `export_har104_upload.py` takes a runs root but `find_harbor_trial_dirs`
  also matches non-HAR-104 job dirs (`har104-canned-gptoss`, smoke, aborted),
  which then fail `--metadata-json` lookup with `KeyError` (export_har104_upload.py:43).
  Workaround: pass the 10 job dirs explicitly. A `--trial-re` filter would help.
- `docent/fetch_results.py` hardcodes the HAR-81 collection/reading IDs; no
  generic fetch helper — result retrieval was ad-hoc DQL (fine, but reusable
  tooling would shorten this).
- `get_reading_models()` (1,219 entries) exposes no price/free flag/quota;
  `reading_results` has no cost column and `served_provider` came back None —
  spend is unverifiable from the SDK; only dashboard/quota-message absence.
- Synthesis output nests at `output.output.text` (double-wrapped); per-run
  outputs sit directly at `output`. Minor wrist-slap for parsers.
- 10 runs → 19 transcripts (some trials carry summarization/continuation
  documents); token spread is wide (29K–380K in per run), so per-run cost
  variance dominates any estimate.

## Blindness disclosure

Hand labels, hand .md pages, and score.py were never opened. To match sibling
row format (keys/order already fixed in predict_context.md) the first row of
`predictions/evallab.jsonl` was read; its values were not used — every
corresponding docent field is null per the mapping above, and all docent
values come solely from the Docent readings fetched before that.
