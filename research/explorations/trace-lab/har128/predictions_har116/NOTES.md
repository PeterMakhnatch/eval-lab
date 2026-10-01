# HAR-128 part 2 — tool prediction runs on the 40 HAR-116 trials (NOTES)

## Clock / provenance

- Worktree: `~/Developer/eval-lab/.worktrees/har128-tools`, branch
  `traces/har-128-tools` tracking `origin/main`. HEAD at run time: `ba8d8358`
  (`Part of HAR-126 ... (#613)`; includes #610 at `dc74d9e9`). No tool file
  was modified; no commits/pushes/PRs (parent owns git).
- `MAPPING_ADDENDUM.md` written **2026-10-01T08:00:15Z**, before any of the
  runs below. Mapping frozen (`har119/predictions/MAPPING.md` reused exactly
  plus the `loop_break` stop and `first_failure_step` additions); nothing
  below retunes it.
- `evallab.jsonl` + `scout.jsonl` + `docent_opus.jsonl` built 2026-10-01
  ~08:05–08:12Z; Docent collection created ~08:04Z, 4 readings ~08:04–08:08Z.
- Blindness: never opened, globbed or grepped
  `har128/labels_har116/`, any `har128/labels*` path, scores file, or rater
  output. Only `trials.json` (job/trial names), `RATER_GUIDE.md`-derived
  frozen prompts, and run artifacts were touched. Docent upload carried
  opaque `sha256(trial)[:12]` ids (`docent_id_map.json` local only); a
  pre-upload audit asserted zero real trial/job names in any run metadata
  (transcript text stays verbatim as evidence).

## Exact commands (cwd = worktree root unless noted)

Eval Lab (all rc=0; 4 parallel workers × 10 trials from `trials.json` order):
- `uv run evallab process-job --no-ingest --no-publish --json --output-dir /tmp/har128-pj/<job> <job_dir>` × 40
  (one per HAR-116 job dir; re-runs on current code, same version for all).
- `uv run evallab report run --json --output-dir research/explorations/trace-lab/har128/predictions_har116/evallab/raw <trial_dir>` × 40
  (emits `<trial>.run_report.json` + a `.run_report.md` sidecar by tool default).
- Each `/tmp/har128-pj/<job>/trial-<trial>.json` copied (`cp`) to
  `predictions_har116/evallab/raw/<trial>.process_job.json` (40/40).
- `uv run python research/explorations/trace-lab/har128/predictions_har116/evallab_har116.py` → `predictions_har116/evallab.jsonl` (40 rows).

Scout (deterministic, no `--model`; all rc=0, 0 scan errors):
- `uv run --no-project --python 3.12 --with harbor==0.21.0 python research/explorations/trace-lab/probe-03-capabilities/capabilities.py <40 trial dirs> --out-dir ~/Developer/eval-lab/derived/trace-lab/har128/probe03 --evallab-src src`
  → `capabilities.jsonl` (40 rows) + `summary.md` + `reading_sheet.md`.
- `... python research/explorations/trace-lab/normalize/harbor_normalize.py <40 trial dirs> --out ~/Developer/eval-lab/derived/trace-lab/har128/normalized --evallab-src src --check ~/Developer/eval-lab/derived/trace-lab/har128/probe03/capabilities.jsonl`
  → `40 trials | 2348 bash calls | 0 invalid/errors | 0 probe-03 mismatches`.
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 python research/explorations/trace-lab/scout/import_evallab.py <40 raw trial dirs> --db ~/Developer/eval-lab/derived/trace-lab/har128/scout_raw/data --staging ~/Developer/eval-lab/derived/trace-lab/har128/staging-har128`
  → `resolved 40 trial dirs`, `inserted 40 transcripts (30 staged files carry tool_calls)`.
  Raw trial dirs imported (HAR-119 v1 lesson: normalized import loses
  job-level caps files, so `rule_stop` would fall back to
  `ceiling:trial_budget`).
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 scout scan research/explorations/trace-lab/scout/scanners.py -T ~/Developer/eval-lab/derived/trace-lab/har128/scout_raw/data --scans ~/Developer/eval-lab/derived/trace-lab/har128/scout_raw/scans --display plain`
  → `scan_id=LZnd8aFdwHb423stCkcjN4` (40 transcripts × 10 scanners = 400 inputs, 0 errors).
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 --with pyarrow --with pandas python research/explorations/trace-lab/har128/predictions_har116/scout_har116.py`
  → `predictions_har116/scout.jsonl` (40 rows) + `predictions_har116/scout/raw/<trial>.scout.json` (40).
- Scanners present (10, as merged): `repeated_assistant_message`,
  `harness_parse_errors`, `native_tool_call_markup`, `restored_tool_calls`,
  `rule_outcome`, `rule_first_failure`, `rule_handshake`, `rule_loops`,
  `rule_stop`, `rule_wedge`.
- Scan↔direct agreement: 40/40 on all six rule dimensions
  (outcome, first_failure, handshake, loops value, stop, wedge); raw↔normalized
  `step_id` sequences asserted equal on all 40.

Docent (all prefixed `keys run --` for `DOCENT_API_KEY`; account auto-approve ON):
- Upload: `... python research/explorations/trace-lab/har128/predictions_har116/docent_upload_har116.py`
  (frozen `export_harbor.convert_trial` on the 40 normalized trials,
  `tags={}`, opaque ids + metadata arm scrub + secret scan + blindness audit)
  → new private collection `c54fcb49-5b01-444d-bfbf-cf1c62379bba`,
  40 runs / 40 transcripts / 4727 messages; collaborators owner-only
  (`p.makhnatch@gmail.com`, admin, single entry) before AND after;
  dashboard `https://docent.transluce.org/dashboard/c54fcb49-5b01-444d-bfbf-cf1c62379bba`;
  block map → `predictions_har116/docent/block_map.json` (40 opaque trials).
- Readings (frozen `docent_strong/reading.py` prompt/schema, model
  `anthropic/claude-opus-5-5`; 4×10 opaque-id batches — a single 40-run
  reading would exceed the 1M context, batches run 365–844k input each):
  same `--plan-name har128-har116-blind-trial-reading`
  (plan `analysis-plan/90c6bcd1-d732-4751-a735-6762b1392819`):
  - batch1 → reading `371a075f-fd42-4e63-8c03-029dfba1bc97`, 660,961 in / 16,741 out
  - batch2 → reading `0d35f23f-b7e9-4412-80f2-b6c739f31af4`, 584,940 in / 16,430 out
  - batch3 → reading `e3f320b1-be3c-4dcf-8732-e0ce780b9463`, 364,688 in / 14,432 out
  - batch4 → reading `f6c83bfa-a3c6-4b1c-a28b-550f675cb2b6`, 843,795 in / 19,767 out
  Total: **2,454,384 in / 67,370 out**, 40/40 results, 0 errors; every cited
  block resolved via the upload block map (asserted in-reader).
- Combine: `uv run python research/explorations/trace-lab/har128/predictions_har116/docent_opus_har116.py`
  (opaque→real re-key only, `first_failure_step` added)
  → `predictions_har116/docent_opus.jsonl` (40 rows, real trial names);
  per-trial tool raws under `predictions_har116/docent/raw/` (opaque names
  as written by the reader; map in `docent_id_map.json`) + `batch{1..4}.jsonl`.
- Prompt/schema untouched (`model-readers/prompts.py` frozen;
  `PROMPTS.sha256`/`FROZEN_AT` predate this run).

## Spend (hard cap $0 of model calls)

- Total: **$0**. No sandboxes/trials launched; no local model calls; no Scout
  LLM scans (would cost money — skipped per assignment); all local compute $0.
- Docent readings (only model spend surface): 2,454,384 in / 67,370 out
  tokens on `anthropic/claude-opus-5-5` (`uses_byok=false` on every raw file),
  40/40 results, zero quota/billing messages. The SDK exposes no
  price/free-flag/quota/billing signal anywhere (same free-hosted conditions
  as the HAR-81/HAR-109/HAR-119 runs); nothing charged. No model in any run
  required paid credits, so no stop-and-report was triggered.
- Cost-cap compliance: no `--model` Scout scans, no other LLM calls.

## Tool failures / incidents per trial

- No per-trial tool failures at finish: Eval Lab 40/40 reports + 40/40
  process-job records; Scout 400/400 inputs, 0 errors; Docent 40/40 readings,
  0 errors.
- Eval Lab batch 1 (indices 0–9): the worker's first bulk report loop passed
  an unsupported `--quiet` flag, so 9 reports silently failed while the loop
  echoed rc=0. Detected by listing the raw dir (files missing), re-ran all 9
  without `--quiet` with `PIPESTATUS` rc capture; all rc=0 and all 20 files
  verified present and non-empty. No other batch had this issue (30/30 first
  try on batches 2–4).
- Scout import note: 30/40 staged files carry `tool_calls` (raw Terminus-2
  import; the other 10 stage without). Rule scanners are unaffected:
  scan↔direct agreement is 40/40 on all six dimensions.
- Frozen-output observations (reported, not tuned): all 10 `loopfix` (v1)
  trials end in `ModuleNotFoundError` (the wave-A adapter import-preflight
  failure) → Eval Lab/Scout `infra_error`, Docent `infra_error` with blame
  `infra` (10/10); only 3/10 `loopfix-r2` trials actually hit `LoopBreakStop`
  → the new `loop_break` value on Eval Lab/Scout, while Docent (whose frozen
  schema has no `loop_break`) answers `other` on exactly those 3; Docent
  shows the known ceiling confusion (`request_ceiling` ×9 vs Eval Lab/Scout
  `token_ceiling` ×22). Scout `first_failure_ref` is null on 11 trials
  (clean passes with no evidence refs, per the frozen fallback).

## What each tool could not express (nulls are `not expressed`, never guesses)

- Eval Lab: `first_failure_ref`/`first_failure_step` (native step is a
  stitched ordinal, not a `head#`/`cont-N#` ref — raw step/kind/evidence
  kept), `blame` (no attribution/verdict), `loop_kind` (spans but no
  claim-vs-repetition distinction). Expressed: `stop_reason` (incl. the new
  `loop_break`), `loop_span` (numeric steps), `pass_copied` (via
  `pass_may_be_copied`; null on fails).
- Scout: `loop_kind` (identical-run spans ≠ claim/repetition kinds),
  `pass_copied` (shared rule output surfaces no fetch flag); `blame` is
  `null` on `unclear` attributions and never `task`/`infra` (R-ENV-02 stays
  `harness` per the frozen literal map). Expressed: `stop_reason` (incl.
  `loop_break`), `first_failure_ref` (29/40; null only for clean passes with
  no evidence refs) + `first_failure_step`, `blame` (none/model/harness),
  `loop_span` (refs).
- Docent strong: `loop_break` has no schema value (loop-break runs read
  `other`); otherwise fully expressed per the frozen reader: `stop_reason`,
  `first_failure_ref` + `first_failure_step` (block map, null on passes),
  `blame` (model/harness/task/infra/none), `loop_kind` + `loop_span`,
  `pass_copied` (reward-gated `upstream_fetch`).

## Output inventory (all under `predictions_har116/` + derived)

- `predictions_har116/MAPPING_ADDENDUM.md` (frozen 2026-10-01T08:00:15Z),
  `predictions_har116/NOTES.md` (this file), `predictions_har116/trials.json`
  (40), `predictions_har116/evallab.jsonl`, `predictions_har116/scout.jsonl`,
  `predictions_har116/docent_opus.jsonl` (40 rows each, trials.json order,
  required fields verified), `predictions_har116/docent_work_collection.txt`
  (collection id), `predictions_har116/docent_id_map.json` (opaque→real, local
  only — never uploaded, never committed).
- Raw: `predictions_har116/evallab/raw/` (40 `*.run_report.json` + 40
  `.run_report.md` sidecars + 40 `*.process_job.json`),
  `predictions_har116/scout/raw/` (40 `*.scout.json` rule dicts),
  `predictions_har116/docent/raw/` (40 `*.docent_strong.json` reading outputs,
  opaque names) + `predictions_har116/docent/{batch1..4}.jsonl`
  (opaque-id reader outputs) + `predictions_har116/docent/block_map.json`.
- Scripts (provenance, under `predictions_har116/`):
  `evallab_har116.py`, `scout_har116.py`, `docent_upload_har116.py`,
  `docent_opus_har116.py`.
- Bulky intermediates (NOT in the worktree):
  `~/Developer/eval-lab/derived/trace-lab/har128/{probe03,normalized,scout_raw,staging-har128}/`
  (capabilities rows/summaries, normalized trials, Scout DB/staging/scans
  incl. `scan_id=LZnd8aFdwHb423stCkcjN4`).
