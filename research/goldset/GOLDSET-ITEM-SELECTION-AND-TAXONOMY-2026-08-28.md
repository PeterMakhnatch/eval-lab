<!-- REVISION 2. An independent review found five blockers in revision 1, all
valid, all fixed at root. Two numbers I reported to Main and Tutor were WRONG:
the universe is 183 items in 20 clusters, not 237 in 23. See §0. -->

---
type: protocol
topic: goldset-item-selection-and-taxonomy
author: analyst
date: 2026-08-28
status: revision-3-after-exact-head-review
readiness: NOT_READY (raters AND cluster adequacy)
epistemic: measured - every census figure computed from artifacts on disk
collection: trajectory-analysis
owns: [item_selection, provenance, label_taxonomy]
delegated_to_tutor: [agreement_statistic, acceptance_threshold, rater_qualification, adjudication_rule, power_argument]
package: research/goldset/labeling_package.json
package_sha256: 4790e490d09fa84882f012840df65302bd1ff5ff65f8df6126c07181181895e8
revision: 3
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

## 1. Readiness — NOT_READY, fail-closed

```
readiness  NOT_READY
blocker    QUALIFIED_RATER_POOL_TOO_SMALL: have 0, need >= 3
blocker    ITEMS_WITH_ZERO_VALID_RATINGS: 183
```

`evaluate_readiness` fails closed on seven conditions: pool below three, invalid
rating records, ratings for unknown items, items with zero **valid** ratings, items
below three **unique** raters, and any rater outside the qualified pool. Label
presence and enum membership are validated — three rater IDs with null labels do
**not** clear readiness (B4). Verified by test.

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
byte-identical package, `sha256 4790e490…`.

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
`context_completeness.builder_verdict` — `COMPLETE` or `DEGRADED` — declared by the
builder, not the rater. Current distribution:

```
COMPLETE 158    DEGRADED 25
```

If raters mark `INSUFFICIENT_CONTEXT` on items the builder called `COMPLETE`, **the
builder missed a defect it believed it had detected.** That disagreement is a
measurement of the package, obtainable only because the two signals are separate.

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

`repeats_prior_action` is **mechanically decidable** and its machine ground truth
ships in the **separate withheld artifact** `machine_truth_WITHHELD.json` — never on
the item. Rater disagreement with it measures **rater attention**, not item
ambiguity, and must never be pooled into taxonomy agreement.

Revision 1 shipped this truth inside the rater-facing item under `machine_facts`,
which showed raters the answer. Asserted absent by test.

### 4.4 Withdrawn machine truth — `prior_error_visible`

Revision 2 shipped a `prior_error_visible` machine fact computed by substring
matching for `traceback` / `error` / `exit code 1` in observation text. **Audited at
88 % false positives** (38 of 43 hits) — it fired on
`'Script completed / Wall time 0.1 seconds'`.

ATIF observations carry **no structured exit codes**, so no deterministic
implementation is available. The fact is therefore **withdrawn, not tightened**: an
88 %-wrong "truth" is worse than none. The `error_response` *facet* remains — a
human can read the observation — but no machine truth is claimed, and
`prior_error_truth_available` is `False` on every row with the reason recorded.

This is the third substring-heuristic-as-fact error in this workstream: I objected
to `"distractor" in tool_name` as a Grade-A input on PR #267, had a `_rate` suffix
heuristic vetoed as a structural gate, and then shipped this one. The rule holds and
I keep breaching it: **a heuristic may generate candidates, never a fact.**

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
  --machine-truth-out research/goldset/machine_truth_WITHHELD.json \
  --boost-per-stratum 3

python3 research/goldset/test_labeling_package.py   # 57 checks
```

Expect `package_sha256 4790e490d09fa84882f012840df65302bd1ff5ff65f8df6126c07181181895e8`
and `readiness NOT_READY`. A differing digest means the source corpus changed;
re-pin before labelling.

## 7. Blockers

**External — the only one requiring people rather than code:**

> **Three qualified independent human rater IDs per item.** Zero exist. 183 items
> await ratings. No substitute is permitted: not LLM judges, not the Analyst, not
> synthetic labels. Readiness cannot clear on rater IDs alone — labels must be
> present and in-enum, and the three raters must be distinct and qualified.

**Internal, pending Tutor:** the five null parameters in §5.

**Contingent:** if Tutor's power argument needs more than $n \approx 20$ independent
trials, the blocker escalates from a protocol gap to a data campaign, and this
package should be re-cut after that campaign rather than labelled now.
