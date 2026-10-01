# HAR-128 G6 — tool prediction runs (NOTES)

No Docent and no Scout LLM in this job (quota spent; no spend): only Eval Lab
(as merged) and the deterministic Scout rules.

## Clock / provenance

- Worktree: `~/Developer/eval-lab/.worktrees/har128-g6`, branch
  `traces/har-128-g6-run` tracking `origin/main`. HEAD at run time: `b1619aa3`
  (`Part of HAR-126 ... (#671)`). No tool file was modified; no
  commits/pushes/PRs (parent owns git).
- Trial set: the 60 sealed trials in
  `~/Developer/eval-lab/derived/trace-lab/har128-g6/SEALED_arm_map.json`
  (opaque id `g6-NN` → trial name), as `predictions_g6/trials.json`. All 60
  trial dirs exist with readable trajectories.
- `MAPPING_ADDENDUM.md` is a byte-identical copy of the G2 r2 addendum
  (itself reusing `har119/predictions/MAPPING.md` exactly), placed
  **before any tool ran** per Main's order. Its header still names the G2 r2
  worktree/HEAD/date — that text is frozen by the copy order; the actual G6
  run provenance is this file (worktree/HEAD above). Mapping content is
  unchanged; nothing below retunes it.
- `evallab.jsonl` + `scout.jsonl` built the same session, after all 120 raw
  tool outputs landed (60/60 process-job + report, all rc=0).
- Blindness: never opened, globbed or grepped `har128/labels*`,
  `g6/metrics*`, `packs`, `rater_tasks.json`, scores, or rater output. The
  only roster read is `SEALED_arm_map.json` (+ the derived `trials.json`).
  Output rows are keyed by opaque id (`trial: "g6-NN"`); real trial names
  never enter rows or filenames. Raw tool outputs are verbatim (filenames
  opaque); rows exclude the one contaminated blob (below). A post-build
  audit over all 120 rows finds zero arm-token hits, zero exact trial-name
  hits; the only `__` hits are benign Python dunders in evidence excerpts
  (`__init__.py`, `self.__open`, `__file__`, 5 rows).

## Mapping drift since the G2 run (recorded, not retuned)

- Commit `85809f01` (HAR-131, between the G2 r2 HEAD `15d8b708` and this
  HEAD) adds `"LoopBreakStop": "loop_break"` to `EXCEPTION_STOP` in
  `src/evallab/probe03.py` only. Verified consequences on the exact paths
  the builders consume:
  - Eval Lab `classify_stop_reason` (`src/evallab/step_layers.py`,
    untouched): `LoopBreakStop` → `error`; the builder maps
    `error` + `LoopBreakStop` detail → `loop_break` per the addendum.
  - Scout rules (`scout/rules.py` → `probe-03-capabilities/capabilities.py`,
    untouched, own `EXCEPTION_STOP` copy without the entry): native stop
    stays `unknown` + `LoopBreakStop` exception; the builder maps it →
    `loop_break` per the addendum.
  - So the addendum's rationale ("probe falls back to unknown") remains true
    for both consumed paths, and the mapped outcome is identical either way.
  - Visible only in kept-raw fields: process-job native `stop_reason` reads
    `loop_break` ×11 (vs `unknown` on the old code); report native reads
    `error` ×16; capabilities native reads `unknown` ×16 (11 LoopBreakStop +
    5 infra). Final predictions agree 60/60 regardless.
- Second drift: process-job `decision` blobs now embed
  `judgments.agreement.additional_loop_calibrations[]` referencing
  prior-cohort label dirs (`labels_har116`, `labels_g2_a1`, `labels_g2_r2`,
  `labels_g2_tail`) and scoring methods. Those PATHS (not label contents —
  never opened) appeared inside tool outputs produced for this run. Because
  rows must stay sealed, `raw_decision` is WITHHELD from `evallab.jsonl`
  rows (marker string in its place; verbatim blob stays only in the opaque-
  named process-job raw files). This is the sole deviation from the
  HAR-119 raw-field set, forced by the higher-order blindness rule.

## Exact commands (cwd = worktree root unless noted)

Eval Lab (all rc=0; 6 parallel workers × 10 ids, opaque ids only end to end):
- `uv run evallab process-job --no-ingest --no-publish --json --output-dir /tmp/har128-g6-pj/<NN> <job_dir>` × 60
  (re-runs on current origin/main, same version for all).
- `uv run evallab report run --json --output-dir /tmp/har128-g6-rep/<NN> <trial_dir>` × 60,
  then per-id `cp` of the single `*.run_report.json` → `predictions_g6/evallab/raw/<g6-NN>.run_report.json`
  and `trial-*.json` → `<g6-NN>.process_job.json`; `.md` sidecars deleted
  (0 remain). No real names in any created path.
- `uv run python research/explorations/trace-lab/har128/predictions_g6/evallab_g6.py` → `predictions_g6/evallab.jsonl` (60 rows).

Scout (deterministic, no `--model`; all rc=0, 0 scan errors):
- `uv run --no-project --python 3.12 --with harbor==0.21.0 python research/explorations/trace-lab/probe-03-capabilities/capabilities.py <60 trial dirs> --out-dir ~/Developer/eval-lab/derived/trace-lab/har128-g6-tools/probe03 --evallab-src src`
  → `capabilities.jsonl` (60 rows) + `summary.md` + `reading_sheet.md`.
- `... python research/explorations/trace-lab/normalize/harbor_normalize.py <60 trial dirs> --out ~/Developer/eval-lab/derived/trace-lab/har128-g6-tools/normalized --evallab-src src --check ~/Developer/eval-lab/derived/trace-lab/har128-g6-tools/probe03/capabilities.jsonl`
  → `60 trials | 5586 bash calls | 0 invalid/errors | 0 probe-03 mismatches`.
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 python research/explorations/trace-lab/scout/import_evallab.py <60 raw trial dirs> --db ~/Developer/eval-lab/derived/trace-lab/har128-g6-tools/scout_raw/data --staging ~/Developer/eval-lab/derived/trace-lab/har128-g6-tools/staging-g6`
  → `resolved 60 trial dirs`, `inserted 60 transcripts (60 staged files carry tool_calls)`.
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 scout scan research/explorations/trace-lab/scout/scanners.py -T ~/Developer/eval-lab/derived/trace-lab/har128-g6-tools/scout_raw/data --scans ~/Developer/eval-lab/derived/trace-lab/har128-g6-tools/scout_raw/scans --display plain`
  → `scan_id=SL5fFTss3kvAyodWacrmMz` (60 transcripts × 10 scanners = 600 inputs, 0 errors).
- `uv run --with inspect-scout==0.5.3 --with harbor==0.21.0 --with pyarrow --with pandas python research/explorations/trace-lab/har128/predictions_g6/scout_g6.py`
  → `predictions_g6/scout.jsonl` (60 rows) + `predictions_g6/scout/raw/<g6-NN>.scout.json` (60).
- Scanners present (10, as merged): same set as prior jobs.
- Scan↔direct agreement: 60/60 on all six rule dimensions
  (outcome, first_failure, handshake, loops value, stop, wedge).
  Raw↔normalized `step_id` sequences asserted equal on 60/60.

## Spend

- **$0 work, $0 model spend surface.** No sandboxes/trials launched; no local
  model calls; no Scout LLM scans; no Docent calls at all (per order).
  All compute is local deterministic tooling.

## Tool failures / incidents per trial

- No per-trial tool failures: Eval Lab 60/60 reports + 60/60 process-job
  records (120/120 rc=0 across 6 workers); Scout 600/600 inputs, 0 errors.
- Stops (Eval Lab ≡ Scout 60/60): token_ceiling 40, loop_break 11,
  model_finished 4 (3 task_complete + 1 prose_completion), infra_error 5
  (2 ServiceUnavailableError + 3 DaytonaNotFoundError). Scout blame
  model 54 / none 6; first_failure expressed on 60/60 (no clean-pass nulls);
  loop_span null on 1; `pass_copied` true ×2 / false ×4 on reward-1.0 runs.
- Builder-repair incident (mine): one `edit` range slip while withholding
  `raw_decision` (stray lines + eaten span block in `evallab_g6.py`) —
  caught by `py_compile`, repaired, re-verified; predictions built only
  from the green script.
- Mapping-drift + decision-blob findings above are reported, not retuned
  (frozen mapping + Main's copy order stand; outcomes identical).

## What each tool could not express (nulls are `not expressed`, never guesses)

- Eval Lab: `first_failure_ref`/`first_failure_step` (stitched ordinal only),
  `blame`, `loop_kind`; plus the ordered `raw_decision` withhold (above).
  Expressed: `stop_reason` (incl. `loop_break` ×11), `loop_span` (numeric
  steps), `pass_copied`.
- Scout: `loop_kind`, `pass_copied`; `blame` null on `unclear` (none this
  round — all attributions were model/none). Expressed: `stop_reason`
  (incl. `loop_break`), `first_failure_ref` (60/60) + `first_failure_step`,
  `blame`, `loop_span` (refs).

## Output inventory (all under `predictions_g6/` + derived)

- `predictions_g6/MAPPING_ADDENDUM.md` (byte copy of G2 r2, placed before
  tools ran), `predictions_g6/NOTES.md` (this file),
  `predictions_g6/trials.json` (60, id→trial/job/dirs; local working file,
  uncommitted — real names never leave it),
  `predictions_g6/evallab.jsonl`, `predictions_g6/scout.jsonl` (60 rows
  each, opaque `g6-NN` keys, trials.json order, required fields verified).
- Raw: `predictions_g6/evallab/raw/` (60 `<g6-NN>.run_report.json` + 60
  `<g6-NN>.process_job.json`, verbatim tool outputs, opaque filenames),
  `predictions_g6/scout/raw/` (60 `<g6-NN>.scout.json` rule dicts).
- Scripts (provenance, under `predictions_g6/`): `evallab_g6.py`,
  `scout_g6.py`.
- Bulky intermediates (NOT in the worktree):
  `~/Developer/eval-lab/derived/trace-lab/har128-g6-tools/{probe03,normalized,scout_raw,staging-g6}/`
  (capabilities rows/summaries, normalized trials, Scout DB/staging/scans
  incl. `scan_id=SL5fFTss3kvAyodWacrmMz`).
