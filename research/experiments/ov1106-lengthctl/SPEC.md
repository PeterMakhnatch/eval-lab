# Length-controlled DetBench scoring

## Frozen analysis plan

This analysis measures existing frozen signals. It does not train a new detector.
All held-out sets are evaluation-only. No new judge calls are required.

- Include all existing signal/set pairs with retained per-run scores and labels.
  Report missing inputs and coverage, including unsupported combinations.
- Preserve the existing signal direction and existing set membership.
  Do not reverse scores after observing evaluation labels.
- Compare each signal with character and step length on identical scored rows.
  Missing scores are unavailable, not negative predictions.
- Use character length pooled across each full set to define 10 quantile bins.
  Equal lengths stay together. Repeated boundaries can reduce the bin count.
  Freeze the boundaries for bootstrap resamples and signal subsets.
- Compute AUROC per eligible, mixed-class bin. Report the row-count-weighted mean,
  the eligible row count, the eligible bin count, and excluded rows.
- Compute within-task AUROC only for mixed-class tasks. Weight by eligible task
  row count. Report eligible task and row counts.
- Fit logistic label probability on log1p(character length) using DEV rows only.
  Freeze this model before evaluation. Report signal AUROC minus model AUROC on
  identical rows. This paired AUROC gain is the requested length-control alternative
  to a residual score. Do not describe it as residualisation or incremental model gain.
- Report the largest recall attainable at an empirical FPR no greater than 0.05.
  Use whole tied score groups, without interpolation or random tie breaking.
  Label this curve statistic separately from a deployable operating point.
- For positive-only HackBench, report recall using a threshold frozen on DEV
  negatives at FPR no greater than 0.05. Report no HackBench AUROC or measured FPR.
  Use only existing scores. If DEV calibration is absent, mark recall unavailable.
- Use 2,000 task-cluster bootstrap replicates and a fixed seed of 1106.
  Use 2.5th and 97.5th percentiles for 95% intervals.
  Keep each resampled task copy distinct for within-task calculations.
  Report the number of valid bootstrap replicates. Do not refit the DEV model
  or recalibrate thresholds within evaluation bootstrap replicates.
- Length-control evidence requires positive paired gain and within-length
  AUROC above 0.5. Distinguish point estimates from intervals that exclude zero
  or 0.5. These are descriptive comparisons, without multiplicity correction.
- Highlight J2b, J3, L0 union, outcome regrade, and retained combinations.
  Evaluate existing frozen combinations only; do not optimize new combinations
  with held-out labels.

## Evidence and outputs

The scoring script records source paths, hashes, join failures, and pair coverage.
Commit code, tests, and aggregates only. Keep raw transcripts outside Git.
The first computed table goes to EARLY.md in the lane directory.
The full controlled table and limitations go to SCOREBOARD.md.
RESULT.md includes receipts, 2 worked examples, and failed or unavailable analyses.
