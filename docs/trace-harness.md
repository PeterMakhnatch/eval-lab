---
status: living
audience:
  - analyst
  - operator
---

# Trace harness: map of every trace component

One page that says what each trace detector or reader is, how to run it, what it
reads and writes, who uses its output, and the latest number we measured. Written
2026-10-28 from code on `origin/main` (380ee1e93) plus the harness branch
`th/trace-harness-core`. Every number gives its corpus and denominator; none is a
held-out result unless said so. Labels never reach a detector; they are joined only
by a scorer (`AGENTS.md` DEV/TEST rules apply to everything below).

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
| Run health (`production/run_health.py`, `run_health.loop\|runaway\|latency\|infra`) | pending: owned by the Run Health tab | steps plus run facts | pending | pending | pending | none, deterministic |

Run health is **pending**: the only code is one local commit on
`run-health/rh-1-features` (5f124f60b), not on `origin/main`.

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
- `scout-watch` keeps a derived `coverage-v1.json` (ledger SHA-256 plus projection and
  scanner contract) and opens the transcript store only when a transcript exists.
  Admission is re-decided every sweep.

## The harness

`evallab trace run|card|score` (branch `th/trace-harness-core`, lands with the
harness PR) runs the detectors above on one shared blinded input and writes one
`trace-findings/v1` record per (run, detector):

- `run_id, detector, detector_version, reader_model, verdict (alarm|clear|abstain|unknown), score, kind, evidence_steps, summary (<=40 words), tokens, cost_usd, latency_ms, produced_at`.
- `evidence_steps` are original step ids, never renumbered blind ids. A run with no
  actor transcript is `unknown`, never `clear`; a run a detector has nothing for is
  omitted.
- Store: `~/.local/state/evallab-prod-live/trace-harness/findings-v1.jsonl`, append
  only, latest line per (run, detector, version, reader) wins. It is an interim
  JSONL, like the facets file; it is not the cloud DB.
- `run --runs goodfire|goodfire-dev|cheatbench|hackbench|dataset:<id>|file:<path>|ids
  --detectors LIST|all [--allow-model] [--refresh]`. Model-backed detectors only call a
  subscription reader when `--allow-model` is given. `card RUN_ID` prints a short
  evidence card (fired flags plus findings). `score` is registered from
  `trace_harness_score` when that module exists.
- The Phoenix shipper moved: on `origin/main` it is still `evallab trace <path>`;
  on the harness branch it is `evallab trace otel <path>` and `docs/observability.md`
  is updated to match. The harness PR lands both.

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
8. **No detector writes `trace_findings` to the cloud DB yet.** `origin/main` has no
   `trace_findings` in `src/`; the table, importer and `qa-data trace-card` exist only on
   local branch `ce23-trace-findings-20261028` (b88981143, no remote head). That lane owns
   the table; the harness JSONL is the interim store.
9. **Unmeasured on HackBench or DEV**: Laminar, harbor analyze, trace feature flags and
   playbook cards have no detection number against GF-200 or CB-150.
