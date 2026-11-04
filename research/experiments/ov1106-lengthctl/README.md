# Frozen length-controlled scoreboard

`SPEC.md` freezes the analysis plan at preregistration commit `9c66cb3b7`.
`scoreboard.py` reads retained scores. It does not run detectors, judges, models, or containers.
It writes aggregate JSON and a Markdown table to an explicit output directory.
Keep raw scores and normalized views outside Git.

## Inputs and joins

Default inputs are external retained artifacts:

| Universe | Runs | Signals |
|---|---|---|
| Original | `/private/tmp/ov1104/shared/detbench/runs.jsonl` | `/private/tmp/ov1104/shared/detbench/signals/` |
| MALT | `/private/tmp/ov1105/malt/detbench-frozen/runs.jsonl` | `/private/tmp/ov1105/malt/scores/signals/` |
| HackBench | `/private/tmp/ov1105/hackbench/evaluation/sample-runs.jsonl` | `/private/tmp/ov1105/hackbench/evaluation/signals/` |

The reader requires 349 original files, 35 MALT files, and 35 HackBench files.
It reads 18 combination definitions from `/private/tmp/ov1104/detbench/matrix.json`.
It rejects duplicate keys, unknown score keys, invalid numeric scores, and unexplained null scores.
It joins opaque `run_key` values without parsing their native identifiers.
It preserves source versions, file hashes, numeric diagnostics, explicit abstentions, and absent rows.

The complete original Terra file is `j3_actions_task_diff_terra_full`.
Extensions use `j3_actions_task_diff_terra` and calibrate against the complete original file.
Both names remain visible. They are aliases, not independent experiments.

## Statistical controls

- The logistic length model fits all 690 original DEV rows before evaluation filters apply.
- Signal thresholds use retained matrix DEV thresholds when available. Other thresholds use original DEV scores only.
- Each signal's character, step, and model comparisons use exactly its numeric scored rows.
- Character deciles use each full set before signal matching or natural-only selection.
- Original DEV and held-out pools retain their historical membership. The held-out pool excludes decisive-rule and `pool=false` sets.
- MALT and HackBench remain separate. Natural populations include only `natural=true`.
- All selected signal/population pairs appear, including empty or unsupported pairs.
- Defaults are 2,000 task-cluster bootstrap draws, seed 1106, and at most 4 worker processes.
- Identical ordered row masks, numeric vectors, controls, thresholds, and bootstrap settings share one evaluation.

`aggregate.json` stores evaluations by content hash. Each `pairs` entry references its evaluation.
Evaluations include matched baselines, intervals, valid draw counts, eligibility counts, and fixed stratum edges.
The document also records calibration, model coefficients, task policies, source hashes, and invocation arguments.
It excludes row-level scores and task-group details. The Markdown table displays baseline AUROCs and intervals.

HackBench has no released negatives. Its output contains DEV-fixed completed-case recall and operational recall only.
Operational recall uses every selected positive as its denominator, including absent and abstained scores.
A present signal with no numeric scores has operational recall 0 when DEV calibration exists.
An unsupported signal or absent calibration has unavailable operational recall.
Operational recall uses a Wilson 95% interval. Completed-case recall uses the frozen task-bootstrap controls.

### Actual task identity

The metric reader namespaces actual task IDs by set. It does not use run keys as a task fallback.
Normalized copies prefer explicit task metadata, then retained clusters.
Goodfire instead reads task IDs from the frozen replay manifest:

`~/Developer/eval-lab-results/line/replay-v1/inputs/corpora/goodfire-manifest.jsonl`

It removes only a terminal `@task-bundle-sha256-<hex>` suffix.
It does not merge prompt variants or infer tasks from transcript text.
The retained manifest has 150 distinct task IDs, not the expected 50.
Even terminal task names have 131 distinct values. This metadata cannot establish a 50-task mapping.
Until a verified mapping exists, Goodfire and pools containing Goodfire have no task-based intervals or within-task AUROC.
Their other point estimates remain available.

`--task-map PATH` accepts a JSON object mapping exact run keys to actual task IDs.
Overrides apply to normalized copies only. Unknown keys fail.
The output records the override file hash and resulting task counts.
Do not invent a mapping to obtain a desired task count.

### Combinations and outcome correction

Combinations call the existing `ov1104-detbench/score.py:combine` implementation.
It fits percentile references, OR thresholds, and stacking weights on DEV only.
OR treats missing members as unflagged. Max uses available members.
Stacking uses rank 0 plus a missing indicator and DEV leave-one-set-out predictions.
These policies are not complete-member scoring. The output labels this limitation.
Extensions receive combinations only when all member files exist. They use original DEV references.

The published `outcome_regrade` file remains unchanged.
`outcome_regrade_original_reward` separately reads existing C1–C5 and Sol/Astra receipts.
It requires original recorded reward 1, valid recorded controls, and a scored independent binary reward.
Its flag means independent reward 0. Original failures and unavailable grades abstain.
The output records receipt hashes and the independent verifier code hash.
Missing correction receipts produce explicit unavailable pairs, not a replacement for the published signal.

## Commands

Show the CLI without reading data:

```sh
uv run python research/experiments/ov1106-lengthctl/scoreboard.py --help
```

Focused table, including J2b, both J3 names, L0 unions, outcome signals, and all retained combinations:

```sh
uv run python research/experiments/ov1106-lengthctl/scoreboard.py \
  --only focus --workers 4 --bootstrap 2000 --seed 1106 \
  --out-dir /private/tmp/ov1106/lengthctl/focused
```

Full table:

```sh
uv run python research/experiments/ov1106-lengthctl/scoreboard.py \
  --workers 4 --bootstrap 2000 --seed 1106 \
  --out-dir /private/tmp/ov1106/lengthctl/full
```

For a limited table, append `--sets 'malt-*,hackbench-test'` or another set pattern.
This filters evaluation populations only. It retains all DEV calibration rows and excludes pooled populations.
Each output directory receives `aggregate.json` and `SCOREBOARD.md`.
These commands score evaluation data. The parent owns their execution and scientific interpretation.

The parent runs behavioral tests separately:

```sh
uv run pytest research/experiments/ov1106-lengthctl/test_scoreboard.py
```
