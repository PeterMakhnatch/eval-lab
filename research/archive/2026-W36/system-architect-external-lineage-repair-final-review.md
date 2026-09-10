# System Architect — Final Review of Staged External-Lineage Repair

## Review target

- Branch: `fix/staged-external-import-lineage`
- Commit: `721afd52`
- Base: staged provisional spine `eaf9984c`
- Repair brief: `/Users/petermakhnatch/Developer/eval-lab/research/inbox/repo-custodian-staged-external-lineage-repair.md`
- Prior blocking review: `/tmp/system-architect-external-import-lineage-review-f33fe069-eaf9984c.md`

Return a strict **APPROVE** or **BLOCK** report at `/tmp/system-architect-external-lineage-repair-final-review-721afd52.md` and page the parent. Do not edit, merge, register a task, or run a model.

Reported validation: 243 focused tests in `tests/test_task_workbench.py` and `tests/test_registry.py`; Ruff clean across `src/` and `tests/`; clean repair worktree. Independently verify the changed diff and the observable closures below.

## Required review

### 1. Scope and preserved strengths

- Verify exact base/head and changed files. The repair must be forward-only from `eaf9984c`; no staged-spine rewrite, LoCoMo activation, unrelated formatting, secret/model use, or real registration.
- Reconfirm every strength from the prior review: typed source/license/policy/transformation/retention/replay lineage; source license and attribution retained; explicit values are equality assertions, not alternate authorities; final candidate/certification/registry/reload/audit equality; artifact reopening; no waiver or free-form bypass.

### 2. F3 package-digest parity

- Independently construct nested ignored directories such as `tests/__pycache__/ignored.py`, nested `.git`, `.pytest_cache`, and ordinary similarly named files that must remain included.
- Confirm workbench and registry call the same non-cyclic canonical package-digest policy, with exact parity for nested ignored path components, relative path normalization, ordering, file bytes, and symlink treatment/refusal.
- Mutating ignored content must not alter either digest; mutating included content must alter both identically. No duplicated basename-only helper may remain.

### 3. Single real CLI lineage ingress

- Exercise the real `evallab registry promote`/`run_cli` path using a valid version-2 external packet.
- The candidate packet must be the single ingress authority for `ExternalImportLineage`; explicit source URI/ref/license/lineage inputs may only assert exact equality and must reject mismatch.
- Verify packet → candidate/envelope → registry → reopen/reload → audit preserves exact lineage and all bound digests. No separate CLI construction path, defaulted lineage, or optional omission may permit external v2 promotion.

### 4. Version downgrade refusal

- Reproduce the prior attack: remove external lineage, relabel candidate and certification as `m049-v1`, recompute every candidate/certification identifier and digest, then use the full promotion/envelope/reload/verify path.
- New external v1 registration must refuse mechanically without waiver or caller-supplied bypass. Prefer rejecting new v1 admission generally while preserving read-only loading/auditing of genuinely pre-existing native/legacy v1 records.
- Verify an attacker cannot regain admission by stripping or changing provenance/source/task-family fields while recomputing identifiers.
- Confirm genuine native/legacy v1 read compatibility remains narrow and cannot be used as a new registration path.

### 5. Durable transformation and reproducibility evidence

- Verify an immutable canonical production timestamp is present and digest-bound.
- Require exactly typed, digest-bound two-build evidence entries with distinct build IDs; canonical evidence refs; evidence digests; environment/toolchain identity digests; per-build timestamps; and exact output package digest.
- Reopen both evidence artifacts and recompute their content digests. Each must independently attest the same exact final package output; aliasing one file/ref/digest/build ID as two builds must reject.
- Reject missing, malformed, noncanonical (`//`, `/./`, traversal, ambiguous absolute/relative) refs; duplicate/aliased entries; mismatched output/environment/timestamp; stale/self-asserted tuples; and unreopenable artifacts.
- Confirm the transformation record itself binds the two evidence entries and all fields survive certification, registry storage, reload, and audit exactly.

### 6. Adversarial and compatibility matrix

Run focused tests/probes sufficient to establish:

- real CLI external-v2 positive path and mismatch refusals;
- nested-ignore/symlink/included-file digest parity;
- recomputed m049-v1 downgrade refusal through the complete path;
- legacy/native v1 read-only compatibility and inability to newly register;
- two distinct build-evidence positive path plus alias/noncanonical/digest/output/timestamp/environment negative matrix;
- registry reopen/reload/audit exact equality;
- no regression to ordinary native/non-external task authoring and promotion contracts.

## Disposition rule

APPROVE only if all four prior blockers are closed through production callers and durable reopenable evidence while prior strengths remain exact. Otherwise BLOCK with the smallest exact production/test closure and integration instruction. An approved commit still authorizes only integration of the generic contract; it does not authorize LoCoMo transformation, certification, registration, canary execution, or a model run.
