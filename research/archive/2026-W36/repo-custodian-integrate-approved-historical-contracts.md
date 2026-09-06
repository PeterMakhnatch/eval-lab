# Repo Custodian — integrate approved historical generator and contracts

## Exact authorities

- Canonical worktree/branch/head: `/private/tmp/eval-lab-staged-spine-integration`, `analyst/synth-data-promotion-hardening @ f414512a728d26d507eccae9792ef89e6414007c`.
- Approved generator head: `09fffde5f4448f3c3a5750de3d7c0681a1792874`.
- Generator semantic delta base: `8fa4d499` → `09fffde5`.
- Architect approval: `/tmp/system-architect-historical-git-snapshot-final-rereview-09fffde5.md`.
- Applied artifact commit: `b22c6f00928537bce15c856c6c85b8836a84d121` (parent `09fffde5`), exactly 131 added files.
- Analyst approval/corrected report: `/tmp/analyst-applied-historical-contract-audit-b22c6f00.md`.
- Immutable evidence source revision: exact canonical `f414512a728d26d507eccae9792ef89e6414007c`.

Own only the canonical integration worktree. Do not touch root or source worktrees, rebase canonical, integrate data-CAS/projection yet, run model/control, or spawn subagents.

## Integration order

1. Verify canonical exact clean `f414512a` state.
2. Semantically replay the complete reviewed generator delta `8fa4d499..09fffde5` onto canonical. Preserve all newer canonical authority: isolation/trial admissibility (`770488cf`), context step ordering (`1ecfc4a5`), staged CLI controls (`8fa4d499`), memory continuity (`9768ad60`), and MemGym source ingestion (`f414512a`). Do not mechanically replay an obsolete full file or drop newer imports/goldens.
3. Land the exact 131 artifact bytes from commit `b22c6f00`: one `research/evidence/historical-contract-regeneration-manifest.json` plus exactly 130 per-trial `artifacts/historical-contract.json`, no other path. Preserve them byte-for-byte; no regeneration against a different revision.
4. Keep clean review boundaries: one generator integration commit and one exact artifact commit (plus an official generated-doc commit only if repository generators require it). No squashing into older canonical history.

## Canonical contract to preserve

- Git selected-blob source authority resolved once from explicit revision; v2 contracts/manifest; snapshot identity path/mode/OID/SHA-256/size, commit-independent.
- Held repo/runs fds for generated-output namespace verification/publication; no intermediate symlink escape; final inode rechecks.
- Selector-deadlined bounded-memory `cat-file` streaming; semantic-document-only retention; stalled children reaped.
- Full exact generated-output namespace; shared verifier; no-clobber/R1 publication; no source rollback/deletion.
- Apply requires independently expected snapshot and plan digests.
- All historical fields remain descriptive-only/HOLD: 170/152/130/128/2/40/18, analysis-ready/admissible 0/0, 1690/1690 design fields explicit present-null.

## Validation on the integrated canonical tree

Run the actual public surface, not only unit tests:

1. Run a dry-run to `/tmp` with explicit source revision `f414512a728d26d507eccae9792ef89e6414007c`, expected promoted/derivable `170/130`, expected snapshot `sha256:fa0af7fb0cece3c143acc2a7b396c66cf478a1d716729d800cd0100a68a9cf70`, expected plan `sha256:fa0e65174261fe0d95c826d801d5292148549ea516436dc3919ae48302d78957`. Require manifest file SHA-256 `1dd403b3523ab1ea583a90cf5514b5e26fb236087b715e8cd737913c8579ba4d` and byte identity with the integrated manifest.
2. Run production `verify_historical_contract_set` against integrated files with independent expected snapshot/plan. Require exact 130 outputs and 170 dispositions.
3. Confirm integrated artifact path set/modes remain exactly 131 additions and no unrelated evidence mutation.
4. Run exact historical root/deadline/namespace/strict/R1 adversaries, CLI golden/registry tests, and sufficient canonical isolation/context/memory/MemGym regressions to prove no protected authority regressed.
5. Run touched Ruff/format, `ty`, compile, `git diff --check`.
6. Run official repo-map/doc-index generators if repository policy says code/evidence additions change generated docs; commit only generator-produced bytes.
7. Leave the canonical worktree clean.

## Handoff

Page `wH:p9` with exact new canonical head, commit chain/parents/stats, semantic conflict resolutions, artifact/path/digest/census/shared-verifier evidence, test/static results, generated-doc result, and clean status. Explicitly confirm no data-CAS/projection integration, model/control execution, rebase, source-worktree edits, or subagents.
