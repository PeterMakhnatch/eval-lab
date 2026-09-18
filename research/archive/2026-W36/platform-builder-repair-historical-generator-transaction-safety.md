# Platform Builder — repair historical generator transaction/CLI blockers

## Exact state

- Worktree: `/private/tmp/eval-lab-strict-historical-regeneration`
- Branch/head: `feat/strict-historical-regeneration @ ba2ef5c8fb837499cfe05de9c17630109a564a5f`
- Architect report: `/tmp/system-architect-strict-historical-generator-review-ba2ef5c8.md`
- Core authority semantics, exact counts, manifest digest, zero-ready/admissible ceiling, and dry-run determinism pass. Analyst separately APPROVES the real-corpus forensic value audit at `/tmp/analyst-strict-historical-dry-run-audit-ba2ef5c8.md`; do not disturb those semantics.

Do not rebase/apply to historical evidence/integrate, run models/controls, or spawn subagents. Repair the four exact closure areas only.

## T1 — no-follow regular-file authority

- Existing output and manifest targets must be inspected with `lstat`/no-follow semantics.
- Refuse symlinks even when they resolve to byte-identical regular content.
- Refuse non-regular targets and any symlinked descendant component beneath the resolved runs root/trial output boundary.
- Preserve standard platform aliases outside the evidence boundary (for example macOS `/tmp -> /private/tmp`) by resolving/anchoring the explicitly provided report parent once; never follow a symlink at the final report filename.
- Revalidate no-follow status at commit, not only preflight.
- Add public identical-output-symlink, dangling symlink, directory/nonregular, and manifest-target-symlink tests. Original/external bytes must remain untouched.

## T2 — true conflict-preserving create-or-verify

Do not use the existing check-then-`os.replace` helper for immutable historical authority.

- Implement a small colocated no-clobber publish primitive using an atomic same-filesystem mechanism such as: create/fsync a unique temp with `O_CREAT|O_EXCL|O_NOFOLLOW` where available, then `os.link(temp, target)` as atomic create-if-absent; if target wins with `EEXIST`, inspect it no-follow and verify exact bytes or refuse conflict. Unlink the temp and fsync the parent.
- Never overwrite a target that appears after preflight.
- Existing identical regular bytes verify; existing conflicting bytes remain byte-for-byte unchanged.
- Concurrent identical writers converge; concurrent conflicting writers leave one winner and a typed refusal for the loser.
- Add deterministic race injection tests for conflicting and identical concurrent creation at the commit boundary.

Use a boring platform-compatible implementation for Darwin/Linux. If a syscall/flag is unavailable, fail closed or use a proven no-clobber alternative; never fall back to replacement.

## T3 — bind apply success to current source bytes

- The plan already binds exact source inventory. Recompute/revalidate the complete source inventory immediately before output publication and again immediately before success-manifest publication.
- If any authoritative source path, no-follow type, size/content digest, or expected inventory membership changes, return typed source-drift refusal and do not publish a success manifest.
- Ensure any outputs created before a late drift can never be treated as a completed session without the manifest. Document the incomplete-session disposition; do not silently call apply successful.
- Consumers/apply reruns must verify record input/source digests rather than treat an unmanifested colocated file as current authority.
- Add exact mutation probes at three boundaries: after planning/before first output; during output publication; after outputs/before manifest. All must refuse success and no success manifest may exist.

Do not solve this by merely hashing the mutable path once more before the first write. The second pre-manifest validation is required.

## T4 — preserve CLI contract/goldens

Update the established CLI surface golden/leaf-count contract for the intentional public `data backfill contracts` command. Run and pass:

- `test_cli_surface_matches_pre_conversion_golden`
- `test_no_extra_commands_outside_golden`
- `test_registry_contract_ast_set_defaults_count_equals_leaf_count`

Also reconcile the already-integrated `registry promote --stage-controls` golden drift from canonical `8fa4d499` so `test_every_command_surface_matches_golden[registry promote]` passes, without changing runtime semantics or hiding the flag. Use the existing golden regeneration/update convention, not ad hoc skips or weakened assertions.

## Preserve and verify

Preserve all 15 strict semantics tests and add the no-follow/concurrency/source-drift regressions. Run:

- new strict historical tests;
- full data-backfill/C0/trial-admissibility/isolation/CLI focused matrix from the Architect report;
- two full historical dry-runs with exact 170/152/130/128/2/40/18, zero ready/admissible, byte identity;
- touched `ty`, Ruff check/format, compile, diff check, clean status.

Commit cleanly and page `wH:p9` with exact head/commit/stat, transaction design, new race/no-follow results, complete focused count, dry-run digest, zero historical writes, and no-model/no-subagent confirmation. Return for re-review; do not claim approval.
