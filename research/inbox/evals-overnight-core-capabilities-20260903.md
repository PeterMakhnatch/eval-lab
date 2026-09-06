---
source_type: internal
---

# Eval Lab overnight core-capabilities program — 2026-09-03

## Operator intent

Peter asked the fleet to keep finding, assigning, improving, and extending relevant work overnight without waiting for replies. The goal is not more scaffolding or another roadmap: leave behind tested core capabilities and explicit evidence about what training/research direction is viable.

This grants autonomy to research and implement repository changes. It does **not** override `AGENTS.md` or `policy/standing-approvals.yaml`: no billable model invocation, cloud/GPU execution, task registration, deployment, publication, or policy loosening. Free `oracle`/`nop` controls and focused local tests are allowed. Work in isolated worktrees and named branches; open PRs, do not merge.

## Direction for tonight

Do not adopt SPADE as the Eval Lab control plane. Treat it as one candidate borrowed backend whose useful pieces are environment generation/validation and hint-regret curriculum logic. Treat TRACE as a methodology for mining capability deficits and synthesizing targeted environments, not as an executable dependency. Agent Lightning/TRL/verl remain possible execution backends.

Build the backend-neutral missing middle first:

```text
immutable Harbor/ATIF evidence
  -> admissible training examples
  -> deterministic capability deficits
  -> validated curriculum candidates
  -> paired experiment specs / portable trainer bundles
  -> external trainer result manifest
  -> frozen held-out Harbor evaluation
```

The existing lab stays authoritative for evidence, provenance, policy, and evaluation. It must not become a trainer or a second agent harness.

## Shared contracts and invariants

1. Reuse existing `ContractModel`, content-digest, CAS, lineage, admissibility, and ATIF normalization patterns. A second convention is a defect.
2. Every derived record binds source trial/job identity, source artifact digest, extractor/version identity, benchmark/task family, split, and exclusion reasons.
3. Secrets, hidden verifier inputs, unredacted prompts, and evaluator-only fields never enter training exports.
4. `syn-funcdag-easy` is refused as training data. Calibration-only, environment-integrity-failed, missing-evaluator, capture-loss, and reward-only-without-semantic-evidence inputs are refused or explicitly typed unavailable.
5. Train/eval separation is cluster-key based and fail closed. No filename- or row-order split.
6. Analysis is deterministic. Model-authored interpretation may propose labels but cannot silently become a mechanical fact.
7. No pooled capability headline where class-specific results differ. Paired/interleaved comparisons require complete pair identity and capture-loss accounting.
8. Tonight's implementation may export or validate trainer bundles, but must never launch a trainer, Harbor billable agent, remote worker, or cloud job.
9. Each PR owns separate source/test modules where possible. Do not edit shared CLI routing, `pyproject.toml`, generated docs, `docs/STATUS.md`, or policy in first-wave PRs; leave integration wiring to a later owner.
10. Focused tests first. Author runs only tests for the touched behavior plus targeted lint; full matrices are left to CI/integration.

## Gate zero: evidence authority and intervention surface

The current shared `main` view still reports `0/170` promoted trial bundles loadable because `benchmark_contract.json` is absent, while 128 historical backfills do not carry resolved admissibility (`G7`). Before any real-corpus export, deficit claim, synthesis input, or trainer bundle is accepted, the integration owner must identify an exact base where G1/G7 are actually closed and re-run the authoritative loadability/admissibility checks. Tracks A–D may develop against fixtures meanwhile, but they must not paper over, bypass, or locally reinterpret these gates.

The existing `ExperimentSpec` also lacks an intervention payload such as `extra_instruction_path`, so paired prompt interventions cannot yet enter the runner through a typed one-variable delta. Track E must audit the existing owner/status before editing shared schema/runner files, then either consume the already-returned implementation or deliver the smallest fail-closed change on a separate branch. Paired planning without an executable typed intervention surface is not complete.

## First-wave tracks

### A. Training-example export — Agent Data

Implement a deterministic library API that converts admissible normalized ATIF/trajectory evidence into a portable, redacted training dataset manifest plus JSONL records. Support at least prompt/response SFT and episode/step representations without trainer-specific tokenization. Fail closed on missing lineage, capture gaps, failed environment integrity, hidden-verifier leakage, and prohibited corpora. Deduplicate by content identity, not path. Emit exclusion records rather than silently dropping rows. Add fixture-based tests including malicious/leaky inputs and latest-history dedup.

Acceptance: a local fixture export produces stable byte-identical output across two runs; every output row traces to immutable source digests; prohibited and incomplete inputs produce typed exclusions; no model or network invocation.

### B. Capability-deficit miner — Analyst

Implement deterministic extraction from existing trajectory facts/verifier components into a versioned `CapabilityDeficitArtifact`: family, failure mechanism, evidence IDs, observed support, counterevidence, capture status, confidence/classification boundary, and candidate intervention dimensions. Start with known families: complete-but-reordered, wrong binding/addressing, wrong-graph traversal, blind retry, malformed output. Unknown mechanisms remain `unclassified`, never guessed. Keep model-generated prose outside the mechanical core.

Acceptance: fixtures reproduce known specimens and distinguish environment non-evaluations from model failures; repeated ingest is idempotent; artifact refuses causal/general claims unsupported by design; no database is required to interpret the output.

### C. Curriculum-candidate synthesis — Synthetic Data

Implement a constrained, deterministic candidate layer that maps certified deficit artifacts to *unregistered* synthetic task candidates. First targets: FuncDAG cross-source conflict and addressing permutation. Generate specifications/seeds and validation plans, not free-form executable code. Bind parent deficit/evidence digests, transform ID/version, cluster key, expected capability, hidden-verifier plan, leak scan, solvability/control requirements, and twin-pair identity. Candidates stay quarantined until existing admission/registration gates approve them.

Acceptance: stable candidates from fixtures; same seed is byte-identical; invalid/uncertified deficits refuse; cluster separation and twin identity are explicit; no registration and no model invocation.

### D. Portable trainer bundle — Eval Platform

Implement a backend-neutral trainer-bundle contract and validator around exported examples. It should describe model/checkpoint identity, dataset/split digests, objective (SFT or verifier-reward episode), rendering contract, seed, expected output manifest, and backend requirements. Provide adapters that *render plans only* for at least generic TRL and a SPADE-shaped external consumer; do not import heavyweight trainer packages or execute training. Record unsupported requirements (on-policy tokens/logprobs, GPU/runtime needs) as typed incompatibilities.

Acceptance: deterministic plans from fixtures; incompatible SPADE/API-only inputs refuse with an exact reason; path traversal, mutable source, hidden split, and digest mutation tests fail closed; no subprocess/network/GPU use.

### E. Paired intervention planner — Eval Runner

Implement or extend the experiment-planning layer so interventions such as the ordering prompt are emitted as interleaved, paired arm specs rather than compared only with historical baselines. Bind pair/block identity, assignment unit, arm, one-variable delta, capture expectations, retry/replacement policy, and analysis gate. Planning only: do not register or run. Reuse existing experiment schemas and policy gates.

Acceptance: fixtures generate balanced deterministic schedules; missing twins, simultaneous variable changes, duplicate assignment, and capture asymmetry refuse; output can be consumed by existing submission code without weakening human approval.

### F. Closed-loop architecture and adoption gate — System Architect + Tutor + Librarian

Review A–E against repository boundaries and upstream SPADE, TRACE, Agent Lightning, ADP, TRL, and verl sources. Define the minimal stable interfaces and a source-verified adoption scorecard. Set explicit kill gates:

- SFT signal gate before RL.
- SPADE spike passes only if stored Eval Lab trajectories and environment contracts can enter without replacing Harbor evidence/provenance.
- TRACE contributes deficit-to-environment methodology only; do not claim reproduction without its full training loop.
- API-only ZAI remains analyzer/generator/evaluated solver, never a weight-updated training target.
- Linux GPU execution stays external and unconfigured tonight.

Acceptance: concrete review referencing code/upstream source, conflicts and resolution instructions for each first-wave PR, and a single recommendation: adopt, adapt, or reject each borrowed component.

## Integration and second wave

After first-wave returns, the orchestration owner will:

1. Re-review exact heads and focused evidence.
2. Ask authors to repair defects on their branches.
3. Route architectural review before integration.
4. Have Repo Custodian construct an ordered integration branch only for approved, non-overlapping contracts.
5. Run an end-to-end **offline fixture** smoke: evidence -> export -> deficit -> candidate -> paired plan -> trainer plan. This smoke must prove no Harbor/model/network/trainer invocation.
6. Use failures to assign a second wave: missing adapters, contract mismatches, negative controls, docs/operator surface, and security review.
7. Leave PRs unmerged and provide Peter one short morning decision record explaining what now works and what evidence is still required before SFT, SPADE, or RL.

## Return contract for every owner

Page back with branch, exact head, PR URL (or precise reason no PR), files/symbols changed, focused verification output, negative controls, unresolved dependencies, and residual risk. If the assigned surface already exists, do not build a duplicate; audit it, harden the weakest real boundary, and explain the substitution.