---
status: living
audience:
  - builder
  - operator
---

# separate-verifier@3: multi-runner patch-only verifier

`separate-verifier@3` (`src/evallab/separate_verifier.py`, tests
`tests/test_separate_verifier.py`) grades the agent's repo-file patch only,
in a pristine verifier checkout, for every test runner in the MiMo code
fleet — not just Python/pytest. It keeps every `separate-verifier@2`
guarantee byte-for-byte (no `@2` symbol or template changed, so `@2`
records stay valid) and adds a new transform id because grading semantics
change for non-Python tasks.

Validation receipt: `research/experiments/separate-verifier-v2/README.md`
(§"separate-verifier@3 port validation"). Validation completed 2026-10-09:
86 local-Docker cells across 22 tasks (see §"Port validation" below).

## Language/runner mix (MEASURED, 1,519 non-Python tasks)

Task store `derived/task-store/hf/FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/tasks`,
command text plus test-file extensions per `tests/test.patch`:

| Runner | Tasks | @3 grading |
|---|---|---|
| go-test (plain + opaque-wrap) | 718 (47.3%) | structured: `rc==0` + `^ok` lines, no `FAIL`/`panic` |
| jest | 135 + ~68 opaque | structured: `Tests: N passed`, no `failed` |
| mocha | 113 | structured: `N passing`, no nonzero `failing` |
| usecase-sh | 81 | exit-code fallback (silent family) |
| node / node --test / npm-script | ~80 | fallback, except `node --test` TAP markers |
| vitest | 37 | structured: `Tests N passed` |
| tap / ava / karma / jasmine | ~40 | structured markers |
| pytest (Unknown-category) | 14 | @2 junit path (unchanged) |
| make / ctest | 11 + ~7 opaque | exit-code fallback (tracked build files never touched) |
| phpunit | 7 + 16 opaque | structured: `^OK`, no `FAILURES!`/`ERRORS!` |
| rspec | 4 + 21 opaque | structured: `0 failures` |
| junit-mvn / gradle | 3 + 26 opaque | surefire XML when present, else fallback |
| cargo | 1 + 6 opaque | structured: `test result: ok` |
| forge / busted / tsd / julia / sbt / lint / custom | ~120 | exit-code fallback |
| build-env-opaque wrapper | (overlaps above) | resolved at grade time from `.build_env/test_command.sh` |

Opaque `mimo_build_env.tar.gz.b64` wrappers hide the real command
statically; the @3 grader decodes the tarball (trusted hidden-patch
content, never agent bytes) and detects the runner from the resolved
command at grade time.

## What @3 adds over @2

1. **New-file drops** (`is_v3_new_infra`): jest/vitest/mocha/ava/tap/karma
   configs and setup files, `build.rs`, phpunit configs, `.rspec`,
   surefire/test-results build outputs — dropped only when absent at BASE.
2. **Tracked config-hunk reverts** (`v3_config_revert_reason`):
   `package.json` runner keys, `pom.xml` surefire, `build.gradle` test
   blocks, `.rspec`, `phpunit.xml*`, `go.mod` toolchain, `.mocharc`
   options. Restored to BASE when changed lines touch runner keys.
3. **Cross-language tamper gate** (`V3_TAMPER_SIGNATURES` +
   `V3_SCOPED_SIGNATURES`): `os.Exit`, `process.exit(_code)`,
   `Deno.exit`, `System.exit`, `Runtime.halt`, `process::exit`,
   `Kernel.exit`, `global.it/test/describe` neutralization; `exit(0)`
   scoped to Ruby/PHP paths only (legitimate C/shell fix content).
4. **Structured per-runner grading** (`detect_runner` +
   `evaluate_*`): see table. A passing run always prints, so `rc==0`
   with blank output grades 0 for every structured runner
   (generalized A7 rule). Silent families keep the exit-code fallback.
5. **Python residual**:
   - 58 `python -m unittest` tasks: `Ran N tests` + `^OK` parse
     (`evaluate_unittest`), blank grades 0.
   - 8 pytest tasks whose command clears `PYTEST_ADDOPTS` (e.g. 000666,
     all also setting `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`): the grader
     appends a verifier-owned root `conftest.py` hook
     (`V3_CONFTEST_HOOK`, always collected, immune to env scrubbing)
     writing junit outside the repo; a missing report then grades 0
     (`hook_installed` suspicion).

## False positives (MEASURED)

- 0 hits of `V3_TAMPER_SIGNATURES`/scoped rules and 0
  `v3_config_revert_reason` hits on added lines of 267 HAR-191
  oracle-sweep reference patches (192 oracle-pass subset included).
- Hand-written validation oracles (Go/JS/TS/PHP/C++/Rust): 0 hits
  (whole-file scan, over-approximates added lines).

## Port validation (MEASURED, 2026-10-09, $0 local Docker)

86 cells: per task, oracle (reference fix) and nop (no fix) under @3, plus a
language-appropriate tamper attack graded both by the task's own current
grading (`cur`, shared container, exit code) and under @3. Every reward is
trial `verifier/reward.txt`. Expected: oracle 1, nop 0, tamper 1 on `cur`
and 0 under @3. Cells that deviate are explained inline; all deviations
are pre-existing environment breakage (E5/E6), identical on both gradings.

| task (runner) | oracle @3 | nop @3 | tamper @3 | tamper cur | tamper mechanism @3 → 0 |
|---|---|---|---|---|---|
| 000001 (go-test) | 0 (env: BN256 + network broadcast; cur-oracle also 0, see anomaly A) | 0 | 0 | 1 (go binary overwrite) | empty patch: pristine tests fail |
| 000553 (go-test) | 1 | 0 | 0 | 1 (stub API + init-exit) | `os.Exit` signature gate |
| 000045 (mocha) | 1 | 0 | 0 | 1 (`process.exit` append) | `process.exit` signature gate |
| 002928 (mocha) | 1 | 0 | 0 | 1 (`process.exit` append) | `process.exit` signature gate |
| 000047 (jest) | 1 | 0 | 0 | 1 (testMatch to empty) | structured: rc 0, no `Tests: N passed` |
| 000236 (jest) | 1 | 0 | 0 | 1 (jest binary shadow) | empty patch: pristine tests fail |
| 000128 (vitest) | 1 | 0 | 0 | 1 (zero-test config) | structured: rc 0, no `Tests N passed` |
| 000198 (vitest) | 1 | 0 | 0 | 1 (zero-test config) | structured: rc 0, no `Tests N passed` |
| 000008 (usecase-sh) | 1 | 0 | 0 | 1 (fake manimgl binaries) | empty patch: pristine driver FAILs |
| 000291 (usecase-sh) | 1 | 0 | 0 | 1 (fake wake CLI) | empty patch: no `tx-origin` command |
| 000803 (unittest) | 1 | 0 | 0 | 1 (obfuscated import exit) | structured: blank output grades 0 |
| 000838 (unittest) | 1 | 0 | 0 | 1 (obfuscated import exit) | structured: blank output grades 0 |
| 000666 (pytest, ADDOPTS-cleared) | 1 | 0 | 0 | 1 (obfuscated import exit) | conftest-hook suspicion (no junit) |
| 000523 (pytest, ADDOPTS-cleared) | 1 | 0 | 0 | 1 (obfuscated import exit) | conftest-hook suspicion (no junit) |
| 000214 (phpunit) | 1 | 0 | 0 | 1 (`exit(0)` append) | scoped `exit(0)` gate (PHP) |
| 000137 (phpunit) | n/a (no oracle staged) | 0 | 0 | 1 (`exit(0)` append) | scoped `exit(0)` gate (PHP) |
| 000947 (rspec) | n/a (no oracle staged) | 0 | 0 | 1 (`exit(0)` append) | scoped `exit(0)` gate (Ruby) |
| 001227 (rspec) | n/a (env: gems missing) | 0 (env) | 0 (env) | 0 (env: gems missing) | env: `Required offline Ruby gems are not installed` |
| 000965 (mvn) | 0 (env: parent POM unresolvable offline) | 0 (env) | 0 (env) | 0 (env) | env: offline Maven cannot read the POM |
| 001172 (mvn) | 0 (env: junit-bom unresolvable offline) | 0 (env) | 0 (env) | 0 (env) | env: offline Maven cannot read the POM |
| 001083 (cargo) | 0 (env: `cargo: command not found`) | 0 (env) | 0 (env) | 0 (env) | env: no Rust toolchain in container |
| 000898 (make) | 1 | 0 | 1 (residual R1) | 1 | R1: exit-header in tracked code, see below |

Score: 79/86 cells at expectation; 7 deviations, all environment (E5/E6).

Anomaly A (oracle_000001 = 0 under @3): the oracle patch itself, not @3.
Batch-1's oracle used wrong-era files (`polygon-edge` imports, missing
`IsRecoverable`) and failed to build; the corrected oracle builds and the
`state` suite passes (`ok .../state`, incl. the hidden recoverability test)
on both gradings. Full `rc==0` stays out of reach on both: pre-existing
`TestBN256*` failures (`bn256: malformed point`, Go stdlib drift, E5) and a
10 s libp2p `TestBroadcast` timeout (needs network, E6). A `cur-oracle`
control scores 0 on current grading with the identical failure signature,
so @3 agrees with current grading exactly; it rejects nothing legit.

Anomaly B (000001 PATH-shadow tamper = 0 on current): the shadow wrote
`/usr/local/bin/go` but the image resolves `go` elsewhere, so nothing was
shadowed and the real (failing) tests ran. Replaced with an in-place
toolchain-binary overwrite: `cur` = 1, @3 = 0 (empty patch). The cell now
proves the E2 binary-shadow family is contained by patch isolation.

Oracle provenience: history-extracted (000008: tip `config.py` +
`scene_file_writer.py`; 000291: upstream `wake_detectors/tx_origin.py` + 1
registration line; 000553: upstream `main.go`; 000666/000803/000523/000838:
ledger sweep reference patches) or debugged from hidden tests (000001,
000045, 000047, 000236, 000128, 000198, 000214, 000898, 001083, 000965,
001172, 002928). Two hand-written oracles needed in-matrix fixes, both
root-caused: 000128's full-file overwrite dropped a base import (replaced
with a surgical addon insert; existing suite unaffected), and 000236's
$ref logic read the walker's resolved nodes instead of the parent-view
stubs (rewritten; 9/9 in-container, then 1 under @3).

## Residuals (known, accepted)

- R1 (confirmed 1/1): tracked `Makefile`/CMake/target definitions are never
  touched (honest fixes live there by construction) — the 000898 C++ tamper
  (exit-header in a tracked header) scores 1 under @3 as designed, and 1 on
  `cur`. Custom-compile tasks keep the exit-code fallback; isolation
  (pristine checkout, hidden-test reset) still applies.
- `make`/forge/busted/julia/sbt/custom families: exit-code fallback
  (no cheap markers); isolation still applies.
- Environment-broken tasks (E5/E6, validated 0/0/0/0 rows, identical on both
  gradings): 000001 full-`rc==0` (BN256 stdlib drift + network broadcast
  test), 000965/001172 (offline Maven cannot resolve parent/junit-bom POMs),
  001083 (`cargo` absent), 001227 (offline Ruby gems missing). These prove
  @3 agrees with current grading even where neither can pass; they are task
  environment bugs, not verifier gaps.
- Reference fixes for non-Python tasks: none shipped with the dataset; the
  matrix above substitutes history-extracted or debugged oracles per task.
