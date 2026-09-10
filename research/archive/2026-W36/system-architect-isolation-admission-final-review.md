# System Architect — Final Review of Darwin Isolation and Trial Admissibility

## Review target

- Branch: `fix/darwin-isolation-evidence-repair`
- Commit: `ba95065b`
- Base: canonical `46794c166c39cb64c7c41350f8906b5ec3badab1`
- Authoritative implementation brief: `/Users/petermakhnatch/Developer/eval-lab/research/inbox/platform-darwin-isolation-evidence-repair.md`
- Prior blocked reviews: `/tmp/system-architect-darwin-isolation-review-9ef3396c.md` and `/tmp/system-architect-analyst-admissibility-review-a08efaca.md`

Return a strict **APPROVE** or **BLOCK** report with evidence at `/tmp/system-architect-isolation-admission-final-review-ba95065b.md`. Do not edit or merge the branch.

## Reported implementation evidence to verify

- Deterministic Darwin Docker smoke, no model: hostname, direct-IP, alternate-port, redirect, and DNS-rebinding all escaped.
- Evidence digest: `sha256:0dea81047ac365ea89e1e3d4be5f10aacfac114b36c6550e0e721dc61a93f792`.
- Derived status remains unavailable, calibration-only, with the exact five-class reason.
- Existing transport v2 qualification digest remains exactly `sha256:a6102664a924a6466799015f8cfcb5864bcc2d78a31dab7ca7ae5ac84d61ba98`.
- Typed task-runtime/trial-admissibility state reportedly propagates through manifest, dispatch, provenance, normalized facts/outcomes, views, cohorts, and governed producers; historical/inferred authority remains descriptive-only.
- Reported validation: 498 focused tests, 13 refreshed-evidence checks, touched-file Ruff clean. No Python LSP was configured; callers were text-traced.
- Rebase risk is reported as high in `schemas/__init__.py`, `campaigns.py`, `queue.py`, `analysis_control.py`, `sql/views.sql`, `facts.py`, and `benchmark_events.py`; later integration must be symbol-by-symbol.

## Required review

1. **Diff and topology**
   - Verify exact commit/base and inspect the complete diff plus all changed exported constructors/callers.
   - Confirm there is exactly one independent isolation-evidence authority and one shared typed trial-admissibility authority, not shadow schemas or generic booleans.
   - Confirm no blocked external-lineage, old isolation (`9ef3396c`), old analyst (`a08efaca`, `25e15812`, `d709cf6d`), or unrelated historical-regeneration changes are stacked.

2. **Isolation evidence integrity**
   - Verify the canonical evidence digest binds requested/effective agent and verifier policies; platform/runtime/adapter; probe code/config identity; timestamps/freshness; and all five observed escape classes.
   - Status, reason, and eligibility must be derived from verified evidence and must reject top-level forgery, contradictory policy, altered identity, missing/partial/stale/malformed evidence, and Linux metadata without probes.
   - Positive enforcement must require exact complete negative probes and exact identity parity. Darwin evidence must remain unavailable/calibration-only.

3. **Transport namespace independence**
   - Independently recompute the original transport-v2 body and confirm the exact `a610...` digest.
   - Ensure isolation fields do not mutate transport schema v2 or enter its canonical body.
   - Confirm readiness/qualification/runtime identity separately pin and parity-check both authorities.

4. **Trial authority and registry binding**
   - Verify each governed trial binds exact canonical registered task revision, certified runtime-package digest, registry state at execution, contract/trajectory/final-state/verifier/outcome/interpretation digests, isolation-evidence digest, derived status/reason, and allowed-use classification.
   - Candidate/retired/missing bindings and unavailable + causal-eligible contradictions must refuse.
   - Historical evidence may remain typed unavailable/descriptive-only; no family/cell/seed/dose/arm/opportunity/task/runtime/platform/isolation inference from labels, paths, mutable booleans, or generic `cell_factors`.

5. **Every governed consumer**
   - Trace manifest, dispatch, queue, loader, provenance, normalized facts/outcomes, materialized views, cohort construction, denominators, comparison/elicitation/capability aggregation, baseline/drift reuse, and promotion.
   - All causal/capability paths must consume the same admissibility decision and mechanically exclude calibration-only/unavailable rows. Descriptive calibration queries may remain possible and clearly labelled.
   - External source provenance must not substitute for certified transformed runtime identity.

6. **Adversarial verification**
   - Re-run focused tests/probes sufficient to establish the brief, including top-level forgery, one-field digest/parity mutations, missing/partial/stale evidence, Linux-without-probes, positive complete-negative representation, causal cohort/promotion exclusion, exact Darwin smoke result, and transport digest independence.
   - Review whether the 498+13 tests defend behavior rather than self-assert implementation details.

## Disposition rule

APPROVE only if the complete evidence → identity → provenance → normalized rows → governed-consumer chain fails closed without inferred authority, the transport namespace is byte-stable, Darwin remains calibration-only, and no old/parallel authority is present. Otherwise BLOCK with the smallest exact production/test closure and integration instruction.
