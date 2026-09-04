---
type: review-rubric
topic: trajectory-to-training-methodology-preregistration-review
author: experimental-methodologist
reviewer_role: independent-reviewer
date: 2026-09-04
status: draft-rubric
charter_base: 6df601b1
mission: M7
review_gate: G2
scope: offline-author-brief-only
---

# Methodology preregistration review rubric

## Review state

This is the independent review rubric for the Trace-to-SFT recipe study in
`trajectory-training-execution-charter-20260904.md`. It is **not** the signed G2
review. The signed decision remains unavailable until the Analyst's selection
recipe and the held-out freeze inputs are present at immutable heads.

The review is intentionally fail-closed. A checked box is not evidence; every
pass must cite a field, table, manifest digest, or executable audit output in the
preregistration. The reviewer records one of:

- **APPROVE** — every required item passes without an unresolved material caveat;
- **APPROVE WITH CONDITIONS** — only clerical, non-outcome-informed corrections
  remain, and each condition is explicit and mechanically checkable before bundle
  materialization;
- **REFUSE** — a design choice permits confounding, leakage, outcome-informed
  analysis, unsupported precision, or an unfenced protected-family regression;
- **UNAVAILABLE** — required source, freeze, identity, or authority evidence is
  absent or cannot be reopened.

`APPROVE WITH CONDITIONS` does not authorize training. G2 opens only after the
conditions are satisfied at a new immutable head and the reviewer signs that
head.

## Evidence pins required for review

The preregistration must bind, by immutable revision or content digest:

1. the execution charter and contract/interface map;
2. the source/authority census and typed exclusion ledger;
3. the four-arm selection recipe and every selector version;
4. the tokenizer, renderer, prompt template, assistant-mask policy, and target
   token accounting implementation;
5. the training/discovery, curation-development, and sealed-test cluster maps;
6. the held-out evaluation identities, pair/block keys, task-family labels,
   verifier identities, capture requirements, and exclusion rules;
7. the `SftSignalFreezeV1` decision rule and analysis implementation;
8. the seed policy and the exact scope of inference licensed by its number of
   independent training runs.

A path without a digest is not a pin. A digest whose bytes cannot be reopened is
not evidence.

## A. Estimands, experimental units, and claim boundaries

### A1. Primary estimand

The preregistration must state the primary estimand separately for every
predeclared task family and recipe contrast. The default binary held-out
estimand is the mean paired change in verifier success:

\[
\Delta_{r,f}=E\left[Y^{\text{candidate }r}_{c}-Y^{\text{baseline}}_{c}
\mid c\in f\right],
\]

where `r` is one of B, C, or D, `f` is a predeclared task family, and `c` is a
held-out cluster. The expectation is over the declared held-out cluster and seed
distribution, not over rows emitted by a trajectory parser.

Required checks:

- [ ] The experimental unit is the independent training run for training-policy
      uncertainty and the held-out cluster for evaluation uncertainty; trials,
      turns, tool calls, transformed siblings, and repeated seeds are not treated
      as independent units.
- [ ] Every endpoint defines direction, scale, unit, opportunity denominator,
      missingness behavior, and whether higher or lower is better.
- [ ] Binary pass/fail is primary unless another verifier-backed endpoint was
      frozen before outcomes; training loss and model-authored quality scores are
      never outcome evidence.
- [ ] The baseline comparison is explicit. Evidence that B, C, and D each beat A
      does not establish which recipe is best; a "best recipe" claim requires
      multiplicity-controlled direct contrasts or must be phrased as descriptive.
- [ ] Family-specific effects are primary. A pooled average is secondary and
      cannot rescue a failed family rule.
- [ ] Any adaptive evaluation, incomplete pair, invalid task, capture loss, or
      verifier failure follows a predeclared disposition; it is not silently
      removed from a denominator.

### A2. Scope of inference

- [ ] The claim population names the checkpoint family/revision, training
      objective, source strata, task families, harness/tool schema, renderer, and
      held-out family. No claim generalizes beyond these factors without a
      separately supported transport argument.
- [ ] If only one independent training seed is run per arm, the study is labelled
      a recipe-instance pilot. It may report held-out evaluation precision
      conditional on those checkpoints, but it may not estimate training-policy
      variance or claim a stable recipe effect.
- [ ] A scientific claim about a selection policy across optimizer randomness
      requires a predeclared minimum of independent training seeds per arm and an
      analysis that treats seed as a training-level unit. Three seeds is the
      minimum interpretable floor; more may be required by the precision design.
- [ ] S0 is excluded from scientific claims. It proves interface compatibility
      only.

## B. Power and precision rationale

Power is not accepted as a generic target such as "80%" detached from the
actual cluster structure. The preregistration must contain a reproducible sizing
table for every primary family and protected family.

### B1. Required sizing inputs

For each `(recipe contrast, task family)` cell, record:

| Input | Required definition |
|---|---|
| `M_clusters` | number of cluster-disjoint held-out clusters |
| `pairs_per_cluster` | complete baseline/candidate pairs by cluster |
| `p_baseline` | blinded pilot or historical baseline rate and evidence pin |
| `discordance` | expected paired disagreement rate, if McNemar-style sizing is used |
| `rho_cluster` | intra-cluster correlation estimate or conservative sensitivity range |
| `delta_min` | smallest positive effect worth acting on |
| `delta_protect` | largest tolerated protected-family regression |
| `alpha_family` | familywise error allocation after multiplicity control |
| `power_target` | target power for improvement and protected non-inferiority |
| `capture_yield` | expected complete-pair fraction and source of estimate |

- [ ] `delta_min` and `delta_protect` are decision-relevant quantities justified
      before outcomes, not values reverse-engineered from available sample size.
- [ ] Historical inputs use task-family-matched evidence. When no credible input
      exists, the design reports a sensitivity grid rather than a single precise
      power number.
- [ ] Scheduled pairs are inflated for capture loss, invalid tasks, and cluster
      correlation, but post-randomization exclusions are not used to manufacture
      the target sample size.
- [ ] Effective sample size is reported under at least low, central, and high
      `rho_cluster` assumptions. Row count is never presented as independent `n`.
- [ ] Precision is shown as the expected or worst-case simultaneous confidence
      interval width for every primary and protected cell.

### B2. Analysis appropriate to clustering and pairing

- [ ] Baseline/candidate trials are paired on frozen task, environment, verifier,
      harness, and seed identities. Broken pairs are reported and cannot be
      repaired by cross-pairing another seed or sibling cluster.
- [ ] The primary interval/test resamples or models at the cluster level. A
      cluster bootstrap, cluster-aware randomization test, or predeclared
      hierarchical model is acceptable; an ordinary row bootstrap is not.
- [ ] Small-cluster behavior is specified. Asymptotic standard errors are refused
      when the number of independent clusters is too small for their calibration.
- [ ] The same interval implementation is used for positive-effect and protected
      non-inferiority decisions and is frozen before outcomes.
- [ ] Training-seed variation and held-out-cluster variation are not collapsed
      into one pseudo-replicated standard error.

### B3. Precision refusal rules

The reviewer refuses G2 when any primary or protected cell lacks enough planned
independent information to distinguish the decision threshold from a practically
important alternative. In particular:

- zero or one held-out cluster in a family is structurally uninterpretable;
- an interval expected to span both `delta_min` and `-delta_protect` cannot
  support either improvement or protection;
- adding repeated trials inside the same cluster does not substitute for adding
  independent clusters;
- a protected family with no eligible cluster is not "no regression"; it makes
  the signal decision **UNAVAILABLE** for any rule that requires that family.

## C. Cluster leakage audit

The audit unit is the transitive provenance cluster, not a file, row, or final
prompt digest. Every derived or transformed sibling of the same upstream parent
must remain in one ownership domain.

### C1. Cluster construction

The preregistration must define a deterministic `cluster_key` from the strongest
available parent identities, including as applicable:

- upstream dataset and immutable revision;
- original conversation/episode/problem identity;
- task and template parent;
- environment/topology generator family and topology class;
- seed lineage and base/variant or mutation parent;
- verifier/template parent;
- source job/trial identity and content digest.

When two records share any parent that can transmit solution, structure, or
verifier information, their cluster graph nodes are connected. Splits operate on
connected components. Missing lineage is a typed exclusion or **UNAVAILABLE**
decision, never a new singleton cluster.

### C2. Mechanical leakage checks

The G2 evidence bundle must include machine-readable counts and offending IDs for:

- [ ] exact content-digest intersections across training/discovery,
      curation-development, and sealed-test domains — required count: zero;
- [ ] cluster-key and transitive-parent intersections across domains — required
      count: zero;
- [ ] base/variant, mutation, translated, reformatted, or regenerated siblings
      assigned to different domains — required count: zero;
- [ ] shared generator templates, topology classes, seeds, solution artifacts,
      verifier fixtures, or hidden inputs crossing into training — required
      count: zero unless the preregistration proves they cannot transmit the
      tested mechanism and the charter permits the reuse;
- [ ] near-duplicate instructions, tool outputs, terminal targets, and answer
      payloads across domains, using a frozen normalization and similarity
      threshold; every hit receives a recorded adjudication before G2;
- [ ] selector features, quality labels, analyst notes, or prompts computed from
      sealed-test content — required count: zero;
- [ ] sealed-test identities exposed to the selector, trainer, environment
      generator, or model-assisted analysis process — required count: zero.

A split label stored on each row is insufficient. The reviewer must be able to
recompute the connected components from pinned bytes and reproduce the zero-
intersection report byte-for-byte.

### C3. Leakage falsification probes

- [ ] Recompute clusters after dropping convenience IDs such as filenames and
      local paths; ownership assignments remain unchanged.
- [ ] Expand each held-out item to all known ancestors and descendants; none
      resolves into training or curation-development.
- [ ] Run the near-duplicate audit with a stricter secondary threshold and inspect
      a blinded sample of both hits and near-misses.
- [ ] Verify that task families with common scaffolds do not share answer-bearing
      constants, hidden verifier inputs, or topology instances across the
      firewall.
- [ ] Demonstrate that the held-out manifest was frozen before any training
      outcome was visible, and that later additions cannot enter the frozen gate.

Any unresolved cross-domain parent, hidden-input exposure, or outcome-informed
split change is a **REFUSE**, not a sensitivity analysis.

## D. Multiplicity and protected-family decision rule

### D1. Families of claims

The preregistration must enumerate all confirmatory cells before outcomes:

1. improvement claims for every tested `(B/C/D versus A, primary task family)`
   cell;
2. protected non-inferiority claims for every `(candidate arm, protected task
   family)` cell;
3. any direct candidate-versus-candidate contrast used to call one recipe best;
4. all confirmatory endpoints within those cells.

Unlisted analyses are exploratory. Renaming a family or endpoint after outcomes
does not create a new multiplicity family.

### D2. Default error-control rule

Unless the preregistration justifies and implements a stronger simultaneous
method, this reviewer requires:

- one-sided familywise `alpha = 0.05` across all confirmatory improvement cells,
  controlled by Holm's step-down procedure;
- one-sided familywise `alpha = 0.05` across all protected non-inferiority cells,
  also controlled by Holm, with simultaneous lower confidence bounds compared
  against the predeclared `-delta_protect` margin;
- the same correction family for any direct arm-versus-arm claims needed to name
  a winner;
- no substitution of false-discovery-rate control for the protected-family
  familywise guarantee.

A predeclared cluster-level max-T randomization procedure may replace Holm if its
exchangeability conditions are defended and its implementation is frozen.

### D3. Signal decision

For candidate arm `r`, the family-specific SFT signal is:

- **SUPPORTED** only if at least one predeclared primary-family improvement cell
  for `r` rejects its null after correction, its simultaneous lower bound exceeds
  `delta_min` or the preregistered action threshold, **and** every protected-
  family lower bound exceeds `-delta_protect`;
- **REFUTED** if a protected family crosses the predeclared regression boundary,
  or the directionally targeted primary effect is materially negative;
- **INCONCLUSIVE** if complete admissible data exist but corrected precision does
  not distinguish the decision thresholds;
- **UNAVAILABLE** if authority, capture, pairing, cluster independence, or a
  required protected-family denominator is missing.

Additional rules:

- [ ] A pooled gain cannot override a family-specific failure or protected-family
      regression.
- [ ] The arm with the largest observed point estimate is not called best unless
      the frozen direct-contrast rule supports that claim.
- [ ] Stopping rules operate on administrative completion or predeclared
      information, never on unblinded effect direction.
- [ ] Exclusions, endpoint substitutions, and family merges are frozen before
      outcomes. Any deviation is labelled exploratory and cannot pass G5.
- [ ] All arms and all predeclared cells are reported, including null, negative,
      incomplete, and invalid results.

## E. Source-quality claim falsification

The quality-selection claim is causal only if recipe quality is separated from
source, teacher, family, difficulty, length, harness, and missingness. Balance on
nominal strata is necessary but not sufficient.

### E1. Selector provenance and blindness

- [ ] Every selector feature is computable from training-authorized evidence
      available before held-out outcomes and is bound to a versioned extractor.
- [ ] No selector feature contains reward leakage, held-out verifier state,
      sealed-test text, post-training outcomes, or model-authored labels treated
      as authority.
- [ ] Grounded-tool-use, verification, retry, and structural-diversity criteria
      have deterministic definitions, explicit missingness states, and negative
      examples.
- [ ] The same tokenizer/template determines target-token budget in every arm;
      semantic tool calls, tool results, or terminal targets are never truncated
      to force budget equality.
- [ ] Selection occurs only within provenance and task-family blocks, and every
      source stratum represented in A remains represented in B, C, and D.

### E2. Required balance and missingness diagnostics

Before training, publish for every arm and block:

- source/revision, producer model, harness/tool schema, task family, difficulty,
  trajectory length, assistant target tokens, tool-call count, success authority,
  redaction/rehydration state, selector missingness, and exclusion rate;
- standardized differences and support/overlap, not only aggregate counts;
- selected and rejected denominators for every quality criterion;
- pairwise corpus overlap and the number of unique provenance clusters;
- the number of rows per cluster and the effective diversity after deduplication.

Material imbalance that is caused by the quality policy must be either removed by
the frozen design or explicitly narrow the estimand. Regression adjustment alone
does not repair absent support.

### E3. Predeclared attempts to falsify the quality explanation

The preregistration must include these probes before outcomes:

1. **Within-block shuffle negative control.** Shuffle quality rank within the
   exact source/family/difficulty blocks while preserving token budget and sample
   count. If the alleged quality advantage is reproducible under shuffled ranks,
   the result does not identify quality selection.
2. **Selector ablation.** Recompute the C and D candidate sets with each quality
   criterion removed in turn. Report membership changes, balance, and criterion
   dependence; do not train extra arms without separate authorization.
3. **Source influence analysis.** Recompute effect summaries leaving out each
   provenance stratum. A claim that reverses or exists only in one source is
   source-contingent, not a general process-quality result.
4. **Length residualization check.** Within the frozen blocks and token budget,
   test whether process-quality rank is effectively a proxy for assistant turns,
   target length, tool-call count, or task difficulty. Arm C/D claims must not be
   relabelled concise-trajectory effects.
5. **Blinded criterion audit.** Independently inspect a predeclared random sample
   of selected and rejected examples without arm or source labels. Report false
   positives, false negatives, disagreements, and unresolved evidence; prose
   agreement is not a substitute for the deterministic selector.
6. **Missingness stress test.** Treat unresolved selector fields pessimistically
   and optimistically. If arm membership or the planned conclusion is unstable,
   the quality claim is inconclusive.
7. **Negative-outcome disclosure.** Report whether the selector preferentially
   excludes hard families, long-horizon tasks, particular producer models, or
   redacted/unrehydratable sources even when overall source counts appear
   balanced.

These are falsification checks, not post-hoc explanations. Their code, thresholds,
and interpretation rules must be pinned at G2. A failed probe may support a
narrow source-contingent claim, but it cannot be ignored while retaining a broad
"quality traces train better" conclusion.

## F. G2 review worksheet

The signed review will fill this table against immutable artifact heads:

| Domain | Decision | Evidence pin | Material finding / required correction |
|---|---|---|---|
| estimand and claim boundary | PENDING | — | Analyst preregistration not yet reviewed |
| power and precision | PENDING | — | Sizing table and seed policy required |
| pairing and cluster-aware analysis | PENDING | — | Frozen held-out plan required |
| cluster firewall | PENDING | — | Recomputable component audit required |
| multiplicity | PENDING | — | Confirmatory cell inventory required |
| protected families | PENDING | — | Families, margins, and simultaneous bounds required |
| selector authority | PENDING | — | Selector versions and feature authority required |
| source-quality falsification | PENDING | — | Frozen probes and interpretation rules required |
| stopping and missingness | PENDING | — | Complete capture/exclusion rule required |
| overall G2 decision | UNAVAILABLE | 6df601b1 | Awaiting Analyst and held-out freeze inputs |

## G. Signature contract

A signed preregistration review must append, without rewriting this rubric:

```text
Reviewer: <name/role>
Review artifact head: <immutable commit>
Charter head: 6df601b1
Analyst preregistration head: <immutable commit>
Held-out freeze head/digest: <immutable identity>
SftSignalFreezeV1 digest: <digest>
Decision: APPROVE | APPROVE WITH CONDITIONS | REFUSE | UNAVAILABLE
Conditions/refusals: <enumerated, mechanically checkable items>
Reviewed at: <UTC timestamp>
Signature digest: <digest over the completed review payload>
```

The reviewer must remain independent of arm selection, bundle materialization,
trainer operation, and outcome production. The post-outcome review is a separate
append-only section that checks the frozen analysis against actual manifests; it
must not revise this rubric, the G2 decision rule, or any threshold after results
are visible.
