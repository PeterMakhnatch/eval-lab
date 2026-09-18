# Eval Platform — Close Isolation/Admission B1–B5

## Target and workspace

Create a fresh isolated repair branch/worktree from exact blocked head `ba95065b`. Do not edit the dirty primary checkout, staged integration spine, old blocked branches, or the original Platform worktree in place. Return one focused repair commit for final Architect review.

Authoritative review: `/tmp/system-architect-isolation-admission-final-review-ba95065b.md`.

Retain all verified strengths of `ba95065b`: separate isolation and transport-v2 digest namespaces; exact transport digest `sha256:a6102664a924a6466799015f8cfcb5864bcc2d78a31dab7ca7ae5ac84d61ba98`; Darwin evidence digest `sha256:0dea81047ac365ea89e1e3d4be5f10aacfac114b36c6550e0e721dc61a93f792`; exact Darwin unavailable/calibration-only reason; top-level forgery refusal; and one typed task-runtime/trial-admissibility authority. Do not regenerate unrelated historical evidence or call a model.

## B1 — Complete, actually isolating policy evidence

1. Add both requested and effective verifier-phase policy fields to the mandatory isolation-evidence set. Missing either field independently must derive unavailable, never enforced.
2. Positive enforcement requires exact requested/effective parity **and** a policy mode whose typed semantics are isolation-capable. Matching `public` policies can never derive enforced/causal-eligible, even when all five host-escape probes happen to be blocked.
3. Reuse the existing typed policy model/enum; do not add a caller-supplied `is_isolating` boolean or free-form waiver.
4. Preserve positive representation only for complete, current evidence with exact isolation-capable policies, pinned identities, and all five negative escape probes.

## B2 — Rebind stored evidence to the actual dispatch runtime

At production campaign admission/queue dispatch, before runner invocation:

1. Recompute/obtain authoritative current container runtime version, exact image digest, adapter version/digest, probe implementation digest, and probe-config digest using the existing runtime/probe identity producers.
2. Compare every field exactly against the bound isolation evidence and campaign/runtime identity.
3. Refuse dispatch on drift even when evidence remains within `valid_until`.
4. Keep status/reason/eligibility derived from the verified evidence; no mutable config booleans, platform-name inference, or copied top-level approval.
5. Add an adversarial matrix changing one runtime/image/adapter/probe field at a time.

## B3 — One deterministic production trial-admissibility writer

Add exactly one production writer at the existing trial finalization/ingestion boundary after authoritative trial artifacts exist:

1. Reopen and compute the exact contract, trajectory, final-state, verifier, outcome, and interpretation source digests from their authoritative artifacts.
2. Bind exact immutable run provenance, exact canonical registered task revision, certified runtime-package digest, registry state at execution, and verified isolation evidence digest/status/reason/eligibility.
3. Build the shared typed `TrialAdmissibilityV1` decision; do not accept precomputed source digests or allowed-use decisions from a caller.
4. Write exactly one canonical `trial-admissibility.json` atomically. Refuse conflicting pre-existing bytes; idempotent identical finalization may recognize exact equality but must not silently overwrite.
5. A finalized future trial must not depend on a hand-authored sidecar. Missing/corrupt inputs remain unavailable and cannot receive causal authority.

## B4 — One strict shared loader/verifier for every consumer

Create/reuse one non-cyclic strict loader used by benchmark ingestion, fact extraction, outcome extraction, cohorts, denominators, comparisons/elicitation/capability aggregation, baseline/drift reuse, and any other governed producer:

1. Reopen the six authoritative source artifacts and recompute their digests.
2. Recompute/verify the sidecar's canonical identity/digest, exact task-runtime binding, registry state, trial ID, and isolation evidence parity against immutable provenance.
3. Reject a self-consistent sidecar that binds arbitrary source digests, wrong task runtime, wrong source path, aliased artifacts, or changed provenance.
4. Expose `admissible/causal` only after the complete shared check. Unavailable/descriptive-only evidence may remain queryable but must be mechanically excluded from all causal/capability paths.
5. Remove weaker field-only checks in `extract_trial_fact` and `_outcome_admissibility_fields`; do not retain parallel validation paths.

## B5 — Promotion consumes the same causal decision

Update control-evidence discovery and final task-promotion verification so every oracle/nop control trial used for registration:

1. Loads through the same strict shared admissibility verifier.
2. Requires exact decision `admissible`, allowed use `causal`, exact task/runtime/source/isolation bindings, and durable control artifact parity.
3. Refuses missing historical sidecars, unavailable/calibration-only controls, arbitrary self-consistent sidecars, and identity mismatch.
4. Does not add a legacy waiver. Existing fixture-only controls lacking this authority must remain non-promotable.

## Required verification

Run focused tests only, covering the five production closures and prior strengths:

- each missing verifier-phase field;
- matching public policy negative control;
- complete isolation-capable positive representation;
- dispatch drift for runtime/image/adapter/probe implementation/config inside validity window;
- real deterministic finalization writes one exact sidecar and refuses conflicting rewrite;
- arbitrary source-digest/task-runtime/provenance sidecars rejected by benchmark ingestion, facts, outcomes, cohorts, denominators, baseline/drift, and other governed consumers;
- promotion refuses controls missing or failing the shared causal decision and accepts only a fully authoritative positive fixture;
- transport-v2 digest remains exact and separate; Darwin projection/digest/reason remain exact; top-level forgery remains rejected.

Use LSP references if a Python server is available; otherwise trace all exported constructors/callers and report the limitation. Run touched-file Ruff/format check and the deterministic evidence verification; do not run project-wide suites or a model. Return branch/worktree/commit, exact changed files/symbols, focused counts/commands, adversarial observations, and rebase risks.
