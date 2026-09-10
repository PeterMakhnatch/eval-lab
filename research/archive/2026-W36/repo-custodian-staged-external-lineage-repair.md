# Repo Custodian: repair staged external-import lineage forward

## Branching and safety

- Provisional staged spine: `analyst/synth-data-promotion-hardening @ eaf9984c06bd2983b582d3ba491b2bc8ae367290`
- Source implementation: `f33fe0697f45ef0771f0c13622f5bb78cc3dd31a`
- Review: `/tmp/system-architect-external-import-lineage-review-f33fe069-eaf9984c.md`

Create a new branch such as `fix/staged-external-import-lineage` from exact `eaf9984c` in a dedicated isolated worktree. Do not reset, rewrite, or advance the shared staged spine. Preserve the existing typed schema/reopening/equality checks and repair forward. One writer only.

## Required closures

### 1. Restore one F3 certified-package digest authority

The staged registry recursively ignores any path component named `.git`, `__pycache__`, or `.pytest_cache`; workbench entry hashing still ignores only a basename and diverges for nested ignored directories.

- Make workbench package digest construction obey the exact staged F3 recursive ignore rule, preferably through an existing non-cyclic shared helper; otherwise keep a minimal identical implementation protected by parity tests.
- Test nested ignored directories, ignored extensions, ordinary similarly named files, and symlink behavior supported by the existing contract.
- Assert workbench candidate package digest, registry `task_directory_digest`, promotion, reload, and audit all agree on the same bytes.

### 2. Migrate the real promotion CLI caller

The normal `evallab registry promote` path must be able to promote a valid `m049-v2` external packet without a second free-form lineage authority.

- Derive the typed lineage object from the packet candidate source as the single ingress authority.
- Any explicit source URI/ref/lineage argument already present must be an equality assertion, not an override.
- Carry the exact lineage through packet → certification envelope → candidate registry record → registry reload/audit.
- Native promotion remains unchanged; missing/mismatched declared external lineage refuses.
- Add a real CLI round-trip test, not only direct Python calls.

### 3. Close the `m049-v1` external downgrade bypass

- Keep explicit read compatibility only for genuinely legacy/native committed v1 records required by the repository.
- No newly created/promoted external packet may remove lineage, relabel itself `m049-v1`, recompute content IDs, and pass envelope creation, verification, registry reload, or audit.
- Derive external/native classification from existing authoritative candidate-source fields/conventions; do not add a caller-supplied waiver or free-form legacy flag.
- Add the complete adversarial downgrade test through packet → envelope → registry reload/audit while retaining a valid native legacy-read test.

### 4. Complete the durable transformation record before V1 freeze

Add and bind:

- an immutable production timestamp with strict canonical timestamp validation;
- typed two-build reproducibility evidence entries, each carrying a canonical repository-relative evidence reference, evidence digest, observed/build timestamp, environment identity/digest, and output package digest;
- reopening and digest verification for each reproducibility evidence artifact;
- exact equality of both clean-build output digests to the declared transformed output package digest;
- distinct evidence identities/runs sufficient to prove two builds rather than a duplicated self-assertion.

Replace the current self-reported two-digest tuple cleanly; no compatibility shim is needed for an unused external V1 contract. Preserve the no-op/transformed invariants and semantic-equivalence evidence checks.

Canonicalize or reject every durable reference spelling. Reject absolute paths, traversal, backslashes where not canonical, repeated separators, `.` aliases, and any spelling not equal to one normalized repository-relative POSIX path. Distinct record/equivalence/reproducibility refs must not alias the same file.

## Verification

- Focused `tests/test_task_workbench.py` and `tests/test_registry.py`, plus the real CLI promotion path test.
- Independent probes for nested-ignore digest parity, recomputed-v1 downgrade refusal, timestamp/reproducibility tamper/missing/alias refusal, and complete v2 CLI round trip/reload/audit.
- Ruff check/format touched files; `git diff --check`; clean worktree after commit.
- Report exact commit, diff surface, test/probe evidence, and any existing legacy v1 records preserved.

## Prohibitions

No LoCoMo package/certification, registration of a real task, secret access, queue submission, model run, main sync, staged-spine rewrite, bespoke dispatch shim, compatibility alias, or unrelated formatting.
