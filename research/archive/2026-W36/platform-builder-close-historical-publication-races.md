# Platform Builder — close last historical publication races

## Exact state

- Worktree: `/private/tmp/eval-lab-strict-historical-regeneration`
- Branch/head: `feat/strict-historical-regeneration @ c2ed462dbb49b542794bc912e95cabcfee272742`
- Rereview: `/tmp/system-architect-strict-historical-generator-rereview-c2ed462d.md`
- No-clobber fresh/concurrent publication, ordinary no-follow checks, CLI goldens, strict semantics/counts/determinism otherwise pass.

Do not rebase/apply real history/integrate/model/control/subagents. Close exactly R1/R2.

## R1 — existing-identical target final named-target validation

In `_atomic_create_or_verify_historical`, the pre-existing-identical branch must not return immediately after its first read.

- Re-read the named target immediately before returning, using the already-open parent dirfd and exact no-follow regular-file helper.
- Require the same exact bytes/identity expected by this invocation.
- A swap from regular identical file to symlink/nonregular/conflicting bytes after the first read must refuse.
- Apply this to both per-trial outputs and pre-existing identical report manifests.
- Add deterministic swap hooks/tests at the existing-identical return boundary; external target bytes remain untouched and no success is reported.

## R2 — source validation through manifest publication

Add a post-publication source barrier, not only a pre-publication check:

1. Build/capture the exact plan and revalidate before outputs (existing).
2. Revalidate immediately before manifest publication (existing).
3. Publish/verify the success manifest with the no-clobber primitive while tracking whether this invocation created it and its exact inode/content identity.
4. Immediately recompute/revalidate the complete source plan **after manifest publication and before returning success**.
5. If drift is detected:
   - return typed source-drift failure, never `applied=True`;
   - if this invocation created the manifest, remove only that exact created regular inode via the held parent dirfd/no-follow identity check, fsync parent, and prove no success manifest remains;
   - never remove a concurrent winner or pre-existing manifest;
   - created outputs remain explicitly incomplete/unmanifested and cannot be treated as a successful session; rerun remains fail-closed.

The manifest remains a digest-bound snapshot; consumers must verify bound source digests. The new post-publication barrier must catch the exact mutation injected on entry to manifest publication. Add that public regression plus a pre-existing-manifest drift case that returns failure without deleting the pre-existing file.

No protocol can prevent an uncooperative writer from mutating source after the command returns; the required commit boundary is that no drift detected before/during manifest publication can produce a successful return. Document that exact invariant rather than overclaiming filesystem immutability.

## Verification

Retain all prior tests. Run new R1/R2 races, the full 316+ focused matrix, two real dry-runs with unchanged 170/152/130/128/2/40/18 and exact digests, touched statics/diff/clean. Confirm zero real historical outputs. Commit cleanly and page `wH:p9` with exact head/evidence; return for re-review.
