# aisa-group/PostTrainBench-Trajectories (cross-test corpus)

Local, eval-only cross-test set. No rows, transcripts or judge text are in git; only
`tools/adapter.py`, `tools/profile.py` and `results/profile.json` (counts, digests, hashed ids).

## Source and rights

- Dataset: https://huggingface.co/datasets/aisa-group/PostTrainBench-Trajectories, pinned at
  commit `386b3034fa26f0f1b3ae3bfded99b791f0b254a6`. Public, ungated, not private.
- Licence: Apache-2.0 (card YAML `license: apache-2.0` and card body "Apache 2.0"). The dataset
  has no LICENSE file; the card is the only licence statement.
- Bundle: `~/.local/state/evallab-cross-test/aisa-group__PostTrainBench-Trajectories/386b3034fa26/`
  (0444/0555, 11,614 manifested files, 2,072,349,261 bytes). Every file was verified against the
  Hub's LFS sha256 or git blob sha1 at download, and again by `profile.py` after locking.
- `CROSS_TEST_ONLY.md`: frozen-design one-shot, no training/tuning/few-shot, no vendor upload.

## Unit and what was staged

One unit = one 10-hour PostTrainBench run: a CLI agent (claude, codex, opencode, glmx, qwen3max) is
asked to post-train a base LLM (Qwen3-1.7B/4B-Base, SmolLM3-3B-Base, Gemma-3-4B-PT) to raise one
benchmark score (AIME 2025, ArenaHardWriting, BFCL, GPQA, GSM8K, HumanEval, HealthBench).
Staged for every run dir that has `contamination_judgement.txt` (1,388 of 1,842 dirs):
`solve_out.txt` (else `trace.txt`, 441 runs), both verdict `.txt` files and their `task/` copies,
`judgement_*.json`, `metrics.json`, `time_taken.txt`, plus the card. Not staged: `error.log`,
`solve_parsed.txt`, `judge_output*.json/txt` (older variants, ~1 GB each), `judgement.log`,
`system_monitor.log`, the agent workspace. The task prompt the agent saw is not a separate file in
the dataset; it is not staged.

## Labels

Construct: **cheating-or-exploit**. The card names the judges "anti cheating" and the verdicts are
benchmark-test-data contamination and disallowed model use, which PostTrainBench forbids.

Positive: any anti-cheating verdict says detected: `contamination_judgement.txt` =
`contamination detected`, `disallowed_model_judgement.txt` = `disallowed use detected`, or a true
boolean in `judgement_{gpt5_4,gpt5_4_rerun,api,ptb_lookup}.json`.
Negative: both txt verdicts explicit (`no contamination detected`; `only allowed use detected` or
`disallowed use not detected`) and every present `judgement_*.json` boolean false.
Ambiguous: missing (`No ... produced.`), unparseable, non-boolean. Excluded: no trace or a trace
that maps to zero steps.

Counts (`results/profile.json`): 1,388 rows = 143 positive, 1,185 negative, 9 ambiguous, 51 excluded;
1,328 scoreable. The card publishes no counts. Of 144 runs with any positive verdict, 64 have a txt
positive (62 contamination, 2 disallowed use) and 80 are positive only in a `judgement_*.json`
(gpt5_4: 106 contamination, 2 disallowed model; api: 4); one of the 144 has an empty trace and is
excluded. The earlier 62-positive figure was the contamination txt alone. Judges disagree
(only 32 positives have both a txt and a gpt5_4 positive), so labels are LLM-judge output, not human
adjudication; negatives are "no judge fired", not certified clean. Positives are concentrated in
BFCL (45) and HealthBench (30), and in claude (74) and opencode (44) agents; glmx has none.

## Adapter

`specs()` yields one record per run: `identity`, `labels`, `_steps`. `_steps` hold only the
agent's text, reasoning, tool calls and results (results clipped at 20,000 chars); harness stdout
and CLI telemetry are counted, not replayed. Stdlib only. `selfcheck()` normalises whitespace and
checks judge justifications and verdict strings against step text: 0 leaks across 1,388 runs.
Run `python3 tools/adapter.py` (selfcheck) or `python3 tools/profile.py` (needs the locked bundle).
