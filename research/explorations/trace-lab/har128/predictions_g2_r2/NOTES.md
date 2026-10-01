# HAR-128 part 2 (G2 r2) — tool prediction runs (NOTES)

## Clock / provenance

- Worktree: `~/Developer/eval-lab/.worktrees/har128-tools3`, branch
  `traces/har-128-tools3` tracking `origin/main`. HEAD at run time: `15d8b708`
  (`fix(sft): G4 launch conditions ... (HAR-129) (#648)`; mapped surfaces —
  `step_layers.py`, `probe03.py`, `scout/scanners.py`, `scout/rules.py`,
  `model-readers/prompts.py`, `docent_strong/reading.py`,
  `docent/export_harbor.py` — unchanged since the G2 run). No tool file was
  modified; no commits/pushes/PRs (parent owns git).
- Trial set: the 19 G2 r2 trials named by Main
  (`predictions_g2_r2/trials.json`); all 19 have result.json + trajectory.
- `MAPPING_ADDENDUM.md` written **2026-10-01T09:56:12Z**, before any of the
  runs below (G2-identical plus the `agent_timeout` section). Mapping frozen;
  nothing below retunes it.
- `evallab.jsonl` + `scout.jsonl` built ~10:01Z; Docent upload ~10:02Z,
  batch-1 reading ~10:02Z; batch-2 readings failed ~10:03Z on quota (below).
- Blindness: never opened, globbed or grepped `har128/labels*`, `sft_gate`,
  scores, or rater output. Only `trials.json` (names from Main),
  `result.json`/`config.json` inputs (exception counts, key provenance), and
  run artifacts were touched. Docent upload carried opaque
  `sha256(trial)[:12]` ids (`docent_id_map.json` local only); a pre-upload
  audit asserted zero real trial/job names in any run metadata (transcript
  text stays verbatim).

## Exact commands (cwd = worktree root unless noted)

Eval Lab (all rc=0; 2 parallel workers, 10 + 9 trials from `trials.json` order):
- `uv run evallab process-job --no-ingest --no-publish --json --output-dir /tmp/har128-g2r2-pj/<job> <job_dir>` × 19
  (re-runs on current origin/main, same version for all).
- `uv run evallab report run --json --output-dir research/explorations/trace-lab/har128/predictions_g2_r2/evallab/raw <trial_dir>` × 19
  (emits `<trial>.run_report.json` + a `.run_report.md` sidecar by tool default).
- Each `/tmp/har128-g2r2-pj/<job>/trial-<trial>.json` copied (`cp`) to
  `predictions_g2_r2/evallab/raw/<trial>.process_job.json` (19/19).
- `uv run python research/explorations/trace-lab/har128/predictions_g2_r2/evallab_r2.py` → `predictions_g2_r2/evallab.jsonl` (19 rows).

Scout (deterministic, no `--model`; all rc=0, 0 scan errors):
- `uv run --no-project --python 3.12 --with harbor==0.21.0 python research/explorations/trace-lab/probe-03-capabilities/capabilities.py <19 trial dirs> --out-dir ~/Developer/eval-lab/derived/trace-lab/har128-g2r2/probe03 --evallab-src src`
  → `capabilities.jsonl` (19 rows) + `summary.md` + `reading_sheet.md`.
- `... python research/explorations/trace-lab/normalize/harbor_normalize.py <19 trial dirs> --out ~/Developer/eval-lab/derived/trace-lab/har128-g2r2/normalized --evallab-src src --check ~/Developer/eval-lab/derived/trace-lab/har128-g2r2/probe03/capabilities.jsonl`
  → `19 trials | 1485 bash calls | 0 invalid/errors | 0 probe-03 mismatches`.
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 python research/explorations/trace-lab/scout/import_evallab.py <19 raw trial dirs> --db ~/Developer/eval-lab/derived/trace-lab/har128-g2r2/scout_raw/data --staging ~/Developer/eval-lab/derived/trace-lab/har128-g2r2/staging-g2r2`
  → `resolved 19 trial dirs`, `inserted 19 transcripts (19 staged files carry tool_calls)`.
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 scout scan research/explorations/trace-lab/scout/scanners.py -T ~/Developer/eval-lab/derived/trace-lab/har128-g2r2/scout_raw/data --scans ~/Developer/eval-lab/derived/trace-lab/har128-g2r2/scout_raw/scans --display plain`
  → `scan_id=3zzNskYwxiTeQRsKXyJgzV` (19 transcripts × 10 scanners = 190 inputs, 0 errors).
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 --with pyarrow --with pandas python research/explorations/trace-lab/har128/predictions_g2_r2/scout_r2.py`
  → `predictions_g2_r2/scout.jsonl` (19 rows) + `predictions_g2_r2/scout/raw/<trial>.scout.json` (19).
- Scanners present (10, as merged): same set as HAR-116/G2.
- Scan↔direct agreement: 19/19 on all six rule dimensions
  (outcome, first_failure, handshake, loops value, stop, wedge).
  Raw↔normalized `step_id` sequences asserted equal on 19/19.

Docent (all prefixed `keys run --` for `DOCENT_API_KEY`; account auto-approve ON):
- Secret-gate refusal FIRST: `... python research/explorations/trace-lab/har128/predictions_g2_r2/docent_upload_r2.py`
  → `refusing: 3 non-false-positive secret-like strings` (exit 3, NOTHING
  uploaded). All 3 hits are an RSA private-key PEM header in
  `har120-000865-a2-r2__T8MhMX4` (converted msgs 25–26). Provenance (key
  bytes never printed or stored): first occurrence is the step-14 observation
  of read-only `sed -n 300,335p /testbed/logstash/tests/logstash_test.py` —
  the key is content of the task's own test file
  (`test_adding_secret_from_file`, ELASTICSEARCH_PASSWORD fixture), not an
  agent-generated credential or environment leak. Other 18 trials clean. An
  empty collection `aae3f046-a96b-483d-9872-af7628121631` had been created
  seconds earlier (owner-only); it holds zero runs.
  Main's ordered decision (2026-10-01, option b): upload the other 18 and
  emit a null row for `har120-000865-a2-r2__T8MhMX4` with raw_error
  `"withheld: secret-gate (task fixture RSA key in test file)"`. The FP
  excuse list was NOT widened. Recorded in `docent_upload_r2.py` (`WITHHOLD`)
  and `docent_opus_r2.py` (per-trial reason).
- Upload (18): same command → new private collection
  `e934d3a1-a347-43e5-890a-4aad03097ab3`, 18 runs / 18 transcripts /
  2763 messages (withheld trial excluded); collaborators owner-only
  (`p.makhnatch@gmail.com`, admin, single entry) before AND after;
  dashboard `https://docent.transluce.org/dashboard/e934d3a1-a347-43e5-890a-4aad03097ab3`;
  block map → `predictions_g2_r2/docent/block_map.json` (18 opaque trials).
- Readings (frozen `docent_strong/reading.py` prompt/schema, model
  `anthropic/claude-opus-5-5`; batches 10 + 8):
  `--plan-name har128-g2-r2-blind-trial-reading`:
  - batch1 → plan `9154838f-90bf-44ee-ad48-aa75e7321e1e`, reading `b7d9647b-0242-4905-ab8c-618995970bfc`, 624,012 in / 18,353 out — 10/10 results, 0 errors
  - batch2 → plans `dd550bc1-71f5-40e5-ab59-c9a751a41bd9`, `4a37fbf1-0e08-4400-a405-8050d5d41fa3` (initial + one retry), readings `.../13c5acba-...` — 0/8, ALL FAILED (below)
  - `predictions_g2_r2/docent/batch1.jsonl` (10 rows) + raws (10 files) landed.
- QUOTA STOP: batch-2 result errors read `{"error": "LLM error", "message":
  "Free weekly usage limit reached. Add your own API key in settings or email
  docent@transluce.org to inquire about custom usage limits.", "error_type_id":
  "docent_usage_limit"}` (input/output tokens null — nothing spent, nothing
  charged). Batch 1 consumed the remaining weekly quota. Per the assignment's
  spend rule (paid credits required → stop and report; cap $0), Docent work
  stopped: no provider key was added, no BYOK, no further model calls.
- Combine (Main, 2026-10-01): `docent_opus.jsonl` WRITTEN with 19 rows —
  the 10 judged rows as normal; the 8 quota-blocked trials all-null with
  `raw_error: "unread: docent_usage_limit (free weekly quota)"`; 000865
  all-null with the ordered withhold reason. No further Docent calls.
- Prompt/schema untouched (`model-readers/prompts.py` frozen).

## Spend (hard cap $0 of model calls)

- Total: **$0**. No sandboxes/trials launched; no local model calls; no Scout
  LLM scans; all local compute $0.
- Docent: 624,012 in / 18,353 out (batch 1, 10 results) + 0/0 on the two
  failed batch-2 attempts (error rows carry null tokens) on
  `anthropic/claude-opus-5-5` (`uses_byok=false` on every raw file), zero
  quota/billing messages on the success path. Nothing charged. The quota
  error demands a provider key or a limit raise — both are spend decisions,
  so work stopped instead.
- Cost-cap compliance: no `--model` Scout scans, no other LLM calls.

## Tool failures / incidents per trial

- Eval Lab 19/19 reports + 19/19 process-job records (all rc=0); Scout
  190/190 inputs, 0 errors; Docent 10/18 readings, 0 errors on what ran.
- Secret-gate refusal (above): first for these three jobs; HAR-116 and G2-a1
  uploads saw only reviewed task-hash FPs. Decision (b) executed as ordered.
- Quota exhaustion (above): stops only Docent batch 2. Eval Lab and Scout
  predictions are complete and unaffected.
- Builder-repair incidents (mine, pre-run): several `edit` range slips while
  writing the r2 scripts fresh (the tools2 worktree had been pruned, so no
  copy-paste) — eaten `def main`/`map_blame`/imports, duplicated loop
  headers — each caught by `py_compile` or first run, repaired, re-verified.
  No prediction content affected.
- Frozen-output observations (reported, not tuned): Eval Lab and Scout agree
  19/19 on `stop_reason` (loop_break 9 = all 9 LoopBreakStop trials,
  token_ceiling 7, model_finished 2, request_ceiling 1); Scout blame
  model 13 / none 6 with zero nulls; every trial had evidence refs (0 ff
  nulls); `pass_copied` expressed on 6 reward-1.0 runs (true ×2, false ×4).

## What each tool could not express (nulls are `not expressed`, never guesses)

- Eval Lab: `first_failure_ref`/`first_failure_step`, `blame`, `loop_kind`.
  Expressed: `stop_reason` (incl. `loop_break` ×9), `loop_span` (numeric
  steps), `pass_copied`.
- Scout: `loop_kind`, `pass_copied`; `blame` never `task`/`infra`/`unclear`
  (none this round). Expressed: `stop_reason` (incl. `loop_break`),
  `first_failure_ref` (19/19) + `first_failure_step`, `blame`
  (none/model), `loop_span` (refs).
- Docent strong: PARTIAL — 10 judged rows in `batch1.jsonl` (+ raws);
  8 trials unread (quota); 1 trial (000865) ordered withheld (null row with
  the ordered raw_error at combine time). Schema has no `loop_break`.

## Output inventory (all under `predictions_g2_r2/` + derived)

- `predictions_g2_r2/MAPPING_ADDENDUM.md` (frozen 2026-10-01T09:56:12Z),
  `predictions_g2_r2/NOTES.md` (this file), `predictions_g2_r2/trials.json`
  (19, as named by Main), `predictions_g2_r2/evallab.jsonl`,
  `predictions_g2_r2/scout.jsonl` (19 rows each, trials.json order, required
  fields verified), `predictions_g2_r2/docent_opus.jsonl` (19 rows, real
  trial names: 10 judged + 8 quota-null +
  1 withheld-null, verified), `predictions_g2_r2/docent/batch1.jsonl`
  (10 judged rows, opaque ids) + `predictions_g2_r2/docent/raw/` (10 files),
  `predictions_g2_r2/docent/block_map.json` (18),
  `predictions_g2_r2/docent_work_collection.txt` (active collection id),
  `predictions_g2_r2/docent_id_map.json` (opaque→real, local only).
- Raw: `predictions_g2_r2/evallab/raw/` (19 `*.run_report.json` + 19
  `.run_report.md` sidecars + 19 `*.process_job.json`),
- Scripts (provenance, under `predictions_g2_r2/`): `evallab_r2.py`,
  `scout_r2.py`, `docent_upload_r2.py`, `docent_opus_r2.py` (as run).
- Bulky intermediates (NOT in the worktree):
  `~/Developer/eval-lab/derived/trace-lab/har128-g2r2/{probe03,normalized,scout_raw,staging-g2r2}/`
  (capabilities rows/summaries, normalized trials, Scout DB/staging/scans
  incl. `scan_id=3zzNskYwxiTeQRsKXyJgzV`).
- Error rows (ordered, Main 2026-10-01): 8 quota-blocked opaque ids
  `ef062a6b338d 32028caa99b3 404f2418a39a be4644db3676 a2c1b8df942e
  ade38a2169bd 7017fc0220eb c4bc65a134c1` (transcripts uploaded and intact;
  re-readable when quota allows) + withheld
  `har120-000865-a2-r2__T8MhMX4` (never uploaded, never excused).
