---
type: architecture-review
topic: trajectory-to-training contract-chain audit (charter Wave 0, Architect row)
date: 2026-09-04
author: wK:p6 (Architect; executed via wS:p9 delegation)
base: integrate/spine-batch1@6df601b1 (charter integrated from be777229)
mission: M1 ARCH-CONTRACT-AUDIT (lease wH:p0)
epistemic: read-only audit; no code change; no second manifest family
---

# Architect contract audit — trajectory-to-training charter

Every claim below cites `module:line` at `6df601b1`. `[INFERENCE]` marks anything not read from source. The charter's chain name `QuarantinedCurriculumCandidate` does not exist as a type: it is `SyntheticTaskCandidate` with `status="quarantined"` (`curriculum_candidates.py:252-255`); the charter should use the real name.

## 1. State diagram (integrated chain, as it exists)

```text
NormalizedTrainingEvidence (training_export.py:321)
  -- source receipt + admissibility + registry bytes-verified (ArtifactAuthority) -->
TrainingExampleRecord (:442) + TrainingExclusionRecord (:474)
  -- staged no-replace publish -->
TrainingDatasetManifestV1 (:592)            [G0 lives here]
  |
  |   CapabilityDeficitArtifactReceipt (capability_deficits.py:463)
  |     -- live reverify_capability_deficit_artifact -->
  |   SyntheticTaskCandidate status=quarantined (curriculum_candidates.py:252)  [training-only pool; never registers]
  v
TrainerBundleV1 (trainer_bundle.py:247) -> ValidatedTrainerBundleV1 (:265)
  -- render only, no execution -->
RenderedTrainerPlanV1 (:372) + ExpectedTrainerResultV1 (:276)   [G3]
  ...external weight update (out of lab)...
TrainerResultManifest (training_result.py:321) + NonContaminationEvidence (:287)  [G4]
  -->
FrozenHeldOutEvaluationPlan (:452)  submission_permitted=False   [G2 identity half]
  -->
SftSignalFreezeV1 (sft_signal.py:209)   [G2 rule half; published before outcomes]
  -- SftSignalObservationV1 (:281) cite freeze_digest -->
SftSignalDecisionV1 (:384)  ready_for_rl=Literal[False]         [G5; advisory only]
  -- (no type) -->                                               [G6: RL review is a human gate; no artifact]
```

Authority flows only through `ArtifactAuthority` (`artifact_authority.py:173`) at `bytes-verified`; no stage mints authority for another. Harbor execution never enters: `FrozenHeldOutEvaluationPlan.submission_permitted=Literal[False]` (`training_result.py:463`); the decision is `ready_for_rl=Literal[False]` (`sft_signal.py` decision/readiness). This satisfies the charter's "no trainer, Harbor, or admission authority moved" acceptance.

## 2. Training / evaluation firewall — as encoded

| Charter rule | Encoded by | Status |
|---|---|---|
| Eval identities frozen before outcomes | `FrozenHeldOutEvaluationPlan.plan_digest` + `verify_plan_digest` (`training_result.py:472-476`); `SftSignalFreezeV1.freeze_digest` recomputed on validation (`sft_signal.py:275-277`); observations must cite `freeze_digest` | PRESENT |
| Cluster-disjoint train vs held-out | `NonContaminationEvidence` refuses equal split digests and equal cluster-key digests, binds `split_integrity_binding_digest` (`training_result.py:304-318`); freeze requires every frozen task's cluster == pairing cluster (`sft_signal.py:265-269`) | PRESENT |
| Candidates never graduate to eval | `SyntheticTaskCandidate.descriptor_only=Literal[True]`, `fixture_only=Literal[True]`, `status="quarantined"`, `training_eligible=Literal[False]` (`curriculum_candidates.py:252-255`, graft review) | PRESENT (as a quarantine literal set; no promotion path exists) |
| Dual authority axes for training admission | `TrainingSourceBinding.registry_allowed_use=Literal["training"]` (`training_export.py:389`) AND `trial_admissibility_allowed_use=="causal"` + `causal-eligible` + `admissible` (`:399-405`; mirrored in `TrainingSourceRefV1:532-538`) | PRESENT — correct dual-axis form per Track F §2 (the `"causal"` literal is the evidence class, not a training authorization on its own) |
| Sealed test content never enters selection/generation/prompting | no type — enforced only by cluster disjointness above | MISSING as a first-class field (see §4, item F3) |

## 3. Exact type/field map for charter requirements

Legend: PRESENT = carried by a typed field; PARTIAL = concept present but not in the charter's form; MISSING = no typed carrier at `6df601b1`.

### 3a. Training-side per-example provenance (charter §"Training side")

| Charter field | Carrier | Status |
|---|---|---|
| source dataset / revision | `NormalizedTrainingEvidence.corpus_id` (:331), `source_cas_uri`/`source_artifact_digest` (:337-338); no dataset *revision* field for pinned public datasets | PARTIAL — local CAS sources fully identified; public dataset revision/license needs a field |
| producer model | none on `NormalizedTrainingEvidence`/`TrainingSourceBinding` | MISSING |
| producer harness | none | MISSING |
| task family | `task_family`, `benchmark_family` (:329-330) | PRESENT |
| cluster key | `cluster_key: str` (:333); freeze side uses `cluster_key_digest` | PRESENT (naming split; see §5) |
| split | `split: TrainingSplit` (:332); manifest `train/validation/test_split` refs (:600-602) | PRESENT |
| verifier / outcome authority | `admissibility: TrialAdmissibilityV1` (:343) + `trial_admissibility_record_authority: ArtifactAuthority` (:388) | PRESENT |
| normalization version | `extractor_name/version` (:395-396) + `TrainingExporterIdentityV1.digest` (:573-576) | PARTIAL — exporter identity only; no ATIF normalization version field |
| redaction result | `redaction_status` (:348); manifest forces `Literal["redacted"]` (:609) | PRESENT |
| content identity | `TrainingExampleRecord.content_digest` + `example_id` recomputed on validation (:460-470) | PRESENT |
| license / use | `registry_allowed_use=Literal["training"]` (:389) only | PARTIAL — no license identifier for public datasets |

### 3b. Study controls (charter §"Trace-to-SFT study")

| Charter control | Carrier | Status |
|---|---|---|
| arm identity A–D + selection policy | none (existing arm enums: `base|variant` in candidates, `baseline|candidate` in freeze) | MISSING |
| provenance/family block balancing across arms | none | MISSING |
| equal supervised assistant-token budget | none (`TrainingDatasetManifestV1.representation_counts` counts rows, :614) | MISSING |
| frozen tokenizer/template revision | `TrainerModelIdentityV1` in `TrainerBundleV1` (:249) `[INFERENCE: carries tokenizer/template digests per #361 review]` | PRESENT for the bundle; not bound at export |
| truncation refusal | `NormalizedTrainingEvidence.terminal_span_status` (:349); `_contains_trainer_only_key` refusal (:458) | PRESENT (terminal span); tool-call/result truncation refusal rides message-binding (#367) |
| per-family analysis, no pooled headline | `FamilyStatus` per family (`sft_signal.py:132-139`); decision derives from per-family statuses only | PRESENT |
| predeclared min effect / min pairs / protected families / interval method | `SftSignalFreezeV1.minimum_effect, minimum_eligible_pairs, protected_families, confidence_level, bootstrap_resamples` (:232-236), all required, no defaults | PRESENT |
| predeclared exclusion list | `SftExclusionCode` has only capture-incomplete codes (:116-120) | PARTIAL — capture exclusions only |
| stopping rule | none | MISSING |
| teacher / harness / action-space / tool-schema as provenance factors | freeze binds `environment_identity_digest` + `runtime_identity_digest` (:224-229); tool schema via `TrainingTool` (:351); teacher model and harness absent (see 3a) | PARTIAL |

### 3c. Model stages

| Charter item | Carrier | Status |
|---|---|---|
| S0: Qwen3-0.6B, `enable_thinking=false`, assistant-only loss, pure TRL | `RenderedTrainerPlanV1.adapter_contract="trl-sft-plan/v1"` (:375); model identity in `TrainerModelIdentityV1` | PRESENT as bundle fields; the specific S0 values are a fixture, not a type |
| S1: 4B LoRA, immutable revision, hardware class | `ImmutableRuntimeReceipts.hardware_receipt_digest` (`training_result.py:215`) binds hardware post hoc; no pre-declared hardware class on the bundle | PARTIAL — hardware class is receipt-side, not freeze-side |

### 3d. Environment pilot

| Charter item | Carrier | Status |
|---|---|---|
| parent deficit receipt | `CapabilityDeficitArtifactReceipt` consumed with live reverify (`curriculum_candidates.py`, graft review) | PRESENT |
| transform id/version, twin identity, seed, topology cluster | `SyntheticTaskCandidate` fields (graft review: transform, twin pair, cluster key, DRBG seed) | PRESENT |
| leak-scan receipts | `LeakScanResult` + typed `leak_scan_failed` (graft `6f2e7f47`) | PRESENT |
| hidden verifier; oracle=1 / NOP=0 / wrong-source mutant=0 | validation *plan* fields only; outcomes require execution | PARTIAL — plan-only by charter design |
| deterministic replay receipt | seed + spec implicit; no replay receipt type | MISSING (a receipt, not a plan) |
| training-only pool flag; never register | `training_eligible=Literal[False]` + `status="quarantined"` + no registration path | PRESENT (note: `training_eligible=False` is the *quarantine* state; certification into the training-only pool needs a distinct typed state — see F5) |

### 3e. Gates

| Gate | Typed refusal surface | Status |
|---|---|---|
| G0 source authority | `TrainingExclusionReason` + `TrainingSourceBinding.authority_is_accepted`; receipt-contract refusals | PRESENT |
| G1 recipe comparability | none | MISSING |
| G2 preregistration | freeze/plan digests; `OBSERVATION_FREEZE_MISMATCH` | PRESENT |
| G3 S0 interface | `BackendIncompatibilityV1` / `IncompatibilityCode` (`trainer_bundle.py:326`) | PRESENT |
| G4 trainer result | `TrainerResultManifest` status-conditional + expected-result parity (#361) | PRESENT |
| G5 held-out signal | `SignalStatus`/`FamilyStatus`, `FAMILY_REGRESSION`, protected-family rules | PRESENT |
| G6 RL review | no artifact; `ready_for_rl=Literal[False]` structural | PRESENT as a refusal; the *review* is human |
| closed-loop rerun rule | none | MISSING |

## 4. Boundary gaps — the minimum empirical interface (proven-missing fields)

No new manifest family. Each gap is one field or one small typed record attached to an existing type, owned by the lane that already owns that type.

- **F1 — provenance factors on the example.** Add `producer_model`, `producer_harness`, `source_dataset_revision`, `source_license` to `NormalizedTrainingEvidence`/`TrainingSourceBinding`/`TrainingSourceRefV1` (all three carry the same binding; see §5). Owner: Data Engineer (training_export). Without these, arms cannot be balanced within provenance strata and the charter's stop condition "fewer than two independent provenance strata" cannot be evaluated.
- **F2 — recipe/arm identity and budget.** One typed `SelectionRecipeV1` record referenced by `TrainingDatasetManifestV1` (not a new manifest): `arm: Literal["A","B","C","D"]`, `selection_policy_id`, `block_keys` (provenance × family), `supervised_assistant_token_budget: int`, `token_budget_tokenizer_digest`. G1 comparability refusals (`arm_changes_source_or_teacher`, `budget_mismatch`) attach here. Owner: Analyst defines, Data Engineer carries.
- **F3 — ownership domain.** Extend `TrainingSplit` (or add `ownership_domain: Literal["training-discovery","curation-development","sealed-test"]`) so sealed content is refusable at selection time, not only by cluster-disjointness after the fact. Owner: Data Engineer + Eval Runner.
- **F4 — freeze completeness.** Add to `SftSignalFreezeV1`: `stopping_rule` (typed), `preregistered_exclusions` (closed set beyond capture-incomplete), `hardware_class` for S1. Owner: Researcher-Evals (sft_signal). These are charter-mandated predeclarations with no carrier.
- **F5 — certified training-only pool state.** `SyntheticTaskCandidate` today has only `quarantined`; the charter's "certification can admit them to a training-only pool" needs a second typed state (`training_only_certified`) that still forbids registration/eval reuse. Owner: Synthetic Environment Engineer. Plus a `ReplayReceiptV1` (seed, spec digest, oracle/NOP/mutant outcomes) as the certification evidence.
- **F6 — closed-loop rerun binding.** `CapabilityDeficitArtifact` needs a `discovery_evidence_epoch`/checkpoint digest so a deficit label can be tied to the checkpoint it was mined against and refused as stale after a model update. Owner: Analyst (capability_deficits).

Everything else the charter names already has a typed carrier; the empirical work (census, recipe, S0, freeze) can proceed against existing types plus F1–F4.

## 5. Second-convention risks (must not grow)

| Concept | Duplicated as | Ruling |
|---|---|---|
| source binding | `TrainingSourceBinding` (:364) vs `TrainingSourceRefV1` (:507) — near-identical field sets | Add F1 fields to BOTH via one shared base, or collapse; never a third copy |
| cluster identity | `cluster_key: str` (export) vs `cluster_key_digest` (freeze/plan) | Digest is derived from key; define the derivation once |
| split vocabularies | `TrainingSplit`, manifest split refs, freeze `heldout_split_digest` | F3 ownership domain must extend `TrainingSplit`, not add a fourth vocabulary |
| backend identity | `TrainerBackendIdentityV1` (`trainer_bundle.py:183`) vs `TrainingBackendIdentity` (`training_result.py:238`) | Result side should consume the bundle type |
| arm enums | `base|variant` (twins), `hinted|unhinted`, `baseline|candidate` (freeze) | Distinct semantics — keep, but arms A–D (F2) must be a fourth, *named* enum, not a reuse |
| capture-status taxonomies | three literal sets across evidence/quality/exclusion | Do not add a fourth; F4 exclusions reuse `SftExclusionCode` |

## 6. Verification

Read-only: every `module:line` above was read from `6df601b1` via `git show`/worktree; a scout map (agent `TrainingChainMap`) produced the initial coverage and duplicate-concept list, which I re-verified against source for every PRESENT/MISSING call in §2–§3a. No tests run, no code changed, no manifest family created. Branch `research/tt-arch-contract-audit` off `6df601b1`, single file at the leased path.


## 7. Addendum — M3 selection-prereg conformance (PR #370 @1dd17b19, docs-only)

PR #370 (`research/inbox/selection-recipe-prereg-20260904.md`) is the first consumer of the F2 shape. Type-conformance findings:

- PASS — arms A–D are a *named* recipe enum with distinct deterministic orderings (ordering-only fill; "prefer, never exclude" matches charter arm semantics). No reuse of `base|variant` / `baseline|candidate` / `hinted|unhinted`.
- PASS — block keys = family × source_stratum × provenance_stratum × difficulty, with `cluster_key = family|task_name`; freeze-side stays `cluster_key_digest`. Cluster naming split respected; no third vocabulary.
- PASS — budget: supervised assistant-target tokens under one frozen student tokenizer/template; census proxy chars/bytes explicitly not the budget; bundle-time recompute refusal; truncation prohibited (G3). Matches F2 `supervised_assistant_token_budget` + tokenizer digest.
- PASS — G1 refusals declared: recipe refuses on any block ordering change after family binding lands; prereg voids on screen change; bundle-time recompute refuses arm-infeasibility/ordering changes. Maps 1:1 onto F2's `arm_changes_source_or_teacher` / `budget_mismatch` refusal intent.
- BOUND (not blocked): `template-family-rule/v1` is an explicitly interim family derivation (registry family binding is MISSING at spine per §3a). Requirement recorded: M1 follow-up (F1) adds registry-bound family binding; the recipe already refuses if re-derivation changes block orderings.
- BOUND (not blocked): exclusion set must EXTEND `SftExclusionCode` when it lands in `SftSignalFreezeV1` (F4 anticipated "closed set beyond capture-incomplete") — a parallel census-missingness enum would violate §5. `tool_sequence_sha256` (arm D) is a row signature owned by the recipe record, not a new identity scheme.
- Honest prereg discipline confirmed: 0/164 strictly eligible, C/D orderings declared degenerate on the current corpus, provenance independence declared borderline — reported, not filled (charter stop conditions honored). No bundle may materialize before G2 per §8.

No conflict with the integrated types; no second manifest family; docs-only file on the leased analyst path.