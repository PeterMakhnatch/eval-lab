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
`score`, `cli`); tests: `tests/test_qa.py`. Plan of record:
`research-context/FOCUS.md`.

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
  `split`, plus optional `pair_id`/`notes`. Items sharing a `pair_id`
  with one `fail` and one `pass` form a discrimination pair.

Never tune on the split you report; every score row names its split.

## Scoring

```bash
uv run evallab qa score --key <key_id> --findings <dir> [--split dev] [--json]
```

An item is *flagged* when any finding on its subject has verdict
`fail`. Per split the score writes one row to
`research/qa-scoreboard/scoreboard.csv` with: recall (flagged
positives / positives), root-cause match (flagged with the expected
category / positives), false-alarm rate (flagged negatives /
negatives), paired discrimination (faulty flag rate − clean flag rate
over `pair_id` items), cost and tokens per item (from `reviewer`), and
unmatched-findings count (findings on subjects outside the key).

## Imported keys

| Key | Contents | Importer |
|---|---|---|
| `envcheck-task-audit` | 39 Terminal-Bench tasks: 13 confirmed EnvCheck grader defects + 26 weak negatives (no EnvCheck finding — not proof of clean) | `evallab.qa.answer_key.import_envcheck_task_audit` from the pinned kev task-audit chunks |
