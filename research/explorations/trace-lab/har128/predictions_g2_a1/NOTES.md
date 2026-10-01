# HAR-128 part 2 (G2 attempt-1) — tool prediction runs (NOTES)

## Clock / provenance

- Worktree: `~/Developer/eval-lab/.worktrees/har128-tools2`, branch
  `traces/har-128-tools2` tracking `origin/main`. HEAD at run time: `36c4c0a4`
  (`G3 selection ... (#642)`). No tool file was modified; no commits/pushes/PRs
  (parent owns git).
- Trial set: the 20 first-wave G2 attempt-1 trials named by Main
  (`predictions_g2_a1/trials.json`; the `HAR-120-har120-*-a1` glob matches 30
  dirs — more jobs landed after Main counted — but only the named 20 ran).
- `MAPPING_ADDENDUM.md` written **2026-10-01T09:19:10Z**, before any of the
  runs below (HAR-116-identical plus an `agent_timeout` section concluding no
  new mapping was needed). Mapping frozen; nothing below retunes it.
- `evallab.jsonl` + `scout.jsonl` built ~09:30Z; Docent collection created
  ~09:32Z, readings ~09:33–09:35Z, `docent_opus.jsonl` ~09:36Z.
- Blindness: never opened, globbed or grepped `har128/labels*`, `sft_gate`
  (present in this worktree, avoided), scores, or rater output. Only
  `trials.json` (job/trial names from Main), `result.json`/`config.json`
  inputs (exception counts, 001647 diagnosis), and run artifacts were touched.
  Docent upload carried opaque `sha256(trial)[:12]` ids
  (`docent_id_map.json` local only); a pre-upload audit asserted zero real
  trial/job names in any run metadata (transcript text stays verbatim).

## Exact commands (cwd = worktree root unless noted)

Eval Lab (all rc=0; 2 parallel workers × 10 trials from `trials.json` order):
- `uv run evallab process-job --no-ingest --no-publish --json --output-dir /tmp/har128-g2-pj/<job> <job_dir>` × 20
  (re-runs on current origin/main, same version for all).
- `uv run evallab report run --json --output-dir research/explorations/trace-lab/har128/predictions_g2_a1/evallab/raw <trial_dir>` × 20
  (emits `<trial>.run_report.json` + a `.run_report.md` sidecar by tool default).
- Each `/tmp/har128-g2-pj/<job>/trial-<trial>.json` copied (`cp`) to
  `predictions_g2_a1/evallab/raw/<trial>.process_job.json` (20/20).
- `uv run python research/explorations/trace-lab/har128/predictions_g2_a1/evallab_g2.py` → `predictions_g2_a1/evallab.jsonl` (20 rows).
- Notably, NEITHER tool failed on `har120-001647-a1__iqMRm5R` (no agent dir;
  `RuntimeError: tmux: command not found` before agent start): report says
  `verdict errored, trajectory absent`, outcome `error / exception
  RuntimeError`; process-job marks the trial excluded (reason infra).
  Standard rows carry all of this; no fallback was needed.

Scout (deterministic, no `--model`; all rc=0, 0 scan errors on the final scan):
- `uv run --no-project --python 3.12 --with harbor==0.21.0 python research/explorations/trace-lab/probe-03-capabilities/capabilities.py <20 trial dirs> --out-dir ~/Developer/eval-lab/derived/trace-lab/har128-g2/probe03 --evallab-src src`
  → `capabilities.jsonl` (20 rows) + `summary.md` + `reading_sheet.md`.
- `... python research/explorations/trace-lab/normalize/harbor_normalize.py <20 trial dirs> --out ~/Developer/eval-lab/derived/trace-lab/har128-g2/normalized --evallab-src src --check ~/Developer/eval-lab/derived/trace-lab/har128-g2/probe03/capabilities.jsonl`
  → `19 trials | 1460 bash calls | 1 invalid/errors | 0 probe-03 mismatches`
  (the 1 error is 001647: `no readable trajectory document`).
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 python research/explorations/trace-lab/scout/import_evallab.py <19 good trial dirs> --db ~/Developer/eval-lab/derived/trace-lab/har128-g2/scout_raw/data --staging ~/Developer/eval-lab/derived/trace-lab/har128-g2/staging-g2`
  → `resolved 19 trial dirs`, `inserted 19 transcripts (19 staged files carry tool_calls)`.
  (First attempt passed all 20 dirs; 001647's `no readable trajectory
  document` aborted the batch with 0 inserted — including it was the bug, not
  the tool. Raw trial dirs imported per the HAR-119 v1 lesson on caps.)
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 scout scan research/explorations/trace-lab/scout/scanners.py -T ~/Developer/eval-lab/derived/trace-lab/har128-g2/scout_raw/data --scans ~/Developer/eval-lab/derived/trace-lab/har128-g2/scout_raw/scans --display plain`
  → `scan_id=dAnXPxgs67XonCy6NKp5fg` (19 transcripts × 10 scanners = 190 inputs, 0 errors).
  (An empty `scan_id=WZjKsiYx4d6XStL5x6pjt9` from the aborted attempt was
  deleted; the transcripts DB it took with it was restored by re-import.)
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 --with pyarrow --with pandas python research/explorations/trace-lab/har128/predictions_g2_a1/scout_g2.py`
  → `predictions_g2_a1/scout.jsonl` (20 rows) + `predictions_g2_a1/scout/raw/<trial>.scout.json` (20).
- Scanners present (10, as merged): same set as HAR-116.
- Scan↔direct agreement: 19/19 on all six rule dimensions
  (outcome, first_failure, handshake, loops value, stop, wedge); the 20th
  trial (001647) is not in the scan — its null row comes from
  `rules.analyze_trial_rules` directly (`R-ENV-01`, attribution `unclear`,
  no refs). Raw↔normalized `step_id` sequences asserted equal on 19/19.

Docent (all prefixed `keys run --` for `DOCENT_API_KEY`; account auto-approve ON):
- Upload: `... python research/explorations/trace-lab/har128/predictions_g2_a1/docent_upload_g2.py`
  (frozen `export_harbor.convert_trial` on the 19 normalized trials,
  `tags={}`, opaque ids + metadata scrub + secret scan + blindness audit)
  → new private collection `c095fc0d-ad10-4dc1-bc04-64a329eebdfb`,
  19 runs / 19 transcripts / 2861 messages (001647 has no transcript by
  construction); collaborators owner-only (`p.makhnatch@gmail.com`, admin,
  single entry) before AND after;
  dashboard `https://docent.transluce.org/dashboard/c095fc0d-ad10-4dc1-bc04-64a329eebdfb`;
  block map → `predictions_g2_a1/docent/block_map.json` (19 opaque trials).
- Readings (frozen `docent_strong/reading.py` prompt/schema, model
  `anthropic/claude-opus-5-5`; 2 batches, 10 + 9 — a single 19-run reading
  would approach the 1M context, batches run 567k/605k input):
  `--plan-name har128-g2-a1-blind-trial-reading`
  (each `reading.py` run registers its own plan; two plans share the name):
  - batch1 → plan `d0a2eaff-f508-483a-a611-57b188c43bfe`, reading `324a488e-81d2-412a-93f7-d7092faf8b2e`, 567,232 in / 20,550 out
  - batch2 → plan `3d6c7641-0241-4f1a-b3c8-2526839af9fc`, reading `773845a2-fe2e-41cb-ae84-de58102922a1`, 605,427 in / 19,129 out
  Total: **1,172,659 in / 39,679 out**, 19/19 results, 0 errors; every cited
  block resolved via the upload block map (asserted in-reader).
- Combine: `uv run python research/explorations/trace-lab/har128/predictions_g2_a1/docent_opus_g2.py`
  (opaque→real re-key only, `first_failure_step` added; 001647 →
  all-null row with `raw_error: "no transcript"` per Main's instruction)
  → `predictions_g2_a1/docent_opus.jsonl` (20 rows, real trial names);
  per-trial tool raws under `predictions_g2_a1/docent/raw/` (opaque names
  as written by the reader; map in `docent_id_map.json`) + `batch{1,2}.jsonl`.
- Prompt/schema untouched (`model-readers/prompts.py` frozen).

## Spend (hard cap $0 of model calls)

- Total: **$0**. No sandboxes/trials launched; no local model calls; no Scout
  LLM scans (would cost money — skipped per assignment); all local compute $0.
- Docent readings (only model spend surface): 1,172,659 in / 39,679 out
  tokens on `anthropic/claude-opus-5-5` (`uses_byok=false` on every raw file),
  19/19 results, zero quota/billing messages. Same free-hosted conditions as
  the HAR-116 run; nothing charged. No model in any run required paid
  credits, so no stop-and-report was triggered.
- Cost-cap compliance: no `--model` Scout scans, no other LLM calls.

## Tool failures / incidents per trial

- No per-trial tool failures at finish: Eval Lab 20/20 reports + 20/20
  process-job records (all rc=0, 001647 included — the tools record the infra
  failure inside their outputs); Scout 190/190 inputs, 0 errors;
  Docent 19/19 readings, 0 errors.
- Import aborts on a trajectory-less trial: passing all 20 trial dirs to
  `import_evallab.py` died on 001647 with 0 inserted (bulk insert happens
  after conversion). Re-ran with the 19 good dirs. Same class of issue as
  the HAR-116 `--quiet` incident: silent-ish batch loss, caught by reading
  tool output, not by rc.
- Scan-dir hygiene: deleted the empty `scan_id=WZjKsiYx4d6XStL5x6pjt9` and
  accidentally the transcripts DB with it (`rm` with two targets); restored
  by re-import (19/19). The final scan id is unaffected.
- Builder-repair incidents (mine, pre-run): three `edit` range slips while
  adapting the HAR-116 scripts (duplicated loop headers, eaten `def main` /
  `map_blame`, a misplaced insert in the combiner) — each caught by
  `py_compile` or the first run, repaired, and re-verified. No prediction
  content was affected (builders ran green after repair; outputs validated
  below).
- Frozen-output observations (reported, not tuned): Eval Lab and Scout agree
  20/20 on `stop_reason` (token_ceiling 6, model_finished 4, loop_break 7,
  agent_timeout 1, infra_error 1 [001647], request_ceiling 1) — the frozen
  `agent_timeout` path fires exactly once, confirming the addendum's
  no-new-mapping conclusion; Docent answers `other` on all 7 loop-break
  trials (schema has no `loop_break`) and shows the known ceiling confusion
  (request_ceiling 4 vs token/request 7 on the deterministic tools); Scout
  `blame` is null on 3 `unclear` attributions (incl. 001647's R-ENV-01).

## What each tool could not express (nulls are `not expressed`, never guesses)

- Eval Lab: `first_failure_ref`/`first_failure_step` (stitched ordinal only),
  `blame`, `loop_kind`. Expressed: `stop_reason` (incl. `loop_break` and the
  once-firing `agent_timeout`), `loop_span` (numeric steps), `pass_copied`.
  001647's row is fully tool-spoken (`error / exception RuntimeError`,
  `first_failure.evidence: "no trajectory"`).
- Scout: `loop_kind`, `pass_copied`; `blame` null on `unclear` (3, incl.
  001647) and never `task`/`infra`. Expressed: `stop_reason` (incl.
  `loop_break`), `first_failure_ref` (19/20) + `first_failure_step`,
  `blame` (none/model), `loop_span` (refs).
- Docent strong: `loop_break` has no schema value (7 read `other`); 001647 is
  the instructed all-null row (`raw_error: "no transcript"`). Otherwise fully
  expressed per the frozen reader.

## Output inventory (all under `predictions_g2_a1/` + derived)

- `predictions_g2_a1/MAPPING_ADDENDUM.md` (frozen 2026-10-01T09:19:10Z),
  `predictions_g2_a1/NOTES.md` (this file), `predictions_g2_a1/trials.json`
  (20, as named by Main), `predictions_g2_a1/evallab.jsonl`,
  `predictions_g2_a1/scout.jsonl`, `predictions_g2_a1/docent_opus.jsonl`
  (20 rows each, trials.json order, required fields verified),
  `predictions_g2_a1/docent_work_collection.txt` (collection id),
  `predictions_g2_a1/docent_id_map.json` (opaque→real, local only).
- Raw: `predictions_g2_a1/evallab/raw/` (20 `*.run_report.json` + 20
  `.run_report.md` sidecars + 20 `*.process_job.json`),
  `predictions_g2_a1/scout/raw/` (20 `*.scout.json` rule dicts),
  `predictions_g2_a1/docent/raw/` (19 `*.docent_strong.json`, opaque names)
  + `predictions_g2_a1/docent/{batch1,batch2}.jsonl` (opaque-id reader
  outputs) + `predictions_g2_a1/docent/block_map.json`.
- Scripts (provenance, under `predictions_g2_a1/`): `evallab_g2.py`,
  `scout_g2.py`, `docent_upload_g2.py`, `docent_opus_g2.py`.
- Bulky intermediates (NOT in the worktree):
  `~/Developer/eval-lab/derived/trace-lab/har128-g2/{probe03,normalized,scout_raw,staging-g2}/`
  (capabilities rows/summaries, normalized trials, Scout DB/staging/scans
  incl. `scan_id=dAnXPxgs67XonCy6NKp5fg`).
