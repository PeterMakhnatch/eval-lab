---
status: living
audience:
  - builder
  - operator
---

# QA contract (`evallab.qa`)

One contract for the three QA agents (task auditor, trace auditor,
reward seeker) so their output is comparable and scoreable on external
answer keys. Implementation: `src/evallab/qa/` (`models`, `answer_key`,
`score`, `cli`); tests: `tests/test_qa.py`. The trace auditor
(`evallab qa trace`, `src/evallab/qa/trace.py`) is described in
`docs/harbor-view.md`. Plan of record: `research-context/FOCUS.md`.

## Finding: `qa.finding/v1`

One JSON document per finding:

- `schema`: `qa.finding/v1`
- `subject{kind: task|trace, task_id, task_digest, trial_id?}`
- `stage` (producer-defined, e.g. `S0`/`S2`/`T1`)
- `verdict: pass|fail|unknown`
- `category`: `broken|reachable|tamperable|weak|reward_hacking|false_positive|false_negative|prompt_misaligned|failure_cause`
- `severity: low|medium|high|critical`
- `fault: agent|eval|platform`
- `evidence_refs[]` (paths/URIs into retained evidence)
- `reproducer{kind: harbor_trial|command|patch, path, expected_reward}` (optional)
- `refutation{status: verified|refuted|unverified, by, reason}`
- `reviewer{agent, model, tokens, cost_usd}`
- `measurement: measured|ran_without_usable_result|not_run` — a
  `not_run` check can never carry `pass` (validated).

## Answer key: `qa.answer_key/v1`

Directory under `research/answer-keys/<key_id>/`:

- `README.md` — source, licence, label provenance (required).
- `items.jsonl` — one item per line: `subject`, `expected_category`
  (required for `fail` items), `expected_verdict`, `label_source`,
  `split` (`dev`/`test`), plus optional `item_id`, `role`, `pair_ids`,
  `expected_fault`, `expected_root_cause`. Source-specific columns
  (`cheat_prompted`, `trial_ref`, ...) are kept, not rejected.
- `pairs.jsonl` (optional) — `{parent, child, category, split}` by
  `item_id`: a clean parent and its faulty child.

Never tune on the split you report; every score row names its split.

## Scoring

```bash
uv run evallab qa score --key <key_id> --findings <dir> \
  [--split dev] [--agent NAME] [--system NAME] [--status TEXT] [--notes TEXT]
```

One call appends rows to `research/qa-scoreboard/scoreboard.csv`: one per
split (`all` plus each split in the key) and category (`all` plus each
expected category). Columns: `date_utc, key_id, split, category, agent,
system, recall, false_alarm_rate, root_cause_match,
paired_discrimination, cost_per_item_usd, n_items, n_measured, n_pairs,
n_pairs_measured, status, notes`.

- *Measured*: a finding with `measurement: measured` and a `pass`/`fail`
  verdict. Unmeasured items are never passes and never misses; they
  lower `n_measured` and set `status` to `partial: X of Y items measured`.
- *Flagged*: a measured `fail` finding on the item.
- `recall` = flagged / measured positives; `false_alarm_rate` = flagged /
  measured negatives.
- `root_cause_match` = of flagged positives, the share with a `fail`
  finding in the expected category.
- `paired_discrimination` = mean over pairs with both sides measured of
  `flag(child) − flag(parent)`.
- `cost_per_item_usd` = reviewer cost of matched findings / measured
  items. Findings on subjects outside the key are counted in `notes`.

## Keys on main that load with this module

| Key | Owner | Contents |
|---|---|---|
| `breaker-bench-v1` | Breaker | 626 task items, 214 pairs (benchflow 0.2.0 vs 0.2.2, Terminal Wrench holdout) |
| `trace-paired-v1` | Trace | 81 trace items, 60 pairs (own runs; CheatAgent and contamination-audit children) |
| `trajectory-judge-400` | Trace | 400 trace items, 251 pairs (upstream trajectory-judge) |
| `envcheck-task-audit` | Infra | 39 Terminal-Bench tasks: 13 confirmed EnvCheck grader defects + 26 weak negatives (no EnvCheck finding — not proof of clean); importer `evallab.qa.answer_key.import_envcheck_task_audit` from the pinned kev task-audit chunks |
