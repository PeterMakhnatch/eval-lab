# System Architect — decide immutable source snapshot protocol for historical apply

## Problem

The strict historical generator is semantically correct but its final mutable-worktree transaction remains impossible to make race-free by rollback. Exact head `b78201be6e6cee221f0176e296bc797d6ad4b967`; report `/tmp/system-architect-strict-historical-generator-final-review-b78201be.md` shows the exact created-manifest rollback has an unavoidable check/unlink name race that can delete a concurrent owner’s replacement.

Do not edit code, apply history, integrate branches, run model/control, or spawn subagents.

## Goal

Choose the smallest mature clean-cutover source authority that removes rollback entirely by deriving from an immutable, recoverable snapshot rather than trying to transactionally freeze mutable worktree paths.

## Evaluate against existing repository conventions

### Candidate A — Git object snapshot

Historical evidence is checked in. Resolve a requested revision, enumerate only the authoritative runs-root input set from the Git tree, refuse symlink/non-regular modes, read exact blob bytes from the object database, and bind every blob OID + SHA-256 into a source-snapshot digest. Generated outputs are based only on those immutable blobs. Exclude generated outputs from the input set so reruns across commits remain byte-identical when input blobs are unchanged. The revision is a retrieval hint, not semantic identity; the aggregate selected-blob snapshot digest is identity.

### Candidate B — EvidenceArchive/CAS snapshot

Use the approved generic CAS content-identity contract (`078cf287`) to freeze legacy source bundles first, then derive from explicit `EvidenceLocator`s. Assess whether current EvidenceArchive supports these historical run directories without a bespoke archive kind/backfill path, and whether integrating CAS before historical generation materially reduces or increases scope.

### Candidate C — self-contained snapshot artifact

Write a content-addressed canonical archive of exact authoritative input bytes (or exact required source documents plus artifact inventory identities) using no-clobber publication, then derive contracts from that immutable archive. Assess size/recoverability/duplicate evidence and whether this creates a competing CAS.

## Required decision

- Select one. Reject the others with concrete repo-grounded reasons.
- Define exact producer/consumer authority, manifest fields, identity domains, retrieval/reopen verification, and apply/idempotence semantics.
- Explain how real `research/evidence/runs` and temporary fixture tests work.
- Define behavior when the working tree changes during/after apply; no source rollback/deletion is permitted.
- Define how generated per-trial outputs and the manifest become authoritative only for the immutable snapshot, and how consumers refuse mismatched live files.
- Preserve 170/152/130/128/2/40/18, zero admissible, no inference.
- Prescribe minimal code/test/CLI changes from `b78201be`, including which existing rollback code is deleted.
- State whether data-CAS integration order must move before historical apply.

## Output

Write `/tmp/system-architect-historical-immutable-source-snapshot-design.md` with `DECISION`, exact design, migration/implementation steps, adversarial tests, and integration-order effect. Page `wH:p9` with the selected protocol. Read-only, no subagents.
