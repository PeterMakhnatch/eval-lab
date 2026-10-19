# QA scoreboard

`scoreboard.csv` has one row per (key, system, split, category). The keys are the
`qa.answer_key/v1` keys under `research/answer-keys/`.

| Column | Meaning |
|---|---|
| `recall` | Detection share of `expected_verdict: fail` items; row notes identify the evidence threshold and denominator |
| `false_alarm_rate` | Detection share of `expected_verdict: pass` items; alerts on weak negatives are not certified semantic false positives |
| `root_cause_match` | Cause agreement under the writer's labelled denominator; category matching is a separate metric |
| `paired_discrimination` | Mean of flag(child) − flag(parent) over pairs where both members were measured |
| `cost_per_item_usd` | Attributed spend per item under the row's scorer; notes disclose bounds and inherited historical cost |
| `n_items`, `n_measured` | Selected items and the writer's decision/quality coverage; not necessarily new-treatment coverage |
| `n_pairs`, `n_pairs_measured` | Pairs in the slice, and how many have a decision for both members |
| `status` | What was measured |

- "Not measured" is never "pass". `evallab qa score` uses measured-only rate
  denominators; `evallab qa task-score` uses the full nonexcluded positive/negative
  cohort and reports flagged and verified evidence separately. Compare rows only
  after matching their evidence thresholds and denominators.
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
  Category-match counts appear in row notes, distinct from root-cause attribution.
  Corpus-only CheatBench and staged MiMo pass-correctness measurements remain in
  their experiment reports rather than using unregistered scoreboard keys.
- `evallab qa task-score` writes digest-bound task-audit rows. Its quality-complete
  coverage, full-cohort detection rate, category matching, and exact root-cause
  attribution are distinct quantities.
- `research/experiments/live-watch-paired-dev/run.py` replays the shipped
  `INTEGRITY_BOARD_RULES` policy on `trace-paired-v1` **dev only**. It emits
  shared findings plus per-rule step references without fitting rules, invoking
  models, or calling the stop actuator. The retained run is
  [`live_watch_paired_dev/2026-10-19/`](live_watch_paired_dev/2026-10-19/).
- `research/experiments/transfer-matrix-20261019/run.py --scoreboard` writes the
  `action-lr@<sha>/train=<key>` rows: an L2 logistic regression over
  `evallab.action_features`, fitted on one key's dev split and scored on every
  registered key's test split at a threshold frozen at 5% dev false alarms. Rows
  cover the actions-only views; AUROC and recall at 2%/5% test false alarms with
  cluster-bootstrap intervals are in the row notes and in that directory's
  `report.md`. Zero model calls.

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

## Reproducing the live-watch dev diagnostic

Use a new output directory; the runner does not overwrite an existing run:

```sh
uv run python research/experiments/live-watch-paired-dev/run.py --out /private/tmp/watch-paired-dev
uv run evallab qa score --key trace-paired-v1 --findings /private/tmp/watch-paired-dev \
  --split dev --agent "TA monitor" --system "live-watch/integrity-board@$(git rev-parse --short HEAD)" \
  --scoreboard /private/tmp/watch-paired-dev-scoreboard.csv
```

These are retrospective candidate-alert label-agreement rows, not a held-out
result or verified cheating verdicts. A measured nonflag is scoped to the
integrity-board policy, not a clean-trace certificate. Missing completed ATIFs,
including oracle and scripted CheatAgent trials, remain unknown. Other
operational/attempt-only alerts stay in `observations.jsonl` but do not become
integrity findings. Root-cause attribution is unmeasured.

## Ordinary-program S2a held-out comparison (2026-10-19)

`qa-task@s2a-ordinary-availability-verified` publishes the frozen historical S0–S3
findings **union** newly qualified ordinary-program findings, not a full new audit
or stand-alone seeker recall. The historical baseline already used verifier-aware
K≤3. [The evidence index](s2a-ordinary-heldout-20261019.json) pins both original
archives, frozen keys, per-case outcomes, executed counterexamples and costs.

The new search made at most three physical model calls per eligible task,
including failed calls. Of 67 nonexcluded held-out members, 38 met the declared
safety/interface/resource scope and 12 received a native candidate trial.
Six candidates passed genuinely fresh S3; only three also had an independent
task-contract counterexample and three distinct passing oracle plus three
distinct failing nop controls. Fixed-input hardcoding alone did not qualify.
The RStan candidate's required script was observably missing, but its reference
controls failed on an unavailable pinned curl dependency, so it was not promoted.

Neither key gained a labelled-positive hit. The two newly detected weak-negative
defects are real ordinary-program counterexamples, not grounds to relabel the
frozen key or call its label-negative alerts semantically false. Unexecuted,
abstained, rejected and control-unqualified cases do not certify clean graders.
`n_measured` retains historical baseline quality coverage, not new seeker coverage.

The OSS-to-Z.AI continuation was explicitly registered after an HTTP429 and
earlier outputs. Runtime corrections are retained. This is exploratory,
post-exposure evidence, not a blinded or prompt-only causal comparison.
The CSV cost includes historical baseline charges plus new reported charges
and the retained upper bound for one unpriced failed request; maximum reservations
are not an observed bill.

The retained raw bundle contains the frozen keys and reconciled metadata.
For example, this exercises the scorer without running another model or trial:

```sh
RAW="$PWD/runs/.qa-task/pj1-s2a-heldout-20261019"
uv run --no-sync evallab qa task-score \
  --key "$RAW/frozen-answer-keys/delphik-open-defect-tb2" \
  --findings "$RAW/reconciliation-final/union/delphik-open-defect-tb2" \
  --split heldout --exclude-item terminal-bench/fix-git
```

