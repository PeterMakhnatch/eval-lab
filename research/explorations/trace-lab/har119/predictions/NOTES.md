# HAR-119 part 2 — tool prediction runs (NOTES)

## Clock / provenance

- Worktree: `~/Developer/eval-lab/.worktrees/har119-trace-review`, branch
  `traces/har-119-trace-review`. HEAD at run time: `71f86346`
  (`Repair 44 more census tasks ... (#580)`); the assignment named merged main
  `57dd7091` (#579), which is HEAD~1 here — the two census commits on top
  touch census/nop tasks, not trace tools. No tool file was modified; no
  commits/pushes/PRs (parent owns git).
- `MAPPING.md` written **2026-09-30T23:42:02Z**, before any of the runs below
  (mtime 19:42:51 EDT). Mapping frozen; nothing below retunes it.
- `evallab.jsonl` built 2026-09-30T23:46:38Z; `scout.jsonl` (v2)
  2026-09-30T23:54:34Z; Docent collection created 2026-09-30T23:56Z, upload
  verified 2026-09-30T23:59:34Z, reading run 2026-10-01T00:00:09–00:00:39Z
  (`docent.jsonl` mtime 00:00:39Z).
- Blindness: never read or globbed `har119/hand*`, `har119/labels*`, or any
  rater output. Only `selection.json`, `trial_dirs.txt` (unused),
  `RATER_GUIDE.md`, `loop_kind.py` (read-only, part-1 helper), task folders
  were not opened either (not needed for tool runs).

## Exact commands (cwd = worktree root unless noted)

Eval Lab (`uv sync` first; all rc=0):
- `uv run evallab report run --json <trial_dir> --output-dir research/explorations/trace-lab/har119/predictions/evallab/raw` × 12
  (one per selected trial dir from `selection.json`).
- `uv run evallab process-job --no-ingest --no-publish --json --output-dir /tmp/har119-pj/<job> <job_dir>` × 12
  (9 single-trial jobs + 3 gepa multi-trial jobs; selected trial's
  `trial-<name>.json` copied to `predictions/evallab/raw/<trial>.process_job.json`).
- `uv run python research/explorations/trace-lab/har119/tools/evallab_har119.py` → `predictions/evallab.jsonl`.
- `evallab report run --help` / `evallab process-job --help` checked for the
  #560 (`outcome.first_failure`) and #572 (per-trial decision page) surfaces.

Scout (project env so `evallab` imports resolve; all rc=0, 0 scan errors):
- `uv run --no-project --python 3.12 --with harbor==0.21.0 python research/explorations/trace-lab/probe-03-capabilities/capabilities.py <12 trial dirs> --out-dir predictions/scout_work/probe03 --evallab-src src`
  → `capabilities.jsonl` (12 rows) + `summary.md` + `reading_sheet.md`.
- `... harbor_normalize.py <12 trial dirs> --out predictions/scout_work/normalized --evallab-src src --check predictions/scout_work/probe03/capabilities.jsonl`
  → `12 trials | 1099 bash calls | 0 invalid/errors | 0 probe-03 mismatches`.
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 python research/explorations/trace-lab/scout/import_evallab.py <inputs> --db <db> --staging <staged>`
  twice: normalized set → `scout_work/scout/` (scan v1), raw trial dirs → `scout_work/scout_raw/` (scan v2, see below).
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 scout scan research/explorations/trace-lab/scout/scanners.py -T <transcripts> --scans <scans> --display plain` × 2:
  v1 `scan_id=nBLkdhPYEMLYBTzfoEBKFn`, v2 `scan_id=jni9V9mxsrdqrLkr7UWcbU` (12 transcripts × 10 scanners = 120 inputs each, 0 errors).
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 --with pyarrow --with pandas python research/explorations/trace-lab/har119/tools/scout_har119.py` → `predictions/scout.jsonl` + `predictions/scout/raw/<trial>.scout.json`.
- Scanners present (10, as merged): `repeated_assistant_message`,
  `harness_parse_errors`, `native_tool_call_markup`, `restored_tool_calls`,
  `rule_outcome`, `rule_first_failure`, `rule_handshake`, `rule_loops`,
  `rule_stop`, `rule_wedge`.
- Scan↔direct agreement on v2: 12/12 on all six rule dimensions
  (outcome, first_failure, handshake, loops value, stop, wedge).

Docent (all prefixed `keys run --` for `DOCENT_API_KEY`; account auto-approve ON):
- Reachability: `... python -c "from docent import Docent; print(len(Docent().list_collections()))"` → `3`.
- `... python research/explorations/trace-lab/har119/tools/docent_har119_upload.py --collection db32cc8f-e610-4373-a8dd-89b697a87a60`
  → 12 runs / 12 transcripts / 2085 messages; collaborators owner-only
  (`p.makhnatch@gmail.com`, admin, single entry) before AND after;
  dashboard `https://docent.transluce.org/dashboard/db32cc8f-e610-4373-a8dd-89b697a87a60`.
- `... python research/explorations/trace-lab/har119/tools/docent_har119_reading.py`
  → plan `har119-blind-trial-reading`,
  `analysis-plan/2cdec6f8-ce5c-489d-8a1b-99ee37797afd`, reading
  `61137b09-7180-4309-a7aa-3496256b5761`, 12/12 results, 0 errors →
  `predictions/docent.jsonl` + `predictions/docent/raw/<trial>.docent.json`.
- Prompt/schema extracted verbatim from `docent/har81_blind_reading.py` via
  AST (`PROMPT`/`SCHEMA` literals; module never imported); model
  `openai/gpt-5.6-luna`; blind context subset
  (`trace_lab.trial/task/reward/exception_type/n_episodes`).

## Spend (hard cap $1 of model calls; must be $0 unless stated)

- Total: **$0**. No sandboxes/trials launched; no local model calls; all local
  compute $0 (seconds per trial; Scout scans ~13–15 s each).
- Docent reading (only model spend surface): 578,175 in / 15,361 out tokens
  on `openai/gpt-5.6-luna` (`uses_byok=false`), 12/12 results, zero quota or
  billing messages. The SDK exposes no price/free flag/quota/billing signal
  anywhere (same free-hosted conditions as the 2026-09-29 HAR-81 and HAR-109
  runs); nothing charged.
- Cost-cap compliance: no `--model` Scout scans, no other LLM calls.

## Tool failures / incidents per trial

- No per-trial tool failures: Eval Lab 12/12 reports + 12/12 process-job
  records; Scout 120/120 inputs × 2 scans, 0 errors; Docent 12/12 readings,
  0 errors.
- Scout scan v1 (normalized import) lost job-level caps files, so `rule_stop`
  fell back to `ceiling:trial_budget` on 11/12 trials. Scan v2 imports the raw
  trial dirs (byte-identical steps: normalized↔raw `step_id` sequences
  asserted equal on all 12); `rule_stop` then matches `capabilities.jsonl`
  and Eval Lab ceilings (token_ceiling ×10, request_ceiling ×1,
  model_finished ×1). `scanners.py` untouched; frozen mapping unchanged; v1
  scan retained under `scout_work/scout/` for provenance.
- Docent upload secret gate: 24 hits, ALL reviewed as false positives — the
  shared `SECRET_RE` `sk-` alternative matching `...task-00<1,2>-<24hex>...`
  job-dir fragments inside SDK-attached `metadata.harbor.*` local paths
  (contexts dumped to `/tmp/har119_hitcheck.py` output; 0 hits on the other 9
  trials, 0 hits in any transcript text). `tools/docent_har119_upload.py`
  excuses a hit only if it fullmatches `sk-<jobhash tail>` AND
  `task-<tail>` is a substring of a known job/trial string; anything else
  still refuses with exit 3. Shared `export_harbor.py` untouched.
- Frozen-output observations (reported, not tuned): Docent returned
  `false_completion_claim=true` on `gepa-...__CFCbfps` despite reward 1.0
  (prompt says reward-1.0 ⇒ always false) and `failure_owner=model` on that
  same passed run (only `har110-dev-002864` read `none_passed`); blame prior
  is model×11/none×1, matching the HAR-81 "blames the model on every
  failure" prior. Scout `har110-dev-002864` has `first_failure_ref=null`
  (R-NONE-01 pass, no evidence refs) per the frozen fallback.

## What each tool could not express (nulls are `not expressed`, never guesses)

- Eval Lab: `first_failure_ref` (native step is a stitched ordinal, not a
  `head#`/`cont-N#` ref — raw step/kind/evidence kept), `blame`
  (no attribution/verdict), `loop_kind` (spans but no claim-vs-repetition
  distinction). Expressed: `stop_reason`, `loop_span` (numeric steps),
  `pass_copied` (via `pass_may_be_copied`; null on fails).
- Scout: `loop_kind` (identical-run spans ≠ claim/repetition kinds),
  `pass_copied` (shared rule output surfaces no fetch flag); `blame` is
  `null` on `unclear` attributions and never `task`/`infra` (R-ENV-02 stays
  `harness` per the frozen literal map). Expressed: `stop_reason`,
  `first_failure_ref` (11/12; null only for the clean R-NONE-01 pass),
  `blame` (none/model/harness), `loop_span` (refs).
- Docent: `stop_reason`, `first_failure_ref` (quote only), `loop_kind`,
  `loop_span`, `pass_copied` (no such notions in the HAR-81 schema);
  `blame` is `null` on `unclear` and never `infra`. Expressed: `blame`
  (model/harness/task/none) + all raw reading fields.

## Output inventory (all under `predictions/` + `tools/`)

- `predictions/MAPPING.md` (frozen 2026-09-30T23:42:02Z), `predictions/NOTES.md`
  (this file), `predictions/evallab.jsonl`, `predictions/scout.jsonl`,
  `predictions/docent.jsonl` (12 rows each, selection order, required fields
  verified), `predictions/docent_work_collection.txt` (collection id).
- Raw: `predictions/evallab/raw/` (12 `*.run_report.json` + 12
  `*.process_job.json`), `predictions/scout/raw/` (12 `*.scout.json` rule
  dicts), `predictions/docent/raw/` (12 `*.docent.json` reading outputs).
- Work: `predictions/scout_work/{normalized,probe03,scout,scout_raw}` (DBs,
  scans incl. v1, capabilities rows/summaries).
- Scripts: `tools/{evallab_har119,scout_har119,docent_har119_upload,docent_har119_reading,docent_block_map}.py`.

## Follow-up: Docent block_idx -> step map (2026-10-01T00:15:19Z)

- `tools/docent_block_map.py` (offline, no upload) re-runs
  `export_harbor.convert_trial` on the 12 normalized trials and writes
  `predictions/docent_block_map.json` ({trial: {str(block_idx): step_id}},
  2085 blocks, 0 nulls), then appends `first_failure_step` to each
  `predictions/docent.jsonl` row (other bytes untouched; verified by prefix
  reconstruction).
- Rule: block_idx = 0-based message index in the converted transcript (one
  transcript per run); step = the message's `metadata.atif_step_id` (merged
  assistant turns -> earliest step; none cited). Validated: max cited block
  = N-1 per trial; 68/78 non-empty citation patterns match the indexed
  message's content (whitespace/ANSI-normalized); 51/129 citations have empty
  `start_pattern`; the other 10 match same-block tool-call text, the
  server-rendered `<tool call>` form, or a same-step observation.
- Result: 12/12 rows got a step (28, 78, 2, 15, 71, 5, 6, 10, 2, 7, 49, 12
  in selection order). Sanity: 7/12 first-citations carry a non-empty
  `start_pattern`, 7/7 appear in the mapped step's message or observation.
