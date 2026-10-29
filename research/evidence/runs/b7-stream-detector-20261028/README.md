# B7: streaming cascade detector for the trace harness (2026-10-28)

Preregistered in research-context `causal-prefix-monitor.md`, section *BURST (Peter, 4:52 PM ET)*, slice B7 (code slice).

## What changed

- New module `src/evallab/production/trace_harness_stream.py`, detector `cascade_stream` (one registry line in
  `trace_harness.DETECTORS`). It feeds each run's steps, in `step_id` order, one `trace.step` event at a time to
  `MisalignmentLiveWorker` (the CM-5/CM-11 replay event shape), with a Stage-2 judge that serves only stored or
  injected verdicts from a JSONL cache keyed like CM-11 `hackbench_cascade.cache_key`
  (`sha256(model \n sha256(system) \n user)`). A miss raises `JudgeCacheMiss`, which the worker records as
  `monitor.triage_unavailable`; with `--allow-model` a miss is judged on `kimi-code/k3.1-pro-highspeed` through
  `omp -p` exactly as `hackbench_cascade.call` does (no tools, thinking low, retry/fallback-off overlay, API keys
  stripped, served model attested), behind a quota gate that stops at 85% of any Kimi window.
- Verdict and score follow the stored cascade adapters (`alarm` iff a confirmed card or a `grader_tamper`
  pass-through; `clear` iff none and no unavailable judgment; else `abstain`). No new `trace-findings/v1` field:
  `evidence_steps` are the alarm cards' flagged steps, and the summary names the first alarm as emitted — its rule,
  flagged step, the causal prefix its judgment was frozen on, and the step and agent turns at which it appeared
  (the two clocks are kept apart; only the frozen prefix is comparable with ID-CM-1). Token counts are the attested
  usage of every judged window: the stored verdicts' recorded footprint when served from cache, every attempt a
  fresh call made otherwise.
- `CascadeStream.ingest(step) -> list[alarm]` (plus `poll`, `close`) for a live supervisor. The worker judges on
  its own thread, so a judged alarm can arrive on a later call; `wait_for_judge=True` makes it return from the
  step that completed its window. `card_prefix` is the same either way.
- No existing detector changed: the only edits outside the new module are one registry line in
  `trace_harness.DETECTORS` and a note in `docs/trace-harness.md`.

## Smoke (`tools/stream_smoke.py`)

No model call (`--allow-model` off). `prepare` copies, into the private state directory
`~/.local/state/evallab-prod-live/run-atlas/comparisons/b7-stream/`, the stored v8 rows
(`comparisons/cm15/dev-v8/after-goodfire.jsonl`, sha256 `59260bc9…`, byte-equal to the harness snapshot pinned in
`trace-harness/sources/cm11b/manifest.json`) and the CM-11b Goodfire Kimi verdict cache (`/tmp/cm11b/c-goodfire.jsonl`,
sha256 `d1c3dda7…`, 750 attested rows); `run` refuses if either changed. The 150 labelled runs come through
`resolve_runs("goodfire")` (DEV admission, strict `assignments.csv` cross-key via `CorpusRegistry`); the 50
considered-only runs are never scored. Labels (stratum) are read only at scoring.

Two inputs, same detector and cache:

- **harness**: the runs as `evallab trace run --runs goodfire` hands them to a detector (Run Atlas `cloud_doc` steps);
- **replay**: the step conversion the stored replay itself used (CM-2 `lib.load_steps`, copied as `replay_steps`),
  read from the same database rows and checked against every stored row's `steps_sha256`.

First-alarm turns are counted on the harness steps exactly as ID-CM-1 `latency.py` does (agent steps with
`step_id <= card_prefix`); the stream's own count (on the steps it ingested) is reported beside it.

Outputs: `results/smoke.json` (aggregates), `results/per-run.jsonl` (run id sha256[:16], verdicts, counts, both
alarm clocks; no trace text, prompts or judge replies), and `results/card-diff.json` (`cards`: the same replay arm
with Stage 1 only, no judge, against the stored card sets — why they differ). Prompts, verdicts and the cache stay in
the private state directory.
