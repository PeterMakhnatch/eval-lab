# Platform Builder — clean-cut historical generator to immutable Git-selected blobs

## Exact state and design

- Worktree: `/private/tmp/eval-lab-strict-historical-regeneration`
- Branch/head: `feat/strict-historical-regeneration @ b78201be6e6cee221f0176e296bc797d6ad4b967`
- Canonical evidence revision for real dry-run: `f414512a728d26d507eccae9792ef89e6414007c`
- Architecture decision: `/tmp/system-architect-historical-immutable-source-snapshot-design.md`
- Selected protocol: **Git selected-blob snapshot authority**.

Do not rebase/apply real historical outputs/integrate, run model/control, or spawn subagents. This is a clean cutover: delete mutable-worktree source authority and rollback; do not add a compatibility alias/fallback.

## Required source authority

Implement exactly the selected-blob algorithm:

1. Require explicit `--source-revision`; resolve `<revision>^{commit}` once.
2. Normalize repo-relative runs root inside the repository.
3. Enumerate the resolved commit tree NUL-safely.
4. Identify promoted markers ending `/artifacts/manifest.json`, derive unique/non-overlapping trial roots.
5. Select every entry in those trial roots except exact generated `<trial>/artifacts/historical-contract.json`.
6. Require regular blob mode `100644` or `100755`; reject symlink/gitlink/unsupported/nonblob, unsafe/non-UTF-8/duplicate paths before output preflight.
7. Read by enumerated OID using verified `git cat-file --batch`/equivalent. Require exact object type, OID, reported size; compute exact SHA-256. Worktree bytes are never read for source semantics.
8. Retain raw bytes only for source documents the planner parses; retain path/mode/OID/SHA-256/size metadata for inventory artifacts.

## Clean v2 identity

Add strict models/domains with no v1 aliases:

- `HistoricalGitBlobV1`
- `HistoricalSourceSnapshotV1`
- snapshot domain `evallab.historical-source-snapshot.v1\0`
- descriptive contract `historical-descriptive-contract/v2` plus `source_snapshot_digest`
- manifest `historical-contract-regeneration/v2`, code version `strict-git-snapshot/v2`, complete source snapshot, v2 manifest domain.

Path, mode, Git OID, SHA-256, and size are identity-bearing. Commit/ref is not in canonical manifest/contract bytes; it is an operational retrieval hint only, so two revisions selecting identical blobs produce byte-identical outputs.

Every disposition/trial source-inventory/output/contract digest must be consistent with the snapshot. Preserve all no-inference fields/holds and exactly zero ready/admissible.

## CLI and apply contract

Preserve `evallab data backfill contracts` and add:

- `--repo-root` (explicit API; CLI may discover Git top-level only as documented convenience)
- required `--source-revision`
- optional dry-run `--expect-source-snapshot`, `--expect-plan-digest`
- apply requires both exact expected snapshot and plan digests copied from reviewed dry-run.

Dry-run prints resolved commit hint plus canonical snapshot and plan digests. Apply refuses digest/count mismatch before output preflight.

Keep output/report no-follow/no-clobber/R1 protections. Delete:

- `HistoricalRegenerationSourceDrift`;
- all live source replanning/revalidation/boundary stages;
- publication receipts used only for source rollback;
- rollback deletion functions and R2 rollback tests.

There is no source mutation rollback or source deletion. Outputs bind the immutable Git snapshot. Partial publication remains incomplete until the complete output set verifies; manifest presence alone is never completion.

## Shared verifier

Add one production verification API, not per-consumer copies. Given canonical manifest bytes and an independently expected snapshot/plan digest, it must:

1. validate v2 manifest/content digest;
2. reopen every listed blob by OID, verify type/OID/length/SHA-256, recompute snapshot;
3. open every generated contract no-follow, verify expected output SHA-256/v2 content digest/snapshot binding;
4. refuse missing/altered/extra-selected/symlink/nonregular/old-snapshot output, manifest substitution, unavailable Git object, and offered live-source mismatch.

Choose a narrowly named storage sibling for Git snapshot/reopen code only if it materially reduces the now-large `data_backfill.py`; do not create a second planning/refusal framework.

## Tests

Implement all tests in architecture report §Required tests, including:

- same selected blobs across commits byte-identical;
- every selected path/mode/blob membership mutation changes snapshot;
- unrelated file and tracked generated output excluded;
- unsafe modes/paths/object mismatches refuse before writes;
- worktree mutations before/during/after ignored for derivation;
- ref movement after resolution cannot redirect reads;
- missing/corrupt object typed unavailable;
- reopen from manifest OIDs in separate clean worktree;
- full R1/no-clobber publication suite retained;
- same-snapshot idempotence, changed-snapshot conflict preservation, partial rerun convergence;
- verifier refuses all output/manifest/snapshot/live-source mismatches;
- manifest alone does not imply completion.

Update CLI goldens by official collector. Remove obsolete mutable-source rollback tests rather than carrying dead semantics.

## Real verification/handoff

Run two public dry-runs against:

- repo root: this repository
- runs root: `research/evidence/runs`
- source revision: exact canonical `f414512a728d26d507eccae9792ef89e6414007c`
- expected counts 170/130
- separate `/tmp` manifests.

Prove byte identity and report the new v2 snapshot digest, plan/manifest content digest, file SHA-256, and exact 170/152/130/128/2/40/18, zero ready/admissible. Confirm zero real outputs.

Run full strict/focused/CLI matrix, touched `ty`, Ruff/format, compile, diff check, clean status. Commit cleanly and page `wH:p9` with exact head/stat/API/module choice/digests/counts/tests and no-apply/no-model/no-subagent confirmation. Return for Architect review.
