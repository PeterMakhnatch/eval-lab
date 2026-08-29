---
type: protocol
topic: adjudication-and-precision-policy
author: tutor
date: 2026-08-28
status: draft-for-review
epistemic: protocol specification; zero labels collected
---

# Adjudication and precision policy

Label population is **blocked** pending real qualified human raters plus Peter's
explicit approval. This policy defines how disagreement is routed and how
precision is specified. It does not collect labels and it does not unlock
`not_hold_gold`. Downstream consumer: `HumanBaselineReport`
(`calibration-report-v1.1`). This package feeds that report's fields; it never
computes or replaces the report.

## Adjudication rule

`adjudicate_item` maps onto `AdjudicationCounts`:

| Protocol outcome | Consumer field | Resolved class |
|---|---|---|
| `majority_no_adjudication` | `majority_without_adjudication` | strict majority `class_id` among non-null labels |
| `adjudicated` | `adjudicated` | **never invented**; `resolved_class_id` stays null until a qualified human adjudicator who is not among `original_rater_ids` records a real label under authorization |
| `unresolved_hold` | `unresolved_hold` | `resolved_class_id` is null |

Strict majority is over non-null `class_id` values (one vote per `rater_id`).
`missing_reason` ratings do not vote. A tie, or any non-majority split, is not
broken by picking a class.

The adjudicator `rater_id` must not appear in `original_rater_ids`. An
adjudicator drawn from the original raters is a protocol error, not a hold.

**Unresolved items are excluded, never tiebroken.** They do not enter agreement
numerators, precision denominators, or bootstrap resamples. Exclusion is the
hold; inventing a class to force a complete matrix is forbidden.

## Precision policy

Primary statistic: **Gwet AC1 (multi-rater)**. Chance agreement uses the
declared category universe `q = 12` (frozen `traj.judge.ontology.v1` size), not
the count of observed labels:

```text
p_e = (1 / (q - 1)) * sum_k pi_k (1 - pi_k)
```

The denominator is `q - 1`. Using `q` is a known defect and must not recur.
`gwet_ac1_chance_agreement` is the single canonical helper.

Complementary statistics exist because the consumer schema requires all four:

- Krippendorff alpha (nominal)
- Fleiss kappa (multiclass)
- pairwise Cohen kappa (`PairwiseAgreement` matrix)

They do not replace Gwet AC1 as the primary statistic.

Bootstrap unit: the **logical trial** (`logical_trial_id`), never the individual
item and never `source_trial_id`. Percentile cluster bootstrap.
`PrecisionPlan.bootstrap_unit` remains the declared bootstrap field; clustering
itself is keyed by `logical_trial_id`.

The +/-0.05 figure is a **precision specification**, not a pass criterion: the
target is a 95% cluster-bootstrap CI half-width `<= 0.05` on the primary
statistic (`target_ci_half_width`). Meeting the half-width does not accept a
gold set. Failing it does not invent a threshold.

## Cluster-sampling ceiling and Kish floor

Effective sample size under cluster sampling is bounded by

```text
n_eff <= K / ICC
```

with `K` = number of logical-trial clusters (`logical_trial_id`). Helper: `required_n_eff_ceiling`.
Unequal cluster sizes further reduce information to Kish's effective cluster
count. Helper: `effective_clusters_kish`; do not reimplement.

```text
K_eff = (sum_i m_i)^2 / sum_i (m_i^2)
```

### Clustering unit

The bootstrap cluster is the `logical_trial_id`, never the item and never
`source_trial_id`. Two items that share a `logical_trial_id` are not independent
observations even when their `source_trial_id` strings differ: re-runs, re-cuts,
and renamed jobs of the same underlying trajectory collapse to one logical trial.

Duplicate trajectory items — the same `(logical_trial_id, step_index,
source_sha256)` appearing more than once — are rejected at freeze because they
inflate both $n$ and apparent $K$. A corpus containing the same trajectory step
twice may not be frozen at all.

$K_{eff}$ must be computed from `cluster_sizes_by_logical_trial`. That helper is
the only sanctioned input to `effective_clusters_kish`.

`assess_feasibility` precedence is fixed and ICC-independent on a measured
floor breach:

1. If `n_clusters_effective` is set and `< min_clusters_floor` (default 20) →
   `INFEASIBLE_INSUFFICIENT_CLUSTERS`. This wins even when
   `measured_within_trial_icc` is `None`.
2. Else if ICC is unset → `UNDETERMINED_PENDING_ICC`.
3. Else compare `required_n_eff_ceiling(n_clusters, icc)` with
   `n_items_required` → `FEASIBLE` or `INFEASIBLE`.

An ICC pilot must still precede any item-count freeze when the Kish floor
clears. `assess_feasibility` computes the verdict; it does not hardcode one.

### Measured cut (replaces the earlier K=44 example)

237 agent-step items nest inside **23 contributing trials**, one carrying
13.1% of all items (31 steps). The remaining 206 steps sit in 22 trials at
mean 9.36 steps. Cluster imbalance is severe:

```text
K_eff = 237^2 / (31^2 + 22 * 9.36^2)
      = 56169 / 2889
      ~= 19.4
```

`19.4 < 20`, so the current cut is `INFEASIBLE_INSUFFICIENT_CLUSTERS` on
measured data, independent of ICC.

Against a required `n_eff ~= 96` for a +/-0.05 half-width near `p_a ~= 0.8`,
the breakeven ICC at `K_eff = 19.4` is `19.4 / 96 ~= 0.20`. Steps that share
task, model, and context make `ICC <= 0.20` implausible; a within-trial ICC
this low would require near-independence of steps that are not independent.

Re-cutting items cannot fix this. Kish's `K_eff` is at most `K`, so perfect
rebalancing of the current 23 trials caps `K_eff` at `K = 23`. That still
fails the unblock rule below, because cluster count is manufactured only by
**new trials**, not by re-slicing steps inside the same trials.

Unblock: required contributing trials `= max(30, 96 * ICC)`, plus a per-trial
contribution cap of `<= 5%` of items:

| ICC | `96 * ICC` | required contributing trials |
|---|---|---|
| 0.2 | 19.2 | 30 |
| 0.3 | 28.8 | 30 |
| 0.4 | 38.4 | 39 |
| 0.5 | 48.0 | 48 |

`notool:early` has `n = 1`, and 90% of items are tool-bearing. Per-stratum
agreement is unreachable on this cut. Only pooled Gwet AC1 is estimable.

## Acceptance threshold is declared but unset

No published agreement floor, kappa minimum, alpha floor, or non-inferiority
margin has been verified for this ontology. A librarian audit found 20 of 31
asserted arXiv IDs were fabricated. Therefore:

- `PrecisionPlan.acceptance_threshold` is permanently `None` in this PR
- unset **never** evaluates as pass
- no code path may treat a missing threshold as a satisfied gate
- `trajectory_runtime` pin `not_hold_gold` remains fail / `class_not_enabled`;
  nothing here may flip it

This policy does not authorize label collection.
