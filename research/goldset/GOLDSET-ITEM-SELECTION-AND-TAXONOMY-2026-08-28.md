---
type: protocol
topic: goldset-item-selection-and-taxonomy
author: analyst
date: 2026-08-28
status: frozen-pending-human-raters
readiness: NOT_READY
epistemic: measured - every census figure computed from artifacts on disk
collection: trajectory-analysis
owns: [item_selection, provenance, label_taxonomy]
delegated_to_tutor: [agreement_statistic, acceptance_threshold, rater_qualification, adjudication_rule, power_argument]
package: research/goldset/labeling_package.json
package_sha256: a84810ac7e4976318db199ba98b15c091d10d3ced01cdc0fb7b10d602fe74daf
---

# Gold-Set Item Selection and Label Taxonomy

Analyst half of the 3-rater gold-set protocol. Tutor (`wK:p4`) owns the statistical
half; those parameters are present in the package as **explicitly null**, not
guessed.

**No ratings exist in this package and none can be produced by it.** The builder
has no code path that writes a rating. `RatingSlot` has no field capable of holding
a value. Ratings arrive only through a separate human-ingest path gated on a
qualified rater ID.

**No LLM judge output was read, imported, or consulted.** Judge output is not gold
and is not permitted as a substitute at any stage.

## 1. Readiness — NOT_READY, fail-closed

```
readiness  NOT_READY
blocker    QUALIFIED_RATER_POOL_TOO_SMALL: have 0, need >= 3
blocker    ITEMS_WITH_ZERO_RATINGS: 237
```

`evaluate_readiness` fails closed on all five conditions: pool below three, items
with zero ratings, items below three raters, duplicate rater IDs on one item, and
any rater ID outside the qualified pool. It cannot return `READY` while any holds.

## 2. Item universe — measured, and smaller than the row counts suggest

| Quantity | Value |
|---|---|
| `trajectory.json` files on disk | 29 |
| **Distinct trial relpaths** | **29** |
| Distinct trial *basenames* | 26 |
| **Basename collisions** | **3** |
| Agent steps (the labelable universe) | **237** |
| Trials contributing >= 1 agent step | **23** |
| Largest single-trial share | 31 / 237 = **13.1 %** |

Only agent-source steps are items. `system` and `user` steps carry no agent
decision to judge, so labelling them would measure the harness, not the agent.

### 2.1 Provenance key — basename is not an identity

Twenty-nine files collapse to twenty-six basenames. Keying items on trial basename
would silently merge three distinct trials and corrupt a frozen package in a way no
later check could detect. Items are therefore keyed on

```
(source_relpath, step_index, source_sha256)
```

with `item_id = sha256(relpath#index#sha)[:16]`. The file digest is recorded per
item, so any edit to a source trajectory invalidates its items rather than silently
changing their content.

### 2.2 Strata — label-independent by construction

Stratification uses only `has_tool_calls` and step position. It deliberately uses
**nothing resembling a label**, because stratifying on a label-correlated variable
biases the prevalence estimate it is meant to support.

| Stratum | n |
|---|---|
| `tool:late` | 141 |
| `tool:early` | 72 |
| `notool:terminal` | 23 |
| `notool:early` | **1** |

Two facts Tutor needs:

- **`notool:early` has n = 1.** No per-stratum quantity is estimable there. Either
  merge it into `notool:terminal` or exclude and report the exclusion.
- **90 % of items are tool-bearing** (213 of 237). The corpus cannot support a
  contrast between tool-using and non-tool-using steps.

### 2.3 Sampling design and weights

Two arms, recorded per item:

- `prevalence_core` — random, `sampling_weight = |universe| / core_n`, valid for
  base-rate estimation.
- `rare_cell_boost` — targeted, `sampling_weight = 0.0`, **excluded from prevalence
  arithmetic** and used only for agreement power on thin cells.

The current build takes the **entire universe** as core (`core_n = None`), so the
boost arm is empty and every weight is 1.0. That is the honest configuration at
this corpus size: there is nothing to sample from.

Seed is derived from the sha256 of the sorted candidate `item_id`s, so selection is
reproducible and **cannot be re-rolled to taste**. Verified: two runs produce a
byte-identical package, `sha256 a84810ac…`.

## 3. The clustering finding — this decides the power argument

**237 items nest inside 23 trials.** Steps within a trial share task, model, and
context, so they are not independent observations.

Any agreement interval computed treating 237 steps as independent will be **too
narrow**. The independent unit for anything generalising across trials is the
**trial**, giving effective $n \approx 23$. One trial contributes 13.1 % of all
items, so per-step statistics carry meaningful single-trial leverage.

Consequences, all Tutor's call, but the numbers are settled:

1. Krippendorff $\alpha$ over items is still the right *estimand* — raters label
   items, not trials — but its **interval must be obtained by cluster bootstrap
   resampling trials**, never by an analytic or item-level bootstrap.
2. If the power argument requires more independent units than 23, the blocker is a
   **data campaign**, not a protocol document.
3. `model_name` is absent from every `trajectory.json` top level, so per-model
   stratification requires a join to `traj_features`. Not attempted here.

## 4. Label taxonomy — grounded in what a rater can actually see

A rater sees the task instruction, all prior steps, and this step's message, tool
calls, and observation. **Every label below is answerable from exactly that.**

### 4.1 Primary label — `step_contribution`

| Value | Definition |
|---|---|
| `PROGRESS` | Moves the task closer to the stated goal: acquires information the agent lacked, or changes state in a direction the goal requires. |
| `NEUTRAL` | Neither advances nor sets back. Valid exploration, re-reading held information, no-op confirmations. |
| `HARMFUL` | Sets the task back: destroys needed state, introduces an error to be undone, or commits to an excluded path. |
| `CANNOT_JUDGE` | Not classifiable from the available context. |

`CANNOT_JUDGE` is a **first-class answer and is not penalised.** Its rate measures
protocol coverage, not rater failure. A taxonomy without this escape hatch forces
raters to guess and converts missing coverage into false agreement.

### 4.2 Orthogonal facets — kept separate deliberately

Collapsing these into the primary ordinal would destroy the distinctions the
analysis needs.

**`error_response`** — `NO_PRIOR_ERROR`, `ACKNOWLEDGED_AND_CHANGED`,
`ACKNOWLEDGED_NOT_CHANGED`, `IGNORED_PRIOR_ERROR`.

This facet is the human counterpart to the blind-retry-versus-divergence
distinction now implemented in `mcp_recovery.py`. `ACKNOWLEDGED_NOT_CHANGED` is
precisely the blind retry that a machine gate can miss when an environment event
reports recovery.

**`abstention`** — `ACTED`, `DECLINED_WITH_REASON`, `DECLINED_NO_REASON`.

Separating the two declines matters: the existing keyword-based abstention screen
cannot distinguish them, and reasoned abstention is the behaviour we want to
reward.

### 4.3 Attention check — not a gold label

`repeats_prior_action_verbatim` is **mechanically decidable** and machine ground
truth ships with each item under `machine_facts`. Rater disagreement with it
measures **rater attention**, not item ambiguity. It must never be pooled into an
agreement statistic about the taxonomy.

### 4.4 Excluded labels, with reasons

| Excluded | Reason |
|---|---|
| `step_necessity` | Requires an oracle optimal path. None exists, so any label encodes the rater's guess at optimality. |
| `step_efficiency` | Same defect, plus it presumes a cost model the instruction never states. |
| `unrecoverability` | Counterfactual — quantifies over all continuations. Blocked on a preregistered predicate with a declared false-positive rate against later success. Not a human-labelable property. |

Recording exclusions is load-bearing. `unrecoverability` is exactly the label a
protocol would reach for to unblock T1.3's $t_{lock}$, and it is exactly the one a
human cannot supply. Asking for it would produce confident labels for an
unobservable quantity.

## 5. Parameters left null — Tutor's, not guessed

```json
"unset_parameters_owned_by_tutor": {
  "agreement_statistic": null,
  "acceptance_threshold": null,
  "required_interval_width": null,
  "adjudication_rule": null,
  "rater_qualification_criteria": null
}
```

**No published floor was imported**, because none is quotable. The Librarian's
audit found 20 of 31 asserted arXiv IDs were fabricated placeholders, and the
$\kappa = 0.87/0.90/0.93$ and $\alpha = 0.78$ figures are attached to papers whose
bodies were never read. AgentProcessBench's real ID is `2603.14465` and its abstract
does carry "agreement", but body extraction is queued and undelivered.

An unset parameter is honest. A guessed threshold would be the defect this lab has
spent the week correcting.

## 6. Reproduce

```bash
python3 research/goldset/build_labeling_package.py \
  --runs-root runs \
  --out research/goldset/labeling_package.json \
  --boost-per-stratum 3
```

Expect `package_sha256 a84810ac7e4976318db199ba98b15c091d10d3ced01cdc0fb7b10d602fe74daf`
and `readiness NOT_READY`. A differing digest means the source corpus changed;
re-pin before labelling.

## 7. Blockers

**External — the only one requiring people rather than code:**

> **Three qualified independent human rater IDs per item.** Zero exist. 237 items
> await ratings. No substitute is permitted: not LLM judges, not the Analyst, not
> synthetic labels.

**Internal, pending Tutor:** the five null parameters in §5.

**Contingent:** if Tutor's power argument needs more than $n \approx 23$ independent
trials, the blocker escalates from a protocol gap to a data campaign, and this
package should be re-cut after that campaign rather than labelled now.
