# MiMo clean-set census receipt

Date: 2026-10-10. Owner: CensusFinish. Slice approval: **$13 cumulative**
within Peter's $25 programme approval. Method: `docs/mimo/verification.md`.
Runner: `evallab mimo-census run|report|record-spend`.

## Scope and package identity

The definitive census targets `mimo-clean-v3`: 1,148 built Python packages,
256 indexed Python reference fixes, and a stratified 218-task non-Python
sample. The committed Python ladder minimum is the union of the random
stratified 300 and all 256 references: **490 unique tasks** (66 overlap).
The sample seed, runner quotas, task IDs, and exact digests are in
`sampling.json`. Its manifest SHA identifies the pre-census canonical
manifest, not a later CSV whose census-owned `verify` cells have changed.

V3 ships `purge-build-caches@4`, `mtime-normalize@2`,
`separate-verifier@4`, and `agent-network-none@1`. Setup and the fresh
separate verifier remain public; the agent alone is blocked by the package.
The census does not add a blanket runtime egress lock. Historical v1/v2
rows remain identified by their original package generation and digest.

## Exercised gates

- Historical Docker runner parity: six v1 tasks matched the existing
  `verify-local` oracle/nop/ladder outcomes. This is historical evidence,
  not a claim that final-v3 fleet controls are complete.
- Final-v3 resource parity: 000085, 000158, and 002552 each returned oracle
  **1**, nop **0**, and a **clean** full ladder on Docker, Modal AUTO, and
  Modal LIMIT. All 18 controls have test-execution evidence; none of the
  27 trials reported an OOM, timeout, or trial exception. Exact caps,
  configs, outcomes, and raw paths: `modal-limit-parity.json`.
- First 40 sorted Python tasks: all attempted on final-v3 Modal LIMIT;
  six reference controls passed. Nop columns recorded **36 `0`**, **three
  `0-noexec`**, and **one `setup-fail`**. `0-noexec` means missing recognized
  execution proof, not a proven absence of every custom test.
- Real SDK fix-probe transport exercised on 002552: actual published setup
  completed, five known-patch patterns scanned, and zero hits established
  **probe-blind**, not cleanliness. Its initial pilot used the former
  adapter that skipped clean after a blind control. The corrected adapter
  always measures both actual setups and requires both completions for
  blind cache reuse. The initial pilot remains historical raw evidence.
- Continuation checkpoint: **73 unique current-digest Modal LIMIT control
  tasks**, including parity controls. Nop: 64 `0`, seven `0-noexec`, two
  `setup-fail`. Oracle: 18 `1`, one `fail:0` (000163), 54 `n/a`.
- Actual two-phase SDK probes completed on **24 reference tasks**: 18 blind
  positive controls and six positive clean scans. Clean location counts:
  000093=7, 000122=20, 000158=7, 000161=1, 000199=1, 000219=36. These are
  observed signature hits, not yet proven usable reference fixes; inspect
  `out-clean/hit_detail.txt` before asserting an exploit.
- Patternless references 001955 and 002378 cannot take the current known-
  patch SDK path. The adapter instead reports unavailable remote archaeology;
  this is an unresolved prerequisite, never a clean zero.

The full Python controls, complete ladder population, all 256 two-phase
fix probes, non-Python sample, final aggregate results, and manifest verify
publication **are not complete in this checkpoint**. The old `results.csv`
is historical; it must not be cited as the final-v3 fleet result.

## Observed first-batch triage

- 000008: custom harness reports `FAIL: F1 missing --open help`; the strict
  automated parser records no recognized countable-case proof.
- 000051: custom Python traceback enters a named test function and reports
  a rendered shell-remediation assertion failure. This is manual evidence
  of execution; the automated custom-runner fallback stays conservative.
- 000050: the selected interpreter cannot run `pytest.__main__`; no test
  cases are evidenced.
- 000080: setup log says `setup done`, but Harbor raises
  `RewardFileNotFoundError` in the fresh verifier. The generic per-cell
  `setup-fail` bucket is a verifier/environment failure, not evidence that
  setup itself failed. No cause or repair is asserted here.

Raw evidence is preserved under
`/Users/petermakhnatch/Developer/eval-lab-results/2026-10-10/mimo-clean-census/`;
older output remains under the corresponding `2026-10-09` directory.

## Spend and attribution

`spend.jsonl` records **$0.73921118** at this checkpoint. That is NOT an
all-provider invoice total: it includes **$0.5244** inherited Daytona
rate-card reconstructions. Daytona provider actuals are unknown; reserve
**$1.25** instead. Shared `__harbor__` app attribution is also uncertain;
reserve its full **$0.04169289** upper bound. Details:
`spend-attribution.json`.

The isolated native app `mimo-clean-census` (ID
`ap-8b0CQZyeIkELvgpjYWb3rH`) was observed cumulatively billed
**$0.21363391**: AUTO parity $0.05553513, LIMIT parity $0.01158964, and
first-40 controls increment $0.14650914. Its immutable observed snapshots
are in `modal-billing-snapshots.jsonl`; current-hour data may still adjust.
These are app-cumulative provider observations, not per-task invoices.

Both temporary workers finished their in-flight batches and stopped new
launches. Native continuation provider observation: cumulative
**$0.30482376**, increment **$0.09118985**. Fix app
`ap-OoWRUfTRAJBUsBkWQ5Hbhe`: cumulative **$0.05649551** (pilot $0.00026187,
batch 1 $0.01650747, batch 2 $0.03972617). These new deltas remain in the
out-of-git finance records and are NOT yet in `spend.jsonl`.

The conservative charged baseline before continuation was $1.50650407.
Workers had $4.50 native and $1.50 SDK additional ceilings; no additional
approval was given. Native's wrap record retains a provisional $8.64
reservation for all 32 selected tasks despite its observed $0.09118985
increment; that reservation exceeds its sub-ceiling and must be reconciled
with hourly billing before further spending. SDK retains $1.4435 unsettled
reservation. Do not assume lagged usage free. Finance is preserved in
`native-controls-finance/` and `fix-content-v3/finance/` under the raw root.
Direct SDK probes are AUTO/default-resource executions, not native LIMIT.

## Code and verification evidence

PR: https://github.com/PeterMakhnatch/eval-lab/pull/826 (draft).
At head `d6cd8e442`, all 12 reporting CI checks passed, including
`quality-required` and `typecheck-required`. This does not certify the
later two-phase adapter/SDK-transport checkpoint changes.

Previously exercised locally: pinned uv 0.9.24 `make check`, focused census
and SDK unit suites, Ruff, and two regressions proving ladder generation
and exact-cell reuse without the optional Harbor SDK. The latest SDK
transport and two-phase adapter tests were updated but have NOT been run;
main owns integration checks, final receipts, protected merge, and cleanup.

The worker-preserved archive of 7,998 generated v2 lineage records is
`preserved-v2-lineage/untracked-v2-lineage.tar.gz` in the raw root, SHA256
`47b2b394e0bb39d32e0b4755be66e56935e305dd850e067e9b0a1cf67c430f19`.
Its disposition file retains per-file hashes and provenance. Originals
remain while runtime readers can be active; do not retire the worktree
until the native workers have stopped and unique ignored output has been
preserved.
