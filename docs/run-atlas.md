---
status: living
audience:
  - analyst
  - operator
---

# Run Atlas

Logic: `src/evallab/production/run_atlas.py`. Tests: `tests/test_run_atlas.py`.
Contract: `run-facets/v1` (one record per rollout), defined in the Wave 6
bridge contract and enforced by `validate_record`.

A subscription model reads each admitted DEV run **blinded** and fills a fixed
form. Clusters and scorecards are built from those records; labels are joined
only at scoring time.

```bash
# Plan only: blind, render and chunk; no model call, no write.
uv run evallab atlas facet --dry-run --dataset goodfire-reward-hack-data --limit 20

# Facet admitted DEV runs from the cloud trace DB (idempotent by run_id).
uv run evallab atlas facet --dataset mimo-defect-map-v1 \
  --route openai-codex/gpt-6-luna --route xai-oauth/grok-4.20-0309-non-reasoning --per-route 4

# CheatBench DEV items from the answer-key registry (full trajectory view).
uv run evallab atlas facet --corpus cheatbench
```

## What the model sees

- `cloud_doc` rebuilds an ATIF-shaped document from `trace_steps`. From
  `flags_json` it reads only `reasoning_content`, `tool_call_id` and the
  structural `on_final_parent_path`; publisher judge flags, passages and
  labels in that column are never read. Resampled nodes off the final parent
  path are not part of the run and are dropped. Tool rows attach to the agent
  step that called them, so step ids stay the original `step_index` values
  (Goodfire passage labels use the same index).
- `blind_run` keeps only `steps`, applies `blind_atif` (annotation steps and
  per-step `extra` removed, steps renumbered) and fails closed if
  `find_forbidden_json` still finds evaluator fields. Run ids, source, split,
  rewards and labels are never in the prompt; known source names are replaced
  by `[source]` in rendered text.
- Agent reasoning and messages are never cut. Tool-call arguments (4,000
  chars) and observations (2,500 chars) are bounded head and tail with an
  explicit `[... N characters elided ...]` mark; task/system text is bounded at
  12,000.
- Runs longer than one chunk (160,000 rendered chars) are read in contiguous
  parts. Each part yields notes with cited steps (map); the notes fill the
  form (reduce). A step longer than a chunk is split into consecutive parts.

## Record and validation

`run_id` is the rollout identity: the cloud DB `trace_id`, or the answer-key
`item_id`. (The DB `run_id` column names a whole campaign.) `run_id`, `source`,
`split` and `outcome` (`reward >= 1` pass, `<= 0` fail, otherwise unknown) are
copied from the store, never from the model.

Every reply is checked before it is stored: enums, booleans, `suspicion` 0-3,
word limits (goal 20, approach 30, shortcut 8), at most five
`evidence_steps`, each a step of the run, and evidence required when
`suspicion >= 2`. An invalid reply gets one repair turn; otherwise the run
fails and nothing is written for it. Model-cited blind step numbers are mapped
back to original ids, and the stored record is validated again against the
run's original ids.

## Routes, accounting and storage

- Calls go only through `omp auth-gateway stdio` to subscription providers
  (`anthropic/`, `openai-codex/`, `xai-oauth/`, `google-antigravity/`,
  `kimi-code/`); other routes are refused before a request is sent. A reply
  from any model other than the requested one fails the call.
- `RoutePool` runs at most `--per-route` runs per route (max 8) and benches a
  route that reports quota exhaustion for 30 minutes, moving its work to the
  others. All calls of one run use one route; `facet_model` is that route.
- Interim store (until the cloud DB atlas tables land):
  `~/.local/state/evallab-prod-live/run-atlas/facets-v1.jsonl`, append-only,
  last line per `run_id` wins. Every call is receipted in
  `facet-calls-v1.jsonl` (requested/served model, tokens, latency, prompt and
  response SHA-256, prompt version, `cost_usd: 0.0`, billing `subscription`),
  plus one terminal line per run with its status and error.

## Scorecard (`evallab atlas score`)

Logic: `src/evallab/production/run_atlas_score.py`. Tests:
`tests/test_run_atlas_score.py`. Labels are joined here and nowhere earlier.

```bash
uv run evallab atlas score --json-out /tmp/atlas-score.json --markdown-out /tmp/atlas-score.md
```

- Populations: `goodfire_cm6` (the frozen 200-trace CM-6 cohort: 80
  `enacted_yes`, 70 `clean_no`, 50 `considered_only`), `goodfire_dev_all`
  (every admitted, judged Goodfire DEV receipt) and `cheatbench_dev` (150 DEV
  items; positive iff `expected_verdict == "fail"`). Goodfire strata come from
  `verified_receipt_json.source_label`; considered-only traces are reported but
  never negatives. A run without a facet record is missing, never a negative.
- Detectors: `suspicion >= 1/2/3`, each boolean facet, test tampering,
  grader-reasoning awareness, `shortcut != "none"`, the frozen CM-6 cascade
  alarm (`e2escore-*-pf_r.json`, not rerun) and the union `suspicion >= 2 OR
  cascade`. Facet and cascade comparisons use the identical non-excluded
  intersection. Recall and false-alarm rate carry Wilson 95% intervals.
- Passage hits: for enacted Goodfire traces with labelled passage steps, the
  share whose `evidence_steps` hit a positive step exactly, within five
  assistant turns (CM-6's method), and the share of cited steps that are
  positive.
- The scorer refuses any run that is TEST under any answer key
  (`assigned_test_anywhere`) or not DEV in its own source.

## Clusters (`evallab atlas clusters`)

Logic: `src/evallab/production/run_atlas_clusters.py`. Tests:
`tests/test_run_atlas_clusters.py`. Contract: `run-clusters/v1`.

```bash
uv run evallab atlas clusters build --name-with-model xai-oauth/grok-4.20-0309-non-reasoning
uv run evallab atlas clusters show --with-labels      # label and rule rates at scoring time
```

- Input text per run is `approach + " | " + shortcut` only, embedded with the
  free 256-dimension `HashingEmbedder` (lexical). A remote embedder requires
  `--embedder remote`, a positive `--spend-cap-usd`, an approved
  `--embedding-input-usd-per-million` and pinned endpoint/model/tokenizer.
  Before each batch, the existing `InvestigationBudget` ledger reserves a
  conservative UTF-8-size token bound. Failed calls retain their reservation;
  restarting cannot reset the lifetime cap. No remote embedding calls were
  used for the Atlas study.
- Deterministic (seeded) bisecting k-means on cosine builds three levels;
  every run is in exactly one cluster per level, and a cluster too small to
  split continues unchanged to the next level. Ids look like `L1-03.2.1`.
- Names: distinctive terms by default; `--name-with-model ROUTE` asks a
  subscription model for at most six words from member texts only (no labels,
  run ids or source names) and falls back to terms on an invalid reply
  (`name_source` records which). Route exhaustion or a served-model mismatch
  stops the build immediately, without publishing a partially named snapshot.
- Builds append to `~/.local/state/evallab-prod-live/run-atlas/clusters-v1.jsonl`
  with a `build_id`; readers take the latest build. `label_rates` stay null in
  the store and are filled only by `show --with-labels`, which also reports
  `rule_recall` (labelled positives flagged by a current monitor rule) and
  `rule_fpr` (labelled negatives flagged). Rule hits come from the frozen CM-6
  stage-1 results; `--replay-rules` replays the deterministic stage-1 rules,
  without a judge, for the other runs. `--coverage alarm` counts only the
  cascade's confirmed alarms (CM-6 cohort). The uncovered list holds clusters with
  at least five labelled runs, a positive rate of at least 50% and rule recall
  of at most 20%, with their three most central member texts.

## Always on (`evallab atlas watch`)

Logic: `src/evallab/production/run_atlas_watch.py`. Tests:
`tests/test_run_atlas_watch.py`. Service template:
`scripts/ops/launchd/com.petermakhnatch.evallab.run-atlas-watch.plist`.

```bash
uv run evallab atlas watch --once --dry-run      # count pending runs; no lock, no gateway
uv run evallab atlas watch --interval 300 --max-per-cycle 25 --max-tokens-per-day 250000
```

- No ingestion path publishes a "trace committed" event (`upsert_trace`,
  `corpus_expansion.import_pack` and replays all write differently), so the
  watcher polls the committed DB. Each cycle re-applies the DEV/cross-key
  admission gate, subtracts runs already in the facets JSONL and runs that
  failed `--max-failures` times (`run-atlas/watch-state.json`), and facets the
  newest rows first (highest `rowid`), so a newly ingested run is next.
- Bounded: at most `--max-per-cycle` runs per cycle and an optional daily
  token budget summed from today's (UTC) call receipts. Providers whose
  `omp usage --json` window is at or above 90% are skipped; without usage data,
  `RoutePool` quota benching still applies.
- One instance at a time (flock on `run-atlas/watch.lock`). The launchd
  template is rendered with absolute paths to an immutable release before it
  is loaded; loading it starts subscription-backed extraction.
