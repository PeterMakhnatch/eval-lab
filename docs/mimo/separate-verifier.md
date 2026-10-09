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
(§"separate-verifier@3 port validation"). Validation is in progress
(2026-10-09): implementation, unit tests, census, and FP measurement are
done; the local-Docker matrix is running (preliminary cells below).

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

## Residuals (known, accepted)

- Tracked `Makefile`/CMake/target definitions: never touched (honest
  fixes live there by construction) — custom-compile tasks keep the
  exit-code fallback; a C++ exit-header tamper cell is queued to confirm
  the expected residual 1/1.
- `make`/forge/busted/julia/sbt/custom families: exit-code fallback
  (no cheap markers); isolation (pristine checkout, hidden-test reset)
  still applies.
- Reference fixes for non-Python tasks: none shipped with the dataset;
  validation oracles are history-extracted (000553 upstream file,
  000666/000803 ledger patches, 000001 targeted tests proven in-container)
  or hand-written from hidden tests; 000001's full `rc==0` is additionally
  blocked by pre-existing precompiled BN256 failures (broken env, E5).
