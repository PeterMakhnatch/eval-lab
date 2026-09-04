---
type: experiment-design
topic: fixed-conflicting-source-training-environment
mission: M5-SYNTH-PILOT-DESIGN
author: synthetic-environment-engineer
date: 2026-09-04
status: proposed
base: 6df601b1
scope: design-only
allowed_use: training-candidate-design
---

# Fixed conflicting-source training environment pilot design

## Decision

Use the existing `funcdag_cross_source_conflict` curriculum transform to define one fixed, cleanroom, training-only base/variant environment pair. The pair tests exact binding under conflicting sources. Its sole intervention is the placement of explicit authoritative-source metadata; source identities, addresses, conflicting records, instructions, tools, action budget, runtime, verifier, and control procedures remain invariant.

This mission produces the design only. It does not generate candidates, register tasks, read or adapt sealed evaluation material, invoke a model, run Harbor, start training, or confer training eligibility.

## Existing contract reuse

No second curriculum manifest family is needed.

| Required concept | Existing authority at `6df601b1` | Pilot use |
|---|---|---|
| Certified parent | `CapabilityDeficitArtifactReceipt` plus independently supplied `CapabilityDeficitOutputExpectation` | Mandatory input; live byte authority must reopen before materialization |
| Eligible mechanism | `TRANSFORM_ELIGIBILITY["funcdag_cross_source_conflict"]` | Accept only `wrong-binding-or-addressing` or `wrong-graph-traversal` parents with an eligible proposed intervention dimension |
| Fixed factors | `CrossSourceConflictSpec` | Preserve `entity_count`, `source_count`, `conflict_axis`, and `distractor_fields` across twins |
| Single delta | `TwinBinding.one_variable_delta` | Must equal `authoritative_source_index` |
| Nonleakage | `NonleakageBinding` | Preserve the parent `train` or `unassigned` split and cluster key; held-out and calibration parents are refused upstream |
| Verifier intent | `ValidationPlan` | Reuse the checked-in hidden-verifier, leak-scan, and control requirements |
| Quarantine state | `SyntheticTaskCandidate` and `SynthesisResult` | Preserve `status=quarantined`, `training_eligible=false`, and non-general authority |

The executable materializer proposed here is downstream of `SynthesisResult`. It requires an externally frozen `contrast_pair_id`, rehydrates the complete result, and resolves exactly one matching cross-source contrast pair; zero or multiple matches refuse. It then live-reverifies both candidates' identical parent receipt against the independently supplied output expectation. A descriptor is selection authority only; it is not task, registration, training, or verifier authority.

## Admission preconditions

Materialization is fail-closed unless all conditions hold:

1. The input rehydrates as one intact `SynthesisResult` at the pinned generator implementation digest.
2. The externally frozen `contrast_pair_id` resolves exactly one ordered `base` and `variant` candidate without score- or outcome-based selection.
3. Both candidates use `funcdag_cross_source_conflict` and declare `authoritative_source_index` as the only delta.
4. Candidate provenance is identical, and its `CapabilityDeficitArtifactReceipt` passes live byte-authority re-verification against an independently obtained `CapabilityDeficitOutputExpectation`.
5. The parent attribution gate is `deficit_supported`, its family and intervention dimension are eligible, and its split is `train` or `unassigned`.
6. All existing candidate leak-scan results pass.
7. Embedded typed parent provenance remains verifier-side metadata only. Parent source bytes, raw trajectories, task text, answers, policies, and evidence excerpts are neither separately supplied nor projected into scenario generation or agent-visible bytes.
8. A separately owned topology-collision gate confirms the declared generated topology identifier is absent from the frozen evaluation inventory. The materializer receives only the collision decision and inventory digest, never sealed contents.

The design binds no empirical parent receipt. Until a certified receipt and independent expectation are supplied, the only valid disposition is `parent_unavailable`; no environment bytes may be emitted.

## Fixed environment

### Shared scenario generation

Derive all shared scenario bytes from:

```text
shared_seed = H(
  "cross-source-binding-pilot/v1",
  twin_pair_id,
  seed,
  entity_count,
  source_count,
  conflict_axis,
  distractor_fields
)
```

Declare a topology identifier separately from content identity:

```text
topology_id = H(
  "cross-source-binding-topology/v1",
  entity_count,
  source_count,
  conflict_axis,
  tool_schema_digest,
  "action-budget=source_count+2"
)
```

It excludes parent identity, candidate identity, seed, values, and authoritative index. This lets a separate owner test topology collision without exposing or adapting to sealed evaluation content.

Do not include `candidate_id`, arm, or `authoritative_source_index` in the `shared_seed` derivation. This guarantees that both twins receive byte-identical source IDs, opaque source addresses, entity IDs, conflicting values, distractors, source order, and instructions.

Each of two to eight sources contains a complete record for every entity. For every entity, at least two sources disagree on the declared conflict axis. Values are synthetic and generated locally; no value or wording is copied from the parent evidence. Source addresses are opaque stable handles, not array indices.

Authority is represented in one public `authority-map.json` projection. It binds exactly one source address to role `authoritative`; all others are `reference`. The runtime's discovery response exposes this metadata, so authority is explicit rather than hidden. Moving this one binding is the intervention.

### Stateful tool surface

The future runtime exposes exactly three tools:

1. `discover_sources()` returns the ordered source catalog: source ID, opaque address, authority role, and schema version.
2. `read_source(address)` returns the records and distractor metadata for one exact discovered address.
3. `submit_binding(source_address, bindings)` records one terminal submission mapping every entity ID to the selected source's value on the conflict axis.

The action budget is exactly `source_count + 2`: one discovery, one read of every source, and one submission. Every attempted tool call consumes one action. Submission is terminal. Calls after submission fail without mutating accepted state.

The agent-visible instruction states only the operational contract: discover the sources, inspect every source, follow the explicit authority metadata, and submit a complete binding. It contains no source address, entity ID, generated value, authoritative index, expected binding, verifier condition, parent evidence, or evaluation reference.

### Staging boundary

A future implementation writes only to ignored derived/CAS staging, under a content-addressed pair directory. Each arm has disjoint `agent/` and `verifier/` trees:

```text
<pair-id>/
  pair-receipt.json
  base/
    agent/instruction.md
    agent/environment/source-catalog.json
    agent/environment/authority-map.json
    agent/environment/sources/*.json
    verifier/truth.json
    verifier/validation-plan.json
    receipts/*.json
  variant/
    agent/...
    verifier/...
    receipts/...
```

Only `agent/` is eligible for an eventual environment image. `verifier/`, receipts, parent provenance, controls, and pair metadata remain outside it. The staging tree is not a Harbor task package and contains no `task.toml`, registry record, or execution spec.

## Twin contract and single-delta proof

The base and variant share:

- parent receipt and output expectation;
- twin pair ID, seed, transform ID/version, topology identifier, and cluster binding;
- entity/source counts, conflict axis, distractors, source order, source addresses, and every source payload;
- instruction, tool schemas, runtime implementation, action budget, and verifier implementation;
- oracle, NOP, wrong-source-mutant, replay, and leak-scan procedures.

They differ only in `CrossSourceConflictSpec.authoritative_source_index`. The derived public difference is therefore exactly one authority-map binding. The hidden expected source and expected binding change only as deterministic consequences of that declared intervention.

Single-delta validation has two levels:

1. **Semantic:** compare the typed candidate specs and require the observed field-difference set to equal `{authoritative_source_index}`.
2. **Materialized:** compare canonical agent-visible file manifests after replacing `authority-map.json` with a sentinel. The manifests must then be identical, and the unsentinelled diff set must equal `{authority-map.json}`. The authority map may differ only at the selected source address and corresponding role labels.

A naive whole-tree one-file claim is intentionally not made: verifier-owned expected truth must follow the authority placement. The pair-integrity receipt must label those hidden changes as derived consequences and prove that no other verifier rule or input changed.

## Deterministic reset and replay

`reset()` reconstructs state solely from the immutable scenario bytes. It sets:

- remaining actions to `source_count + 2`;
- discovered addresses to empty;
- read addresses to empty;
- submission to absent;
- terminal state to false;
- event sequence to zero;
- event-chain root to a domain-separated digest of the candidate and runtime implementation.

Each tool event records sequence, tool name, canonical arguments, canonical result or typed error, pre-state digest, and post-state digest. The next event binds the previous event digest. Wall time, process ID, absolute path, random UUID, and unordered container iteration are forbidden from semantic output.

Required replay proofs per arm:

1. Two clean resets produce identical initial-state bytes and digest.
2. Running the same oracle call list after each reset produces byte-identical events, terminal state, and verifier result.
3. Reset after a completed or failed episode returns to the same clean state.
4. Reordering, deleting, duplicating, or mutating an event breaks the chain and yields reward zero.

## Hidden verifier plan

The verifier receives immutable truth and the runtime event log through a verifier-only mount. Neither path is present in the agent-visible image or tool output.

Reward is exactly `1.0` only when all checks pass:

1. candidate, twin, transform, runtime, verifier, and scenario digests match the frozen plan;
2. the event chain and every state transition recompute exactly;
3. the action budget was not exceeded and no post-terminal action occurred;
4. discovery succeeded before any read or submission;
5. every discovered source was read successfully before submission;
6. exactly one terminal submission exists;
7. `source_address` equals the authoritative source address;
8. the binding key set equals the complete entity set;
9. every submitted value equals the authoritative source value on the conflict axis;
10. no out-of-band state mutation or direct-answer channel was used.

Any failed check returns `0.0` with a stable reason code. Required codes are `missing_submission`, `malformed_event_chain`, `action_budget_exceeded`, `incomplete_source_reads`, `wrong_source`, `incomplete_binding`, `wrong_binding`, `post_terminal_action`, and `identity_mismatch`. Final assistant prose is never scored; only the authenticated `submit_binding` event can satisfy the task.

## Controls

Controls use only public tool outputs and the same runtime/verifier as a future training trajectory.

### Oracle: expected reward 1

For each arm and each of three fresh resets:

1. call `discover_sources`;
2. read every returned address once;
3. select the address marked `authoritative`;
4. construct the complete binding from that source's returned records;
5. call `submit_binding` once.

All six oracle executions must return `1.0`. The oracle may not read hidden truth or verifier files.

### NOP: expected reward 0

Perform no tool action and invoke verification. Both arms must return `0.0` with `missing_submission`.

### Wrong-source mutant: expected reward 0

For each arm:

1. perform the same discovery and full-source reads as the oracle;
2. choose the lowest-address source whose authority role is `reference`;
3. submit that source's complete, internally consistent binding.

Both arms must return `0.0` with `wrong_source`. This mutant is deliberately plausible: its output is complete and comes from a real source, so rejection proves authority selection rather than formatting or missing-work detection.

Recommended defense-in-depth mutants, not additional acceptance gates, are mixed-source binding, direct submission without reads, stale/unknown address, over-budget retry, and post-submission mutation.

## Leak-scan and validation receipts

Reuse the candidate's checked-in `ValidationPlan`; do not invent a competing verifier-plan manifest. A future implementation may emit the following digest-bound receipts only after execution:

| Receipt | Required binding and evidence |
|---|---|
| Materialization | exact `SynthesisResult` digest, candidate IDs, parent receipt/expectation identities, transform/version, seed, topology identifier, runtime code digest, per-file digests, and `status=quarantined` |
| Pair integrity | both twin IDs, semantic diff set, agent-visible file diff set, hidden derived-difference set, and pass/fail |
| Reset/replay | initial-state digest repetitions, oracle transcript digests, terminal-state digests, verifier-result digests, and equality result |
| Controls | arm, control name, attempt, transcript digest, verifier digest, reward, reason code, and denominator; oracle `3/3=1.0`, NOP `1/1=0.0`, wrong-source `1/1=0.0` |
| Leak scan | scanner implementation/version/digest, scanned tree manifest, rule result for every file, excluded verifier-only tree manifest, and overall pass/fail |
| Firewall | parent split/cluster, generated topology identifier, frozen-evaluation inventory digest, collision decision, allowed-use decision, and reviewer identity |

All JSON is canonicalized before hashing. A receipt never certifies itself: file and execution identities must be reopenable through existing artifact authority, and the trusted parent expectation must come from orchestration rather than from the candidate payload.

Materialized leak scans must cover more than the current descriptor scan:

- no parent task/evidence text or citations in any agent-visible file;
- no sealed-evaluation IDs, template IDs, topology IDs, prompts, entities, criteria, or digests;
- no verifier code, truth, expected binding, reward condition, or control script in the agent-visible tree;
- no `tests/`, `solution/`, registry, policy, queue, trainer, model, or network credential path in the environment image;
- no exact authoritative source address, entity ID, or value in the instruction;
- no absolute host path, secret, network endpoint, or nondeterministic provenance value.

Source values returned by `read_source` and authority roles returned by `discover_sources` are legitimate task observations, not leaks. The scanner must distinguish those runtime inputs from forbidden instruction/verifier disclosure.

## Quarantine-first lifecycle

```text
certified deficit receipt + independent expectation
    -> existing SynthesisResult (quarantined descriptor)
    -> live parent re-verification
    -> deterministic materialization to ignored derived/CAS staging
    -> single-delta, replay, controls, leak, and firewall receipts
    -> still quarantined; training_eligible=false
    -> separate Tasks/Research certification decision
    -> training-only pool, if explicitly admitted
```

Passing controls proves environment and verifier validity only. It does not prove model difficulty, capability improvement, evaluation validity, registration eligibility, or training benefit.

The materializer must use an atomic staging directory. Any refusal or failed receipt deletes the partial staging tree and emits only a typed refusal. It must never write `library/registry/`, `research/registration/`, `queue/`, `runs/`, a trainer bundle, or a sealed-evaluation path.

## Training/evaluation firewall

The eventual executable family has `allowed_use=training_candidate_only`. It is permanently ineligible for evaluation registration. Certification may move it only into a training-only pool; it cannot become a measurement, calibration, canary, or held-out task.

The implementation must not:

- inspect a target model's behavior while generating or selecting pair parameters;
- tune entity count, source count, conflicts, wording, addressing, or budget until a model fails;
- consume a sealed evaluation parent, template, seed, entity, source graph, topology, verifier, or result;
- register a task or emit a registry proposal;
- invoke Harbor, a paid model, cloud compute, SFT, RL, or an external trainer;
- weaken the existing candidate's `training_eligible=false` state.

A future training admission must be a separate reviewed action. A future evaluation of the same mechanism must be independently authored from disjoint parents, templates, topology classes, and seeds.

## Refusal matrix

| Condition | Required disposition |
|---|---|
| Parent receipt/expectation absent, malformed, stale, or fails live authority | `parent_authority_unverified`; emit no environment |
| Parent attribution not supported or transform ineligible | existing typed curriculum refusal; emit no environment |
| Held-out/calibration parent or topology collision | `nonleakage_refusal`; emit no environment |
| Candidate/twin/digest/transform mismatch | `identity_mismatch`; emit no environment |
| Any semantic or materialized extra delta | `single_delta_failed`; quarantine and remove partial bytes |
| Reset or replay mismatch | `determinism_failed`; quarantine |
| Oracle not 3/3 at reward 1 | `oracle_failed`; quarantine |
| NOP reward nonzero | `nop_failed`; quarantine |
| Wrong-source reward nonzero | `mutant_failed`; quarantine |
| Any materialized leak scan fails | `leak_scan_failed`; quarantine |
| Any registry, evaluation, paid execution, trainer, or RL request | `scope_refusal`; perform no action |

## Acceptance evidence for a later implementation mission

The implementation is ready for independent review only when it can return, for one certified parent and one fixed pair:

- exact parent receipt and expectation identities that reopen;
- quarantined base and variant candidate identities;
- semantic diff `{authoritative_source_index}`;
- agent-visible file diff `{authority-map.json}` after canonical comparison;
- deterministic reset and replay receipts for both arms;
- oracle `6/6` successes across twins;
- NOP `2/2` failures;
- wrong-source mutant `2/2` failures;
- complete passing leak-scan and firewall receipts;
- proof that no registry, sealed-evaluation, paid-model, cloud, trainer, or RL path was touched.

## Risks and unresolved dependencies

1. **No empirical parent is bound here.** The design must not be mistaken for a candidate family. Actual materialization remains blocked on an exact certified deficit receipt and independently sourced output expectation.
2. **Descriptor authority is intentionally weak.** Current candidates are `descriptor_only`, `fixture_only`, quarantined, and not training-eligible. An implementation cannot silently reinterpret those literals; the Architect/Tasks owners must define the later certification boundary.
3. **Explicit authority may be easy.** That is acceptable for the fixed pilot: the purpose is to isolate correct source selection, not to optimize difficulty. Model-contingent tuning is prohibited.
4. **Hidden truth changes with the intervention.** The public pair differs in one authority-map binding, while hidden expected source/value fields follow deterministically. Review must inspect the derived-difference proof rather than demand byte-identical hidden truth.
5. **The current descriptor leak scan is insufficient for materialized bytes.** A future implementation needs the broader file-boundary scan specified above without treating legitimate source observations as leaks.
6. **Local controls are harness evidence only.** Oracle/NOP/mutant outcomes cannot support a model capability or training-effect claim.
7. **Topology nonleakage needs external authority.** The generator must not inspect sealed contents; a separate owner must provide the frozen inventory digest and collision decision.
