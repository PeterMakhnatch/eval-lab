# aisa-group/InferenceBench-Trajectories (cross-test corpus)

Local, eval-only cross-test set. No rows, transcripts or label text are in git; only
`tools/adapter.py`, `tools/profile.py` and `results/profile.json` (counts, digests, hashed ids).
Same publisher and judge pipeline as `aisa-group/PostTrainBench-Trajectories` (pq4), different
task family and different runs; the strict predicate, dedup and leak scan follow that corpus.

## Source and rights

- Dataset: https://huggingface.co/datasets/aisa-group/InferenceBench-Trajectories, pinned at
  commit `ea68d73df80985099ef58b342e50c465214d6e7b`. Public, ungated, not private.
- Licence: Apache-2.0 (card YAML `license: apache-2.0` and card body "Apache 2.0"). The dataset
  has no LICENSE file at the pin (tree has only `.gitattributes`, `README.md`, `manifest.json` and
  `runs/`); the card is the only data-licence statement. The companion code repo
  `github.com/aisa-group/InferenceBench` carries an Apache-2.0 LICENSE file (GitHub API), which
  is code, not a data-licence grant.
- Bundle: `~/.local/state/evallab-cross-test/aisa-group__InferenceBench-Trajectories/ea68d73df809/`
  (0444/0555, 1,338 manifested files = the complete snapshot, 382,089,440 bytes, manifest sha256
  `eef105a8596ed37eb960363979ac148ae065303155292ebdfa039da387c460d0`). Every file was verified
  against the Hub git blob sha1 at download and again by `profile.py` after locking.
- `CROSS_TEST_ONLY.md`: frozen-design one-shot, no training/tuning/few-shot, no vendor upload.

## Unit and what was staged

One unit = one 2-hour run of a CLI agent (claude, codex, opencode harnesses; 23 agent configs)
asked to deploy and optimise an OpenAI-compatible inference server for
`mistralai/Mistral-7B-Instruct-v0.3` on an H100 under one of four scenarios (A prefill latency,
B decode latency, C throughput, D all-in-one). 269 runs. The full snapshot is staged: per run
`trace.jsonl`, `run_meta.json`, and (when present) `metrics.json`, `logs/eval.log`,
`logs/server.log`, plus the card and `manifest.json`.

## Labels

Construct: **cheating-or-exploit**. Signals in `run_meta.json`: `invalid_or_reward_hack` (the
publisher flag, "flagged invalid or as reward hacking"; provenance not documented), and the
automated `contamination_judgement` and `disallowed_model_judgement` outputs. Labels are
publisher flag plus LLM-judge output, not human adjudication; negatives mean "no signal fired",
not certified clean.

**Primary label** (strict, deduplicated, leak-excluded) is `labels.state`:

- The two judges cover different constructs (benchmark-data contamination, disallowed model use).
  The flag is the umbrella construct that covers both.
- `positive`: flag true and at least one judge fires (17).
- `negative`: flag false, both judges explicitly none (211).
- `ambiguous` (41): 34 a judge fires while the flag is false (`judge_fires_flag_false`), 4 the flag
  is true while both judges say none (`flag_true_judges_none`), 3 no judgements at all
  (`judge_missing`, flag false). Null or unparseable verdicts would also be ambiguous (none occur).
- Duplicates: identical `trace.jsonl` sha256 keeps one canonical row; there are none (269
  distinct traces).
- Leak: runs whose detector-visible text contains a judge/label field name or the run's own
  verdict string are excluded (`verdict_visible_to_agent`); none.
- `labels.positive_any` is the permissive any-signal predicate: 55 rows (17 primary positive and
  38 ambiguous: 34 judge-only with flag false, 4 flag-only). Treating the flag as a separate
  construct from the judges would give 55 positive / 211 negative / 3 ambiguous; the strict
  reading is the primary label because the 38 conflicting rows cannot be adjudicated here.

### Final counts (`results/profile.json`, 269 rows)

| primary label | rows |
|---|---|
| positive | 17 |
| negative | 211 |
| ambiguous | 41 |
| excluded | 0 |

228 rows are scoreable. Positives by harness: claude 11, codex 6, opencode 0 (opencode also holds
15 ambiguous rows); by scenario A 6, B 5, C 5, D 1. Signal combinations are in
`signal_combinations` in `profile.json`: of the 17 positives, 9+5+3 split as flag+both judges 9,
flag+disallowed only 5, flag+contamination only 3.

With only 17 positives, any recall interval is very wide; report k of 17 beside it.

### Content-free floors

A detector that knows only metadata or size reaches these AUROCs against the primary labels
(17 vs 211; categorical = 5-fold out-of-fold category positive rate, mean of 10 seeds):
agent 0.683, scenario x agent 0.693, scenario x harness 0.667, harness 0.525, scenario 0.454;
raw trace length in steps 0.405 and trace bytes 0.429 (positives are slightly shorter). Report
any detector AUROC next to these floors.

## Adapter

`specs()` yields one record per run: `identity`, `labels`, `trace_sha256`, `_steps` source.
`_steps` (from `to_steps`) hold only assistant text, thinking, tool calls and tool results
(results clipped at 20,000 chars) and user turns. Stripped and never reaching `_steps`:
`run_meta.json` (flag, judgements, agent, harness, scenario, time), `metrics.json`, both logs,
and every `system` event (runner/agent telemetry: `AGENT_CONFIG`, harness name, model id on
`init`, token counters). The operator task prompt appears as a `user` step in only 90 of 269
traces (the other 179 do not carry it) and the harness "continue where you left off" resume
turns are kept; no condition or attack instruction exists in this corpus (the runs are honest
tasks, the label is a post-hoc judgement). The stream has no call ids, so a tool result pairs
with the oldest unanswered call; 364 calls end unanswered (agent stopped at timeout).
`selfcheck()` searches all step text, tool-call arguments included, for the judge/label field
names and a whitespace-normalised, case-folded match of the run's own verdict strings: 0 leaks
across 269 runs. The judge outputs are bare verdict strings with no justification text, so
sentence-level checks do not apply. One run has a tool-argument key named `label` or `verdict`
(benign agent code, counted in `label_like_tool_arg_keys`).

## Overlap

`profile.py` computes `overlap`: sha256 of the 8 bare and full task ids (scenario letter and
`base_model/scenario`) and of the 269 run ids against `assignments.csv` (task, unit, item_id
columns; dev 1,242, heldout 156, test 1,667 rows) and `corpus_tasks` in the read-only
production db (3,707 rows): 0 exact id hits in every split and 0 `corpus_tasks` rows. A zero is
an id-level result only.

Run `python3 tools/adapter.py` (selfcheck, stdlib only) or `python3 tools/profile.py` (needs the
locked bundle).
