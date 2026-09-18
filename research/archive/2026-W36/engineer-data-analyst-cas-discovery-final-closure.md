# Engineer Data — close analyst CAS discovery authority bypass

## Target

Continue in the same clean writer worktree and branch:

- Worktree: `/private/tmp/eval-lab-run-cas-content-identity-final`
- Branch: `fix/run-cas-content-identity-final`
- Current exact head: `eea29875abb88b3a50e1b50418d19aed83c777d5`
- Core settlement review: PASS
- Architect finding: `src/evallab/analyst.py:_cas_uri_for_trial` trusts mutable `records/*.json` discovery and lets a replaced record redirect analysis to attacker-selected, self-consistent CAS content.

Do not rebase, integrate, run models, or edit another worktree. Preserve the completed content-identity settlement contract.

## Required repair

The branch is not full-plane integration-ready until analyst hydration uses an independently authenticated selection authority.

1. Remove `_cas_uri_for_trial` and all automatic recursive scanning/trust of `store/records/*.json` for trial discovery.
2. A CAS URI/content digest may authenticate the bytes it names, but it does not authenticate that those bytes belong to the requested trial. Do not treat self-consistent URI content as an independent trial-to-record binding.
3. Automatic analyst hydration must either:
   - obtain the exact `EvidenceLocator` (`store_root`, `kind`, `record_id`, expected record digest, expected content digest) from an already-authenticated durable queue/catalog binding and use `materialize_evidence`; or
   - fail closed when no such binding is available.
4. Explicit caller-selected hydration must be represented as an explicit authority, not silently conflated with automatic discovery. Prefer the existing `EvidenceLocator` and `materialize_evidence` protocol. If preserving an explicit `cas_uri` user selection remains necessary for analyst CLI behavior, label it as caller-selected and ensure it cannot be reached through mutable record auto-discovery; do not add a compatibility shim that retains the unsafe scan.
5. Migrate all `resolve_trial` / `run_analysis` / CLI call sites needed by the clean cutover. Keep `TrialData` provenance explicit about the exact selected content identity.
6. Add public adversarial tests proving:
   - a replaced/forged record for the requested trial cannot redirect automatic analyst hydration;
   - missing independently authenticated discovery authority fails closed instead of accepting a matching mutable record;
   - an exact valid locator hydrates the expected captured bytes;
   - record or blob replacement after locator issuance refuses on the next hydrate through expected digest checks;
   - ordinary filesystem trial analysis remains unchanged.
7. Delete obsolete URI-only private helpers/tests if the cutover makes them unnecessary. No aliases, deprecated paths, fallback record scan, or broad test deletion.

## Scope

Expected primary files:

- `src/evallab/analyst.py`
- `tests/test_analyst.py`
- exact direct analyst CLI/caller files only if required for the clean API cutover

Do not broaden into unrelated interpretation CAS consumers. The current defect is analyst trial discovery and hydration authority.

## Verification

Run at exact final head:

- focused new analyst adversarial tests;
- `uv run pytest tests/test_analyst.py tests/test_evidence_store.py tests/test_runner.py tests/test_queue.py tests/test_campaigns.py`;
- touched-file Ruff check and format check;
- `git diff --check`;
- clean status after commit.

## Handoff

Commit the repair and page `wH:p9` with exact base/head, changed paths, test counts, and explicit explanation of the authority source used for automatic versus explicit hydration. Do not integrate.
