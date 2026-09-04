---
type: design-freeze
program: trajectory-to-training
author: eval-runner
mission: M4-EVAL-HOLDOUT-FREEZE
date: 2026-09-04
status: local-control-frozen-m1-conformant-scientific-freeze-blocked
base: integrate/spine-batch1@e3856849
execution: none
---

# Held-out identity and checkpoint-pair freeze design

## Decision

Use the existing contract chain; do **not** create a second held-out manifest family.

1. `TrainerBundleV1.evaluation_set` is the pre-training held-out identity freeze.
2. After a valid, authority-reverified trainer result exists, `FrozenHeldOutEvaluationPlan` binds that immutable evaluation set to the produced candidate checkpoint.
3. Before any evaluation outcome exists, `SftSignalFreezeV1` binds the baseline/candidate checkpoint pair, exact pair membership, harness/runtime identities, and the decision rule.
4. Only a later, separately approved execution request may turn the frozen pairs into Harbor specs.
5. Outcomes are admissible only when they rehydrate against the exact freeze and complete pair membership.

This brief freezes a deterministic **local-control-only** identity set and pair contract. It does not freeze the scientific held-out set: M1’s field map is now authoritative, while the source/authority census has not yet established a safe, cluster-disjoint scientific set. Filling scientific identities here would violate the charter and the training/evaluation firewall.

## Lease and scope

Mission: `M4 EVAL-HOLDOUT-FREEZE`.

Exclusive writer lease: `research/inbox/held-out-freeze-design-20260904.md`.

Approved scope:

- plan-only;
- `submission_permitted = false`;
- no outcome ingestion;
- no training-data reads;
- no task registration;
- no billable model, network, GPU, Harbor, queue, or trainer action;
- no edits to `sft_signal.py`, `paired_intervention.py`, `paired_outcome.py`, `training_result.py`, or shared schemas.

M1 published its field map at `e3856849` in `architect-contract-audit-20260904.md`. This design now conforms to F3, F4, and §5; the contingent code lease still requires explicit activation by the Integration Lead.

## Existing authority map

| Required boundary | Existing owner | Evidence on base | Decision |
|---|---|---|---|
| Canonical held-out task identities | `TrainerTaskIdentityV1` / `TrainerEvaluationSetV1` | `trainer_bundle.py:190-237` | Reuse exactly. |
| Pre-training attachment of held-out set | `TrainerBundleV1.evaluation_set` | `trainer_bundle.py:214-237`, bundle field at `:261` | Treat the bundle digest plus evaluation-set digest as the first freeze. |
| Candidate-result/checkpoint handoff | `FrozenHeldOutEvaluationPlan` | `training_result.py:452-476`, constructed only through result validation at `:797-867` | Reuse after trainer result; it is not the pre-training freeze. |
| Checkpoint-specific signal preregistration | `SftCheckpointIdentityV1` / `SftSignalFreezeV1` | `sft_signal.py:194-278` | Reuse as the pre-outcome pair contract. No subclass of `PairedInterventionOutcomeV1`. |
| Result/checkpoint/runtime chain | `_chain_reasons` | `sft_signal.py:538-595` | Keep fail-closed and authority-reverified. |
| Observation membership audit | `SftSignalObservationV1` / `_audit_observations` | `sft_signal.py:281-310`, `:663-738` | Reuse after the missing pair fields are placed by M1. |
| Generic intervention statistics | Track G primitives | `paired_outcome.py:63-79`, `:190-281` | Statistical shape may be reused; SFT owns a checkpoint-specific contract and never subclasses or edits Track G’s type. |

## M1 conformance

- **F3 — ownership domain:** M4 consumes the exact `ownership_domain` carried through the existing `TrainingSplit` boundary. It never defines an Eval Runner split/domain enum. Scientific held-out tasks require `sealed-test`; training/discovery and curation-development are refused. Cluster-disjointness remains an independent required proof.
- **F4 — freeze completeness:** the existing `SftSignalFreezeV1` must own `stopping_rule`, `preregistered_exclusions`, and `hardware_class`. Pair membership composes into that same freeze; no parallel freeze manifest is created.
- **§5 — no third binding copies:** pair rows reference the canonical `TrainerTaskIdentityV1` and existing `SftCheckpointIdentityV1`; they do not restate source, task, split, backend, or capture schemas under new names. F3 extends the existing split boundary, and exclusions extend `SftSignalRefusalCode` / `SftExclusionCode` rather than creating Eval Runner vocabularies.

## Why a separate pre-training manifest is rejected

`TrainerBundleV1` already freezes `TrainerEvaluationSetV1` before external weight updates. Creating `HeldOutEvaluationManifestV1` would duplicate task, suite, split, and cluster identities and allow two authorities to drift. The only missing information belongs as a subordinate exact-pair projection inside the existing SFT freeze boundary, subject to M1’s field map.

`FrozenHeldOutEvaluationPlan` cannot serve as the first freeze because its constructor requires a validated completed trainer result and produced checkpoint (`training_result.py:797-867`). Conversely, `SftSignalFreezeV1` is correctly pre-outcome but currently does not enumerate pair IDs or seeds. The chain therefore needs composition, not replacement.

## Minimal subordinate type sketch after M1

This is a placement sketch, not an implementation or a parallel schema. Every nested identity reuses the named existing type:

```python
class SftFrozenPairIdentityV1:
    pair_id: str
    pair_ordinal: int
    task: TrainerTaskIdentityV1
    generator_seed: int | str
    baseline_checkpoint: SftCheckpointIdentityV1
    candidate_checkpoint: SftCheckpointIdentityV1
    harness_identity_digest: Digest
    runtime_identity_digest: Digest
    arms: Literal[("baseline", "candidate")]

# F4 and exact-pair additions to the existing SftSignalFreezeV1:
frozen_pairs: tuple[SftFrozenPairIdentityV1, ...]
pair_set_digest: Digest
stopping_rule: SftStoppingRuleV1
preregistered_exclusions: tuple[SftExclusionCode, ...]
hardware_class: SftHardwareClassV1
submission_permitted: Literal[False]
separate_run_approval_required: Literal[True]
```

`SftStoppingRuleV1` and `SftHardwareClassV1` names above denote F4-owned subordinate records; Researcher–Evals owns their exact fields. M4 neither defines them independently nor substitutes free-form dictionaries.

F3 is not copied into `SftFrozenPairIdentityV1`. The freeze constructor must consume the existing training/export split binding and require its canonical `ownership_domain == "sealed-test"`. The literal and its validation live on the extended `TrainingSplit` boundary, not in an Eval Runner enum or a new split record.

Required invariants:

- canonical unique `pair_id`, ordinal, canonical `TrainerTaskIdentityV1`, and seed;
- exactly one existing `SftCheckpointIdentityV1(role="baseline")` and one with `role="candidate"` per pair;
- every task in `held_out_plan.evaluation_set.tasks` has exactly its predeclared pair membership;
- every scientific evaluation input resolves through the existing split binding to `ownership_domain == "sealed-test"`;
- no task, seed, pair, template, verifier, environment, harness, runtime, checkpoint, F4 rule, or ownership domain may appear after freeze unless already bound;
- baseline and candidate checkpoint digests differ, while model family/revision compatibility is explicit;
- both arms use identical task, seed, verifier, environment, harness, runtime limits, and capture contract;
- `pair_set_digest` canonically binds the ordered tuple;
- `preregistered_exclusions` is canonical and uses the existing `SftExclusionCode`;
- no outcome, trial ID, reward, metric value, or result-derived label is a freeze input;
- construction cannot authorize submission.

Exact template/variant identity must resolve through the canonical task identity selected by M1; M4 does not introduce task, source, split, capture, backend, or refusal binding copies.

## Freeze chronology

```text
F0 — before training
  TrainerBundleV1.evaluation_set + bundle digest
  -> exact held-out task/suite identities; training census must exclude these clusters

F1 — after external training, before evaluation
  bytes-verified TrainerResultManifest
  -> exact candidate checkpoint and submitted bundle/plan chain

F2 — still before evaluation outcomes
  FrozenHeldOutEvaluationPlan
  + baseline checkpoint identity
  + candidate checkpoint identity
  + exact frozen pair tuple
  + decision/protected-family rule
  -> SftSignalFreezeV1

F3 — separate approval
  frozen pair contract + valid trainer result + explicit run authorization
  -> ordinary ExperimentSpec/Harbor planning path

F4 — outcomes
  complete baseline/candidate observations rehydrated against F2
```

No F3 or F4 action is authorized by this mission.

## Frozen local-control identities

These identities reuse the existing `test_sft_signal.py:52-125` fixture and are intentionally obvious non-scientific sentinels. They prove deterministic contract composition only. They are not registered tasks, sealed evaluation content, trainer inputs, or evidence of model capability.

### Evaluation set

- Suite name: `funcdag-heldout-core`
- Suite digest: `sha256:4eb30a17d9bef97514d434b2b7fea3da422ed0f989d4e216e3edbf85cb648f55`
- Task-set digest: `sha256:c053e981289c32301e6ed434d14d57227c77057b30a7373ac76253365f33e18b`
- Held-out cluster digest: `sha256:6f2e5895285549948171ab9704a2211b5d95f99dec28be82713e0a16dbcf2def`
- Harness identity digest: `sha256:11eb0f5544ae92ae3a7a84e6497b720fd2d3d2ef216563ae79f97ed44ee5daac`

| Task | Task digest | Seed | Verifier digest | Environment digest |
|---|---|---:|---|---|
| `funcdag/conflict-heldout-01` | `sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee` | 17 | `sha256:1111111111111111111111111111111111111111111111111111111111111111` | `sha256:2222222222222222222222222222222222222222222222222222222222222222` |
| `funcdag/permutation-heldout-02` | `sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff` | 29 | `sha256:3333333333333333333333333333333333333333333333333333333333333333` | `sha256:4444444444444444444444444444444444444444444444444444444444444444` |

### Checkpoint identities

Both are fixture sentinels over `Qwen/Qwen3-0.6B@fixture` with model digest `sha256:9999999999999999999999999999999999999999999999999999999999999999`.

- Baseline checkpoint: `sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb`
- Candidate checkpoint: `sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc`

### Exact pairs

| Ordinal | Pair ID | Task | Seed | Arms |
|---:|---|---|---:|---|
| 1 | `pair-1622ca059b337ec8adfe` | `funcdag/conflict-heldout-01` | 17 | baseline, candidate |
| 2 | `pair-c5112e03ef703c7c8967` | `funcdag/permutation-heldout-02` | 29 | baseline, candidate |

Frozen decision rule for the control:

- metric: `task_success`;
- direction: higher;
- minimum effect: `0.05`;
- confidence level: `0.95`;
- minimum eligible pairs: `2`;
- protected family: `funcdag`;
- claim permission: false.

Canonical pair-contract digest:

`sha256:37db3a34f154b2fc5e58aba1e61db0d21f39d189e7be3f460f7e4b86cef35e69`

Canonical local-control freeze digest:

`sha256:f44f26529ebdb248925fcde7017a64d2b716420211efcee2c79b6234354b3c65`

The digest calculator uses only typed in-memory fixture identities and the repository’s existing `compute_prefixed_sha256`, `trainer_task_set_digest`, `trainer_evaluation_suite_digest`, and `compute_cluster_key_digest` functions. It does not open task content, training data, outcomes, Harbor, a network, or a trainer.

Two independent local invocations produced the same canonical output SHA-256:

`83903a33449459a2aa17a7b2097f90a1391e90f0f9b873fbca6a81b41d63d4b7`

## Admission and refusal controls

The future implementation must extend the existing `SftSignalRefusalCode` and `SftExclusionCode` surfaces before ordinary run planning. It must not introduce an Eval Runner refusal or capture taxonomy:

| Condition | Existing surface / required disposition |
|---|---|
| F3 split binding lacks canonical `ownership_domain` | extend `SftSignalRefusalCode` with `ownership_domain_unavailable`; no scientific freeze |
| Ownership domain is not `sealed-test` | extend `SftSignalRefusalCode` with `ownership_domain_mismatch` |
| F4 stopping rule, preregistered exclusions, or hardware class absent | extend `SftSignalRefusalCode` with `freeze_completeness_missing` |
| Scientific task identity/source authority absent | extend `SftSignalRefusalCode` with `heldout_identity_unavailable` |
| Data census has not proved train/development/held-out cluster disjointness | existing split/held-out mismatch surface; no freeze |
| Trainer result missing, invalid, or not bytes-reverified | existing result structure/authority refusal |
| Candidate checkpoint does not descend from the frozen baseline/input chain | existing `checkpoint_chain_mismatch` |
| Pair ID, canonical task identity, seed, environment, harness, runtime, checkpoint, or arm differs | extend `SftSignalRefusalCode` with `pair_identity_mismatch` |
| Any frozen pair lacks exactly two completed arms | extend existing `SftExclusionCode` with whole-pair capture exclusion |
| Separate run approval absent | ordinary policy/queue refusal; remain plan-only |
| Any outcome is supplied while constructing the freeze | existing invalid-input/freeze mismatch surface; no freeze |
| Local fixture is presented as scientific evidence | fixture scope remains structurally claim-ineligible |

Refusals are data, not defaults. No caller-set boolean may upgrade unavailable identity or approval into permission.

## Ordinary execution controls, for the later lease

When F3 and F4 are separately implemented and execution is separately authorized:

- use the ordinary `ExperimentSpec`/queue/Harbor path; no Track-specific queue;
- baseline and candidate specs differ only by checkpoint artifact identity;
- exact same task instance, seed, template, verifier, environment, harness version, runtime class, token/time limits, and capture requirements;
- deterministic interleaving may order pairs but may not change membership;
- retries mint replacement attempts without changing pair assignment; any failed arm excludes the complete pair;
- oracle and NOP run only as local task-validity controls and never enter checkpoint-effect denominators;
- every requested arm settles to completed, typed-failed, or typed-rejected; silent loss is impossible;
- no execution begins until the trainer result is valid and Peter separately approves the run.

## Blockers and handoff

1. **Contingent code lease:** M1 is complete at `e3856849`; Integration Lead must now grant exact code paths. Implementation must follow F3/F4/§5 and may not edit owner files without an additional coordinated lease.
2. **F3 producer field:** the canonical `ownership_domain` extension to the existing `TrainingSplit` boundary is not yet implemented. Scientific freeze must refuse until it is available; M4 will not create a substitute field.
3. **F4 owner fields:** Researcher–Evals must place the typed stopping rule, preregistered exclusions, and hardware class on `SftSignalFreezeV1`; M4 will consume them rather than define another freeze.
4. **Scientific held-out identity set:** unavailable until Program Lead/Architect select exact identities and Data proves source/parent/template/topology/cluster disjointness. M4 will consume that proof; it will not inspect training data.
5. **Valid candidate trainer result:** required before binding the real candidate checkpoint or rendering `FrozenHeldOutEvaluationPlan`.
6. **Separate run approval:** required after freeze and before any baseline/candidate execution.

Until blockers 2–6 resolve, the only frozen artifact is the local-control identity set above, with `submission_permitted=false` and `outcome_claim_permitted=false`.
