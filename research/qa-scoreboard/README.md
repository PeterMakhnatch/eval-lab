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
  Category-match counts appear in row notes, distinct from root-cause attribution.
  Corpus-only CheatBench and staged MiMo pass-correctness measurements remain in
  their experiment reports rather than using unregistered scoreboard keys.
- `evallab qa suite` compares explicitly version-proved retained findings
  against every registered key's frozen native splits. It atomically replaces
  `suite-<date>.md`, **does not append to the CSV**, and runs no new benchmark.
- `research/experiments/live-watch-paired-dev/run.py` replays the shipped
  `INTEGRITY_BOARD_RULES` policy on `trace-paired-v1` **dev only**. It emits
  shared findings plus per-rule step references without fitting rules, invoking
  models, or calling the stop actuator. The retained run is
  [`live_watch_paired_dev/2026-10-19/`](live_watch_paired_dev/2026-10-19/).

## Frozen retained-version suite

Use `evallab qa suite --agent task|trace|breaker|tripwire --version <exact-producer>`.
Repeat `--version` to compare retained versions; absent evidence stays absent.
The default report is `research/qa-scoreboard/suite-<UTC-date>.md`; use
`--date YYYY-MM-DD` for a fixed reporting date and `--output <path>` for an
independent destination. Existing reports are replaced, not appended, only
after every input and split/exposure guard succeeds.

For example, save the following manifest as
`research/qa-scoreboard/retained-breaker-runs.json`. The relative paths below
are relative to **that manifest**, not the command's working directory:

```json
{
  "runs": [
    {
      "agent": "breaker",
      "version": "cheat-ladder-v1.4.0",
      "key": "breaker-bench-v1",
      "findings": "../experiments/breaker-bench-ladder-floor/findings/ladder-v1.4.0"
    },
    {
      "agent": "breaker",
      "version": "cheat-ladder-v3.0.0",
      "key": "breaker-bench-v1",
      "findings": "../experiments/breaker-bench-ladder-floor/findings/ladder-v3.0.0"
    }
  ]
}
```

```sh
uv run evallab qa suite --agent breaker \
  --version cheat-ladder-v1.4.0 --version cheat-ladder-v3.0.0 \
  --runs research/qa-scoreboard/retained-breaker-runs.json \
  --date 2026-10-19 --output /private/tmp/retained-breaker-suite.md --json
```

These retained floor sources cover only their actually measured dev subjects;
listing all keys/splits does not execute the unmeasured cells. The version
identity must be proved by each raw `qa.finding/v1` record's `reviewer.agent`.
`cheat-ladder-3.0.0` (without `v`) is a different identity, not an alias of
`cheat-ladder-v3.0.0`. Point manifests at a precise `findings/` directory or
raw-finding JSONL, not a broad run root containing other producers' findings.
An explicit binding replaces key-local discovery for that cell and cannot
relabel or combine alternative same-version runs. Optional `run` and `notes`
are metadata only. Without a binding, only the key's `findings*.jsonl` and
`findings/` are inspected, and unversioned producers remain unavailable.
The full manifest/producer rules are in [`docs/qa-contract.md`](../../docs/qa-contract.md#frozen-version-comparisons-qa-suite).

Read a suite row with its denominators and custody receipt:

- One row is **version × registered key × native split**, `category=all`;
  keys and labels are never pooled into an overall score. Mixed keys retain
  their complete frozen item count and state the agent-applicable count.
- Recall and false alarms use measured positive and negative labels only.
  Unknown labels are shown separately; a scoped nonflag is not a certified
  clean negative. Pairs require both sides measured, otherwise they do not
  enter discrimination. An empty denominator is `n/a`, never zero.
- `no-run`, `inapplicable`, source-present-but-`unmeasured`, unknown decisions,
  explicit not-run, unusable runs and absent per-item evidence remain distinct.
  Package findings need exact task id/digest; trace findings need exact
  task/trial ids. A similarly named package or trial cannot fill a cell.
- Recorded reviewer USD is attributable only to matching raw evidence in that
  version/key/split. Missing raw cost fields are `n/a`; a partially known
  charge has a separately labelled known subtotal. Per-measured item cost
  requires recorded cost for every matched measured finding. Explicit raw
  `$0` is preserved but may mean publication cost, not a free historical
  audit. **New re-score cost is $0; total historical cost remains unknown.**
- Frozen assignment/exposure checks run before publication. A claimed
  exclusion must actually exclude those item ids. Disclosed exposures and
  source-derived labels retain their caveats: this is retrospective label
  agreement, not a fresh blind test, independent cross-key replication,
  repaired-grader qualification or operational cheating verdict.
- `--json` includes full rows and SHA256 receipts for raw sources, key/pair
  files, the frozen split/exposure files and the manifest. It does not export
  the shared CSV scorer's default-zero legacy cost field; nullable suite
  cost fields retain the recordedness distinction.

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
