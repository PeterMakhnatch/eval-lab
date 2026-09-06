# Ops Eval Runner — read-only run-CAS integration map onto canonical spine

## Exact heads

- Canonical worktree/head: `/private/tmp/eval-lab-staged-spine-integration @ 9768ad60a6d5a0cb90e4ff5dd1fbe116b050cc63`
- Approved run-CAS candidate: `/private/tmp/eval-lab-run-cas-content-identity-final @ 078cf287b05d80bb88bd20d87b112b33b250f688`
- CAS approval: `/tmp/system-architect-run-cas-analyst-final-rereview-078cf287.md`
- Projection candidate is built on the CAS head and is separately under final review; do not inspect/modify it in this assignment.

Read-only conflict/semantic replay mapping. Do not edit/rebase/cherry-pick/integrate, run models/controls, or spawn subagents.

## Goal

Give Repo Custodian an exact safe replay plan that lands only the Architect-approved run-CAS/runtime-lock/content-identity contract onto the current canonical authority stack without reintroducing stale pre-isolation, pre-context, pre-CLI, or pre-memory code.

## Required map

1. Enumerate exact source delta from the CAS branch’s original base through `078cf287`: commits, paths, symbols, tests, generated docs, additions/deletions.
2. For every changed path, compare against canonical `9768ad60` and classify:
   - no canonical overlap / exact source file safe;
   - textual overlap but semantically independent;
   - semantic conflict requiring manual composition;
   - obsolete source-side hunk prohibited because canonical has newer authority.
3. Pay special attention to shared runtime surfaces: queue, campaigns, analyst APIs, evidence store, schema exports, CLI/registry, docs indexes, and tests. Preserve canonical:
   - `TaskRuntimeIdentityV1` and strict trial admissibility/isolation decisions;
   - context step index and payload digest;
   - CLI two-phase staged controls;
   - memory-continuity producer/feature registry;
   - external-lineage/digest resolver contracts.
4. Identify every source test that must be retained/adapted and every canonical test function/assertion that must not be lost. Give exact before/after inventories for overlapping test files.
5. Define the semantic landing contract:
   - generic completed Harbor result settles only to `EvidenceLocator` CAS content identity;
   - absolute store root + kind/id + expected record/content digest;
   - captured completed producer bytes immutable against later mutation;
   - SettledRun/queue/ingest/campaign/analyst carry locator only;
   - runtime lock compatibility exact and fail closed;
   - no mutable record discovery/path authority or caller digest surrogate.
6. Prescribe integration order and focused validation commands after replay, including isolation/admissibility regressions and exact no-path searches.
7. Flag generated docs/indexes to regenerate only with official commands, rather than copy from the source branch.

## Output

Write `/tmp/ops-run-cas-canonical-integration-map-9768ad60.md` with:

- exact commit/path/symbol matrix;
- conflict classification and approved resolution per overlap;
- protected canonical test inventory;
- proposed replay commit boundaries;
- focused verification matrix;
- any blocker requiring source repair before integration.

Page `wH:p9` with GO/BLOCK for Custodian replay and report path. Confirm no files changed, no model/control, no subagents.
