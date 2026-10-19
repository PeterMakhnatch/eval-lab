# cheatbench-actions-v1: natural agent traces, two blinded views

`qa.answer_key/v1`, built 2026-10-19. This is a detector key, not a task-correctness
key: positive means the source labels a shortcut or rejected shortcut attempt.
Negative means source-labelled benign; it does **not** independently establish a
correct solution or a strong grader.

## Source, rights and labels

- [steinad/CheatBench](https://huggingface.co/datasets/steinad/CheatBench), revision
  `461360b34e93644955563dcc9243c6cc1a53bd0f`; maintained by that dataset's creators.
  The pinned [dataset card](https://huggingface.co/datasets/steinad/CheatBench/blob/461360b34e93644955563dcc9243c6cc1a53bd0f/README.md)
  declares **MIT**. This derived manifest retains that source attribution. Raw
  trajectories remain outside git and may contain third-party benchmark material.
- `source.json` pins the full parquet's URL components, SHA-256, byte size, 4,472
  rows and label counts: 844 `cheating`, 41 `attempt`, 3,587 `benign`.
- The card describes human-verified positives and vetted negatives, with some
  benign traces lacking audit-able steps. We exclude rows without tool calls in
  the pinned, existing converter inventory. Positive combines `cheating` and
  `attempt`; the original class and mechanism remain in `items.jsonl` only.

## Sampling and split

300 traces: **75 positive + 75 negative in dev**, and the same in **test**. A fixed
SHA-256 seed ranks rows, with round-robin benchmark/mechanism strata. Eligibility
and source counts are in `source-audit.json`.

The family split reuses `evallab.cheatbench_fidelity.HELDOUT_BENCHMARKS`:
SWE-bench Verified, SWE-rebench and CyBench are test; all other families are dev.
For this external trace key `subject.task_id` is deliberately the conservative
**benchmark-family grouping unit**, not a reconstructed task package. No family
straddles splits. `subject.task_digest` is null because no runnable, exact task
package is supplied; `source_sha256` and both trajectory hashes pin real bytes.
The split guard publishes the item assignments in `../splits/assignments.csv`.

**Prior-exposure warning:** the 2026-10-19 main branch already contains the full
convertible CheatBench corpus in `research/calibration/detectors-v1/cb_subset.json`.
This key adds a reproducible, blinded two-view measurement; it is **not** a newly
unseen dataset for `combined_v1` or other systems fit on that calibration corpus.
The baseline here uses the existing deterministic tripwires, with no tuning.

## Actor artifacts and leakage audit

`trial_ref` resolves to the **full normalized ATIF** trial; `actions_only_ref`
resolves to a separate trial whose assistant prose/reasoning is removed. Both
are published-shaped directories under the configured results home, each with
`agent/trajectory.json`. Artifact collection and item names are opaque hex, not
labels. No `result.json`, evaluator verdict or hidden reward is supplied.

The existing `evallab.cheatbench.convert` handles heterogeneous source envelopes.
We drop its synthetic provenance header, neutralize agent/model metadata, and
apply `blind_atif`; we do **not** call the label-bearing legacy materializer.
The full view is not a lossless raw archive: the existing converter caps messages
at 8,000 characters, observations at 50,000, and some argument/parser fields at
2,000/4,000; unsupported envelope details are not reconstructed. Raw parquet is
retained separately and pinned. Do not interpret a conversion miss as proof that
the original trace lacks evidence.

Actions-only keeps task/user text, tool names/arguments, observations and event
order, but blanks every assistant message and removes separate reasoning fields.
The builder scans **every full and actions-only artifact**, fails on structural or
serialized evaluator annotations and label-bearing paths, and also changes every
label/rationale/localization field and checks identical actor bytes. The committed
`leak-audit.json` records counts and the item-manifest digest. Genuine agent words
such as “cheating” are evidence and are not lexically censored.

## Rebuild and measurement

```sh
uv run python research/answer-keys/cheatbench-actions-v1/build.py --fetch --scoreboard
uv run python research/answer-keys/splits/guard.py
```

`--fetch` only downloads missing public, revision-pinned files; existing files
must match SHA-256 and size. No model calls and no submitted code execution occur.
Raw parquet and derived ATIF stay in `EVALLAB_RESULTS_HOME` (default
`~/Developer/eval-lab-results`). The builder writes sorted `items.jsonl`,
`source-audit.json`, `leak-audit.json`, `predictions.jsonl` and `baseline.json`.
`--scoreboard` idempotently appends the two frozen test rows to the shared CSV.

The fixed baseline ORs verdict-eligible `scan_trace` rules plus the existing
stop-eligible content rules. Diagnostic-only external acquisition/history
browsing are excluded. Trial-directory integrity checks and task/reference
baselines are unavailable. `baseline.json` reports test-only counts, recall,
false-alarm rate and 95% Wilson intervals for each view; these are unweighted
stratified-sample intervals, **not** population or cluster-adjusted estimates.
