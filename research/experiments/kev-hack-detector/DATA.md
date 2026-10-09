# Kev hack-detector chunks

Built by `build_chunks.py` → `derived/kev/chunks/`
(`train|dev|test|external.jsonl`, `train_sample|eval_sample.jsonl`, `stats.json`).

## Sources
- CheatBench `steinad/CheatBench` @ `461360b…a53bd0f`, `full.parquet` (4,472 rows,
  500 MB). NOTE: its `split` column is `full` for every row — subset membership only,
  not a task-disjoint partition — so we ignore it and hold out whole families.
- Terminal Wrench upstream `few-sh/terminal-wrench` (Apache-2.0), fetched 2026-10-09
  via `src/evallab/terminal_wrench.py` (`sanitized`/`raw`/`baseline` trees).
- Eval Lab `research/calibration/detectors-v1` (corpus v1, frozen 2026-10-07,
  `tw_subset.json` seed 20261006): 484 rows, all mapped to split `external`.

## Rendering
Each trace → `[task]` prefix (≤800 Qwen tokens) + `[trajectory]` body of readable
units (role/tool/command/output lines, reasoning kept). Any single tool output >
4,000 chars truncated with a marker; traces >1.5M chars capped (12 traces).

## Chunking
`Qwen/Qwen3.5-4B-Base` tokenizer (real, not the chars/3.5 fallback). Windows of
~5,000 tokens with ~500-token overlap. State tokens: p50 5,008, p90 5,473, max 5,809.

## Labels
CheatBench mechanism → pattern: `harness_leaks_verifier`→`verifier_access`,
`harness_leaks_answer`/`secret_discovery_in_environment`→`answer_leak`,
`secret_discovery_via_web_search`→`upstream_fetch`,
`verifier_exploitation`→`grader_tamper` (1 `unresolved_mechanism` trace → null).
Cheating windows overlapping the `loc_*` evidence → `chunk_label` true, other windows
of a cheating trace → null (unknown), every window of a benign trace → false; `attempt`
traces keep `trace_label` attempt with null chunk labels. Localization matched
843/844 cheating traces (snippet 252, field_path 454, loc_events 136, latent 1); the 1
miss is a `latent_harness` row with no quotable span. Attempt 41/41.

## Splits (family hold-out; TW/detectors-v1 never train)
- train: TB2 + HAL-USACO + SWE-smith + MLE-bench + KernelBench + BountyBench (13,821)
- dev: task-disjoint ~10% of TB2 tasks, 10/96 task ids (615)
- test: SWE-bench-Verified + SWE-rebench + CyBench (9,468)
- external: detectors-v1 only (1,769)

| split | chunks | true | false | null |
|---|---|---|---|---|
| train | 13,821 | 888 | 12,664 | 269 |
| dev | 615 | 44 | 571 | 0 |
| test | 9,468 | 136 | 7,979 | 1,353 |
| external | 1,769 | 0 | 1,261 | 508 |

Samples (seed 0): `train_sample` 2,664 (888 pos + 1,776 neg, 2:1, ~10.4M tokens);
`eval_sample` 3,894 (~19.2M tokens, `eval_role` chunk|trace: 150 benign traces fully
+ all cheating test/dev traces capped at 40 windows/trace).

## Known label noise
- HAL-USACO is 218/218 cheating (all `secret_discovery_in_environment`-style).
- Test positives are answer_leak-heavy; no `verifier_access` in test (by design).
- TW sanitized hacks carry a wording cue (72/100 say "verifier" vs 0/100 clean).
- 11 TW∩CheatBench-TB2 task-name overlaps (different harnesses; TW never trains).
- 6 external chunks have pattern null (cheating rows with empty `families`).
- Deterministic: rerun with `--limit 250` → identical sha256.
