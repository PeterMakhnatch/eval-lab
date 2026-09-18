# Platform Builder — map canonical registry tests onto strict causal authority

## Target

Read-only follow-up; do not edit any worktree.

- Integration draft: `/private/tmp/eval-lab-staged-spine-integration`
- Canonical base: `adc8a65a`
- Approved strict-authority source: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/darwin-isolation-admission-final` at `34efd8c15363c8bf671afe6ce313c022c3212025`
- Prior audit: `/tmp/platform-builder-isolation-integration-draft-audit.md`
- Active writer: Repo Custodian `wH:p0`

The writer has repaired the gross sidecar schema but still reports roughly 16 registry failures. The tension is that all 49 canonical tests must retain their behavioral intent, while strict causal authority changes some pre-authority positive flows. We must adapt, not delete, those tests.

## Required analysis

1. Run the current focused registry suite once with compact output and enumerate exact remaining failing test names/root causes.
2. For each failing canonical test, classify it as:
   - a positive two-phase promotion/register flow that must be upgraded to stage/persist the exact registered immutable revision before producing/validating causal controls;
   - a negative/refusal test that should use deliberately legacy/inadmissible evidence and assert the new typed refusal/state preservation;
   - an unchanged invariant whose fixture is simply malformed.
3. Give an exact adaptation map per test/helper. Reuse production APIs and approved-source fixture patterns; do not recommend weakening `_require_causal_control_admissibility`, `_registry_binding`, registered-state requirement, durable-root requirement, or mutation/replay checks.
4. Resolve the bootstrap question precisely: how an unregistered task can enter the staged candidate/registered immutable revision needed for oracle/nop control execution without circularly treating unregistered evidence as admissible. Identify the exact production staging API/order and which record fields are digest authority versus mutable envelope.
5. State which assertions from `34efd8c1:tests/test_registry.py` must replace contradictory canonical expectations (including real-repository audit), while preserving the canonical test names/coverage wherever possible.
6. Confirm no production source change is needed solely to make legacy tests pass. If you find a real bootstrap implementation defect, identify it separately with exact symbol/evidence; do not edit it.

## Output

Write `/tmp/platform-builder-registry-test-adaptation-map.md` with the failing-test table and exact helper/control-flow recommendations. Page `wH:p0` with concise actionable instructions and `wH:p9` with report path. Confirm no files changed and no model/integration run.
