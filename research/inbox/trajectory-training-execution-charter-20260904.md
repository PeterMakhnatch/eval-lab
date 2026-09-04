---
type: research-program
topic: trajectory-to-training-and-targeted-environments
author: researcher-evals
date: 2026-09-04
status: active
base: integrate/spine-batch1@1702966d76fce38fdb6f0809365780529644d045
epistemic: preregistration-and-execution-charter
---

# Trajectory-to-Training Execution Charter

## Decision

Run two related but firewalled programs:

1. **Trace-to-SFT recipe study:** determine which provenance-aware selection recipe produces held-out behavioral improvement at equal supervised assistant-token budget.
2. **Capability-targeted environment pilot:** turn one certified failure mechanism into a fixed, executable, training-only environment family with independent verification.

Do not start GRPO, PPO, SPADE co-evolution, or any other online RL loop. Eval Lab remains the evidence, admission, and evaluation control layer; an external system performs weight updates and returns a digest-bound result manifest.

No billable model call, cloud/GPU execution, task registration, policy change, or publication is authorized by this charter.

## Current starting point

The integrated base already implements the offline chain:

```text
NormalizedTrainingEvidence
  -> TrainingDatasetManifestV1
  -> CapabilityDeficitArtifactReceipt
  -> QuarantinedCurriculumCandidate
  -> TrainerBundleV1
  -> RenderedTrainerPlanV1
  -> TrainerResultManifest
  -> FrozenHeldOutEvaluationPlan
  -> SftSignalFreezeV1 / SftSignalDecisionV1
```

The chain is deliberately plan-only until an external result is supplied. The missing work is empirical: a real, source-diverse corpus; a preregistered recipe comparison; an external SFT smoke; and a frozen held-out comparison.

A read-only census of the committed promoted corpus on this base found:

| Observed property | Count |
|---|---:|
| Promoted ATIF trajectories | 164 |
| Job cohorts | 18 |
| Reward 1.0 / 0.0 | 100 / 64 |
| `zai-coding-plan/glm-5.3-flash` / `gpt-5.6-terra` / `zai-coding-plan/glm-5.3` | 152 / 9 / 3 |
| Assistant turns 1–4 / 5–9 / 10+ | 80 / 55 / 29 |
| Current `benchmark_contract.json` / historical contract / neither | 3 / 130 / 31 |
| ATIF traces with committed system or user text redacted | 164 |

This is useful diagnostic evidence, not a ready scientific SFT corpus. The committed ATIF files alone cannot reconstruct complete conversational inputs, provenance is dominated by one model and a small set of related synthetic families, and no real-corpus `TrainingDatasetManifestV1` is present. The first execution gate is therefore a source and authority census, not training.

## Frozen boundaries

### Training side

Training candidates may come from:

- license-compatible, digest-pinned public trajectory datasets;
- source-authorized local runtime/CAS evidence admitted for training;
- newly generated **training-only** environments and fresh rollouts.

Every included example must preserve source dataset/revision, producer model and harness, task family, cluster key, split, verifier/outcome authority, normalization version, redaction result, and content identity. Model-authored quality or capability labels remain hypotheses unless certified by a deterministic rule or reviewed evidence contract.

### Evaluation side

Evaluation tasks, templates, seeds, environments, verifiers, and checkpoint-comparison rules are frozen before training outcomes are observed. They are cluster-disjoint from training and curation-development material. Sealed evaluation content never enters source selection, environment generation, prompt construction, training, or model-assisted analysis.

Training candidates never graduate into the evaluation pool. A separate evaluation family may test the same mechanism only when it was independently authored from disjoint parents, templates, topology classes, and seeds.

### Closed-loop rule

Discovery evidence may generate a training hypothesis. After a model update, the lab must rerun the diagnostic on fresh discovery evidence before generating another curriculum. A prior capability label is not permanent ground truth.

## Trace-to-SFT study

### Primary question

Holding checkpoint family, tokenizer/template, renderer, optimizer, seed policy, total supervised assistant-token budget, source strata, and held-out evaluation fixed, which selection policy produces the strongest predeclared family-specific held-out effect without a protected-family regression?

### Arms

| Arm | Selection policy | Purpose |
|---|---|---|
| A — stratified random | Verified successes sampled within source, task-family, and difficulty strata | Honest baseline |
| B — concise process | Same strata, prefer fewer than five assistant turns; no source stratum may disappear | Test the short-trajectory hypothesis without changing provenance mix |
| C — process quality | Same strata, prefer grounded tool use, post-mutation verification, valid terminal output, and no blind identical retry | Test process-quality selection |
| D — quality plus structure | Arm C plus diversity over tool-call graph, action sequence, and recovery pattern | Test whether nonredundant process coverage adds signal |

A later repair arm may use certified failure-targeted examples, but it is not mixed into the first selection ablation. Its causal question differs from success-trace selection.

### Design controls

- Split by upstream source, task/template family, and topology cluster; never by row or filename.
- Use three ownership domains: training/discovery, curation-development, and sealed test.
- Balance each arm within provenance and task-family blocks before applying quality selection.
- Equalize the number of supervised assistant target tokens, not rows or raw trajectory tokens.
- Freeze one tokenizer/template revision and reject truncation of tool calls, tool results, or terminal assistant targets.
- Analyze each task family separately; any pooled summary is secondary.
- Predeclare minimum effect, minimum eligible pairs, protected families, interval method, exclusions, and stopping rule in `SftSignalFreezeV1`.
- Treat teacher model, harness, action space, and tool schema as provenance factors. ATIF normalization does not make semantically different trajectories interchangeable.

### Model stages

- **S0 plumbing:** Qwen3-0.6B, `enable_thinking=false`, assistant-only loss, deterministic fixture bundle, pure TRL plan. This proves formatting and result-manifest compatibility only.
- **S1 scientific run:** Qwen3 4B-class LoRA target, with exact immutable revision and hardware class frozen after the corpus census and template preflight. No claim is made from S0.

## Capability-targeted environment pilot

Target one existing supported mechanism: **wrong binding or addressing under conflicting sources**.

Build a fixed training-only tool environment with:

- multiple sources carrying conflicting values and explicit authority metadata;
- stateful tools for source discovery, source reading, and final binding submission;
- a base/variant twin whose only delta is authoritative-source placement or addressing permutation;
- a deterministic reset and bounded action budget;
- a hidden verifier that checks exact binding and rejects shortcut answers;
- oracle success, NOP failure, wrong-source mutant failure, and deterministic replay;
- parent deficit receipt, transform ID/version, twin identity, seed, topology cluster, and leak-scan receipts.

The generator emits quarantined candidates first. Certification can admit them to a training-only pool. It may not register them as evaluation tasks, adapt them against the sealed test checkpoint, or optimize difficulty until a target model fails.

## Employee task chart

| Employee type | Immediate owned task | Deliverable | Acceptance evidence | Depends on |
|---|---|---|---|---|
| **Researcher–Evals / Program Lead** | Own the hypothesis, arm semantics, source-comparability judgment, and final interpretation | This charter; source decisions; experiment decision record | Every conclusion names evidence grade, denominator, provenance limits, and next decision | None |
| **Architect** | Audit the integrated contract chain and freeze the minimum empirical interface; do not invent a second manifest family | Contract review mapping each charter field to an existing type or one proven missing field | State diagram; exact type/field map; training/eval firewall; no trainer, Harbor, or admission authority moved | Charter |
| **Data Engineer** | Produce a non-mutating source/authority census across local CAS/runtime evidence and pinned public datasets | Machine-readable source manifest plus typed exclusion ledger | Counts by source/revision/license/model/harness/family/outcome/redaction/authority/split; every rate has denominator; byte identities reopen | Architect field map |
| **Analyst** | Define and run the provenance-aware selection census, then freeze the four-arm sampling recipe | Analysis artifact and preregistration inputs | Same source/family blocks across arms; equal target-token budget calculation; no cross-family headline; uncertainty and missingness explicit | Data census |
| **Training Engineer** | Validate S0 rendering, materialize the four S1 bundles, and operate the external TRL wrapper when separately authorized | Four bundle manifests, four rendered plans, external result manifests | Tool-call round-trip; assistant masks only; no labels/logprobs/reward fields; no semantic truncation; checkpoint and effective config digests | Data + Analyst + Architect |
| **Eval Runner** | Freeze held-out identities before training and execute baseline/candidate pairs only after valid trainer results and separate run approval | Frozen held-out plan, run specs, immutable outcome receipts | Exact pair membership; complete capture accounting; same harness/environment; oracle/NOP task validity; no training data access | Architect now; Trainer result later |
| **Synthetic Environment Engineer** | Implement the fixed conflicting-source environment pilot from certified deficits | Quarantined base/variant candidate family and validation receipts | Single delta; deterministic reset/replay; oracle=1; NOP and wrong-source mutants=0; no registration or sealed-eval reuse | Certified deficit receipts |
| **Experimental Methodologist / Independent Reviewer** | Review the preregistration before bundle materialization and the analysis after outcomes | Signed design review and result review | Power/precision rationale; cluster leakage audit; multiplicity and protected-family rule; attempts to falsify source-quality claims | Analyst prereg; Eval outcomes |
| **Integration Lead** | Register missions, grant disjoint path leases, sequence merges, and preserve exact-head reviews | Mission rows, branch/PR order, integration receipts | No overlapping writer leases; focused checks at exact heads; generated docs only after integration | All work briefs |

## Execution order

```text
Wave 0: Integration Lead registers disjoint missions
    |
    +-- Architect contract audit
    +-- Data Engineer source/authority census
    +-- Analyst census specification
    +-- Eval Runner held-out ownership/freeze draft
    +-- Synthetic Environment Engineer fixed-pilot design
    |
Wave 1: Program Lead + Methodologist freeze sources, arms, exclusions, and signal rule
    |
Wave 2: Data Engineer materializes authoritative examples
        Training Engineer proves S0 and renders equal-budget S1 bundles
        Synthetic Environment Engineer certifies training-only candidates
    |
Wave 3: external S1 weight updates (separate explicit approval required)
    |
Wave 4: Eval Runner executes frozen baseline/candidate pairs
    |
Wave 5: SFT signal gate decides supported / refuted / inconclusive / unavailable
    |
Wave 6: only a supported, mechanism-specific signal licenses an RL design review
```

## Gates

| Gate | Opens when | Refuses when |
|---|---|---|
| G0 — source authority | Source bytes, license/use, lineage, admission, redaction, and split identities re-open and agree | Missing/ambiguous authority, hidden verifier content, secrets, redacted-but-unrecoverable input, prohibited corpus |
| G1 — recipe comparability | Arms share provenance/family blocks and equal supervised target-token budget | Quality policy also changes source, teacher, family, template, or action space |
| G2 — preregistration | Held-out identities and `SftSignalFreezeV1` are published before outcomes | Outcome-informed rule, pooled-only success, missing protected-family rule |
| G3 — S0 interface | Template/tool round-trip, masks, truncation, bundle, and expected result contract validate | Empty/misaligned assistant target, lost call identity, labels/logprobs/reward input, semantic truncation |
| G4 — trainer result | External manifest matches exact plan, bundle, input, runtime, and produced checkpoint | Missing/mismatched digest, non-finite update, hidden field, incomplete artifacts |
| G5 — held-out signal | Complete cluster-disjoint baseline/candidate pairs meet the predeclared family rule | Null, negative, incomplete, contaminated, pooled-only, or protected-family regression |
| G6 — RL review | G5 is supported for at least one certified mechanism and the new RL question requires on-policy learning | Training loss alone, S0 completion, adaptive-eval gain, or generic average improvement |

## Immediate stop conditions

Stop and report rather than filling gaps if:

- the source census cannot form at least two independent provenance strata;
- public dataset licenses or redistribution terms are unresolved;
- local committed redactions cannot be rehydrated through trusted source authority;
- source and quality policy cannot be separated experimentally;
- a sealed evaluation parent/template/topology appears in training material;
- the external trainer cannot emit the required digest-bound result manifest.
