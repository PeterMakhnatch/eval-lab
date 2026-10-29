# Worktree disposition report

Produced by `evallab worktrees` (`src/evallab/worktree_disposition.py`) from
`git worktree list --porcelain`, read-only Git plumbing, and one batched
`gh pr list`. Observational only: nothing was deleted, moved, pruned, or
fetched inside any worktree, and the report is not approval to retire any tree.

- Generated: 2026-10-29T23:04:39+00:00
- Base: `origin/main` at `69944145492135c7cc016e73cd0ed96895c48e43`
- PR state: available (1612 PRs read)
- Registered worktrees: 174
- delivered: 118 | unique-unmerged: 37 | blocked: 19
- Retirement-ready (delivered and no blocker): 45

## Verdict rules (first match wins)

1. Directory missing, prunable registration, or Git cannot report state: **blocked**.
2. Commits not accounted for on the base and HEAD detached: **blocked**.
3. Commits not accounted for on the base and HEAD on a branch: **unique-unmerged**.
4. All commits accounted for but uncommitted tracked changes, untracked paths, or a
   merge/rebase/cherry-pick/bisect operation: **blocked**.
5. Otherwise: **delivered**.

A commit is accounted for when it is reachable from the base, patch-equivalent to a
base commit (`git cherry`), or reachable from the recorded head of a merged PR into
the base branch. Locked registrations, open PRs, and ignored `runs/`, `derived/`,
`queue/` entries (presence only) are listed as retirement blockers and never change
a verdict.

## Unique-unmerged (unique work at risk): 37

| Worktree | Branch | HEAD | Ahead/Behind | PR | Reason / retirement blockers |
|---|---|---|---|---|---|
| `~/Developer/eval-lab/.worktrees/SB3AtlasRetriever-20261028` | `SB3AtlasRetriever-20261028` | `f15c6725ca` | 1/199 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/ce1-self5-labelproof-d9a2-20261025` | `ce1-self5-labelproof-d9a2-20261025` | `67cfb432ed` | 1/387 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/cloud-trace-retriever-20261025` | `cloud-trace-retriever-20261025` | `497f7f86bf` | 1/396 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/cloud-trace-retriever-devin-20261025` | `feat/cloud-trace-retriever-devin-20261025` | `13d8797be5` | 1/396 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/cm1-monitor-20261025` | `cm1-monitor-20261025` | `e08844b635` | 2/388 | none | 2 of 2 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. 3 untracked path(s); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/codex-seeker-attestation-20261025` | `codex/seeker-attestation-20261025` | `525a77e2db` | 1/389 | #1203 closed | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. 4 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/devin-exporter-store-fix-20261025` | `devin-exporter-store-fix-20261025` | `5a87e13236` | 1/394 | #1210 closed | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. 3 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/engine-queue-eq2-20261024` | `engine-queue-eq2-20261024` | `52aa6a3e86` | 21/409 | none | 8 of 21 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. 3 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/engine-queue-eq2-oci-cache-20261024` | `engine-queue-eq2-oci-cache-20261024` | `baffb459a8` | 18/409 | none | 5 of 18 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. 3 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/engine-queue-eq2-refresh-20261024` | `engine-queue-eq2-refresh-20261024` | `67b148f380` | 19/409 | none | 6 of 19 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. 3 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/engine-queue-eq2-refresh-cli-20261024` | `engine-queue-eq2-refresh-cli-20261024` | `102785f156` | 20/409 | none | 7 of 20 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. 3 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/eq1-cli-20261024` | `eq1/cli-20261024` | `092159d9b8` | 1/413 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/eq1-ingest-20261024` | `eq1/ingest-20261024` | `82e815395c` | 1/413 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/eq1-relay-20261024` | `eq1/relay-20261024` | `b9264eb9b6` | 1/413 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/eq1-runtime-20261024` | `eq1/runtime-20261024` | `8db1fc23a7` | 1/413 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/eq2-assets-20261024` | `eq2/assets-20261024` | `c61b61e192` | 1/413 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/eq2-images-20261024` | `eq2/images-20261024` | `398cea3b51` | 1/413 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/eq2-main-recipe-20261024` | `eq2/main-recipe-20261024` | `e97e6d7146` | 1/413 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/har-221-capture-smoke` | `foreman/har-221-tests-test-model-capture-py-test-capture` | `8402a38e1a` | 2/2 | #1544 open | 1 of 2 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. open PR #1544 |
| `~/Developer/eval-lab/.worktrees/har-225-worktree-disposition` | `har-225-worktree-disposition` | `79cb2c062c` | 1/2 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. 1 uncommitted tracked change(s); 2 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/har-230-txn-recon` | `foreman/har-230-library-tasks-transaction-reconciliation` | `59eb8e3fee` | 2/2 | #1588 open (other head) | 1 of 2 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. 5 uncommitted tracked change(s); 3 untracked path(s); open PR #1588; ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/np38-linuxarena-20261029` | `np38-result-review-20261029` | `d9dc2e0e0f` | 9/33 | #1591 merged | 2 of 9 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/p0f7` | `th/p0f7` | `7bc7a0049d` | 5/4 | none | 1 of 5 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. 3 untracked path(s); operation in progress (index.lock) |
| `~/Developer/eval-lab/.worktrees/qa-trace-regrade-20261015` | `qa-trace-regrade-20261015` | `9b5d870165` | 3/677 | none | 3 of 3 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. locked registration (QA Trace native runs/ evidence (2026-10-16 regrade campaign); migrate before removal - see research-context bridge/overnight-2026-10-15/qa-trace.md); 2 uncommitted tracked change(s); 1 untracked path(s); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/sb3abharness-20261028` | `sb3abharness-20261028` | `78add02f2c` | 1/199 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/ta-twp1-monitor-20261024` | `ta/twp1-monitor-2-20261024` | `b3fc059c1c` | 12/413 | none | 2 of 12 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/ta-twp1-policy-20261024` | `ta/twp1-policy-3-20261024` | `0496e267ff` | 16/411 | none | 1 of 16 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/ta-twp1-structural-20261024` | `ta/twp1-structural-20261024` | `e2ace2693e` | 1/413 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/ta-twp1-tripwires-20261024` | `ta/twp1-tripwires-20261024` | `52f272ee7d` | 2/413 | none | 2 of 2 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/trace-tools-burst-20261028-cost-inventory` | `trace-tools-burst-20261028-cost-inventory` | `d3f6f6d730` | 1/195 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/trace-tools-burst-20261028-docent-fastpath` | `trace-tools-burst-20261028-docent-watch` | `db534c3083` | 1/195 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/trace-tools-burst-20261028-harbor-prep` | `tt/trace-tools-burst-20261028-harbor-prep` | `d6da88a88f` | 1/195 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/trace-tools-burst-20261028-laminar-verifier` | `trace-tools-burst-20261028-laminar-verifier` | `9eab44db0a` | 1/195 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/trace-tools-burst-20261028-metr-plan` | `tt/trace-tools-burst-20261028-metr-plan` | `55575862e9` | 1/195 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/trace-tools-burst-20261028-scorecard-watch` | `trace-tools-burst-20261028-scorecard-watch` | `ada8a4f30b` | 2/195 | none | 2 of 2 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/trace-tools-burst-scout-incremental-20261028` | `trace-tools-burst-20261028-scout-incremental` | `b4afad516b` | 1/195 | none | 1 of 1 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |
| `~/Developer/eval-lab/.worktrees/trace-tools-store-20261027` | `trace-tools/trace-store-20261027` | `7e8ab325b7` | 6/318 | none | 6 of 6 commit(s) ahead of origin/main are neither patch-equivalent on the base nor in a merged PR head. none |

## Blocked (no safe disposition): 19

| Worktree | Branch | HEAD | Ahead/Behind | PR | Reason / retirement blockers |
|---|---|---|---|---|---|
| `~/.local/state/foreman/worktrees/20261017T141247171642Z-8046eab32b2044638348365ca40c95a5` | `foreman/har-206-every-results-view-treats-copied-answers` | `e2f7a79eff` | 0/579 | none | HEAD delivered but 73926 uncommitted tracked change(s), 23 untracked path(s). locked registration (initializing); 73926 uncommitted tracked change(s); 23 untracked path(s) |
| `~/Developer/eval-lab` | `main` | `fdaf5fe92a` | 0/746 | none | HEAD delivered but 1 uncommitted tracked change(s), 1 untracked path(s), operation in progress (index.lock). primary checkout; 1 uncommitted tracked change(s); 1 untracked path(s); operation in progress (index.lock); ignored evidence present: derived/ (9494 entries); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/ce1-retriever-20261025` | `ce1-retriever-20261025` | `86c9f7dd79` | 11/389 | #1208 merged | HEAD delivered but 2 uncommitted tracked change(s), 3 untracked path(s). 2 uncommitted tracked change(s); 3 untracked path(s); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/ce1-retriever-final-b7e4-20261025` | `ce1-retriever-final-b7e4-20261025` | `ff28231aeb` | 9/390 | none | HEAD delivered but 12 uncommitted tracked change(s), 3 untracked path(s), operation in progress (MERGE_HEAD). 12 uncommitted tracked change(s); 3 untracked path(s); operation in progress (MERGE_HEAD); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har-206-counting-contracts` | `har-206-counting-contracts` | `42bf8a8fbe` | 0/2 | none | HEAD delivered but 6 uncommitted tracked change(s). 6 uncommitted tracked change(s) |
| `~/Developer/eval-lab/.worktrees/har-224-mimo-v5-gap-rank` | `har-224-mimo-v5-gap-rank` | `42bf8a8fbe` | 0/2 | none | HEAD delivered but 2 untracked path(s). 2 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/har110-live` | `detached` | `7fcc63906e` | 0/1021 | none | HEAD delivered but 2 untracked path(s). locked registration (HAR-110 evidence (runs/, gitignored staging) pending RH acceptance); 2 untracked path(s); ignored evidence present: derived/ (26 entries); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har95-nop-20260929` | `feat/nop-wavea-code-cyber-qual` | `e404cb87e9` | 0/1062 | none | HEAD delivered but 13 untracked path(s). locked registration (HAR-130 evidence preservation: HAR-95; 13 published provenance references); 13 untracked path(s); ignored evidence present: derived/ (2 entries); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/mimo-shared-image-restoration` | `detached` | `42904afa8d` | 1/636 | none | detached HEAD carries 1 unaccounted commit(s); no branch ref protects them. locked registration (Live frozen MiMo manifest consumed by Trainer, QA Trace, EnvCheck and RH; retain until consumers migrate or finish) |
| `~/Developer/eval-lab/.worktrees/muse-tripwires-v4-fix-20261025` | `muse-tripwires-v4-fix-20261025` | `1e23700a38` | 18/396 | none | HEAD delivered but 6 uncommitted tracked change(s). 6 uncommitted tracked change(s) |
| `~/Developer/eval-lab/.worktrees/opus-retriever-seal-20261025` | `opus-retriever-seal-20261025` | `0ca23f1eb0` | 0/393 | none | HEAD delivered but 3 uncommitted tracked change(s), 1 untracked path(s). 3 uncommitted tracked change(s); 1 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/pj-self-cb2a4d-4-finding-cap-20261029` | `pj-self-cb2a4d-4-finding-cap-20261029` | `0edabdb8ad` | 0/6 | none | HEAD delivered but 2 untracked path(s). 2 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/ps7j1` | `ps7-seal-sweep-full` | `4fbccb6320` | 0/8 | none | HEAD delivered but 1 untracked path(s). 1 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/ps7j2` | `ps7-export-audit` | `4fbccb6320` | 0/8 | none | HEAD delivered but 3 uncommitted tracked change(s), 1 untracked path(s). 3 uncommitted tracked change(s); 1 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/sb2-replay-native-20261028` | `sb2-replay-native-20261028` | `6d6ad1f807` | 0/198 | none | HEAD delivered but 1 uncommitted tracked change(s), 2 untracked path(s). 1 uncommitted tracked change(s); 2 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/ta-tw-p1-gate-20261024` | `ta/tw-p1-gate-20261024` | `6f7c811dbc` | 17/411 | none | HEAD delivered but 4 uncommitted tracked change(s). 4 uncommitted tracked change(s) |
| `~/Developer/eval-lab/.worktrees/tq-p1-20261024` | `tq/task-quality-p1-20261024` | `5f18de3d80` | 0/413 | none | HEAD delivered but 6 uncommitted tracked change(s), 2 untracked path(s). 6 uncommitted tracked change(s); 2 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/trace-monitor-md-p1-20261024` | `trace-monitor/md-p1-20261024` | `5f18de3d80` | 0/413 | none | HEAD delivered but 3 uncommitted tracked change(s), 7 untracked path(s). 3 uncommitted tracked change(s); 7 untracked path(s) |
| `~/Developer/eval-lab/.worktrees/ts14-native-repair-validation-20261028` | `detached` | `31743660fe` | 0/197 | none | HEAD delivered but 4 uncommitted tracked change(s). 4 uncommitted tracked change(s); ignored evidence present: derived/ (1 entry); ignored evidence present: runs/ (1 entry) |

## Delivered: 118

| Worktree | Branch | HEAD | Ahead/Behind | PR | Retirement blockers |
|---|---|---|---|---|---|
| `~/.local/state/foreman/worktrees/20261017T160314507405Z-c874bfd7bc22445ca1685ced036dd34d` | `detached` | `92d7f35448` | 0/572 | none | locked registration (HAR-204 fifth-resume unique admission, JUnit and compatibility evidence; archive before retirement); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab-worktrees/data-flywheel` | `detached` | `3c881fbc29` | 0/30 | none | none |
| `~/Developer/eval-lab-worktrees/judge-route-p12-7` | `detached` | `5c848c241f` | 0/33 | none | none |
| `~/Developer/eval-lab-worktrees/live-supervisor` | `detached` | `10e6f6636a` | 0/135 | none | none |
| `~/Developer/eval-lab-worktrees/replay-reader-p12` | `detached` | `5c848c241f` | 0/33 | none | none |
| `~/Developer/eval-lab-wt/th2-detcache` | `th2/detcache` | `2e8b738895` | 1/200 | none | none |
| `~/Developer/eval-lab-wt/th2-exp` | `th2/exp` | `704745c265` | 1/199 | none | none |
| `~/Developer/eval-lab-wt/th2-health` | `th2/health` | `88170308bd` | 1/200 | none | none |
| `~/Developer/eval-lab-wt/th2-hodo` | `th2/hodo` | `654a990f35` | 1/199 | none | none |
| `~/Developer/eval-lab-wt/th2-own` | `th2/own` | `2810371713` | 1/200 | none | none |
| `~/Developer/eval-lab-wt/th2-run` | `detached` | `f44586730f` | 0/199 | none | none |
| `~/Developer/eval-lab/.worktrees/breaker-l1-prepare-20261012` | `detached` | `2d59a70c34` | 0/734 | none | locked registration (rh-agents: PR 855 merged; exact frozen 5+7 task bundle retained for Peter budget decision; no paid execution authorized); ignored evidence present: derived/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/ce1-self6-identities-d9a2-20261025` | `ce1-self6-identities-d9a2-20261025` | `0679330890` | 1/387 | none | none |
| `~/Developer/eval-lab/.worktrees/cm12-blinded-acceptance-20261028` | `cm12-blinded-acceptance-20261028` | `cdf6519efb` | 16/244 | #1369 merged (other head) | locked registration (CM-12: retain clean owned worktree for open PR 1369; protected merge needs current-main CI; private evidence retained externally); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/envcheck-correct-pass-v7-20261017` | `detached` | `f437f1da12` | 0/562 | none | locked registration (EnvCheck v7 correct-pass CPU controls use this exact isolated runtime and immutable native jobs; preserve source paths); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/envcheck-heldout-v7-20261017` | `envcheck/heldout-v7-20261017` | `2472ffd9a9` | 1/572 | #1020 merged (other head) | locked registration (EnvCheck v7 immutable packages, private CAS and raw native controls are live MiMoData/QA/Verifier consumer dependencies; preserve source paths); ignored evidence present: derived/ (4 entries); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/envcheck-patch-bytes-v7-20261018` | `detached` | `d7373c8396` | 0/552 | none | locked registration (EnvCheck raw-diff byte preservation implementation and integrated CI/native source proof; preserve while active); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/eval3-harness-20261021` | `detached` | `111fc49fbe` | 0/465 | none | locked registration (E3-1 retained DEV judgments, source custody and paid budget ledgers; preserve runs); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/fl-core-20261027` | `detached` | `d9d550f961` | 0/216 | none | ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/fl-fixmine-20261027` | `fl/fixmine-20261027` | `df881f788e` | 2/318 | none | none |
| `~/Developer/eval-lab/.worktrees/fl-tasks-tb-20261027` | `fl/tasks-tb-publication-safety-20261027` | `41aab95ab2` | 6/316 | none | none |
| `~/Developer/eval-lab/.worktrees/fl-verify-20261027` | `fl/verify-20261027` | `0ba0e2f033` | 2/322 | #1276 merged | none |
| `~/Developer/eval-lab/.worktrees/fleet-ls-freeze` | `fleet/ls-freeze-20261029` | `42bf8a8fbe` | 0/2 | none | none |
| `~/Developer/eval-lab/.worktrees/fleet-own-heldout` | `fleet/own-heldout-20261029` | `42bf8a8fbe` | 0/2 | none | none |
| `~/Developer/eval-lab/.worktrees/fleet-tq-unverified` | `fleet/tq-unverified-20261029` | `42bf8a8fbe` | 0/2 | none | none |
| `~/Developer/eval-lab/.worktrees/gepa-subscription-20260930` | `detached` | `79586a9b6f` | 0/1008 | none | locked registration (Qualified GEPA subscription runtime; retained seed evidence and pending candidate review); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har-169-verify` | `har-169-verify` | `42bf8a8fbe` | 0/2 | none | none |
| `~/Developer/eval-lab/.worktrees/har-202-closeout` | `har-202-closeout` | `42bf8a8fbe` | 0/2 | none | none |
| `~/Developer/eval-lab/.worktrees/har-205-egress-verify` | `har-205-egress-verify` | `42bf8a8fbe` | 0/2 | none | none |
| `~/Developer/eval-lab/.worktrees/har-222-doctor-launchagents` | `foreman/har-222-evallab-doctor-and-headless-readiness-re` | `4300760785` | 3/2 | #1564 merged | ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har-223-process-job-timing` | `har-223-process-job-timing` | `42bf8a8fbe` | 0/2 | none | none |
| `~/Developer/eval-lab/.worktrees/har-226-nightly-exit` | `har-226-nightly-exit` | `42bf8a8fbe` | 0/2 | none | none |
| `~/Developer/eval-lab/.worktrees/har-231-sheet-recalc-ladder-v3` | `har-231-sheet-recalc-ladder-v3` | `42bf8a8fbe` | 0/2 | none | ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har-235-ci-gates` | `detached` | `42bf8a8fbe` | 0/2 | none | none |
| `~/Developer/eval-lab/.worktrees/har104-runs` | `detached` | `f1bce0f9f1` | 0/1037 | none | locked registration (HAR-104/HAR-110 run evidence (runs/har104-d-*)); ignored evidence present: derived/ (3 entries); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har105-explore-20260929` | `detached` | `a8d895376a` | 0/1041 | none | locked registration (HAR-130 evidence preservation: HAR-105; 3 published provenance references); ignored evidence present: derived/ (2 entries); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har105-python-20260930` | `detached` | `c3e2a13a11` | 0/1038 | none | locked registration (HAR-130 evidence preservation: HAR-105; 13 published provenance references); ignored evidence present: derived/ (1 entry); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har105-taskfix-20260929` | `detached` | `a8d895376a` | 0/1041 | none | locked registration (HAR-130 evidence preservation: HAR-105; 11 published provenance references); ignored evidence present: derived/ (2 entries); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har108-census-20260930` | `detached` | `e8304c199c` | 0/1020 | none | locked registration (HAR-108 nop run data: runs/har108-nop-* (522 jobs) are the log evidence for research/experiments/har108-python-census/task_health.parquet); ignored evidence present: derived/ (1 entry); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har113-variants-20260930` | `detached` | `b62d41d6ea` | 0/1015 | none | locked registration (HAR-113 evidence: runs/har113-*, derived probes); ignored evidence present: derived/ (2 entries); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har115-census-20261001` | `detached` | `ff65f52f59` | 0/993 | none | locked registration (HAR-115 evidence (runs/)); ignored evidence present: derived/ (2 entries); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har116-live` | `detached` | `a87e01afed` | 0/991 | none | locked registration (HAR-116 run evidence pending acceptance); ignored evidence present: derived/ (41 entries); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har122-egress-evidence` | `detached` | `3f5c7ce483` | 0/998 | none | locked registration (HAR-122 egress-lock probe evidence (runs/har122-probe-*)); ignored evidence present: derived/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har126-live` | `detached` | `b1619aa3e3` | 0/914 | none | locked registration (HAR-130: preserve active HAR-126 G2/G5 evidence and mixed-revision file-chain hold); ignored evidence present: derived/ (133 entries); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har164` | `pmakhnatch/har-168-live-harbor-trials-for-mimoagent-incremental-atif-stream-on` | `370f8de1fb` | 0/858 | #725 merged (other head) | locked registration (HAR165 actual paid SDK qualification failed; immutable original3 cost/source ledger and unchanged held HAR157 twelve require explicit Peter corrective-delivery and replacement-admission decision before campaign completion); ignored evidence present: derived/ (28 entries); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har177-verdicts-20261006` | `pmakhnatch/har-177-ledger-refresh` | `7f9351580c` | 1/847 | #741 merged | ignored evidence present: derived/ (1 entry); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har188` | `detached` | `f3829ce9f5` | 0/823 | none | locked registration (no reason); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har81-dispatch-528` | `detached` | `7de1ce6e4b` | 0/1055 | none | locked registration (HAR-130 evidence preservation: HAR-81; 17 published provenance references); ignored evidence present: derived/ (20 entries); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har81-dispatch-531` | `detached` | `48b787b5e4` | 0/1052 | none | locked registration (HAR-130 evidence preservation: HAR-81; 27 published provenance references); ignored evidence present: derived/ (30 entries); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/har92-step-layers` | `detached` | `91eafa2db1` | 0/1047 | none | locked registration (HAR-130 evidence preservation: HAR-90; 5 published provenance references); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/id-oq1-miss-analysis-20261029` | `id-oq1-miss-analysis-20261029` | `501f530b2b` | 1/27 | #1599 merged | none |
| `~/Developer/eval-lab/.worktrees/idsb1-preflight-20261028` | `idsb1-preflight-20261028` | `e74acbd1b0` | 1/127 | #1485 merged | none |
| `~/Developer/eval-lab/.worktrees/infra-accounting-refusals-20261017` | `detached` | `96b968e5db` | 0/584 | none | locked registration (Retain unique Infra A035 premerge/adopted runtime and CI proof; protected merge 96b968e5db7bff2408ef93536618457359b7415e; no active cloud jobs); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-host-loop-airgap-20261018` | `detached` | `233be4e704` | 0/517 | none | locked registration (Quota-paused PR 1056; retain unique native airgap evidence and archived fixtures); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-modal-20261017` | `detached` | `9d7b657845` | 0/595 | none | locked registration (Unique ignored overnight Infra Modal proof artifacts referenced by the 2026-10-17 bridge; retain until archived); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-reliability-20261017` | `detached` | `8389843e8c` | 0/593 | none | locked registration (Unique ignored merged-head reliability and canonical-consumer receipts referenced by the 2026-10-17 bridge; retain until archived); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-reward-controls-20261018` | `detached` | `9814742d98` | 0/536 | none | locked registration (Retain owned no-model TB2 control evidence from merged PR 1055); ignored evidence present: derived/ (5 entries); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-reward-pmars-check-20261018` | `detached` | `343342ec47` | 0/525 | none | locked registration (Retain exact merged PR 1067 oracle/blank control evidence); ignored evidence present: derived/ (5 entries); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-reward-pmars-closure-20261018` | `detached` | `c043f9a22d` | 1/526 | #1067 merged | locked registration (Retain pmars dependency-recovery probes and exact-merge controls); ignored evidence present: derived/ (6 entries); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-reward-pv1-20261019` | `detached` | `621eb8d24d` | 1/525 | none | locked registration (PV-1 native control and diagnostic evidence retention); ignored evidence present: derived/ (16 entries); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-reward-pv1-merged-20261019` | `detached` | `8e40f3ad6c` | 0/515 | none | locked registration (PV-1 merged native control evidence retention); ignored evidence present: derived/ (9 entries); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-rollout-accounting-20261017` | `detached` | `79bf79a518` | 0/590 | none | locked registration (Unique Infra rollout native-state/accounting and final-custody proof receipts; archive before pruning); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-rollout-benchmark-contract-20261017` | `detached` | `2ccce48cd6` | 0/586 | none | locked registration (Retained PR1002 exact-merge benchmark boundary proof with unique ignored run evidence); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-tb4-hub-trials-20261020` | `detached` | `5537596467` | 0/478 | none | locked registration (IN-2: preserve original public TB4 trial corpus, immutable smoke proofs and census handoff); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-tb4-provenance-20261020` | `detached` | `4b6f766674` | 0/470 | none | locked registration (pC-SELF-1 PR1120 merge4b6f766 immutable evidence: runs/pc-self1-provenance/{native,packages,join,delivery}; handoff /Users/petermakhnatch/Developer/research-context/bridge/2026-10-20-open-qa/infra.md; preserve, never prune); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-tb4-upstream-grader-20261020` | `detached` | `0ba6ec1510` | 0/480 | none | locked registration (IN-1: preserve unique original TB4 census, native controls and shared grader evidence); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/infra-v7-modal-rates-20261017` | `detached` | `ef6a513b3d` | 0/570 | none | locked registration (Retained actual billing calibration and exact merged provider-free consumer evidence); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/mimo-batch-verifier-20261024` | `mimo-batch-verifier-20261024` | `5f18de3d80` | 0/413 | none | locked registration (Retained MiMo Batch1 QA evidence and absolute :8095 payload inputs; preserve until approved runtime input migration.); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/mimo-v7-heldout-20261017` | `detached` | `b8c47aace8` | 0/565 | none | locked registration (Retain six 001948 v7 native controls and exact manifest custody until durable archival); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/oq10-run-20261028` | `detached` | `f6cba1ca4b` | 0/132 | none | none |
| `~/Developer/eval-lab/.worktrees/oq6-tq-dev-continuation-20261028` | `detached` | `9033641b2a` | 0/238 | none | locked registration (OQ6 v5 frozen continuation source; owner OQ4TaskQuality; retain until native consumers and final custody join finish); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/oq6-tq-gate-rerun-20261027` | `detached` | `0d9010f0c0` | 0/295 | none | locked registration (OQ6 v4 live frozen-source dependency; owner OQ4TaskQuality; release only after workers and final-summary custody checks finish); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/oq6-tq-v3-runtime-20261027` | `detached` | `7762ea56a1` | 0/324 | none | locked registration (OQ6 v4 live scientific runtime; owner OQ4TaskQuality; release only after workers and final-summary custody checks finish) |
| `~/Developer/eval-lab/.worktrees/oq7-atlas-guided-audit-20261029` | `oq7-atlas-guided-audit-20261029` | `88ea77e532` | 1/63 | #1552 merged | none |
| `~/Developer/eval-lab/.worktrees/oq8-data-access-ab-20261029` | `oq8-data-access-ab-20261029` | `6753e30088` | 2/59 | #1567 merged | none |
| `~/Developer/eval-lab/.worktrees/oq9-atlas-facet-join-20261029` | `oq9-atlas-facet-join-20261029` | `08abe8eb46` | 1/48 | #1570 merged | none |
| `~/Developer/eval-lab/.worktrees/pj-self-cb2a4d-1-atlas-targets-20261029` | `pj-self-cb2a4d-1-atlas-targets-20261029` | `7d66bf4e74` | 1/13 | #1607 merged | none |
| `~/Developer/eval-lab/.worktrees/pj-self-cb2a4d-2-auditor-union-20261029` | `pj-self-cb2a4d-2-auditor-union-20261029` | `46c5dccc63` | 1/12 | #1609 merged | none |
| `~/Developer/eval-lab/.worktrees/pj-self-cb2a4d-3-consensus-20261029` | `pj-self-cb2a4d-3-consensus-20261029` | `957bebb698` | 1/8 | #1610 merged | none |
| `~/Developer/eval-lab/.worktrees/pk2-sonnet-op-20261029` | `pk2-sonnet-op-results-20261029` | `4b785bb3c2` | 1/24 | #1600 merged | none |
| `~/Developer/eval-lab/.worktrees/pk3-consensus-20261029` | `pk3-consensus-20261029` | `54d30902dd` | 1/19 | #1604 merged | none |
| `~/Developer/eval-lab/.worktrees/pk3-consensus-results-20261029` | `pk3-consensus-results-20261029` | `cd2a0b96cb` | 1/12 | #1608 merged | none |
| `~/Developer/eval-lab/.worktrees/ps7j3` | `ps7-data-map-audit` | `7122ff9c8d` | 0/7 | none | none |
| `~/Developer/eval-lab/.worktrees/qa-open-agent-20261020` | `detached` | `22eac18811` | 0/474 | none | locked registration (QA-2 frozen campaign interpreter dependency; owner QA Task retains until scoring closes); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/qa-task-mimo-semantic-checks-20261018` | `detached` | `27c8a7ba82` | 0/530 | none | locked registration (QA Task: exact merged-code full aa6 audit, fresh replay and independent positive probes; preserve non-rebuildable runs); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/qa-task-mimo-semantic-s2a-20261018` | `detached` | `22b73d12ee` | 7/531 | #1062 merged | locked registration (QA Task: unique source-bound full-audit attempts and six actual baseline control trials referenced by published receipts); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/qa-task-pj1-20261019` | `detached` | `8e1d1bd78f` | 0/507 | none | locked registration (QA Task PJ-1: retain native evidence and live shared $1 overnight ledger; PR #1084); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/qa-task-v7-wave2-20261018` | `qa-task/v7-wave2-20261018` | `3b71dc4ca1` | 0/541 | none | locked registration (QA Task: published frozen raw evidence and read-only native setup helper; retain until explicitly archived and consumers migrated); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/qa-trace-pk1-20261019` | `detached` | `f130e5e553` | 0/511 | none | locked registration (PK-1 frozen input and consumer paths; qa-trace w1C:pK; full comparison quota-blocked, retain immutable runtime until dependency migration or completion); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/qa-trace-regrade-delivery-20261015` | `detached` | `52e4d941ab` | 0/639 | none | locked registration (QA Trace native runs/ evidence (2026-10-16 regrade campaign); migrate before removal - see research-context bridge/overnight-2026-10-15/qa-trace.md); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/qa-trace-v7-sprint-frozen` | `detached` | `dd8ba6e3de` | 0/554 | none | locked registration (qa-trace/7 dd8ba6e3 pre-label runtime; retain until v7 pass labels and cheat-set are scored) |
| `~/Developer/eval-lab/.worktrees/qa-trace-v8-frozen` | `detached` | `4e47eaf9fb` | 1/540 | none | locked registration (QA Trace 8 frozen test-aware experiment runtime; preserve pending benchmark and handoff scoring) |
| `~/Developer/eval-lab/.worktrees/r29-oq1-codex-cross-arm-20261029` | `r29-oq1-codex-cross-arm-20261029` | `bde13ece6f` | 1/19 | #1605 merged | none |
| `~/Developer/eval-lab/.worktrees/rds2-merged-20261017` | `detached` | `ead645e47f` | 0/597 | none | locked registration (initializing) |
| `~/Developer/eval-lab/.worktrees/recalc-task-20261028` | `task/recalc-engine` | `39c01ffc99` | 1/207 | #1467 merged | ignored evidence present: derived/ (1 entry); ignored evidence present: queue/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/sb1-cohort-census-20261027` | `detached` | `bea49e18d6` | 4/287 | #1322 merged | none |
| `~/Developer/eval-lab/.worktrees/sb1-seeker-b-20261027` | `detached` | `de286f477f` | 0/200 | none | none |
| `~/Developer/eval-lab/.worktrees/sb2-error-first-policy-20261028` | `detached` | `a63fd147f4` | 0/218 | none | none |
| `~/Developer/eval-lab/.worktrees/sb2-known-hacks-20261028` | `detached` | `4dbff68863` | 0/217 | none | none |
| `~/Developer/eval-lab/.worktrees/ta-np45-tw1-20261029` | `ta/np45-tw1-creation-rule-20261029` | `42d5c122a0` | 3/5 | #1612 merged | none |
| `~/Developer/eval-lab/.worktrees/ta-tw1-integration-20261025` | `ta/tw1-integration-20261025` | `999df1850d` | 28/363 | #1204 merged | ignored evidence present: derived/ (1 entry); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/ta-tw10-scout-tripwires-20261027-541fd1` | `ta/pg-dd3185-tripwires-20261027` | `ca471df12b` | 0/317 | none | locked registration (initializing) |
| `~/Developer/eval-lab/.worktrees/ta-twp1-measure-20261024` | `ta/twp1-tune-canonical-20261024` | `6f7c811dbc` | 17/411 | none | none |
| `~/Developer/eval-lab/.worktrees/trace-tools-actor-provenance-20261028` | `detached` | `456b6eacc3` | 0/229 | none | ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/trace-tools-docent-only-20261028` | `trace-tools/docent-only-sync-20261028` | `c771d1b4ad` | 3/227 | #1381 merged | ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/trace-tools-docent-watch-20261028` | `detached` | `5906bb6d66` | 0/227 | none | ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/trace-tools-export-preservation-20261028` | `trace-tools-burst-integration3-20261028` | `ba28a53996` | 9/138 | #1475 merged (other head) | ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/trace-tools-metr-20261027` | `detached` | `922a1c7ab0` | 0/204 | none | locked registration (TT-4 Kimi resumable runner is a live handoff dependency; Main owns reset launch) |
| `~/Developer/eval-lab/.worktrees/trace-tools-own-cascade-20261027` | `detached` | `9db33952c2` | 0/311 | none | locked registration (TT-3 Kimi resume command at 10:22 PM ET uses this isolated venv and script; retain until lane owner completes the batch.) |
| `~/Developer/eval-lab/.worktrees/trace-tools-tt1-20261027` | `detached` | `2c8df041b3` | 3/302 | #1300 merged | ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/trace-tools-tt7-codex-continuation-20261028` | `detached` | `b04fdb02ac` | 0/211 | none | ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/traces-ref-subset-mutants` | `traces/ref-subset-mutants` | `cd14f6d814` | 8/673 | #918 merged | locked registration (MiMo ref-subset sweep with --mutants runs from this venv; Traces removes after) |
| `~/Developer/eval-lab/.worktrees/trainer-window3-source-20261019` | `detached` | `bad1663ee6` | 0/519 | none | locked registration (initializing); ignored evidence present: runs/ (1 entry) |
| `~/Developer/eval-lab/.worktrees/ts13-seeker-emit-spec-20261028` | `prod/ts13-seeker-emit-spec-20261028` | `456b6eacc3` | 0/229 | none | none |
| `~/Developer/eval-lab/.worktrees/verifier-hb1-20261021` | `detached` | `46a4795b90` | 0/467 | none | locked registration (VE-1 hackbook verification through 17:00 ET) |
| `~/Developer/eval-lab/.worktrees/verifier-st1-20261021` | `detached` | `1450d62da9` | 0/468 | none | locked registration (VE-1 merged ST-1 audit through 2026-10-21 17:00 ET) |
| `~/Developer/eval-lab/.worktrees/verifier-ve1-20261021` | `detached` | `4b6f766674` | 0/470 | none | locked registration (VE-1 rolling audit and timed status through 2026-10-21 17:00 ET) |
| `/private/tmp/ps2/before-wt` | `detached` | `969840228a` | 0/201 | none | none |
