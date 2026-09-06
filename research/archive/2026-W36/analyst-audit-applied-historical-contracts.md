# Analyst — audit applied strict historical contracts

## Exact applied source

- Worktree: `/private/tmp/eval-lab-strict-historical-apply`
- Branch/head: `ops/strict-historical-apply @ b22c6f00928537bce15c856c6c85b8836a84d121`
- Parent/approved generator: `09fffde5f4448f3c3a5750de3d7c0681a1792874`
- Architect approval: `/tmp/system-architect-historical-git-snapshot-final-rereview-09fffde5.md`
- Ops expected exactly 131 created files: one `research/evidence/historical-contract-regeneration-manifest.json` plus 130 per-trial `artifacts/historical-contract.json`.
- Immutable source revision: `f414512a728d26d507eccae9792ef89e6414007c`.

Read-only audit. Do not edit/rebase/integrate/apply/regenerate/run model/control/spawn subagents.

## Independent audit contract

Audit the exact committed bytes, not the Ops summary.

1. Verify exact clean head/parent and changed path set: exactly one canonical manifest plus exactly 130 generated contract files; no unrelated paths.
2. Verify canonical manifest file SHA-256 `1dd403b3523ab1ea583a90cf5514b5e26fb236087b715e8cd737913c8579ba4d`, manifest content/plan digest `sha256:fa0e65174261fe0d95c826d801d5292148549ea516436dc3919ae48302d78957`, source snapshot digest `sha256:fa0af7fb0cece3c143acc2a7b396c66cf478a1d716729d800cd0100a68a9cf70`, exact v2 schemas/domains, and complete selected-blob inventory.
3. Run production `verify_historical_contract_set` with independently expected snapshot/plan digests and immutable Git object authority; require exact 130-output success.
4. Independently recompute every listed output file SHA-256, contract v2 content digest, `source_snapshot_digest`, trial locator, trial source-inventory digest, and manifest disposition/output consistency. No missing, extra, duplicate, symlinked, or nonregular output.
5. Recompute the complete census:
   - promoted 170;
   - event journals 152;
   - descriptive/truth 130;
   - truth with final state 128;
   - truth missing final state 2;
   - truth missing 40;
   - truth plus events missing 18;
   - analysis-ready 0;
   - admissible 0.
6. Audit all previously unavailable design/control fields across all descriptive contracts. Every unavailable value must remain explicit present-null with typed reason/provenance per v2 semantics. No task-name/path/label-derived inference, no default opportunity/control values, no fabricated final state, and no readiness/admissibility elevation.
7. Specifically inspect all 2 truth-missing-final dispositions and all 40 truth-missing dispositions. Confirm only the 130 source-supported descriptive contracts exist and every missing axis remains a typed HOLD/unavailable rather than guessed.
8. Compare the committed manifest byte-for-byte with `/tmp/strict-historical-dry-run-manifest.json` if available; otherwise regenerate only a `/tmp` dry-run at exact approved generator/source and compare. Do not alter real outputs.
9. Confirm the second apply's claimed idempotence from repository bytes/commit scope: no additional generated names, no changed canonical bytes, and verifier success. Do not reapply unless strictly necessary; the Ops result is execution evidence, not a need to mutate.

## Verdict

Write `/tmp/analyst-applied-historical-contract-audit-b22c6f00.md` with exact head/tree, path/count/digest tables, unavailable-field matrix, any findings ordered by severity, and final **APPROVE** or **BLOCK**.

APPROVE means these are authoritative **descriptive-only historical contracts**, not analysis-ready trials, certified controls, new measurements, or model results. State that boundary explicitly.

Page `wH:p9` with exact verdict/evidence. If BLOCK, give the exact file/path/field/digest and source-correct repair; do not repair yourself.
