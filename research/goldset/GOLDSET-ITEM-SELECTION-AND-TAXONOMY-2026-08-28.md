<!-- REVISION 2. An independent review found five blockers in revision 1, all
valid, all fixed at root. Two numbers I reported to Main and Tutor were WRONG:
the universe is 183 items in 20 clusters, not 237 in 23. See §0. -->

---
type: protocol
topic: goldset-item-selection-and-taxonomy
author: analyst
date: 2026-08-28
status: revision-6-after-exact-head-review
readiness: NOT_READY (6 blockers)
epistemic: measured - every census figure computed from artifacts on disk
collection: trajectory-analysis
owns: [item_selection, provenance, label_taxonomy]
delegated_to_tutor: [agreement_statistic, acceptance_threshold, rater_qualification, adjudication_rule, power_argument]
package: research/goldset/labeling_package.json
package_sha256: 77317e371b7203344f1539cf17dce19aa8b531098b34a0f305067c62699bff6f
revision: 6
blockers_fixed_from: independent review (Grok) - 5 blockers, all root-caused
---

# Gold-Set Item Selection and Label Taxonomy

Analyst half of the 3-rater gold-set protocol. Tutor (`wK:p4`) owns the statistical
half; those parameters are present in the package as **explicitly null**, not
guessed.

**No ratings exist in this package and none can be produced by it.** The builder
has no code path that writes a rating. `LabelItem` has no rating field at all.
Ratings arrive only as separate typed `RatingRecord` sidecars through a
human-ingest path gated on a qualified rater ID.

**No LLM judge output was read, imported, or consulted.** Judge output is not gold
and is not permitted as a substitute at any stage.

## 0. Revision 2 — five blockers found by independent review, all valid

Revision 1 shipped a package that **could not be labelled**. Every blocker below
was found by independent review (Grok), and all five were real.

| # | Blocker | Root cause | Fix |
|---|---|---|---|
| B1 | Rater context unlabelable | `task_instruction` was **always `None`** — no such key exists in ATIF, the instruction lives in the trailing user step. Prior steps carried only a digest with no message/arguments/observation. 69 % of agent steps have an empty message, so **126 of 183 items showed the rater nothing judgeable**. | Extract instruction from user steps; render full prior-step content |
| B2 | Machine truth leaked | I put the attention check's own answer and `prior_error_exists` **into the rater-facing context** — destroying the check and priming `error_response` | Machine truth moves to `machine_truth_WITHHELD.json`, never shipped to raters |
| B3 | Byte-identical trajectories double-counted | 29 files carry only **26 distinct sha256**; 3 shas appear at 2 paths each. Keying on relpath inflated 183 unique steps to 237 and corrupted every cluster statistic | Identity is `(source_sha256, step_index)`; relpaths become `source_aliases` |
| B4 | Ratings mutated the frozen digest; readiness never validated labels | `ratings` was a field **inside** the item, so labelling changed the package digest. Readiness counted rater IDs while ignoring whether labels existed or were in-enum | Items immutable with no rating field; typed sidecar `RatingRecord`; readiness validates enum + completeness |
| B5 | Facets lacked `CANNOT_JUDGE` | I argued at length that the escape hatch prevents false agreement, then omitted it from both facets | `CANNOT_JUDGE` on **every** human-judged field |

**Two numbers I reported to Main and Tutor were wrong.** The universe is **183
items in 20 clusters**, not 237 in 23. Both were inflated by B3. Corrected
throughout, and re-paged.

Two further corrections to revision 1's own claims, found while fixing B1:

- The task instruction **is** recoverable — it is the trailing user step before the
  agent turn. Revision 1 read a non-existent `instruction` key and silently got
  `None` for all 237 items.
- `model_name` **is** present at `doc["agent"]["model_name"]` (e.g. `gpt-5.6-terra`).
  Revision 1 claimed it was absent and that stratification needed a `traj_features`
  join. Wrong; it is in the trajectory.

## 1. Readiness — NOT_READY, fail-closed, SIX blockers

```
readiness NOT_READY
EFFECTIVE_CLUSTERS_BELOW_FLOOR: K_eff=13.33 < 20.0
CLUSTER_CONCENTRATION_TOO_HIGH: 16.9% > 5%
CONTEXT_INCOMPLETE_TOO_HIGH: 149/183 (81.4%) > 20%
QUALIFIED_RATER_POOL_TOO_SMALL: have 0, need >= 3
ITEMS_WITH_ZERO_VALID_RATINGS: 183
REGISTRY: REGISTRY_ABSENT
```

**Recruiting raters clears only two of these.** The cluster, context and registry
blockers are independent of rater supply — see §7 for the required order.

`evaluate_readiness` fails closed on every condition and cannot return `READY`
while any holds. It validates label presence and enum membership, not merely the
presence of a rater key ID, and it rejects duplicate or conflicting submissions
from one `(item, rater)` rather than collapsing them into a set.

## 2. Item universe — measured, deduplicated

| Quantity | Value |
|---|---|
| `trajectory.json` files seen | 29 |
| **Distinct content digests** | **26** |
| **Duplicate paths dropped** | **3** |
| **Unique agent steps (the universe)** | **183** |
| **Clusters contributing >= 1 agent step** | **20** |
| Items with a task statement | **183 / 183** |
| Items with an empty message | 126 (**68.9 %**) |

Only agent-source steps are items. `system` and `user` steps carry no agent
decision to judge, so labelling them would measure the harness, not the agent.

**68.9 % of items have an empty message.** Those are tool-call-only steps, and they
are judgeable *only* because the package now renders tool arguments and
observations in full for both the item and every prior step. Revision 1 rendered
neither for prior steps, which is why B1 was fatal rather than cosmetic.

### 2.1 Provenance key — basename is not an identity

Twenty-nine files collapse to twenty-six basenames. Keying items on trial basename
would silently merge three distinct trials and corrupt a frozen package in a way no
later check could detect. Items are therefore keyed on

```
(source_sha256, step_index)
```

with `item_id = sha256(source_sha256#step_index)[:16]`. Identity is **content-
addressed**: relpaths are recorded as `source_aliases` and in the top-level alias
manifest, never as identity. Any edit to a source trajectory changes its digest and
therefore invalidates its items, rather than silently changing their content.

Revision 1 keyed on `(source_relpath, step_index, source_sha256)`. Including the
relpath made byte-identical trajectories at two paths into distinct items, which is
what inflated 183 to 237.

### 2.2 Strata — label-independent by construction

Stratification uses only `has_tool_calls` and step position. It deliberately uses
**nothing resembling a label**, because stratifying on a label-correlated variable
biases the prevalence estimate it is meant to support.

| Stratum | n |
|---|---|
| `tool:late` | 112 |
| `tool:early` | 50 |
| `notool:terminal` | 20 |
| `notool:early` | 1 |

Two facts Tutor needs:

- **`notool:early` has n = 1.** No per-stratum quantity is estimable there. Either
  merge it into `notool:terminal` or exclude and report the exclusion.
- **90 % of items are tool-bearing** (of 183). The corpus cannot support a
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
byte-identical package, `sha256 77317e37…`.

### 2.3 Alias manifest — the dedup is auditable

`census.alias_manifest` maps every content digest to its canonical relpath, all
relpaths, and a duplicate count. 26 entries; duplicate counts sum to exactly the
3 dropped paths, asserted by test.

The manifest also exposes the *cause* of the duplication: three trials appear both
under a campaign prefix and at top level, e.g.

```
minimal-harbor-luna-20260824/minimal-luna-gaia2-ambiguous-amd64/gaia2-ambiguous__np3YExC/...
                             minimal-luna-gaia2-ambiguous-amd64/gaia2-ambiguous__np3YExC/...
```

A nested copy, not two distinct runs. Content addressing collapses them correctly
and the manifest records that it happened, so the dedup is checkable rather than
asserted.

### 2.4 `CANNOT_JUDGE` vs `INSUFFICIENT_CONTEXT` — different failures

Every human-judged field now offers **both**, and they must never be pooled:

| Value | Meaning | What its rate measures |
|---|---|---|
| `CANNOT_JUDGE` | Context **is** present; the step is genuinely ambiguous | **Taxonomy** ambiguity |
| `INSUFFICIENT_CONTEXT` | Context is **absent or truncated in the package** | **Package** completeness — a builder-fixable defect |

Conflating them hides package defects inside a taxonomy-ambiguity number. A rising
`CANNOT_JUDGE` rate says the label set needs work; a rising `INSUFFICIENT_CONTEXT`
rate says the builder does.

**Cross-check, and this is the point of the pair.** Each item carries
`context_completeness.builder_verdict` — `COMPLETE` or `INCOMPLETE` — declared by the
builder, not the rater. Current distribution:

```
COMPLETE 34    INCOMPLETE 149
```

**Current distribution is 34 COMPLETE /
149 INCOMPLETE — 81.4% incomplete.**
Revision 5 reported 158/25, which was false: the verdict examined only the item's
own truncation and ignored prior-step truncation entirely.

**This is now a readiness gate.** `MAX_INCOMPLETE_CONTEXT_FRACTION = 20%`;
at 81.4% the package emits
`CONTEXT_INCOMPLETE_TOO_HIGH` and refuses. A rater forced into
`INSUFFICIENT_CONTEXT` on four items in five is measuring the builder, not the
agent.

If raters mark `INSUFFICIENT_CONTEXT` on items the builder called `COMPLETE`, **the
builder missed a defect it believed it had detected.** That disagreement is a
measurement of the package, obtainable only because the two signals are separate.

### 2.5 Delivery guard — raters receive ONLY the exported bundle

The default tree co-locates `labeling_package.json` and
`machine_truth_WITHHELD.json`. **That directory must never be handed to a rater.**

Raters receive exactly one artifact, produced by:

```bash
python3 research/goldset/build_labeling_package.py \
  --runs-root runs \
  --out research/goldset/labeling_package.json \
  --machine-truth-out research/goldset/machine_truth_WITHHELD.json \
  --export-rater-bundle <clean-empty-dir>
```

`export_rater_bundle` refuses if the target directory holds anything matching
`*machine_truth*`, `*WITHHELD*`, `*truth*` or `*attention*`, and re-asserts after
writing. The bundle omits `attention_check_field`, omits every truth row, and
strips prose revealing that a withheld truth exists.

**Open follow-ups, recorded not fixed:** the build lock leaves an empty
`.goldset-build.lock` artifact, and `builder_verdict` may bias raters toward
`INSUFFICIENT_CONTEXT` on items it marks `INCOMPLETE` — a blinding question for
Tutor, since the cross-check value and the priming risk trade off directly.

## 3. Cluster adequacy — Tutor's power verdict: HOLD LABELING

**183 items nest inside 20 clusters, but the design effect is what binds.**

| Quantity | Value | Target |
|---|---|---|
| Raw clusters | 20 | — |
| **Kish $K_{\text{eff}}$** | **13.33** | $\ge$ 20 |
| Max cluster concentration | **16.9%** | $\le$ 5% |

$$K_{\text{eff}} = \frac{\left(\sum n_i\right)^2}{\sum n_i^2} = \frac{183^2}{2513} = 13.33$$

Tutor's verdict, independently reproduced here: $K_{\text{eff}} = 13.33 < 20$, and
even a perfectly balanced 20-cluster split reaches only 19.97 — so **20 raw clusters
cannot clear the floor at any concentration.** One cluster carries 31 of 183 items
(16.9 %) against a 5 % target.

**Labelling is on HOLD. A data campaign is required before raters are recruited.**
Recruiting three raters now would spend human time on a package that cannot yield a
usable interval.

### 3.1 Campaign target

- **~35–50 new distinct trajectory digests**
- **$\le$ 5 % concentration** per cluster
- **$K = \max(30,\; 96\rho)$** after an ICC pilot establishes $\rho$

### 3.2 The gate is in code, not prose

`evaluate_cluster_adequacy` computes $K_{\text{eff}}$ and concentration and emits
readiness blockers. It fails closed and is asserted by test, including that a
balanced 40-cluster design clears it. Current output:

```
blocker EFFECTIVE_CLUSTERS_BELOW_FLOOR: K_eff=13.33 < 20.0
blocker CLUSTER_CONCENTRATION_TOO_HIGH: 16.9% > 5%
```

An agreement interval must still come from a **cluster bootstrap resampling
`cluster_id`** (the content digest) even once the floor is met. The floor makes the
bootstrap possible; it does not make item-level independence true.

## 4. Label codebook

**Every human-judged field carries BOTH escape values.** They are different
failures and must never be pooled:

| Value | Meaning | Its rate measures |
|---|---|---|
| `CANNOT_JUDGE` | The rater **has** the context; the step is **genuinely ambiguous** | **Taxonomy** ambiguity |
| `INSUFFICIENT_CONTEXT` | The context needed is **missing or truncated in the package** | **Package** completeness — a builder-fixable defect |

Neither is penalised. Conflating them hides package defects inside a
taxonomy-ambiguity number: a rising `CANNOT_JUDGE` rate says the label set needs
work; a rising `INSUFFICIENT_CONTEXT` rate says the builder does.

### 4.1 Complete value lists

| Field | Allowed values |
|---|---|
| `abstention` | `ACTED` · `DECLINED_WITH_REASON` · `DECLINED_NO_REASON` · `CANNOT_JUDGE` · `INSUFFICIENT_CONTEXT` |
| `error_response` | `NO_PRIOR_ERROR` · `ACKNOWLEDGED_AND_CHANGED` · `ACKNOWLEDGED_NOT_CHANGED` · `IGNORED_PRIOR_ERROR` · `CANNOT_JUDGE` · `INSUFFICIENT_CONTEXT` |
| `repeats_prior_action` | `YES` · `NO` · `CANNOT_JUDGE` · `INSUFFICIENT_CONTEXT` |
| `step_contribution` | `PROGRESS` · `NEUTRAL` · `HARMFUL` · `CANNOT_JUDGE` · `INSUFFICIENT_CONTEXT` |

### 4.2 Primary label — `step_contribution`

| Value | Definition |
|---|---|
| `PROGRESS` | Moves the task closer to the stated goal: acquires information the agent lacked, or changes state in a direction the goal requires. |
| `NEUTRAL` | Neither advances nor sets back. Valid exploration, re-reading held information, no-op confirmations. |
| `HARMFUL` | Sets the task back: destroys needed state, introduces an error to be undone, or commits to an excluded path. |
| `CANNOT_JUDGE` | Context present; step genuinely ambiguous. |
| `INSUFFICIENT_CONTEXT` | Context missing or truncated in the package. |

### 4.3 Facets — each with both escapes

**`error_response`** — `NO_PRIOR_ERROR`, `ACKNOWLEDGED_AND_CHANGED`,
`ACKNOWLEDGED_NOT_CHANGED`, `IGNORED_PRIOR_ERROR`, `CANNOT_JUDGE`,
`INSUFFICIENT_CONTEXT`.

`ACKNOWLEDGED_NOT_CHANGED` is the blind retry a machine gate can miss when an
environment event reports recovery.

**`abstention`** — `ACTED`, `DECLINED_WITH_REASON`, `DECLINED_NO_REASON`,
`CANNOT_JUDGE`, `INSUFFICIENT_CONTEXT`.

**`repeats_prior_action`** — `YES`, `NO`, `CANNOT_JUDGE`, `INSUFFICIENT_CONTEXT`.

### 4.4 Attention check — identity withheld from the bundle

`repeats_prior_action` is mechanically decidable and its machine ground truth lives
in the **separate withheld artifact**, never on the item. **The rater bundle does
not name which field is the check** — naming it is itself a leak, so
`attention_check_field` is stripped from the bundle along with any prose revealing
that a withheld truth exists. Rater disagreement measures **attention**, not item
ambiguity, and must never be pooled into taxonomy agreement.

### 4.5 Withdrawn machine truth — `prior_error_visible`

Computed by substring matching for `traceback` / `error` / `exit code 1`, **audited
at 88 % false positives** (38 of 43 hits) — it fired on `'Script completed / Wall
time 0.1 seconds'`. ATIF observations carry **no structured exit codes**, so no
deterministic implementation exists. **Withdrawn, not tightened.** The
`error_response` facet remains — a human can read the observation — but no machine
truth is claimed.

### 4.6 Excluded labels

| Excluded | Reason |
|---|---|
| `step_necessity` | Requires an oracle optimal path. None exists. |
| `step_efficiency` | Same defect, plus it presumes an unstated cost model. |
| `unrecoverability` | Counterfactual — quantifies over all continuations. Exactly the label one would reach for to unblock T1.3 `t_lock`, and exactly the one a human cannot supply. |

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
  --machine-truth-out research/goldset/machine_truth_WITHHELD.json \
  --boost-per-stratum 3 \
  --export-rater-bundle /tmp/rater-bundle

python3 research/goldset/test_labeling_package.py   # 92 standalone checks + 12 pytest
```

Expect `package_sha256 77317e371b7203344f1539cf17dce19aa8b531098b34a0f305067c62699bff6f`
and `readiness NOT_READY`. A differing digest means the source corpus changed;
re-pin before labelling.

## 7. Blockers — labeling must NOT start

**Five blockers. Recruiting raters clears only two of them.**

```
EFFECTIVE_CLUSTERS_BELOW_FLOOR: K_eff=13.33 < 20.0
CLUSTER_CONCENTRATION_TOO_HIGH: 16.9% > 5%
QUALIFIED_RATER_POOL_TOO_SMALL: have 0, need >= 3
ITEMS_WITH_ZERO_VALID_RATINGS: 183
REGISTRY: REGISTRY_ABSENT
```

### 7.1 Design blockers — a data campaign, not recruitment

$K_{\text{eff}} = 13.33$ against a floor of 20, concentration 16.9 % against a 5 %
target. **Even a perfectly balanced 20-cluster split reaches only 19.97**, so the
present corpus cannot clear the floor at any concentration.

**No amount of rater recruitment fixes this.** Labelling must not begin before new
data exists. Campaign target: **~35–50 new distinct logical trajectory digests,
$\le$ 5 % concentration, $K = \max(30,\; 96\rho)$ after an ICC pilot.**

### 7.2 Rater blockers — external, and second in order

Three qualified independent rater key IDs per item, supplied through a **signed
registry** (`goldset-rater-registry/v1`) verified against an authority secret. An
absent, unsigned, or tampered registry yields an **empty pool plus an explicit
problem**, never a silent pass. Duplicate or conflicting submissions from one
`(item, rater)` **fail** rather than collapsing into a set.

No substitute is permitted: not LLM judges, not the Analyst, not synthetic labels.

### 7.3 Internal, pending Tutor — honestly null

`agreement_statistic`, `acceptance_threshold`, `required_interval_width`,
`adjudication_rule`, `rater_qualification_criteria` all remain `null`. **No
published floor was imported**, because none is quotable: 20 of 31 asserted arXiv
IDs were fabricated placeholders, and the $\kappa$ / $\alpha$ figures are attached to
papers whose bodies were never read.

### 7.4 Correct order

1. Run the data campaign (§7.1)
2. Re-cut the package; confirm the cluster gate clears
3. Tutor sets the agreement statistic and threshold
4. Publish the signed rater registry
5. Recruit and qualify three raters
6. Label

Steps 1–2 are **prerequisites** for 5–6, not parallel to them.
