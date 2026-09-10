# Task 2 — pass@k vs pass^k per task (vendored Vestige)

Date: 2026-09-06. Functions: `vestige.analysis.variance.pass_at_k` /
`pass_hat_k` (vendored, run under ephemeral `uv run --with scipy pandas numpy`;
no repo env change). CI: vendored `wilson_ci`. Evidence: `evidence/s2_out.json`.
Reproduce: `uv run --with scipy --with pandas --with numpy python
/tmp/hn-track/s2_vestige.py`.

## k policy

k = 1..n_scored per task; null where k > n_scored. n<3 flagged **thin** (no
variance claim). Reward-None trials are outcome-missing: excluded from scored
denominators (null, never 0). Job-level `result.json` aggregates (18 files, no
`agent/` subdir) are not trials and are excluded — counting them would
double-count. Tasks grouped by portable task name (basename of `task_id.path`).

## Provenance caveat (load-bearing)

15 trials carry a foreign absolute task path
(`/Users/petermakhnatch/Developer/harbor-experiment-lab/...`) — run from a
different checkout on 2026-08-14. They are grouped with same-basename tasks and
split out in `by_cohort` (foreign vs local). Any pooled curve mixes checkout +
date + crash-status with agent behavior; read the cohort split, not the pool.

## Curves (denominators stated; flat curves carry no variance information)

| Task | n_trials | n_scored | c | rate [Wilson 95%] | pass@k | pass^k | Reading |
|---|---|---:|---:|---|---|---|---|
| event-summary | 9 | 6 | 6 | 1.00 [0.610, 1.0] | 1.0 ∀k≤6 | 1.0 ∀k≤6 | degenerate (all-pass): no stochasticity information |
| terminal-bench-html-js-filter | 12 | 9 | 0 | 0.00 [0.0, 0.299] | 0.0 ∀k≤9 | 0.0 ∀k≤9 | degenerate (all-fail): no stochasticity information |
| transaction-reconciliation | 12 | 8 | 5 | 0.625 [0.306, 0.863] | k1 .625, k2 .893, k3 .982, k≥4 1.0 | k1 .625, k2 .357, k3 .179, k4 .071, k5 .018, k≥6 0.0 | **only non-degenerate curve — but cohorts are perfectly confounded** (below) |
| syn-funcdag-easy | 2 | 0 | null | null (0 scored) | — | — | no curve; both trials unscored (verifier crash + launch-config crash) |
| adapted-syn-funcdag-hard | 1 | 1 | 0 | 0.0 [0.0, 0.793] | k1 0.0 | k1 0.0 | thin (n=1) |
| adapted-syn-funcdag-medium | 1 | 1 | 1 | 1.0 [0.207, 1.0] | k1 1.0 | k1 1.0 | thin (n=1) |
| adapted-task | 1 | 1 | 1 | 1.0 [0.207, 1.0] | k1 1.0 | k1 1.0 | thin (n=1) |

## The transaction-reconciliation fan — do not misread it

Pooled n=8, c=5 gives a textbook pass@k/pass^k fan. Cohort split:

| Cohort | n | c | rate |
|---|---|---:|---|
| foreign (2026-08-14, other checkout, all agent-launch crashes) | 3 | 0 | 0.00 |
| local (2026-08-15/16, this checkout, all clean passes) | 5 | 5 | 1.00 |

The separation is cohort difference (checkout + date + crash status), **not**
agent stochasticity. Within each cohort the curve is degenerate. Claiming
"the agent is flaky on transaction-reconciliation" from the pooled fan would be
false. The honest statement: task is solved 5/5 when the harness launches the
agent, failed 0/3 when it does not.

## Unestablished

- No task has n≥3 scored trials with 0<c<n under one checkout/harness: the
  stochasticity-vs-flakiness separation is currently unmeasurable in this corpus.
  Needs: ≥3 repeats of one task on one harness (the --skill spec in this folder
  is designed to produce exactly this).
- Whether foreign-checkout tasks are byte-identical to local ones (paths differ;
  content not diffed).
