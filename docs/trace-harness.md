---
status: living
audience:
  - analyst
  - operator
---

# Trace harness: map of every trace component

One page that says what each trace detector or reader is, how to run it, what it
reads and writes, who uses its output, and the latest number we measured. Written
2026-10-28; updated for the unified integrations and retained-reader burst below.
Native implementations remain authoritative; the harness normalizes their input,
identity, findings and scoring. Every number names its cohort and denominator.
Labels reach only the scorer, never a detector or reader.

Corpora used in the table: **GF-200** = frozen Goodfire sample (80 enacted-hack,
70 clean, 50 considered-only, never counted as clean); **CB-150** = CheatBench DEV
(75 hack, 75 benign; already examined, so not held-out); **HB-169** = HackBench
cross-test (169 trajectories, 41 natural, hack rows only, so no false-alarm rate;
local-only, counts only here). "Reader" is the model that reads the trace.

## Components

| component | command | reads | writes | consumed by | latest measured number | reader |
|---|---|---|---|---|---|---|
| Stage 1: tripwires, incremental monitor, command cues (`tripwires.py` RULES_VERSION 3, `production/monitor_worker.py`, `production/command_prefilter.py`) | no CLI; library calls (`scan_trace`, `scan_prefix`, `MisalignmentLiveWorker`) | agent steps of one run (observed prefix) | nothing; cards are in memory. Frozen results: `trace-tools/cascade-stage1-goodfire-dev.jsonl` | window cascade; `atlas clusters --replay-rules`; tool scorecard (Stage-1 baseline) | "any card" on GF-200: 60/80 enacted, 32/70 clean (TT-4 README). HB-169: 105 alarmed; exact gold command 9/169 = 5.3% (CM-11) | none, deterministic |
| Window cascade v4 / v7 / v8 (`production/window_cascade.py`, `CASCADE_VERSION = window-cascade/v8`; `PASS_THROUGH_RULES = {grader_tamper}`) | no CLI; run inside `monitor_worker`; replays by `research/evidence/runs/cm12-blinded-acceptance-20261028/tools/blinded_acceptance.py` | Stage-1 cards, then the card step plus a window of neighbouring steps | per-run rows only in `/tmp/cm11b/after-{goodfire,cheatbench}.jsonl`; judged-window cache in local state | atlas score (frozen v4 file only); tool scorecard baseline; harness cascade adapters | v8: GF-200 10/80 = 12.5% recall, 2/70 = 2.9% false alarm; CB-150 19/75 = 25.3%, 0/75 (CM-12). Frozen v4: 12/80, 2/70; 20/75, 5/75. HB-169: v7 = v8 = 7 alarmed, 2 exact (1.2%) (CM-15) | Stage-2 judge `kimi-code/k3.1-pro-highspeed` (CM-15, CM-11); v4 judge not re-checked |
| Run Atlas (`production/run_atlas*.py`): facets, clusters, watch | `evallab atlas facet\|clusters build\|clusters show\|score\|watch` | blinded runs from the cloud DB or CheatBench answer-key items | `run-atlas/facets-v1.jsonl` (+ `facet-calls-v1.jsonl`), `clusters-v1.jsonl`, `watch-state.json` | clusters, score, `qa-data runs/clusters/recount`, dashboard | facets `suspicion >= 2`: GF-200 7/80 = 8.8%, 1/70 = 1.4%; CB-150 30/75 = 40.0%, 4/75 = 5.3% (CM-12; `scorecards/ra3-FULL-dev-20261028.md`). HB-169 natural: 20/41 exact commands (48.8%) (CM-11) | GF-200 mixed: Haiku 161, Kimi 16, luna 12, grok 11 of 200. CB-150: Haiku 148/150. Watch pinned to `anthropic/claude-haiku-5-5` by plist flag |
| Trace feature flags (`production/trace_feature_flags.py`) | no CLI; `compute_trace_feature_flags(steps)` | agent commands and text only | nothing (pure function) | retriever cards (`cloud_trace_db._with_feature_flags`), `modal_cloud`, playbook cards, harness card | none: routing hints, not verdicts; no recall measured | none |
| Playbook cards (`hackbook/playbook_cards.py`, 12 cards in `playbook_cards_v1.json`) | `evallab hackbook cards [--flag NAME] [--kind task\|trace]` | fired flag names | nothing | `qa-data history`, Task QA, dashboard | none: advice text, not a detector | none |
| Inspect Scout scan, four text scanners (`readers/scout_clouddb.py`, `scout_watch.py`, PR #1315) | `uv run --with inspect-scout==0.5.4 python -m evallab.readers.scout_clouddb run`; `evallab qa-data scan` | blinded DEV transcripts (actor-v2 projection) | `trace-tools/results-v1.jsonl` (45,169 rows; 41,823 are Scout) | tool scorecard; `qa-data findings` | any flag, tool scorecard: Goodfire DEV 131/1,636 = 8.0% recall, 107/752 = 14.2% false alarm; CB-150 19/75 = 25.3%, 34/75 = 45.3% (`tt1-scorecard.md`). Noisy | none, deterministic |
| METR reward-hacking scanner (`metr-scanners 0.1.11`; `readers/scout_metr.py` is the older GLM path; native arms in `research/evidence/runs/trace-tools-tt4-20261027/`, #1378; #1388 open) | `python -m evallab.readers.scout_metr` (GLM); arms via that directory's `run.py` | blinded GF-200 (actor-v2) | `results-v1.jsonl` (`scout_metr`, 1,800 rows, 3 arms) | tool scorecard | Kimi arm, GF-200: 37 of 200 scored (163 not run, quota stop), at >=5: 3/13 recall, 0/12 false alarm (TT-4). Not a full-cohort number | Kimi `k3.1-pro-highspeed` (current), Grok 4.3 arm (8 scored), GLM 5.3 flash (`scout_metr.py` constant). OpenRouter Gemini arm: zero calls |
| Laminar and Docent: private sync and readers (`trace_store/`, `readers/laminar_*`, `readers/docent_*`; #1317 #1334 #1365 #1381) | `evallab traces sync --target laminar\|docent`; `evallab traces fetch --source ...`; `python -m evallab.readers.docent_watch` | admitted DEV traces (HackBench and CheatBench excluded by design) | hosted stores; `results-v1.jsonl` (Docent 769, Laminar 608 rows) | tool scorecard; `qa-data findings` | Docent any flag, GF-200: 3/42 scored hacks flagged, 0/27 clean; 38 of 80 hacks abstained (`tt1-scorecard.json`). Laminar: no scored DEV rows | Docent `openai/gpt-5.4-mini`; Laminar Signals run Laminar-side, model not recorded here |
| Harbor analyze reader (`readers/harbor_analyze.py`) | `keys run -- python -m evallab.readers.harbor_analyze` (`reward_hacking`, `task_specification`) | Harbor trial copies | verdict files under `~/Library/Application Support/evallab/readers` | task pages, `evallab detectors` | none on DEV cohorts; only the small `detectors-v1` sets (not quoted: different corpus) | `openai/glm-5.3-flash` (Z.ai) |
| Failure library (`failure_library/`, #1276 #1309) | `evallab failures ingest\|runs\|mine\|pack\|loop\|stats` | published findings and runs | `research/failure-library/` in git; run records in local state | Task QA loop, seeker lanes | none for trace detection; it holds records, not detectors | none (loop lanes use subscription agents) |
| `evallab qa-data` (#1335; 13 calls incl. `scan`, `export`, `steps`) | `evallab qa-data <call> --help` | admitted DEV store only; one identical refusal for TEST, cross-key, unknown ids | nothing | read-only QA agents | not a detector. CE-21: 26 of 27 agent answers right (9 Haiku agents, 3 rounds) | none |
| Scorecards: `atlas score`; `readers/tool_scorecard.py`; `evallab detectors` (`detectors.py`, `production/detector_scope.py`) | `evallab atlas score`; `python -m evallab.readers.tool_scorecard --results ...`; `evallab detectors list\|verify\|score\|run` | facets + frozen cascade files; `results-v1.jsonl`; `research/calibration/detectors-v1` cache | `run-atlas/scorecards/*`; `trace-tools/tt1-scorecard.{json,md}`; cache verdicts | analysts, Trace Tools page | the tables above come from these; see Gaps for why they do not agree | n/a |
| Run health (`production/run_health.py`, `run_health.loop\|runaway\|latency\|infra`) | `evallab trace run --runs goodfire --detectors run_health.loop,run_health.runaway,run_health.latency,run_health.infra`; native `evallab run-health detect` | steps plus allowlisted times, stop reasons and exception facts | `trace-findings/v1`, version `run-health/4` | harness card and score, cloud DB importer | 4,492 overlapping retained records: 0 verdict/score/evidence mismatches; separate native CLI comparison: 160 records, 0 mismatches. GF-200 loop 1/80 at 0/70; CB-150 latency abstains 150/150 (no clocks) | none, deterministic |
| Hodoscope (MIT, pinned `AR-FORUM/hodoscope@e9b6930`; `readers/trace_product_trials.py`, TT-8) | `PYTHONPATH=src <hodoscope venv>/bin/python -m evallab.readers.trace_product_trials analyze <dir> --out a.hodoscope.json --calls calls.jsonl --cap-usd N`, then `hodoscope sample --group-by cohort -n 50` | blinded ATIF runs (149 safe of the GF-200 sample; the 50 CheatBench runs are never sent to OpenRouter); cohort = one of six anonymised source runs | `results-v1.jsonl` (`hodoscope`, detectors `top{10,25,50}_per_cohort`); per-call ledger; private evidence under `trace-tools/tt8-products-20261028/` | tool scorecard | A run is flagged iff one of its actions is in the top N of its cohort. GF-149 (80 enacted / 69 clean): top10 37/80 = 46.3% recall, 14/69 = 20.3% false alarm; top25 77.5%, 42.0%; top50 93.8%, 63.8%. A random-action null flags runs as often (top50: 88.6% / 73.2%) and run length alone has AUC 0.81. A behaviour ranker for humans, not a run classifier. Measured $1.44 for 149 runs = $9.67 per 1,000 | OpenRouter `openai/gpt-4o-mini` (served-model checked on all 8,624 + 1,586 calls) and `text-embedding-3-large` |
| Phoenix PXI, local (Elastic-2.0, Phoenix 20.26.1, no Docker; `readers/trace_product_trials.py` driver, `readers/openrouter_ledger_proxy.py`, TT-8) | `evallab.readers.trace_product_trials pxi --model <m> --count N --proxy-calls calls.jsonl --cap-usd N` against a loopback Phoenix whose `OPENAI_BASE_URL` is the ledger proxy | the same blinded ATIF runs shipped as OTLP through `evallab.tracing.convert_atif` over HTTP to `127.0.0.1` (observed: Phoenix 20.26.1 also opens its unauthenticated gRPC OTLP port 4317 on all interfaces; there is no switch, so run it only while needed) | `results-v1.jsonl` (`pxi`, `generic_investigation_r{1,2,3}`, `coverage_investigation_r1`); session JSON; per-call ledger | tool scorecard | Frozen generic prompt: cited -> flagged, listed as reviewed -> clear, unlisted -> abstain. Each investigation read only 5 to 17 of 149 runs; union of three generic runs flagged 9 runs (8 enacted, 1 clean). Recall over all 80 enacted = 8/80 = 10%. Forcing full coverage failed: 25 turns, $1.35, 17 runs read, no final answer until a closing turn. A generic investigation costs $0.09 to $0.14 regardless of corpus size | OpenRouter `anthropic/claude-haiku-5.5` via the proxy (served model checked on all 166 calls) |


Trace Tools operations (offline, read-only unless stated):

- `python -m evallab.readers.cost_inventory`: counts the exact actor bytes of admitted DEV
  traces by safe, actor-history-unavailable, hydration-unavailable and secret-veto bucket.
  Prices only an explicit versioned scenario; a missing rate, token count, generation,
  retry or tariff stays `null`, never a full-corpus quote.
- `python -m evallab.readers.scorecard_watch`: re-derives the existing whole-run scorecard
  into identity-bound immutable snapshots, moving `current.json` atomically only after a
  complete valid calculation. Incomplete, malformed or other-projection inputs keep the
  last valid card; missing results remain abstentions.
- `python -m evallab.trace_store.owner_cleanup`: read-only check that exactly the 108
  owner-action Laminar copies are absent. Only a complete, project-bound inventory with
  108 `absent` returns exit 0; partial, failed, empty or wrong-project output is `unknown`.
- **Laminar quarantine** (`trace_store/laminar_quarantine.py`): those 108 copies cannot be
  deleted. The owner's UI/API delete answered HTTP 500 (`DELETE query is not allowed for
  table default.spans because it has projections ...`, 2026-10-28; nothing deleted), so
  `owner_cleanup` stays `cleanup_verified: false` and the ids are quarantined in code
  instead. One predicate, `is_quarantined`, takes a Laminar trace id, a cloud-DB trace id
  or a Harbor trial name. Uploads (`LaminarTarget.upload`) refuse; `traces sync --target
  laminar` and the watch tick skip and report `quarantined`; `traces fetch --source laminar`,
  the live exporter, `laminar_import`/`laminar_signals`/`laminar compare` and the `laminar`
  and `laminar_copy` harness detectors skip and count. Signal filters have no trace-id
  predicate, so they stay excluded by the required opt-in tag: `laminar signals` and the
  opt-in upload guard refuse if a copy carries that tag. `owner_cleanup` still reads them.
- `scout-watch` keeps a derived `coverage-v1.json` (ledger SHA-256 plus projection and
  scanner contract) and opens the transcript store only when a transcript exists.
  Admission is re-decided every sweep.
- `python -m evallab.readers.metr_campaign_plan prepare|export`: offline planner for the
  frozen 200 Goodfire runs plus the disjoint rest of admitted DEV. `export` converts only
  already-stored METR results into `trace-findings/v1`; an unread run has no finding, a
  stored null stays an abstention, and numeric 0 is `clear` only with exact `false` flags.
  It runs no model: native generation stays sealed (#1388), so unread runs stay unread.
- `evallab.readers.harbor_trace_inputs`: `preflight_harbor_inputs` (writes nothing) and
  `prepare_harbor_analysis_inputs` stage label-free native Harbor 0.24 analysis tasks for
  admitted DEV runs. A selection is refused whole when any run lacks full actor history,
  would lose recorded content, contains a secret, or fails the DEV and cross-key gates.
  Backend and funding are reported, never probed or launched; the tests run in the native
  Harbor CI step because they skip unless Harbor is exactly 0.24.0.

## The harness

`evallab trace run|merge|card|score|coverage|length-control` (`src/evallab/production/trace_harness*.py`)
runs the detectors above on one shared input and writes one `trace-findings/v1`
record per (run, detector):

- `run_id, detector, detector_version, reader_model, verdict (alarm|clear|abstain|unknown), score, kind, evidence_steps, summary (<=40 words), tokens, cost_usd, latency_ms, produced_at`.
  Records are validated with the cloud DB importer's own rules (`trace_findings._finding`),
  so every stored record is importable by `evallab prod import-trace-findings`.
- `evidence_steps` are original step ids, never renumbered blind ids. A run without
  actor inputs is unknown, never clear. Explicit run-level stops or exceptions
  may still alarm; a detector with no retained result emits nothing.
- Store: `~/.local/state/evallab-prod-live/trace-harness/findings-v1.jsonl`, append
  only, latest line per (run, detector, version, reader) wins. It is an interim
  JSONL, like the facets file; the cloud DB `trace_findings` table imports it.
- `run --runs goodfire|goodfire-dev|cheatbench|own|own-dev|hackbench|hackbench-all|staged:<set>|detectors-v1[:source]|dataset:<id>|file:<path>|ids
  --detectors LIST|all [--allow-model] [--refresh]`. `all` attempts every non-cache
  adapter; on `detectors-v1` it also expands the cached native detector family.
  Model-backed adapters read retained results. Only facets can generate, gated by
  `--allow-model`; there is no automatic call when a retained result is absent.
  `all` also adds every stored import-only reading found in the Trace Tools ledger (each METR arm
  of a known reader, each Docent and OMP form reader) and `ledger` (below), so a new arm or reader
  appears without a code change. Those only read stored rows; none can start a model call.
- `merge INPUT.jsonl [INPUT.jsonl ...] --findings TARGET.jsonl` validates every input
  before writing and deduplicates by run/detector/version/reader. Newest timestamp
  wins (ties use input order); an older import cannot replace a newer target, and
  replay is idempotent.
- `card RUN_ID` prints one label-free evidence card (≤1.5k tokens): every verdict with
  its reader, cited steps, fired flags and playbook cards.
- `score --corpus …` reports recall/false alarms (Wilson intervals), thresholds at 3% and
  5% false-alarm budgets, localization and cost per detector and per OR/AND combination,
  after a reproduction gate of the published single-detector numbers; `--publish` writes
  the UI Harness tab's `trace-harness/scorecard-v1.json`.
  Where admitted run lengths and both labels are available, each native detector
  also has a same-run length AUROC and length-only recall at no more than its
  observed FAR. The length threshold is chosen in-sample, so is diagnostic, not
  held-out performance. Abstentions and missing lengths are excluded; detector
  AUROC intervals are stratified run bootstraps, not task-clustered intervals.
  Consult `length-control --matched` for the separate task-and-length-matched
  analysis.
- `coverage --corpus … [--corpus …]` lists findings per detector identity and corpus,
  and every registered detector that has no findings there.
- `length-control [--corpus goodfire|cheatbench]` (`trace_harness_length.py`, NP30-TT-1) asks what each
  stored tool adds over run length (`len(HarnessRun.steps)`): per tool, AUROC overall and inside length
  strata with 95% task-clustered bootstrap intervals, native recall/FAR per stratum, and a length-only
  alarm rule matched to the cascade v8 false-alarm count. It reads only stored findings and
  `trace-tools/results-v1.jsonl` rows up to a fixed cutoff, joins labels inside scoring, never changes a
  threshold, and never calls a model. Definitions are fixed in
  `research/evidence/runs/np30-length-control-20261029/PREREGISTRATION.md`.
- `length-control --matched` (`trace_harness_matched.py`, NP31-TT-1) repeats that on the strict `goodfire-dev`
  population (every `CorpusRegistry`-admitted enacted-or-clean run with a stored length; `--corpus cheatbench`
  for contrast) for every stored finding identity plus the model-free fill. It pairs each enacted run with a
  clean run of the same task within ±10% length (greedy in `sha256(run_id)` order, without replacement; a
  same-family secondary match is used only if fewer than 30 primary pairs exist), and reports per tool
  recall/FAR with Wilson intervals, a McNemar-style table, pair AUROC, length-tertile AUROCs and a
  length-only baseline at the tool's own FAR. The decision rule (≥100 primary pairs to be "measurable";
  target 15% at 3% on the matched set, or beating the length-only rule outside the longest tertile) is fixed in
  `research/evidence/runs/np31-length-matched-20261029/PREREGISTRATION.md`.
- `trace_harness_fused.py` (P0-F9D7D6-2) holds the pure rules, CM-14-style grouped CV, and NP30-tertile length
  control for fused native rules (`v8 OR facet term`, k-of-n votes); it is driven by the evidence script
  `research/evidence/runs/p0-f9d7d6-2-fused-scorecard-20261029/tools/score.py`, not by a subcommand. Result:
  no Family-A fusion beat cascade v8 by the preregistered 15%-at-3% criterion on Goodfire DEV
  (`RESULTS.md` there); the chosen rule's recall gain replicates on CheatBench and CM-18 at v8's own false-alarm rate.
- The Phoenix shipper is `evallab trace otel <path>`.
- `--detectors cascade_stream` is the only live path: it feeds a run's steps one
  `trace.step` event at a time to `MisalignmentLiveWorker`
  (`production/trace_harness_stream.py`), and it is the second adapter that may
  generate, gated the same way: Stage 2 serves only stored or injected verdicts, so a
  missing verdict is an unavailable judgment and the run abstains, and `--allow-model`
  is the only route to a fresh call. The reader is `kimi-code/k3.1-pro-highspeed` unless
  overridden: `--detectors cascade_stream:anthropic/claude-haiku-5-5 --allow-model` keeps Stage 1 and the
  v8 prompt, keys the verdict cache by that model (`judge-cache-<reader>.jsonl` beside the Kimi cache), gates fresh
  calls on that provider's `omp usage` windows (stop at 85%, empty usage reads retried), requires the served model to
  equal the requested one on every call, replays runs concurrently, and writes no finding for a run whose call was
  refused for quota or stopped by the call guard (so it is retried, never cached as abstain). The call guard
  (`CallGuard`) counts refusals per call (a refusal ends the call without retry; an incomplete reply such as `toolUse` is
  retried), stops fresh calls after 3 consecutive refusals or 3 consecutive failed calls, and can hold a hard
  budget of `omp` attempts. Its summary keeps two clocks apart: the causal
  prefix the first alarm's judgment was frozen on (comparable with ID-CM-1) and the step
  at which that alarm was actually emitted, with the agent turns elapsed at each. The
  `cascade*` adapters stay stored-result views. P0-F9D7D6-3
  (`research/evidence/runs/p0-f9d7d6-3-matched-cascade-20261029/`) ran it with Haiku 5.5 on the matched Goodfire pairs;
  NP34-TT-1 (`research/evidence/runs/np34-sonnet-matched-cascade-20261029/`) ran the same 246 runs with Sonnet 5.5.

## Gaps and duplicates

Checked against code or files unless marked **[not verified]**.

1. **Three scorers, three label joins.** `atlas score` uses `run_atlas_score._read_labels`
   and the frozen CM-6 v4 cascade files. `tool_scorecard` re-derives Goodfire labels with
   its own SQL over `verified_receipt_json.source_label` and CheatBench labels from
   `splits/assignments.csv`. `evallab detectors` scores its own `detectors-v1`
   manifest, with 2% and 5% false-positive cuts set on source `tw`; its numbers are
   not comparable to the DEV cohorts and are not quoted above. The two cascade baselines
   even differ on the same 80 hacks: 12/80 (CM-12, frozen v4) against 8/80 (TT-4 README,
   "frozen CM-9 final cascade"). **[Why they differ: not verified.]**
2. **Three blinding paths, not two.** `run_atlas.blind_run` (bounded render for facet
   readers, source names replaced), `cloud_trace_export` (used by `qa-data export`), and
   `readers/clouddb_blind` (lossless, opaque ids; used by Scout, Docent and METR). Whether
   they give identical step numbering for one run is **[not verified]**; the harness
   must pick one and carry original step ids.
3. **Cascade v8 per-run rows live in `/tmp/cm11b/`** (both files exist today: 03:55 and
   22:32). CM-12's committed JSON holds hashed selections and aggregate counts; whether
   any committed file holds per-run v8 decisions is **[not verified]**. The harness
   snapshots them.
4. **The facets store is last-wins by `run_id`** (`run_atlas.load_facets`). In the live
   file (mtime 14:00 ET) 4,282 lines cover 2,704 runs and 1,434 runs have lines from
   more than one reader; all 103 Kimi lines are superseded, so none survives as a latest
   record. A reader comparison overwrites itself unless run on a copy. The harness
   keys findings by reader, which avoids this.
5. **METR and harbor analyze share a GLM route, with separate transports.** Both
   default to `openai/glm-5.3-flash`; `scout_metr.py` goes through an Inspect model on the
   Z.ai chat-completions endpoint, `harbor_analyze` through Harbor's own readers. The live
   METR arms do not use GLM at all (Kimi, Grok through OMP).
6. **The tool scorecard does not record the METR model, and mixes arms.** It keys on
   `(tool, detector)` and drops `detector_version`, where the model name lives. In
   `results-v1.jsonl` the latest row per run is now the Kimi actor-v2 arm (200 rows: 3
   alarms, 34 clear, 163 null); the Grok arm's 8 scored runs are overwritten, 7 of them by
   nulls. `tt1-scorecard.md` (METR 1/3 recall, 0/5 false alarms) was built before those
   rows landed **[inferred from its paired counts of 3 and 5]**; rerunning it would
   silently change arm.
7. **Facet readers disagree with each other.** Cross-reader kappa 0.42-0.47 on
   `suspicion >= 2`, against 0.64 for a Haiku retest (`docs/run-atlas.md`; morning
   report). Every score in this page must state its reader, hence the column.
8. **Cloud persistence is an explicit operator step.** The cloud table/importer and
   `qa-data trace-card` are now on `main`. `trace merge` only writes local JSONL;
   `evallab prod import-trace-findings --cloud-database … --jsonl …` admits cloud
   DEV runs and stored step ids. CheatBench item ids and local-only HackBench rows
   cannot be inserted as cloud trace ids. No live import is part of this integration.
9. **Unmeasured on HackBench or DEV**: Laminar, harbor analyze, trace feature flags and
   playbook cards have no detection number against GF-200 or CB-150.

## Additional unified integrations (2026-10-28)

All rows use the same `trace run`, finding store, `trace card`, `trace coverage`
and `trace score` interfaces. Frozen-reader adapters verify the retained prompt,
model/version and input checksums; they do not silently generate replacement calls.

| detector selector | retained/native input | measured operating point | interpretation |
|---|---|---|---|
| `facet_windows_baseline` | CM-13 arm A, Goodfire 200 | 9/80 TP, 2/70 FP | whole-run baseline; frozen Codex Terra reader |
| `facet_windows_only` | CM-13 arm B, Goodfire 200 | 23/80 TP, 5/70 FP | selected windows only |
| `facet_windows_first` | CM-13 arm B1, Goodfire 200 | 17/80 TP, 4/70 FP | first selected window |
| `facet_windows` | CM-13 arm C, Goodfire 200 | 25/80 TP, 6/70 FP | whole-run facets plus selected windows |
| `facet_windows_random` | CM-13 arm D, Goodfire 200 | 16/80 TP, 5/70 FP | random-window control |
| `facets_prefix25`, `facets_prefix50`, `facets_prefix100` | CM-16, the same 50 Goodfire DEV runs outside GF-200; Haiku 5.5 | each 0/25 TP, 0/25 FP | read coverage is complete, but native suspicion produced no alarms; no tuning on the cross-test |
| `recheck_sol` | CM-12 frozen Sol rechecks, theta 86 | 14 GF-200 and 9 CB-150 retained judgments | scored only on the runs actually judged; `unclear` is abstain, not clean. This is not the original full Stage-1 denominator |
| `whole_run_logistic` | frozen pK-SELF M3 per-run scores, CB-150 | 12/75 TP, 4/75 FP | native threshold, not a new fit; GF/HB aggregate-only results cannot become per-run findings |
| `qa_trace_t0` | fixed `combined_v1` agentic-default literals computed from steps | measured literals agree with native deterministic features on 120 runs | this is the steps-only projection, not a source-specific retune. Missing score/file literals force abstention unless a measured OR literal fires |

### Trace-tool readings as detectors (stored-result adapters)

Scout, METR, Laminar Signals, Docent and OMP form readings are imported from the
`evallab.trace_tool_result/v1` ledger (`trace-tools/results-v1.jsonl`; Scout actor-v2 has its own
ledger). Nothing here runs a tool or model. A run with no row is **missing** (no finding);
a row with a null flag is **abstain**; neither is ever `clear`. Run selection still goes through
the DEV admission gate before any actor file is opened; labels are joined only by `trace score`.
Verdicts are the tool's own flag: Scout, Laminar and `ledger` use the any-flag union of the
tool's flagging detectors (always-null informational ones excluded); METR the `@ge5`/`@ge7`
union on the native 0-10 score; Docent/OMP form suspicion >= 2.

| selector | stored rows read | identity (`detector_version` / reader) | GF-200 findings (alarm / clear / abstain) |
|---|---|---|---|
| `scout` | Scout 0.5.4 four text scanners, `native-actor-v2` ledger | `inspect-scout/0.5.4+native-actor-v2` / none | 200 (0 / 200 / 0) |
| `scout_legacy` (new) | same scanners, pre-projection ledger behind `tt1-scorecard` | `inspect-scout/0.5.4+unversioned-pre-native-actor-v2` / none | 200 (0 / 200 / 0) |
| `metr` | METR 0.1.11 Kimi `native-actor-v2` arm (default) | `metr-scanners/0.1.11@c12893d1+scanner-v0+native-actor-v2` / `kimi-code/k3.1-pro-highspeed` | 200 (3 / 34 / 163); native 3/13 TP, 0/12 FP |
| `metr:<reader>@none` (new) | Kimi arm stored without a projection | `metr-scanners/0.1.11@c12893d1+scanner-v0` / same Kimi reader | 200 (0 / 0 / 200) |
| `metr:xai-oauth/grok-4.3` | Grok arm | `…+scanner-v0` / `xai-oauth/grok-4.3` | 200 (1 / 7 / 192); native 1/3 TP, 0/5 FP |
| `metr:openrouter/google/gemini-2.5-flash-lite` (new) | OpenRouter arm; no rows stored yet | `…+scanner-v0+<option digest>+native-actor-v2` / `openrouter/google/gemini-2.5-flash-lite` | 0 (nothing stored) |
| `laminar` | Laminar Signals read back for our own Harbor runs | `existing-signals/readback-v1` / `zai/glm-5.3-flash` | own runs only: none on Goodfire |
| `docent:<reader>` | TT-7 form readings, one reader each | `tt7-frozen-20261027` / the reader | Sonnet 5.5: 150 (3 / 66 / 81); GPT-5.4 mini, GPT-6.1 Sol, Gemini 3.8 Flash (new in `all`): 150 each, all abstain (`docent_usage_limit`) |
| `omp_form:<reader>` | the same form read through OMP | `tt7-frozen-20261027/v1` / the reader | Opus 5.5: 41 (0 / 24 / 17); Luna: 10 (1 / 7 / 2) |
| `ledger[:<tool>]` (new) | every (tool, version, reader) no adapter above reads | compacted ledger version / the row's own `reader_model` or none | nothing unclaimed in today's ledger |

METR arms are parsed from the ledger `detector_version` (`<build>;<route>-<model slug>[;options][;projection-<v>]`),
not listed: a reader selects the arm whose slug it produces, `@<projection>` chooses among a reader's
arms (`@none` is the unprojected one; with no `@` the actor-v2 arm wins), and an ambiguous or unstored
reader is refused. Option segments fold into a 6-hex digest only when the version would exceed the
64-character identifier limit. Arms of a reader not in the known list are still imported by
`ledger` with an unrecorded reader (never decoded from the slug). Facets (`facets:<reader>`),
the Docent rubric (`docent:rh_reading_v1`) and Laminar copy (`laminar_copy`) were already
first-class and are unchanged.

### Local-run admission and cached native detectors

`own` and `own-dev` read the retained TT-3 cohort, but neither trusts a cohort's
`dev` string. A real `CorpusRegistry` must admit the task/item under the current
cross-key assignments and seals **before any actor file is opened**. The score
join uses the identical admitted set; reviewed agent reward hacks are positive,
clean parents/evaluation-fault children are negative, and unreviewed runs remain
unlabelled. The historical 169-run TT-3 table is not the new DEV denominator.

| admitted `own` integration | findings / 37 admitted runs | native verdicts (alarm / clear / abstain) |
|---|---|---|
| `cascade_own`, Kimi | 14/37 | 4 / 4 / 6 |
| `cascade_own:xai-oauth/grok-4.3` | 23/37 | 12 / 11 / 0 |
| `docent:rh_reading_v1`, GPT-5.4 mini | 37/37 | 8 / 25 / 4 |
| `laminar`, GLM 5.3 Flash | 32/37 | 25 / 7 / 0 |
| `laminar_copy`, GLM 5.3 Flash | 31/37 | 12 / 19 / 0 |

The two cascade readers cover disjoint retained runs; they are not a matched
reader comparison. `all` includes both and the separate hosted Docent rubric.

`detectors-v1[:source]` exposes only materialized, explicitly assigned DEV trial
identities from the checksum-verified native corpus. Old ordinal ids (for example
`cb-000001`) are not converted into canonical item ids by guessing. Published
trials may supply their real task identity from `result.json`; results/rewards
never enter detector inputs. `detcache:<name>` imports native cached booleans or
scores, not synthetic actor transcripts. Score-only readers abstain natively but
may participate in numeric FAR-budget sweeps; unknown/missing rows never do.
Use `trace score --corpus 'detectors-v1:g6#stuck_loop'` for loop labels or
`#infra` for infrastructure labels. The old full-G6 published gate is not
re-pinned to a smaller admitted cohort.

The strict native-cache smoke admitted **44 materialized trials** from the
6,768-row historical manifest and emitted results for **20 cached detector
families**, each with 5–35 retained rows. These include Harbor analyze, Kev,
Sol, QA Trace, Laminar Signals, rules/content rules and both Run Health kinds.
Absent cached judgments are missing, not successful detector runs or clean verdicts.

A missing reader identity is displayed as `none / unrecorded` in cards and
score tables. It is not evidence of a deterministic detector: some native
model-backed caches retain the model version but no provider-qualified reader.

Run Health uses cloud-native times/token facts on admitted cloud runs and the
same allowlisted `trial_metadata` on local Harbor trials. Those facts are not
included in `HarnessRun.atlas_run` or the blinded model view. A generic staged
trajectory with no exception/stop/timing facts remains steps-only; no facts are
invented. Health alarm rates against Goodfire cheating labels are associations,
not estimates of loop/infra detection accuracy.

On the current admitted G6 label cohorts, the native and cached Health paths
agree: loop 6/7 TP at 0/1 FP, infra 1/1 TP at 0/8 FP. These tiny subsets prove
adapter parity; they are not precise estimates for the historical full G6 population.

### Reader burst provenance and limits

Seven retained stores from six independent workers were merged without reader
overwrites. Quota stops, refusals and missing reads remain explicit, not clean
results. A denser card or scorecard does not make partial reader comparisons
full-cohort experiments.

| burst reader/method | GF-200 findings; native TP/positive, FP/negative | CB-150 findings; native TP/positive, FP/negative |
|---|---|---|
| Facets Sonnet 5.5 | 200/200; 5/80, 0/70 | 93/150; 19/56, 0/37 |
| Facets Opus 5.5 | 155/200; 4/55, 0/60 | 3/150; 2/2, 0/1 (not a useful estimate) |
| Facets Kimi K3.1 | 61/200; 3/22, 0/23 | 35/150; 2/17, 1/18 |
| OMP form Sonnet 5.5, new reads only | 65/200 findings, 9 scored; 1/4, 0/2 (56 abstentions) | 80/150 findings, 69 scored; 18/40, 0/29 (11 abstentions) |

The separate Haiku DEV fill retained 491/600 selected non-GF-200 runs; it is not
another GF-200 arm. All counts are actual retained findings, not extrapolated
completion rates. Refusals and unread runs do not become false negatives or true
negatives in these conditional rates; coverage remains visible alongside them.

The burst incurred **29 unintended DeepSeek Flash fallback calls, estimated
$0.380644 total** (token-price estimate, not an invoice). Every call is in the
private spend ledger; served-model mismatches were rejected, not attributed to
Claude or Kimi. Generation stopped on quota/fallback barriers. METR Kimi was
retained-output import only: the sealed native OMP transport was not bypassed.
No Docker, local inference, or HackBench upload was used.
