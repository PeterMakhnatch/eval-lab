# Research status — 2026-08-15

Projection of `PROGRAM.json`. Names work, not only queue counts.
Smoke/oracle operational jobs are counted at the bottom, not in the
headline.

## RECENT

**Codex canary night 2026-08-15** (scored; 0 trial exceptions):

- event-summary — 3/3 `reward==1.0` — `runs/canary-event-summary-codex-20260815/`
- transaction-reconciliation — 3/3 `reward==1.0` — `runs/canary-transaction-reconciliation-codex-20260815/`
- html-js-filter — 0/3 `reward==1.0` (all three `reward==0.0`) — `runs/canary-terminal-bench-html-js-filter-codex-20260815/`

These are three task-family observations. They are not a ranking.

**Still on the record, not rewritten:**

- 2026-08-14 first-wave canaries — 9/9 `ValueError` “Model name is required” (`queue/failed/codex-01KZZFDC*`, `runs/canary-*-codex-20260814/`). Invalid harness. Outside capability denominators.
- 2026-08-14 r2 txn + html-js — 6/6 `NonZeroAgentExitCodeError` with reward 0.0 (`queue/done/codex-01M00850KV*` / `MBPM*`). Completed scored attempts with exceptions beside the denominator.
- RUNNER 2026-08-14 journal — Study 01 “no Codex trials” was true of *that* worktree’s queue. It is stale as a lab-wide claim.

Trajectory brief: `analysis/html-js-filter-codex-20260815-brief.md` (3 ATIF files).

## RUNNING NOW

Nothing in `queue/running/` or `queue/approved/` on the primary checkout
(inspected 2026-08-15). Launchd ticks log `tick_deferred` /
`no_approved_specs`.

## NEXT

Unsubmitted drafts only (`PROGRAM.json` status `designed`):

1. **EXP-N1** official-test instruction on html-js-filter — blocked on `extra_instruction_path`.
2. **EXP-N2** gpt-5.6-sol vs terra on event-summary — only if Peter still wants a model-pin A/B after terra 3/3 already scored.
3. **EXP-N3 / Study 04** claude-code on event-summary — blocked on Claude keychain.

Do not submit these from PROGRAM.

## TASK DECISIONS

Human-owned, unresolved:

1. Store `harbor-practice-claude-oauth` if claude-code nights should exist.
2. Whether `registered/*` is a real namespace (the proposed
   `queue/proposed/codex-01M023RP03KGSHB4WZ29WE9DGR.json` uses
   `registered/event-summary` plus `calibrated_judges_only`).
3. Whether to run sol vs terra at all (see review below).
4. Whether to register query-optimize or curated nominees (Studies 05–06).
5. BUILDER: add `extra_instruction_path` if N1 / Study 03 should exist.

**Review of proposed gpt-5.6-sol spec**
(`queue/proposed/codex-01M023RP03KGSHB4WZ29WE9DGR.json`):
Hypothesis is that an explicit `gpt-5.6-sol` Codex run will *complete
with a scored result instead of the model-less ValueError*. Newer
evidence already answers that for default-model Codex:
`runs/canary-event-summary-codex-20260815/*/result.json` shows three
scored trials, `exception_info=null`, `reward=1.0`,
`model_info.name=gpt-5.6-terra`. PROGRAM does not approve, reject,
delete, or submit this spec.

### Operational smoke (not the research headline)

Primary `queue/done/` also holds five `oracle-01M00*` event-summary
controls (checkpoint / reframe / mender / pipeline). They are task-
validity smoke. Count: 5 oracle specs. Not listed above as research
results.
