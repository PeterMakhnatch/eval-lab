# separate-verifier@2 ("patch-only verifier"): validation receipt

Date: 2026-10-09. $0: local Docker (`--network none` via the task's declared
policy), cached `xiaomimimo/mimo-v2.6-rl-oss` images, no model calls.
Transform: `separate-verifier@2` (`src/evallab/separate_verifier.py`;
tests `tests/test_separate_verifier.py`).

## What @2 does

The verifier trusts nothing from the agent environment except repo file
bytes. It reruns the bundled clean setup itself (`tests/_verifier-setup/`),
computes `BASE` there, diffs the workspace snapshot with its own git under
the BASE tree's ignore rules, drops test-infra paths, gates added lines
against `TAMPER_SIGNATURES`, then applies the hidden test patch and grades
with a structured junit check (`PYTEST_ADDOPTS=--junitxml=<outside repo>`
plus `PYTHONUNBUFFERED=1`). Reward 1 iff the command exits 0 and (the
report has no failure/error/skip and contains every pytest node id named in
the command, or no report exists while pytest demonstrably did *not* run
with our addopts). When the output shows a pytest session started for a
pytest command that keeps `PYTEST_ADDOPTS` (base64-encoded command forms
decoded) — or shows nothing at all, since a real pytest run always prints
at least its header — a missing or unparsable report grades 0 (`junit
missing after pytest start`): the report was suppressed, not unconfigured.
Commands that unset `PYTEST_ADDOPTS` or never start pytest (unittest,
custom) keep the exit-code fallback.

## Matrix (jobs under `runs/sepsv2/` in the validation worktree)

`v2` = derived `@2` package graded by the new wrapper; `cur` = parent
package with the current (shared-container) grading. Attack solutions make
NO attempt to fix the bug. A1/A5/A6 are 002552-source-specific (tracked
`miio` tree); other tasks mark them n/a with reasons inline.

| task | oracle v2/cur | nop v2/cur | A1 v2/cur | A2 v2/cur | A3 v2/cur | A4 v2/cur | A5 v2/cur | A6 v2/cur |
|---|---|---|---|---|---|---|---|---|
| 002552 | 1 / 1 | 0 / 0 | 0 / 1 | 0 / 1 | 0 / 1 | 0 / 1 | 0 / 1 | 0 / 1 |
| 001809 | 1 / 1 | 0 / 0 | n/a (no tracked conftest in this tree; untracked-conftest class scores 0/0 per evidence A7) | 0 / 1 | 0 / 1 | 0 / 1 | n/a (002552 `miio` source path; exit-forge class covered live by A2) | n/a (002552 `miio` source path; runner-patch class covered live by A5/A6 gates on 002552) |
| 002391 | 1 / 1 | 0 / 0 | n/a (as above) | 0 / 0 (scores 0 on cur already today — matches evidence) | 0 / 1 | 0 / 1 | n/a (as above) | n/a (as above) |
| 000666 | 1 / 1 | 0 / 0 | n/a (as above) | 0 / 1 | 0 / 1 | 0 / 1 | n/a (as above) | n/a (as above) |

All rewards are `verifier/reward.txt` from live trials (46 cells, every
trial graded, no exceptions). Job names: `sepsv2-v2-<task>-<oracle|nop|aN>`
and `sepsv2-cur-<task>-<...>`.


## Commands (from the `patch-only-verifier` worktree)

```bash
# derive (throwaway records+store under /tmp; repo stays clean)
uv run python /tmp/sepsv2/derive.py
# run one cell the real Harbor way (pinned 0.24 runtime! plain `uv run`
# shells out to the global 0.21 CLI, which lacks bundled-tests support)
uv run --extra laminar evallab run --task <pkg> --agent oracle|nop \
  --name <job> --jobs-dir runs/sepsv2 --environment docker
```

## Mechanism spot-checks (from trial `verifier/` dirs)

- A1/002552: `dropped miio/tests/conftest.py (test-infra)`, kept diff
  empty, hidden tests genuinely fail → 0.
- A5/A6/002552: kept diff carries the payload, `REWARD=0 tamper signature
  in agent diff` → 0.
- junit engagement (`verifier/junit-grade.log`): 002552 oracle
  `rc=0 cases=4 bad=0 named=4 missing=[]` → 1; 002552 nop
  `rc=1 cases=4 bad=1 named=4 missing=[]` → 0; 001809 oracle
  `rc=0 cases=13 bad=0 named=0 missing=[]` → 1 (no parseable `::` ids under
  the task's `-k` filter, so the presence check is skipped); 000666 oracle
  `rc=0 no junit: exit-code grading` → 1 (its command unsets
  `PYTEST_ADDOPTS`, so the exit-code fallback applies by design).

## Follow-up: junit absence after a pytest start (jobs `runs/sepsv2-jc/`)

A7 (002552 only): import-time obfuscated exit
`getattr(__import__('o'+'s'), '_e'+'xit')(0)` appended to the tracked
package `__init__.py`. No tamper signature survives the string-splitting,
so the gate passes; the kill lands during collection import, before pytest
flushes anything (its capture layer holds all output when `os._exit` skips
teardown), so the log is empty and no junit report is written — yet rc is 0.
The old exit-code fallback graded this 1; the new rule grades 0.

| cell | reward | `junit-grade.log` |
|---|---|---|
| oracle 002552/001809/002391/000666 (v2) | 1 / 1 / 1 / 1 | `rc=0 cases=4 bad=0 named=4 missing=[]` / `rc=0 cases=13 bad=0 named=0 missing=[]` / `rc=0 cases=8 bad=0 named=0 missing=[]` / `rc=0 no junit: exit-code grading` (000666 unsets `PYTEST_ADDOPTS`: fallback by design) |
| nop 002552/001809/002391/000666 (v2) | 0 / 0 / 0 / 0 | `rc=1 cases=4 bad=1 ...` / `rc=1 cases=13 bad=11 ...` / `rc=1 cases=8 bad=6 ...` / `rc=1 no junit: exit-code grading` |
| A7 002552 v2 / cur | 0 / 1 | v2: `rc=0 junit missing after pytest start` (test log empty apart from the reward echo; the gate correctly passes — no literal signature) |
| A1/A5/A6 002552 v2 regression | 0 / 0 / 0 | A1 `rc=1 cases=4 bad=1 ...` (genuine failure); A5/A6 stop at the signature gate (no junit step) |

Residual: an import-time kill that first prints non-marker junk (non-blank,
markerless output, rc 0, no junit) still takes the fallback. Closing it
needs the pure command-based rule (pytest-cmd + intact addopts + missing
junit ⇒ 0), which would trade away the custom-echo edge currently kept on
fallback.

## separate-verifier@3 port validation (2026-10-09, $0 local Docker)

Transform under test: `separate-verifier@3`
(`src/evallab/separate_verifier.py`; tests
`tests/test_separate_verifier.py`). 86 cells across 22 tasks: per task,
oracle (reference fix) and nop under @3, plus a language-appropriate tamper
graded both by the task's own current grading (`cur`, shared container,
exit code) and under @3. Every reward is trial `verifier/reward.txt`.
Tamper solutions make NO attempt to fix the bug. Full table, anomaly
resolutions, and residuals: `docs/mimo/separate-verifier.md`
(§"Port validation").

| family | tasks | oracle @3 | nop @3 | tamper @3 | tamper cur |
|---|---|---|---|---|---|
| go-test | 000001, 000553 | 0 (env) + 1 | 0, 0 | 0, 0 | 1, 1 |
| jest | 000047, 000236 | 1, 1 | 0, 0 | 0, 0 | 1, 1 |
| mocha | 000045, 002928 | 1, 1 | 0, 0 | 0, 0 | 1, 1 |
| vitest | 000128, 000198 | 1, 1 | 0, 0 | 0, 0 | 1, 1 |
| usecase-sh | 000008, 000291 | 1, 1 | 0, 0 | 0, 0 | 1, 1 |
| unittest | 000803, 000838 | 1, 1 | 0, 0 | 0, 0 | 1, 1 |
| pytest (ADDOPTS-cleared) | 000666, 000523 | 1, 1 | 0, 0 | 0, 0 | 1, 1 |
| phpunit | 000214 (+000137 tamper-only) | 1 | 0, 0 | 0, 0 | 1, 1 |
| rspec | 000947 (+001227 env-broken) | n/a | 0 (+0 env) | 0 (+0 env) | 1 (+0 env) |
| mvn | 000965, 001172 | 0, 0 (env) | 0, 0 (env) | 0, 0 (env) | 0, 0 (env) |
| cargo | 001083 | 0 (env) | 0 (env) | 0 (env) | 0 (env) |
| make | 000898 | 1 | 0 | 1 (residual R1, confirmed) | 1 |
| anomaly control | 000001 cur-oracle | 0 (matches @3) | — | — | — |

79/86 cells at expectation. The 7 deviations are environment breakage
(E5/E6), identical on both gradings, each with a quoted failure signature
in the table above: 000001 full-`rc==0` (BN256 stdlib drift + a
network-dependent libp2p broadcast test; the corrected oracle builds and
the `state` suite passes on both), 000965/001172 (offline Maven cannot
resolve parent/junit-bom POMs), 001083 (`cargo` absent), 001227 (offline
Ruby gems missing). Two preliminary anomalies were root-caused and
re-proven: the batch-1 000001 oracle failed to build (wrong-era files),
and the batch-1 000001 PATH shadow never triggered (wrong absolute path);
both replacements discriminate correctly now.

Mechanism spot-checks (from trial `verifier/` dirs): `os.Exit` gate
(000553), `process.exit` gate (000045/002928), scoped `exit(0)` gate
(PHP/Ruby), empty-patch isolation (go-binary overwrite, fakes, all with
pristine failures), conftest-hook suspicion (ADDOPTS-cleared obfuscated
exits), blank-output structured rules (unittest/vitest/jest/mocha
zero-test configs — including a kept tracked `vitest.config.ts`, graded 0
despite rc 0), and the `*_test.go` / TestMain drops.
