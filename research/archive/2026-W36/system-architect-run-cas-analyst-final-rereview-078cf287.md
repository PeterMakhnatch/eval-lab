# System Architect — final run-CAS analyst authority re-review

## Exact target

Read-only review:

- Worktree: `/private/tmp/eval-lab-run-cas-content-identity-final`
- Branch: `fix/run-cas-content-identity-final`
- Previously reviewed core head: `eea29875abb88b3a50e1b50418d19aed83c777d5`
- Final candidate head: `078cf287b05d80bb88bd20d87b112b33b250f688`
- Prior report: `/tmp/system-architect-run-cas-content-identity-final-review-eea29875.md`
- Prior verdict: core settlement PASS, full-plane integration BLOCK only on unauthenticated analyst `records/*.json` discovery.

Do not edit, rebase, integrate, or run a model.

## Required closure

Verify the sole remaining analyst blocker is fully closed without regressing the approved settlement contract:

1. `_cas_uri_for_trial` and all mutable `records/**/*.json` automatic discovery/trust are absent.
2. When filesystem evidence is absent, analyst resolution without an independently authenticated locator fails closed.
3. Explicit hydration accepts `EvidenceLocator`, invokes the exact record/content digest-authenticated `materialize_evidence` protocol, and never accepts URI-only record selection as trial binding authority.
4. Materialized content is bound to the requested trial without consulting mutable store metadata; ambiguous/mismatched trial selection refuses.
5. Durable analyst provenance clearly labels `caller-selected-locator` and retains store root, record kind/id, expected record digest, expected content digest/CAS identity, selected member, and member digest.
6. Public adversarial tests prove forged/matching mutable records cannot redirect automatic hydration; missing locator fails closed; exact locator succeeds; record and blob replacement after locator issuance refuse on next hydrate; ordinary filesystem analysis is unchanged.
7. Direct callers/API surface were cleanly migrated: no legacy `evidence_store_root` + `cas_uri` authority path remains in analyst resolution/run-analysis.
8. Re-run a sufficient combined exact-head matrix to ensure the already-approved core settlement remains intact.

## Writer evidence

- Changed from `eea29875`: only `src/evallab/analyst.py`, `tests/test_analyst.py`
- Exact-head aggregate: 231 passed, 8 warnings
- Focused analyst adversaries: 7 passed before aggregate
- Ruff format/check, `py_compile`, `git diff --check`, clean status passed
- No model, integration, rebase, or projection work.

## Required output

Write `/tmp/system-architect-run-cas-analyst-final-rereview-078cf287.md` with first-line `APPROVE` or `BLOCK`, closure evidence for items 1–8, exact commands/results, remaining blockers if any, and confirmation no files changed. Page `wH:p9` with exact verdict, report path, and exact head.
