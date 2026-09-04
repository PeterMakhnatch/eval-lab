---
type: preregistration-spec
mission: M3-ANALYST-SELECTION-SPEC
author: wK:p7 (Fable 5.1, Analyst)
date: 2026-09-04
status: prereg-plan-v2-g2-blocked-authority-stop
charter: research/inbox/trajectory-training-execution-charter-20260904.md
base: integrate/spine-batch1@e3856849 (rebased; spine delta 6df601b1..e3856849 is the M1 audit doc only)
depends: M1 field map, M2 source census (freeze blocked until both land)
aligned: architect-contract-audit-20260904.md §3b + F2 (SelectionRecipeV1); F6 post-outcome duty accepted
---

# Selection recipe preregistration — provenance-aware census spec and four-arm recipe

## 1. Primary question (frozen)

Holding checkpoint family, tokenizer/template, renderer, optimizer, seed policy, total
supervised assistant-target budget, source strata, and held-out evaluation fixed, which
selection policy (arms A-D below) produces the strongest predeclared family-specific
held-out effect without a protected-family regression?

## 2. Arms (frozen semantics)

All four arms draw from the **same block pool**. Outcome and authority are the only hard
gates; arms differ exclusively in the deterministic **ordering** used to fill the block
budget (charter says "prefer", never "exclude" - a hard quality filter would conflate
selection policy with corpus shrinkage and break the source-comparability control).

| Arm | Ordering within block | Tests |
|---|---|---|
| A | bundle/task order; Fisher-Yates with `seed = sha256(block_key + freeze_digest)` at materialization | honest baseline (arm enum: NEW named `Literal["A","B","C","D"]` per M1 F2 — never a reuse of `base/variant` or `baseline/candidate`) |
| B | shortest `assistant_turns` first, ties by source path; preferred region `assistant_turns < 5` (transparency metric only) | short-trajectory hypothesis |
| C | process-quality pass first (grounded tool use, post-mutation inspection when mutating, non-empty terminal output, no blind identical retry), then shortest-turns | process-quality selection |
| D | arm C's order with one row per tool-sequence signature (`tool_sequence_sha256`) in the leading positions | nonredundant process coverage |

Screen definitions are the typed, deterministic ones in the census prototype
(`MUTATION_TOOLS`, `INSPECTION_TOOLS`, `process_quality_pass` - digests in §7). Changing
a screen after outcomes are observed voids the prereg.

## 3. Blocks (frozen)

`block = family x source_stratum(bundle) x provenance_stratum(model@harness) x difficulty`,
with `cluster_key = family|task_name` (design cell) and difficulty in
`turns_1_4 / turns_5_9 / turns_10_plus`.

**Family rule (interim, explicit):** `template-family-rule/v1` - `action-memory-*` ->
`action-memory`; `*funcdag*` -> `funcdag`; `mcp-rec-*` -> `mcp-recovery`; else full stem.
Historical sidecars carry no registry family binding (`family_binding_absent` is a
recorded hold reason). When M1 binds families from the task registry, blocks are
re-derived; **if any block's arm ordering changes, the recipe refuses** (G1).

**Hard pool gates (per row):** `verifier_result.rewards.reward == 1.0`; authority either
`current_contract` or a reopened historical record whose disposition is
`descriptive-complete` (`refused` and `descriptive-incomplete` never admit); zero
missingness types outstanding. Input rehydration (R1 system/user text via PROMOTION
parent digests) must succeed before any row is admitted.

**Splits** follow blocks (source x family x cluster), never rows or filenames; three
ownership domains (training/discovery, curation-development, sealed test) are assigned
at cluster granularity by the Program Lead + Methodologist in Wave 1.

## 4. Budget and `SelectionRecipeV1` carrier (frozen rule; typed per M1 F2)

Typed carrier is M1's `SelectionRecipeV1` record (referenced by
`TrainingDatasetManifestV1`; not a new manifest family). Mapping of this spec onto its
fields:

| `SelectionRecipeV1` field | This spec binds |
|---|---|
| `arm: Literal["A","B","C","D"]` | §2 orderings; one recipe record per arm per bundle materialization |
| `selection_policy_id` | `provenance-census/v1#arms` — the §2 orderings plus the sub-stratum rule below |
| `block_keys` | `provenance_stratum\|family` (M1's provenance × family). The census's bundle × difficulty cells are sub-strata of a block key used for stratification at materialization; they never split a block into separate recipes |
| `supervised_assistant_token_budget: int` | per block_key: `0.9 ×` pool total, recomputed under the pinned tokenizer before materialization; refuse if any arm ordering flips |
| `token_budget_tokenizer_digest` | Training Engineer's frozen student tokenizer/template digest; the dry-run char budgets in §6 are draft values until this digest exists |

Equalized quantity = **supervised assistant target tokens** under the pinned
tokenizer/template (charter S1). Census proxy = assistant target characters/bytes
(explicitly draft until the pin; producer completion tokens are reported but are NOT
the budget — producer tokenizers, may include hidden reasoning).

**G1 comparability refusals attach to F2** (M1 §4): `arm_changes_source_or_teacher` —
an arm's ordering may never alter which sources, teachers, families, templates, or
action spaces a row's provenance carries; `budget_mismatch` — arms within a block_key
must carry equal `supervised_assistant_token_budget` or the bundle refuses.

`budget(block) = 0.9 × pool target total` (all arms share one pool, so the
min-over-arms attainable size equals the pool total). Each arm fills the budget in its
own order; **semantic truncation of any tool call, tool result, or terminal assistant
target is prohibited** (G3 refuses; tool-call/result truncation refusal rides message
binding per #367). At bundle time the Training Engineer recomputes budgets under the
frozen tokenizer; the recipe refuses if any block's arm-feasibility or ordering changes.

## 5. Preregistration fields to `SftSignalFreezeV1` (Wave 1 freeze checklist)

Declared NOW by this spec: family rule, block definition, pool gates, arm orderings,
budget rule, analysis rule (below), typed exclusions (census `missingness` taxonomy +
`refused`/`descriptive-incomplete` authority + exception rows).

Declared in Wave 1 by Program Lead + Methodologist (NOT defaulted here, per charter):
minimum effect, minimum eligible pairs per family, protected families (recommendation:
**all** families protected - any cross-family regression vetoes), interval method
(per-family percentile cluster bootstrap, deterministic seed — consistent with
`SftSignalFreezeV1` I4's no-pooled-headline rule), and stopping rule (all four arms
materialize for a block or the block is excluded from the primary analysis).
Carrier note (M1 F4): `stopping_rule`, `preregistered_exclusions`
(extending `SftExclusionCode` — never a fourth taxonomy), and `hardware_class` land on
`SftSignalFreezeV1` via the Researcher-Evals lane; this spec's exclusion taxonomy feeds
that closed set.

**No pooled summary is primary.** Per-family decisions only; cross-family numbers may
appear as descriptive denominators with explicit "not a headline" labels.

## 6. Census dry-run on this base (read-only; no model calls)

Command: `uv run python -m evallab.provenance_census --repo-root . --out <dir> --head 6df601b1`.
Byte-identical across reruns (verified). Artifacts digests (prototype run):

- `census.jsonl` `sha256:9fa76ce394a3fde1de5524c86630a488047f5b6aeb2dffd990225081ff737758`
- `report.md` `sha256:ddb11cc545d80e1ddb7e610f40f4bae9ab11bf5c3782e932334143c033274235`
- `prereg-inputs.json` `sha256:a20fcf01f96a6985f280c90f8abdf664cb0f3485a31a99894a3793cdf62d3035`

### Denominators (descriptive, not a headline)

| Quantity | Value |
|---|---|
| Promoted trajectories | 164 (18 bundles, 3 provenance strata) |
| Families (rule v1) | action-memory 130 / mcp-recovery 15 / funcdag 10 / event-summary 3 / transaction-reconciliation 3 / html-js-filter 3 |
| Authority | current_contract 3 / historical_sidecar 161 / none 0 |
| Verified success (reward=1.0) | 100/164 |
| G0-strict SFT-eligible | **0/164** |
| Rehydration-pending (would admit iff CAS rehydration + descriptive-complete reopen) | 75/164 (action-memory 72, event-summary 3) |
| Authority hold classes | descriptive-complete 128 / refused 31 / descriptive-incomplete 2 |
| Exception rows | 2 |

### Feasibility of the four-arm study

- Strict pool: **0 feasible blocks** - no row is SFT-admissible today. Track A's own
  admission gate agrees; this is consistent, not contradictory.
- Pending pool: **13 feasible blocks** (29 formed), all in action-memory + event-summary;
  total action-memory block budgets **51,438 assistant target chars** (~a four-arm study
  on ~72 rows). This is an underpowered pilot by construction; the prereg must predeclare
  it as such, not as a decisive recipe comparison.
- Arm C quality screen: **0 rows pass** among pending rows - blind identical retries are
  endemic (the exact loop behavior Track B mines). The C/D orderings are therefore
  **degenerate** on the current corpus and the study cannot detect a process-quality
  effect until a corpus with quality-passing rows exists (environment loop or rehydration
  of richer producers). Declared now so it cannot be spun later.
- Arm B preferred region (<5 turns) empty in 3 strata (informational; B orders within
  block, never drops a stratum).
- Provenance strata: 3 model@harness strata exist; **independence NOT established**
  (same lab, same harness family, one teacher per family). Charter stop condition
  "cannot form at least two independent provenance strata" is **borderline and
  declared**: M2 must resolve source-comparability before materialization.

### Missingness -> concrete asks

| To | Ask | Unblocks |
|---|---|---|
| M2 (Data) | Rehydrate R1 system/user text for the 75 pending rows via PROMOTION parent digests; reopen 128 descriptive-complete authorities; refuse the 33 refused/incomplete | every admitted row |
| M2 (Data) | License/redistribution statement for the local corpus + any pinned public set | G0 |
| M1 (Architect) | Registry-bound family/template binding to replace `template-family-rule/v1` | block stability |
| Training | Student tokenizer/template pin | budget unit (chars -> tokens recompute) |
| Program Lead | Protected families + minimum effect + minimum pairs (Wave 1) | `SftSignalFreezeV1` |

## 7. Prototype provenance (not part of this lease's paths)

The census prototype lives uncommitted at
`/private/tmp/eval-lab-analyst-census/src/evallab/provenance_census.py`
(sha256 recorded at handoff; deterministic, ruff-clean, read-only). M2 owns census
machinery per the mission split; adopt verbatim or supersede - do not fork silently.

## 8. Freeze gate

This spec becomes the preregistration only when: (a) M1 field map lands and family
binding replaces rule v1; (b) M2 census reproduces the denominators above from an
independent implementation (or diffs are reconciled on the record); (c) Wave 1 declares
the remaining `SftSignalFreezeV1` fields. Until then this document is the spec + dry-run
inputs, and no bundle may materialize (G2 refuses).

## 9. Post-outcome obligation (M1 F6, Analyst-owned)

After outcomes exist, this lane owns **F6**: add `discovery_evidence_epoch` (checkpoint
identity digest) to `CapabilityDeficitArtifact` so every deficit label is tied to the
checkpoint it was mined against and is refused as stale after a model update — the
charter's closed-loop rerun rule as a typed carrier. Implementation follows on
`capability_deficits.py` as a separate exact-head change once the SFT signal gate
produces the first checkpoint transition; nothing in this PR touches that type.


---

# Revision v2 — response to M7-G2-01..08 (methodology-prereg-review-20260904.md @79220dd5, BLOCK)

M7's BLOCK is accepted in full; the candor findings pass and stay unchanged. This
revision resolves every condition the Analyst lane can resolve NOW, pins the probes and
inventories so they cannot drift, and files the explicit charter stop-condition report
for what belongs to M2 / the Program Lead. Status: **G2 remains CLOSED**; this document
is a conditional prereg plan, not a live preregistration.

## 11. Resolution map (condition -> action)

| Condition | Resolution | Owner | State |
|---|---|---|---|
| M7-G2-01 estimand + precision | §12 | Analyst now; deltas by lead | RESOLVED (as scoped pilot) |
| M7-G2-02 provenance components | §13 | Analyst defines; M2 executes | SPEC'D, EXECUTION PENDING M2 |
| M7-G2-03 multiplicity | §14 | Analyst | RESOLVED |
| M7-G2-04 all-protected | §14.3 | Analyst proposes; lead signs margins | PROPOSED |
| M7-G2-05 degenerate C/D | §15 | Analyst | RESOLVED (arms narrowed) |
| M7-G2-06 falsification probes | §16 | Analyst pins; runs at materialization | PINNED |
| M7-G2-07 stopping | §17 | Analyst | RESOLVED |
| M7-G2-08 authority stop | §18 (stop report) | M2 + Program Lead | **BINDING STOP FILED** |

## 12. M7-G2-01 — estimand, seed scope, precision table

Primary estimand per family `f` and contrast `r in {B,C,D} vs A`:

Delta_{r,f} = E[ Y^{r}_c - Y^{A}_c | c in held-out clusters of f ],

expectation over declared held-out clusters and the training-seed distribution; unit of
training uncertainty = independent training run; unit of evaluation uncertainty =
held-out cluster. Binary verifier success is primary; direction: higher better;
denominator: complete pairs with typed dispositions for every incomplete/invalid member.

**Seed scope declared:** this corpus licenses ONE training seed per arm ->
**recipe-instance pilot**. Claims are checkpoint-conditional; training-policy variance is
NOT estimable; no stable-recipe-effect and no "best recipe" claim is licensed at any n.
Direct contrasts (C-B, D-C, D-B) enter the confirmatory inventory only if a future
revision predeclares >=3 seeds/arm (M7 A2 floor).

**Precision table (sizing schema frozen; values = sensitivity grids, not singletons).**
Per (contrast, family) cell: M_clusters and pairs_per_cluster come from the frozen
eligible-block set at G0/G1 (current dry-run values: 13 feasible blocks; action-memory 55
design-cell clusters, pending-pool rows 72); p_baseline grid {0.5, 0.7, 0.9}; discordance
grid {0.1, 0.2, 0.3}; rho_cluster sensitivity {0, 0.1, 0.3}; capture_yield planned 1.0
with typed capture accounting (post-randomization exclusions never inflate n);
delta_min = +0.10 absolute success change, delta_protect = -0.05 — **PROPOSED VALUES for
Program Lead sign-off in Wave 1, never defaulted silently**; alpha_family = 0.05
one-sided per claim family (Holm; §14); power_target: not claimed for this corpus —
instead the freeze publishes expected simultaneous interval widths, and §12-B3 refusal
is pre-acknowledged: **with current sizes the widths are expected to span
[delta_min, -delta_protect]; therefore the pilot is estimation-only and the SFT signal
decision for arms B/C/D on this corpus is UNAVAILABLE by design**, not merely likely.

Cluster-aware analysis: cluster bootstrap (resample held-out clusters) for every
interval, improvement and protection alike; same frozen implementation both sides; no
row bootstrap; small-cluster rule: families with <4 independent clusters report no
asymptotic SEs, intervals only, flagged UNAVAILABLE for decisions.

## 13. M7-G2-02 — provenance-component audit (spec; execution = M2 bytes)

`cluster_key = family|task_name` is DEMOTED to a design-cell label. The audit unit is
the transitive parent component over edges: source bundle, upstream task template,
task_name design cell, generator family + topology class + seed lineage, verifier
parent, content digest (trajectory + acceptance outputs), PROMOTION parent digests.
Connected components partition ALL corpus rows; whole components are assigned to
training-discovery / curation-development / sealed-test; missing lineage is a typed
exclusion (`lineage_incomplete`), never a singleton cluster.

Machine-readable G2 evidence (all zero-intersection unless a charter-permitted reuse is
proven non-transmitting): exact content-digest intersections; component and
parent-key intersections across domains; sibling misassignment; shared
template/topology/seed/verifier-fixture/hidden-input exposure; near-duplicate audit with
frozen normalization + similarity threshold and recorded adjudications; selector-feature
sealed-content exposure. Reproducibility: the component report must be recomputable
byte-for-byte from pinned bytes (recompute after dropping filenames/paths must leave
ownership unchanged - C3 probe pinned). **Execution requires M2's byte-level lineage
census; the analyst dry-run could not build the parent graph from committed evidence
(PROMOTION digests are present but source bytes are unopened) - this is part of the §18
stop.**

## 14. M7-G2-03 + M7-G2-04 — multiplicity inventory and protected families

**14.1 Confirmatory cell inventory (frozen; anything unlisted is exploratory):**
- Improvement: {B,C,D} x A over families in the frozen eligible set (dry-run families:
  action-memory, event-summary; others enter only if their blocks become feasible) —
  currently 2 families x 3 = 6 cells.
- Protected non-inferiority: {B,C,D} x every eligible protected family — currently 6 cells.
- Direct best-arm contrasts: NONE at one seed (§12); if a future >=3-seed revision adds
  them, they join the same Holm family.

**14.2 Error control:** one-sided familywise alpha = 0.05, Holm step-down, separately
within improvement and protection claim families; simultaneous lower bounds compared to
delta_min (improvement) and -delta_protect (protection). Cluster-level max-T
randomization may replace Holm if exchangeability is defended and the implementation is
frozen before outcomes. No FDR substitution for protection.

**14.3 All-protected operationalized:** every family with >=1 eligible cluster is
protected (the ALL-protected recommendation, now named): margins delta_protect = -0.05
(proposed, lead signs); minimum denominator per protected cell = 4 complete pairs; a
protected cell with fewer pairs, or any missing/incomplete protection evidence, makes
the corresponding arm's signal decision **UNAVAILABLE** — never a pass by absence.
Family-specific failure cannot be rescued by any pooled number (pooled = descriptive
only, labelled).

**14.4 Stopping rule (also M7-G2-07):** collection stops on administrative completion
or predeclared information, never unblinded direction. The eligible block set freezes at
G0/G1; every attempted block stays in capture accounting forever. A block whose
materialization fails is `unavailable`, reported with cause in the frozen denominator
table; an arm-specific or estimand-altering failure makes the affected arm x family
comparison UNAVAILABLE. Exclusion of a block never redefines the target population.

## 15. M7-G2-05 — degenerate quality arms refused

Dry-run fact: 0 pending rows pass the process-quality screen; arm C collapses to arm B's
ordering, and arm D has no quality-positive structure to diversify. Therefore:
- Arms C and D are **NOT materialized** as process-quality / quality-plus-structure
  tests on the current corpus. The study on this corpus is **A vs B, estimation-only
  pilot** (§12); C and D are recorded as `unavailable: degenerate support`, reported in
  every denominator table.
- C/D re-enter only via a revised immutable prereg head after a corpus with both
  passing and failing quality examples in relevant blocks exists (environment-loop
  rollouts are the intended source), with screens re-pinned and the §16 probes passed
  as a precondition, not post-hoc.

## 16. M7-G2-06 — falsification probes (pinned now; run at materialization)

Selector: `provenance-census/v1#arms` screens (deterministic, versioned); every feature
computable from training-authorized evidence only; missingness states typed; negative
examples recorded. Probes and interpretation rules:
1. Within-block shuffle negative control: shuffle quality rank inside block preserving
   budget/count; if shuffled-C selection reproduces C's set overlap above a
   predeclared 0.9 Jaccard, C carries no quality information -> stays unavailable.
2. Selector ablation: leave one criterion out in turn; report membership deltas;
   C/D claims require the quality criterion to be load-bearing (membership change > 0).
3. Leave-one-source-out: effect summaries recomputed per omitted provenance stratum;
   source-contingent effects are labelled source-contingent.
4. Length-proxy: within-block association of quality rank with turns/target
   chars/tool-calls; |Spearman| >= 0.7 -> any C/D effect is relabelled a length effect
   and refused as process-quality.
5. Blinded criterion audit: predeclared random sample (20 rows: 10 selected / 10
   rejected) adjudicated without arm/source labels; disagreement rate reported.
6. Missingness stress: selector fields treated pessimistically and optimistically;
   unstable arm membership or flipped planned conclusion -> inconclusive.
7. Negative-outcome disclosure: exclusion profile by family/producer/length/redaction
   published with every arm's balance table (M7 E2 diagnostics: standardized
   differences, support/overlap, unique provenance clusters, post-dedup diversity).
Failed probes support at most narrow source-contingent claims; they can never be
ignored while retaining a broad quality claim.

## 17. M7-G2-08 (partial, analyst-side) — authority prerequisites accepted as binding

Adopted verbatim as prerequisites for any materialization: M1 interface map at immutable
head (DONE: e3856849); M2 source/authority census + typed exclusion ledger at immutable
head; successful R1 rehydration of the 75 pending rows and reopen of the 128
descriptive-complete authorities (33 refused/incomplete never admit); pinned student
tokenizer/template digest (budget recompute; refuse on ordering flip); >=2 DEMONSTRATED
independent provenance strata; held-out freeze identities; `SftSignalFreezeV1` digest.
Zero strictly eligible rows (0/164) is an immediate materialization stop exactly as the
charter wrote it.

## 18. Charter stop-condition report (formal, to Program Lead via wH:p9)

Under "Immediate stop conditions" the charter directs: stop and report rather than fill
gaps. This program is STOPPED for materialization pending all of:
1. **Provenance independence (stop condition 1):** three nominal strata exist; none is
   demonstrated independent (same lab, same harness family, single teacher per family).
   M2's source census must either demonstrate independence (distinct upstream sources
   with distinct provenance factors) or the Program Lead must re-scope the estimand to a
   single-provenance descriptive pilot. Until then, G2 stays closed and no bundle is
   materialized.
2. **Authority/rehydration (stop condition 3):** committed redactions (164/164 rows)
   cannot yet be rehydrated through trusted source authority; the historical
   descriptive-complete holds (128) are unproven until reopened. M2 owns the byte-level
   proof; my §13 audit consumes it.
3. **License/redistribution (stop condition 2):** no license statement exists for the
   local corpus or any pinned public set.
4. **Degenerate quality support (charter §"Trace-to-SFT study" scope):** arms C/D cannot
   test their chartered estimands on any currently available corpus (§15). Acquiring
   nondegenerate support is a Program Lead decision (environment loop timing), not an
   Analyst filler.
Unstop checklist (all required): M2 census + exclusion ledger head; rehydration +
authority reopens done; independence demonstrated or estimand re-scoped by the lead;
license statement; tokenizer pin; held-out freeze + Wave-1 declarations (delta_min,
delta_protect sign-off, protected set confirmation) -> then this document's Sections
12-17 become the live preregistration at a new immutable head and G2 review is
requested from M7.
