# aisa-group/PostTrainBench-Trajectories (cross-test corpus)

Local, eval-only cross-test set. No rows, transcripts or judge text are in git; only
`tools/adapter.py`, `tools/profile.py` and `results/profile.json` (counts, digests, hashed ids).

## Source and rights

- Dataset: https://huggingface.co/datasets/aisa-group/PostTrainBench-Trajectories, pinned at
  commit `386b3034fa26f0f1b3ae3bfded99b791f0b254a6`. Public, ungated, not private.
- Licence: Apache-2.0 (card YAML `license: apache-2.0` and card body "Apache 2.0"). The dataset
  has no LICENSE file; the card is the only licence statement.
- Bundle: `~/.local/state/evallab-cross-test/aisa-group__PostTrainBench-Trajectories/386b3034fa26/`
  (0444/0555, 14,110 manifested files, 4,183,653,395 bytes, manifest sha256
  `c28adf9a4b25530de3d944e50e47e7e14faef386a5e2e6a5dc0ebc3a218566da`). Every file was verified
  against the Hub's LFS sha256 or git blob sha1 at download, and again by `profile.py` after
  locking. The first staging (manifest `2f029de5...`, 11,614 files) covered only run dirs with
  `contamination_judgement.txt`; the repair extended the SAME pinned revision to every run dir
  with any judge output.
- `CROSS_TEST_ONLY.md`: frozen-design one-shot, no training/tuning/few-shot, no vendor upload.

## Unit and what was staged

One unit = one 10-hour PostTrainBench run: a CLI agent (claude, codex, opencode, glmx, qwen3max,
cursor, kimi) is asked to post-train a base LLM (Qwen3-1.7B/4B-Base, SmolLM3-3B-Base,
Gemma-3-4B-PT) to raise one benchmark score (AIME 2025, ArenaHardWriting, BFCL, GPQA, GSM8K,
HumanEval, HealthBench).
Staged for every run dir that has ANY judge output file (`contamination_judgement.txt`,
`disallowed_model_judgement.txt` or `judgement_*.json`): 1,815 of 1,842 dirs (the other 27 have no
judge file). Per dir: the trace (`solve_out.txt`, else `trace.txt`; 56 runs have only
`trace.txt`), the verdict `.txt` files and their `task/` copies, `judgement_*.json`,
`metrics*.json`, `time_taken.txt`, plus the card. Not staged: `error.log`, `solve_parsed.txt`,
`judge_output*.json/txt` (older variants, ~1 GB each), `judgement.log`, `system_monitor.log`, the
agent workspace. The task prompt the agent saw is not a separate file in the dataset.

Selection bias fixed: the first pass kept only dirs with the contamination txt, which dropped
427 dirs that carry only JSON judges, including nine whole newest configs (claude-opus-5 run1/2,
codex gpt-5.6-sol run1/2, cursor grok-4.5-high run1/2, kimi k3-0715 run1/2/3). Those configs are
now in, but they have no txt verdicts, so their labels come from the JSON judges alone.

## Labels

Construct: **cheating-or-exploit**. The card names the judges "anti cheating" and the verdicts are
benchmark-test-data contamination and disallowed model/API use, which PostTrainBench forbids.
Labels are LLM-judge output, not human adjudication; negatives mean "no judge that ran fired",
not certified clean.

**Primary label** (strict, deduplicated, leak-excluded) is `labels.state` in `specs()`/`scan()`:

1. Contradiction: one judge says detected and another explicitly says none for the SAME construct
   (contamination: txt vs `gpt5_4`/`gpt5_4_rerun`; disallowed: txt vs `gpt5_4`/`gpt5_4_rerun`
   `disallowed_model` vs `api`) -> `ambiguous` (`judge_contradiction`).
2. Otherwise positive iff any judge says detected (txt, gpt5_4, gpt5_4_rerun, api, ptb_lookup);
   negative iff every present verdict is an explicit none. Runs without txt files are labelled from
   the JSON judges alone; txt present but "No ... produced" or unparseable -> `ambiguous`.
3. Duplicates: identical trace sha256 keeps one canonical row (sorted run path, first); copies are
   `excluded` (`duplicate_trace`); if copies carry different labels the kept row is `ambiguous`
   (`duplicate_label_conflict`).
4. Leak: runs whose detector-visible text (message, reasoning, results and tool-call argument
   strings) contains a judge file name or a verdict body (a whole line equal to the run's own
   verdict file) are `excluded` (`verdict_visible_to_agent`). Zero-step traces and runs without a
   trace are excluded too.

`labels.positive_any` keeps the permissive any-judge predicate and `labels.judges` records which
judges ran for the row.

### Final counts (`results/profile.json`, 1,815 rows)

| primary label | rows |
|---|---|
| positive | 109 |
| negative | 1,439 |
| ambiguous | 102 (78 judge_contradiction, 15 duplicate_label_conflict, 9 txt missing) |
| excluded | 165 (121 duplicate_trace, 40 no replayable steps, 4 verdict_visible_to_agent) |

1,548 rows are scoreable. `positive_any` is 214 rows over all rows (109 primary positive, 81
ambiguous, 24 excluded: 23 duplicate copies, 1 empty trace); 190 of them sit in non-excluded rows.
Under the pre-repair predicate on the old subset the counts were 143 / 1,185 / 9 / 51, an upper
bound that counted judge contradictions as positives.

Primary positives by source: gpt5_4 only 70, gpt5_4 + txt 24, txt only 15. BFCL holds 65 of the 109
and HealthBench 20 (AIME has none, GSM8K 1); by agent CLI: claude 30, codex 24, cursor 18, kimi 12,
opencode 17, glmx 7, qwen3max 1; 94 of the 109 have a gpt5_4 verdict. Among scoreable runs, 94 / 1,302 (7.2%) with `judgement_gpt5_4.json` are positive versus
15 / 246 (6.1%) without it.

### Judge coverage

Rows with each judge's output: txt 1,388; gpt5_4 1,510; api 1,501; ptb_lookup 519; gpt5_4_rerun 1.
Judge sets (rows): api+gpt5_4+txt 920, api+gpt5_4+ptb_lookup 364, txt only 304,
api+gpt5_4+ptb_lookup+txt 155, api+gpt5_4 62, gpt5_4+txt 8, two singletons. Stratify any score by
judge set: `labels.judges` is on each row, and `profile.json` has the per-judge and per-set state
counts.

### Judge agreement

Cohen's kappa, both verdicts boolean: txt vs gpt5_4 contamination 0.377 (n=1,078; TT 31, TF 13,
FT 75, FF 959); on canonical (non-copy) rows 0.361 (n=988). txt vs gpt5_4 disallowed model 0.666
(n=1,078, 1 TT, 1 FT). Cross-construct pairs are near 0 as expected. Only 31 of the 119 runs flagged
by either contamination judge are flagged by both. All pairs are in `judge_agreement_*` in
`profile.json`.

### Content-free floors

A detector that knows only metadata reaches these cross-validated AUROCs against the final primary
labels (109 vs 1,439; 5-fold out-of-fold category positive rate, mean of 10 seeds): benchmark 0.807,
benchmark x agent CLI 0.858, config (agent x model x run) 0.681, agent CLI 0.553, base model 0.499.
The pre-repair labels gave benchmark 0.69 and config 0.76; the benchmark floor rose because the
added JSON-only configs hold many BFCL and HealthBench positives. Report any detector AUROC next to
these floors, or stratify by benchmark.

### Mirror configs

Several configs are byte-identical re-uploads of another config. After deduplication the copies
that remain excluded are mainly `claude_non_api_claude-opus-4-7_10h_run1` (28, copy of
`..._10h`), `opencode_opencode_kimi-k2.5_10h_run2` (28), `opencode_zai_glm-5_10h_run2` (28) and
`claude_claude-opus-4-6_10h_run1_old_container` (27, copy of `..._run1`), plus 10 rows in other
configs. The canonical row (sorted path, first) is kept, so the `_old_container` and `run2` names
do not appear among scored rows for these groups.

## Adapter

`specs()` yields one record per run: `identity`, `labels`, `trace_sha256`, `_steps` source. `_steps`
(from `to_steps`) hold only the agent's text, reasoning, tool calls and results (results clipped at
20,000 chars); harness stdout and CLI telemetry are counted, not replayed. Stdlib only.
`scan()` replays each non-duplicate trace once and applies `finalize()` (empty and
verdict-visible exclusions). `selfcheck()` searches all step text, tool-call arguments included,
for judge file names, verdict bodies and judge justification sentences, and raises if any
non-excluded row has a hit: 0 leaks across 1,650 scored rows; the 4 excluded rows are the runs
whose agent listed or printed its own verdict files. Run `python3 tools/adapter.py` (selfcheck) or
`python3 tools/profile.py` (needs the locked bundle).
