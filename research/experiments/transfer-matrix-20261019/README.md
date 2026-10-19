# Transfer matrix: $0 action-sequence detectors across five trace keys

Question: without the agent's reasoning, how far do deterministic tripwires and
an action-sequence model carry from one labelled trace key to another?

- Code: `src/evallab/action_features.py` (extractor), `run.py` (arms, matrix,
  bootstrap), `report.py` (tables + feature verdicts). Generated tables:
  [`report.md`](report.md); numbers: `results.json`; per-item features, rule
  hits, scores and step evidence: `predictions.jsonl`.
- Cost: **0 model calls, $0**, no submitted code executed, no containers.
- Reproduce (needs the published views under the results home):
  `uv run python research/experiments/transfer-matrix-20261019/run.py && uv run python research/experiments/transfer-matrix-20261019/report.py`.

## Inputs

| key | dev (train) | test | view | positive means |
|---|---|---|---|---|
| `cheatbench-actions-v1` | 150 (75 pos) | 150 (75 pos) | published actions-only (PR #1083) | source-labelled cheating; **prior-exposed** (combined_v1 calibration) |
| `hacktrace-act-v1` | 150 (75) | 150 (75) | published actions-only (PR #1083) | source ACT label (prompted shortcut) |
| `trajectory-judge-400` | 212 (162) | 188 (138) | dev projected in memory; test published (PR #1087) | any injected actor fault |
| `trace-paired-v1` | 25 of 54 with ATIF (18) | 11 of 27 available (8) | dev projected in memory; test published (PR #1087) | reward-hacking child or honest wrong fix |
| `mimo-v7-canonical-pass70` | none | 70 (6) | published (PR #1087) | wrong submission among v6 passes |

Dev splits without a published view use the same projection: actor step fields
only, `blind_atif`, agent prose and reasoning removed. The extractor reads only
tool names, arguments and observations, so full and actions views give identical
features (tested).

## Arms

- **(a) Tripwires:** the frozen evidence-only OR from
  `research/answer-keys/external_traces.py` (unchanged; recomputed predictions
  match the committed CheatBench/HackTrace baselines on all 600 items). A
  diagnostic variant renames harness shell tools (`execute_bash`, …) to `bash`
  first, because the shared shell-name set does not include them.
- **(b) Logistic regression:** L2 (λ = 1), train-standardized, 26 features in 9
  families; constant-on-train columns dropped. Fitted on one key's dev split,
  scored on every test split. Reported: AUROC; recall at 2% and 5% false alarms
  with the threshold taken from the test split's own negatives; and recall/false
  alarms at a threshold frozen at 5% dev false alarms.
- **(c) Rule list chosen on dev only:** OR of every feature whose dev AUROC
  interval lies above 0.5 on two or more dev keys and below 0.5 on none. The
  selection picked `history_cmds` and `tripwire_any`.

Intervals: 2,000 (matrix) or 500 (univariate/strata) cluster-bootstrap
resamples, percentile 95%; cluster = problem/task id (CheatBench: item, because
its task id is one of two benchmark families). Wilson intervals for fixed-rule
counts. Missing opportunities are `None` (e.g. no source edit means no
"tests read before first source edit" value) and a family with no opportunity in
a key is reported **N/A**.

## Disclosures

- Before writing the extractor, the tool-call sequences of three positive
  CheatBench SWE-bench-verified **test** items were read (they showed
  `git log`/`git show`), and an aggregate HackTrace written-path × label table
  over both splits was printed. A smoke pass also printed per-label feature means
  for every split before the grader-path role fix (`solution.py` is a submission,
  not grader material). These are recorded in
  `research/answer-keys/splits/exposure.jsonl`
  (`action-features-transfer-design-inspection-20261019`). The history-command
  pattern therefore cannot be treated as blind to CheatBench test.
- The transfer rule for features was written before test numbers were read; the
  split of the non-transferring bucket into finer labels was made afterwards.
- CheatBench is prior-exposed, so none of its rows are a fresh hold-out.
- The paired test subset (8/3) and MiMo positives (6) are too small for stable
  2%/5% operating points; their intervals span most of [0, 1].
