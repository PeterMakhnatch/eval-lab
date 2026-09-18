# Ops Eval Runner — execute approved strict historical contract apply

## Authority and isolation

- Architect-approved generator head: `09fffde5f4448f3c3a5750de3d7c0681a1792874`
- Approval: `/tmp/system-architect-historical-git-snapshot-final-rereview-09fffde5.md`
- Immutable source revision: `f414512a728d26d507eccae9792ef89e6414007c`
- Expected source snapshot: `sha256:fa0af7fb0cece3c143acc2a7b396c66cf478a1d716729d800cd0100a68a9cf70`
- Expected plan digest: `sha256:fa0e65174261fe0d95c826d801d5292148549ea516436dc3919ae48302d78957`
- Expected manifest file SHA-256: `1dd403b3523ab1ea583a90cf5514b5e26fb236087b715e8cd737913c8579ba4d`
- Expected census: promoted/events/descriptive/final/missing-final/missing-truth/missing-truth+events = `170/152/130/128/2/40/18`; ready/admissible `0/0`.

Create and own a fresh isolated worktree `/private/tmp/eval-lab-strict-historical-apply` and branch `ops/strict-historical-apply` at exact approved head `09fffde5`. Do not touch the root worktree, historical builder worktree, or canonical spine. Do not integrate/rebase/run models or controls/spawn subagents.

## Controlled execution

1. Verify the isolated worktree exact head and clean state.
2. Run a dry-run through the public CLI against:
   - repo root: the isolated repository/worktree;
   - runs root: `research/evidence/runs`;
   - explicit source revision exact `f414512a728d26d507eccae9792ef89e6414007c`;
   - `--expect-promoted 170` and `--expect-derivable 130`;
   - a dedicated `/tmp` dry-run manifest;
   - expected snapshot and plan digests above.
3. Verify the dry-run manifest is byte-identical to the approved file SHA-256 and exact census/zero-ready/zero-admissible. Confirm no real historical contracts exist before apply.
4. Run the public CLI in `--apply` mode with both expected snapshot and plan digests and canonical manifest destination:
   - `research/evidence/historical-contract-regeneration-manifest.json`
5. The apply must create/verify exactly 130 per-trial `artifacts/historical-contract.json` outputs plus the one canonical plan manifest. Any count/digest/path/symlink/conflict/refusal mismatch is a stop; preserve partial outputs and page the exact typed error, do not delete or retry with weaker expectations.
6. Run the exact same apply a second time to prove idempotent verify-only convergence.
7. Exercise the production `verify_historical_contract_set` API on the complete applied set with independently expected snapshot/plan digest and immutable Git repository authority. It must succeed.
8. Inspect every generated contract/manifest for schema/digest/snapshot binding via the shared verifier; independently count exact generated paths and validate the census and zero ready/admissible. Confirm no field inference and no unrelated mutation.

## Commit and handoff

Commit only the canonical manifest and exactly 130 generated `historical-contract.json` files on `ops/strict-historical-apply`. Keep generator commits in ancestry; do not amend approved commits. Run focused strict/shared-verifier tests and touched statics after apply, plus `git diff --check`; leave tree clean.

Page `wH:p9` with:

- exact execution branch/head/parent/tree;
- exact dry-run and first/second apply commands and exit results;
- exact snapshot/plan/manifest file digests;
- exact 170/152/130/128/2/40/18 and 0/0 counts;
- exact generated file count/path summary;
- shared verifier result;
- focused tests/statics;
- commit stat and clean status;
- confirmation of no model/control/integration/rebase/subagents and no unrelated files.

Return for Analyst artifact audit before any integration.
