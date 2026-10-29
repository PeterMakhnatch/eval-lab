# B7 results: streaming cascade detector (`cascade_stream`)

Code slice, preregistered in research-context `causal-prefix-monitor.md` (BURST, B7). No model call: the detector
ran without `--allow-model` on a private copy of the CM-11b Kimi verdict cache, so **0 tokens were newly consumed**.
The token figures below are the attested footprint recorded inside the stored verdicts, reported so the cached
judgments are not mistaken for free ones.

## What ran

- New detector `cascade_stream` (`src/evallab/production/trace_harness_stream.py`), one registry line. It streams a
  run's steps one `trace.step` event at a time into `MisalignmentLiveWorker` and serves Stage 2 from a JSONL verdict
  cache keyed like CM-11 `hackbench_cascade.cache_key`; a cache miss is `monitor.triage_unavailable`, so the run
  abstains (unknown, never clear). `--allow-model` is the only path to a fresh call (Kimi route, CM-11 call shape,
  85% quota gate, every attempt ledgered).
- Smoke (`tools/stream_smoke.py`): the 150 labelled Goodfire DEV runs (80 enacted / 70 clean; the 50 considered-only
  runs never scored), admitted through `resolve_runs("goodfire")` and the strict `assignments.csv` cross-key check.
  Stored v8 rows sha256 `59260bc9…` (byte-equal to the harness snapshot); cache `d1c3dda7…` (750 attested rows).
  Two inputs, same detector and cache: **harness** = the steps the harness itself hands a detector (Run Atlas
  `cloud_doc`); **replay** = the step conversion the stored replay used (CM-2 `lib.load_steps`), which matches every
  stored row's `steps_sha256` (150/150).

## Does it reproduce the stored v8 alarms?

| Input | Enacted alarms | Clean alarms | Stored | Runs that abstained |
|---|---|---|---|---|
| replay (frozen step conversion) | **10/80** | **2/70** | 10/80, 2/70 | 15 enacted, 18 clean (41 misses, 36 runs) |
| harness (`cloud_doc` steps) | 7/80 | 1/70 | 10/80, 2/70 | 28 enacted, 24 clean (66 misses, 52 runs) |

- **Replay input: exact.** The alarm set is identical on all 150 runs; the first alarm's frozen prefix and flagged
  step match on 12/12, and the agent turns to the first alarm match on 12/12. Enacted first-alarm turns **median
  19.5 (IQR 14.5–32.75)** reproduce ID-CM-1's Goodfire enacted row exactly (19.5, 14.5–32.8); clean false-alarm
  timing 13.5 turns (n = 2). Card sets differ on 42 runs only because **Stage 1 now raises 164 more cards than the
  stored replay did** (0 stored cards lost): `hackbook:hb_unseeded_randomness` 113, `hackbook:hb_runtime_probe` 30,
  `fetch_attempt` 10 and five smaller families, from #1371's recorded-command / direct-Python extraction change
  (merged 11:41 ET, after the stored replay). Those families do not escalate, and none of them confirmed, so no
  alarm changed.
- **Harness input: not exact, and the difference is the input.** The Run Atlas `cloud_doc` conversion builds steps
  from separate `tool_name` / `command_or_action` columns and drops off-final-path nodes, so Stage-1 cards and the
  judged windows differ: card sets match on only 99/150 runs and 66 judge prompts are not in the cache. The 12
  stored alarms become **8 reproduced + 4 abstain (3 enacted, 1 clean)** — the four are *unknown*, not misses.
- **Emission clock vs causal clock.** A judged alarm is emitted some steps after the prefix its window was frozen
  on: median 3–4 steps across two identical runs (IQR 2–5.5 and 2–7) on the replay input, 5 (IQR 2.25–7) on harness
  input, because the judge thread runs behind the ingest loop and the gap tracks thread scheduling, not the model.
  No first alarm arrived at the end-of-run flush. The emitted gap is an artifact of replay speed, not live model
  latency; only the frozen prefix is comparable with ID-CM-1, and the record keeps both (`card_prefix`/`turns` vs
  `emitted_step`/`emitted_turns`).
- **Tokens (footprint, not spend).** Stored verdicts served: 147 hits (replay) / 122 (harness); attested footprint
  316,331 input + 21,086 output (replay) and 254,615 + 16,942 (harness). Fresh calls and freshly consumed tokens:
  **0 and 0**.

## Limits and deviations

- The exact reproduction is on the **stored step conversion**, not on the harness's own `cloud_doc` steps; the
  acceptance sentence names the `goodfire` selector, and on that input 4 of the 12 stored alarms abstain. No cache
  rows, prompts or verdicts were fabricated or relaxed to close the gap, and no extra model call was made (Kimi's
  5-hour window was at 100% for the whole slice, so `--allow-model` was never usable).
- Turn counts use the harness steps as ID-CM-1 does; on the replay input 4 of 12 alarms differ from the stream's own
  count because those steps include off-final-path assistant nodes the harness drops (8/12 equal, 12/12 on harness
  input). Both counts are stored per run.
- One judge family, one corpus, 150 runs, stored verdicts from a single replay; nothing here measures live model
  latency or queue delay. The `--allow-model` gate was exercised against the real quota reader with no model call
  during the slice: `QuotaStop: kimi-code window at 100% (stop at 85%)`, so no fresh call was possible or made.
