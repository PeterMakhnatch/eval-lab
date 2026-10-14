# QA scoreboard

`scoreboard.csv` has one row per (key, system, split, category). The keys are the
`qa.answer_key/v1` keys under `research/answer-keys/`.

| Column | Meaning |
|---|---|
| `recall` | Flagged share of measured items with `expected_verdict: fail` |
| `false_alarm_rate` | Flagged share of measured items with `expected_verdict: pass` |
| `root_cause_match` | Share of flagged faults whose stated cause matches `expected_root_cause` |
| `paired_discrimination` | Mean of flag(child) − flag(parent) over pairs where both members were measured |
| `cost_per_item_usd` | Spend per item |
| `n_items`, `n_measured` | Items in the slice, and how many of them have a decision |
| `n_pairs`, `n_pairs_measured` | Pairs in the slice, and how many have a decision for both members |
| `status` | What was measured |

- An unmeasured item counts in `n_items` but never in a rate. "Not measured" is never
  "pass".
- `category` = `all` covers every item in the split. A per-category row covers that
  category's pairs: their children and parents.
- Rows say which split they are on. Nothing here was tuned on `test`.

Writers so far:
- `combined_v1_baseline.py` writes the $0 combined_v1 baseline on `trace-paired-v1`
  and `trajectory-judge-400`, plus upstream reference rows for the latter. Its
  per-item decisions go to `combined_v1_baseline/<key>.jsonl`.
- `evallab qa score` (planned, Infra, `src/evallab/qa/`) should write the same
  columns.
