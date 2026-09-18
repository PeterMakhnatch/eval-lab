# System Architect final review — generic run CAS content-identity closure

## Review target

- Worktree: `/private/tmp/eval-lab-run-cas-content-identity-final`
- Branch: `fix/run-cas-content-identity-final`
- Base: `f406bc06bf713caf39820daa7099244b3743704f`
- Exact clean head: `eea29875abb88b3a50e1b50418d19aed83c777d5`
- Prior blocked review: `/tmp/system-architect-run-cas-harbor-rereview-f406bc06.md`

Review this exact head only. Do not edit, rebase, integrate, run models, or broaden scope.

## Required closure contract

Determine APPROVE or BLOCK against every remaining blocker from the f406 review:

1. `EvidenceArchive` is authenticated content identity only and contains no mutable `manifest_path` or `blob_path` authority.
2. Every later reopen uses an independent locator containing normalized absolute store root, `kind`, `record_id`, mandatory expected record digest, and mandatory expected content digest.
3. The current reopen/materialization operation captures exact record and archive bytes, authenticates those captured bytes, and restores only those captured bytes. A same-user replacement after the final snapshot must not invalidate an already-returning operation; the next reopen must refuse the replacement via independent digest anchors.
4. A completed Harbor job is atomically removed from the producer namespace, on the same filesystem and through descriptor-anchored no-follow directory operations, before any evidence digest/archive authority is created.
5. Source changes during hashing/archive creation cannot settle inconsistent evidence: the mandatory exact reopen must restore and verify archive content against the independently anchored record/content identities before returning `SettledRun`.
6. `SettledRun`, queue events/reconciliation, ingest, campaign archive/backfill, analyst, and CLI callers use the CAS locator/materialized bytes rather than a mutable Harbor `job_dir` or authenticated CAS pathname.
7. Settlement failure remains typed/terminal and staged Harbor executable lock behavior from the previously reviewed lineage is preserved.
8. New tests are genuinely adversarial for post-snapshot record/blob replacement and former producer-path recreation/mutation; they must exercise public behavior and the next-reopen refusal.

The previously documented arbitrary same-user mutation of the separately staged `.harbor-launch` executable remains a threat-model hotspot, not a configured executable-path blocker. Do not reopen that accepted scope decision unless this head regresses the staged/configured-path behavior.

## Writer evidence

Exact-head verification reported by Engineer Data:

- `tests/test_evidence_store.py tests/test_runner.py tests/test_queue.py`: 135 passed
- `tests/test_campaigns.py`: 69 passed
- `tests/test_analyst.py`: 21 passed
- Ruff format check: 11 files already formatted
- Ruff check: all checks passed
- `py_compile`: passed
- `git diff --check`: passed
- clean status
- known warnings only: two pre-existing `semantic_facts` construct-shadow warnings

Changed paths (12):

- `docs/GLOSSARY.md`
- `src/evallab/campaigns.py`
- `src/evallab/cli.py`
- `src/evallab/evidence_store.py`
- `src/evallab/queue.py`
- `src/evallab/runner.py`
- `src/evallab/schemas/__init__.py`
- `tests/test_analyst.py`
- `tests/test_campaigns.py`
- `tests/test_evidence_store.py`
- `tests/test_queue.py`
- `tests/test_runner.py`

## Required output

Write a concise evidence-backed verdict to `/tmp/system-architect-run-cas-content-identity-final-review-eea29875.md`:

- `APPROVE` or `BLOCK` on the first line;
- closure table for requirements 1–8 with file/symbol/test evidence;
- exact remaining blockers, if any;
- exact focused commands/results you ran;
- confirmation that no files were changed and no integration/model run occurred.

Then page `wH:p9` with the verdict, exact report path, and exact head reviewed.
