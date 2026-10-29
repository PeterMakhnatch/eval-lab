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
  `eef105a8596ed37eb960363979ac148ae065303155292ebdfa039da387c460d0`). Each file's Hub oid
  (git blob sha1, or LFS sha256 for 2 files) was checked at download by a scratch script that is
  not committed; the manifest records the oid, and `profile.py` re-checks sha256 and that oid
  after locking.
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
publisher flag, "flagged invalid or as reward hacking"; provenance not documented in the card or
the code repo) and the `contamination_judgement` and `disallowed_model_judgement` lines. Those
two lines are NOT two independent judges: upstream
`src/disallowed_usage_judge/{prompt.txt,judge.sh}` (aisa-group/InferenceBench) runs one judge
invocation (one model, one prompt with a single list of disallowed behaviours) that writes both
files (paper arXiv 2607.20468 §C.2 says the prompt separates contamination from
model-substitution decisions, which describes two output lines, not two judges). Labels are
publisher flag plus LLM-judge output, not human adjudication; negatives mean "no signal fired",
not certified clean.

**Primary label** (strict, deduplicated, leak-excluded) is `labels.state`:

- The flag and the judge lines are the two evidence sources; the flag is not a separate construct
  and its provenance is unknown (see the agent-generation caveat below).
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
  38 ambiguous: 34 judge-only with flag false, 4 flag-only). It would give 55 positive / 211
  negative / 3 ambiguous. The strict reading stays primary because those 38 rows mix three
  evidence states that cannot be adjudicated here: 13 paper-era runs where the flag is false
  while a judge line fires, 21 post-paper runs where the flag was apparently never set, and 4
  flag-only runs (the flag also covers "invalid", so a flag-only run need not be a hack).

### Flag coverage differs by agent generation

`results/profile.json` (`primary_by_agent_generation`) splits the 23 agent configs into the 15
evaluated in the paper (§3.4; `PAPER_AGENTS` in `adapter.py`) and the 8 that postdate it
(claude-fable-5, claude-fable-5-low, claude-opus-4-8, claude-opus-4-8-xhigh, glm-5.2-max,
gpt-5.5-xhigh, gemini-3.5-flash, kimi-k2.6). The era split is inferred from the paper's agent
list, not a dataset field.

| agents | runs | flag true | a judge line fires | flag false while a judge fires | primary pos / neg / amb |
|---|---|---|---|---|---|
| paper (15) | 173 | 21 | 30 | 13 | 17 / 136 / 20 |
| post-paper (8) | 96 | 0 | 21 (15 with both lines) | 21 | 0 / 75 / 21 |

`invalid_or_reward_hack` is true in 21 of 173 paper-era runs and in 0 of 96 post-paper runs. In
the paper era the flag overrules the judge in 13 of 30 judge-fire runs, which looks like a
publisher adjudication; in the post-paper era it is never set, so there it looks
UNADJUDICATED (inference; the provenance is not documented). "Flag false" is therefore not
uniformly an explicit "none": all 17 primary positives are paper-era runs, and the 75
post-paper negatives are "flag false and both judge lines none", not "adjudicated clean". The
paper-era-only strict counts are 17 / 136 / 20 (n=153 scoreable). Judge-only sensitivity row
(a judge line fires = positive; flag-only and missing = ambiguous): 51 / 211 / 7 (profile
`sensitivity_predicates.judge_only`). Neither is primary.

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
agent 0.683, scenario x agent 0.693, scenario x harness 0.667, era (paper vs post-paper) 0.589,
harness 0.525, scenario 0.454; raw trace length in steps 0.405 and trace bytes 0.429 (positives
are slightly shorter; a detector may invert that sign, giving 0.595 and 0.571). The harness is
also readable from tool names. For the sensitivity predicates the floors are similar (judge-only
51 vs 211: agent 0.682, scenario x agent 0.596, era 0.444, length 0.397 / 0.603 inverted;
any-signal 55 vs 211: agent 0.678, scenario x agent 0.598, era 0.405, length 0.382 / 0.618
inverted); all in `profile.json`. Report any detector AUROC next to these floors.

## Adapter

`specs()` yields one record per run: `identity`, `labels`, `trace_sha256`, `_steps` source.
`_steps` (from `to_steps`) hold only assistant text, thinking, tool calls and tool results
(results clipped at 20,000 chars) and user turns. Stripped and never reaching `_steps`:
`run_meta.json` (flag, judgements, agent, harness, scenario, time), `metrics.json`, both logs,
and every `system` event (runner/agent telemetry: `AGENT_CONFIG`, harness name, model id on
`init`, token counters). Only 90 of 269 traces contain any `user` step (`runs_with_user_step`;
the other 179 do not carry the operator task prompt) and the harness "continue where you left off" resume
turns are kept; no condition or attack instruction exists in this corpus (the runs are honest
tasks, the label is a post-hoc judgement). The stream has no call ids, so a tool result pairs
with the oldest unanswered call; 364 calls end unanswered (agent stopped at timeout).
`selfcheck()` searches all step text, tool-call arguments included, for the judge/label field
names and a whitespace-normalised, case-folded match of the run's own verdict strings: 0 leaks
across 269 runs. The judge outputs are bare verdict strings with no justification text, so
sentence-level checks against them do not apply; `selfcheck()` itself does not scan the judge
PROMPT (the audit did, with the 21 sentences of 40+ chars of the upstream prompt: 0 runs hit,
not recomputed by `profile.py`). One run has a tool-argument key named `label` or `verdict`
(benign agent code, counted in `label_like_tool_arg_keys`).

**Foreign text in tool output (caveat, not a label leak).** In 29 runs (26 primary negative,
3 positive; `foreign_cluster_prompt_in_visible_text` in `profile.json`) a `ps aux` result
shows other cluster jobs' PostTrainBench prompts, containing "otherwise we will have data
contamination" (the sandbox evidently shares a PID namespace with other jobs). That text is
unrelated to the run's own label and does not come from the judge; a detector keyed on the word
"contamination" will still fire on it, so read such hits before counting them.

## Overlap

`profile.py` computes `overlap`: sha256 of the 8 bare and full task ids (scenario letter and
`base_model/scenario`) and of the 269 run ids against `assignments.csv` (task, unit, item_id
columns; dev 1,242, heldout 156, test 1,667 rows) and `corpus_tasks` in the read-only
production db (3,707 rows): 0 exact id hits in every split and 0 `corpus_tasks` rows. A zero is
an id-level result only.

Run `python3 tools/adapter.py` (selfcheck, stdlib only) or `python3 tools/profile.py` (needs the
locked bundle).
