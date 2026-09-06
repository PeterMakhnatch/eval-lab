# System Architect — review immutable Git-snapshot historical generator

## Exact source

- Worktree: `/private/tmp/eval-lab-strict-historical-regeneration`
- Branch/head: `feat/strict-historical-regeneration @ d349aaaaba98246e394e2d588cfd702ae8723e5e`
- Parent: `b78201be6e6cee221f0176e296bc797d6ad4b967`
- Design authority: `/tmp/system-architect-historical-immutable-source-snapshot-design.md`
- Builder reports clean tree; 6 files, +1551/-376; new `historical_git_snapshot.py` and adversarial tests.

Read-only review. Do not edit/rebase/integrate/apply outputs/run model/control/spawn subagents.

## Review contract

Verify the exact committed tree, not the page summary. Return **APPROVE** only if the entire clean-cut design is satisfied:

1. Source semantics derive exclusively from a once-resolved Git commit and exact selected blobs. No live-worktree source read/replan/revalidation/fallback remains.
2. NUL-safe tree selection exactly matches promoted marker → unique/non-overlapping trial roots → every regular blob except exact generated output. Unsafe/non-UTF-8/duplicate paths, symlink/gitlink/unsupported modes/nonblob/object mismatch refuse before writes.
3. Blob reopen validates OID/type/reported size/SHA-256; missing/corrupt objects are typed unavailable.
4. Snapshot identity binds normalized path, mode, OID, SHA-256, size and excludes commit/ref. Two commits with identical selected members remain byte-identical.
5. Strict contract/manifest v2 domains and snapshot binding are complete; no v1 compatibility or hidden inferred fields; 170/152/130/128/2/40/18 and zero ready/admissible semantics remain exact.
6. CLI requires explicit source revision; apply requires independently expected exact snapshot+plan digests before destination preflight.
7. Existing no-follow/no-clobber/R1 output protections remain. All source rollback/deletion semantics are removed. Failure after partial outputs never deletes them and rerun converges.
8. Shared `verify_historical_contract_set` verifies canonical manifest/digests, every blob, complete planned output set, output bytes/content digest/snapshot binding, extras, symlink/nonregular files, optional live-source exact match, and never treats manifest presence as completion.
9. No avoidable copy/allocation of the whole ~22 MiB tree: only parsed semantic documents retain bytes.
10. Tests actually prove the architecture report’s complete required matrix, including worktree/ref mutation, identical commits, all identity changes/exclusions, separate-repository reopen, object failure, partial convergence, verifier substitutions, and retained R1 publication adversaries.

Independently run the strict/focused tests and statics needed to substantiate the decision. Inspect public pinned dry-run evidence if helpful but do not apply. Builder reports:

- exact source revision `f414512a728d26d507eccae9792ef89e6414007c`
- snapshot `sha256:fa0af7fb0cece3c143acc2a7b396c66cf478a1d716729d800cd0100a68a9cf70`
- plan `sha256:fa0e65174261fe0d95c826d801d5292148549ea516436dc3919ae48302d78957`
- manifest file SHA-256 `1dd403b3523ab1ea583a90cf5514b5e26fb236087b715e8cd737913c8579ba4d`
- 419 focused tests, statics clean, zero real outputs.

Write `/tmp/system-architect-historical-git-snapshot-review-d349aaaa.md` with exact head/tree, findings ordered by severity, matrix evidence, and final APPROVE/BLOCK. If BLOCK, give minimal source-correct repair and tests. Page `wH:p9` with verdict.
