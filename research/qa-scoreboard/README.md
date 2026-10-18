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
- Rows say which split they are on. If a row's system was tuned or developed on items in
  the split it reports, `research/answer-keys/splits/exposure.jsonl` discloses it
  (`acknowledged_rows`), and `tests/test_answer_key_splits.py` fails on undisclosed
  ones. Examples: `combined_v1` on `trace-paired-v1`, and the qa-trace/5 fusion fix.

Writers so far:
- `combined_v1_baseline.py` writes the $0 combined_v1 baseline on `trace-paired-v1`
  and `trajectory-judge-400`, plus upstream reference rows for the latter. Its
  per-item decisions go to `combined_v1_baseline/<key>.jsonl`.
- `evallab qa score` appends findings-based rows using the shared contract in
  `src/evallab/qa/`. It does not execute the task or independently validate its
  labels.

## Retrospective MiMo audit publication

The `qa-task/mimo-v7-audit` and `qa-task/mimo-sample100-strict` systems publish
the retained October 17–18 audit evidence, not a new blinded benchmark run.
Their source-derived positive labels and output inspection are disclosed in
`research/answer-keys/splits/exposure.jsonl`. Read each key's README and
`audit.json` for package bindings, measured evidence, and limitations.

Rebuild the committed findings from their compact source inventories:

```sh
uv run python research/answer-keys/mimo-heldout-v6-to-v7/build.py
uv run python research/answer-keys/mimo-stratified-sample100/build.py
```

Then exercise the actual score consumer, supplying a **new output path**
because `qa score` appends rather than replacing rows:

```sh
uv run evallab qa score --key mimo-heldout-v6-to-v7 \
  --findings research/answer-keys/mimo-heldout-v6-to-v7/findings \
  --split heldout --agent "QA Task" --system qa-task/mimo-v7-audit \
  --status "retrospective source reconciliation; not independent recall" \
  --scoreboard /private/tmp/mimo-heldout-scoreboard.csv
uv run evallab qa score --key mimo-stratified-sample100 \
  --findings research/answer-keys/mimo-stratified-sample100/findings \
  --split all --agent "QA Task" --system qa-task/mimo-sample100-strict \
  --status "retrospective source reconciliation; not independent recall" \
  --scoreboard /private/tmp/mimo-sample100-scoreboard.csv
```

The published CSV notes carry the descriptive weak-test rates, atlas agreement,
historical reservations, and scientific caveats. A recall of `1.0` here denotes
rediscovery/reconciliation of source-derived positive labels, not independent
detection performance. Unknown/nonpositive labels do not supply a false-alarm
denominator; `n/a` is intentional. A scoped nonflag does not certify a clean
task. The numeric zero in `cost_per_item_usd` means no newly attributed charge
for publishing these retained findings, not zero historical audit/model cost.
The audit rows neither qualify a repaired grader nor flip `scoring_allowed`.
