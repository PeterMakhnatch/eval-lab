# Code-harden-night receipts

Overnight slice: harden the ~1,518 non-Python code tasks. Evidence tiers:
MEASURED (ran it), SOURCE-QUOTED (file:line), [INFERENCE]. Small samples are
samples: n stated everywhere.

## 1. Fleet derivation (MEASURED, n=1,519)

`derive_fleet.py` derives, per non-Python task, `strip-future-history@1` then
`mtime-normalize@1` chained strip → mtime (the mtime parent is the strip
variant package; parent linkage `kind: variant`, verified by
`task_variants.verify` with zero failures on the sample). Transforms
unchanged; records `candidate`, `created_by: code-harden-night`, rationale in
each record. Full report: `derive-report.json` (failures: none).

| Transform | Derived | Already existed | Failed |
|---|---|---|---|
| strip-future-history@1 | 1,516 | 3 (2 smoke-test + 002361 pre-existing `har177-default-strip` on main, untouched) | 0 |
| mtime-normalize@1 | 1,516 | 2 (smoke-test) | 0 (+1 relinquished: 002361 belongs to the Python ledger — ValsClosure's fleet owns its mtime; record dropped, pre-existing main strip untouched) |

Census recount says the fleet is **1,519**, not 1,518 (§3). Records: 3,032 new
lineage JSON files across 1,518 task dirs under `library/task-variants/`
(002361 carries only main-tracked records now); packages in the shared
variants store.

## 2. Sample validation (MEASURED, local Docker, egress locked)

`validate_sample.py` stages chain/strip packages + oracle `solve.sh`
(post-setup mtime listing + reference fix from the image's own git history);
`validation-manifest.json` pins variant digests + solve.sh sha12. Harbor
shim: `.local/bin` `harbor` is 0.21.0 (no `user_agent_dir`);
`/private/tmp/mimo-night/code-harden/bin/harbor` routes to the pinned 0.24.0
wheel (local-only, shared tool untouched).
| Trial | Agent | Reward | Verifier signal |
|---|---|---|---|
| 000045 chain (JS mocha) | nop | 0 | 9 passing / 5 failing (report: 5 hidden quick-reply) |
| 000045 strip | nop | 0 | same 9/5 |
| 000047 chain (TS jest) | nop | 0 | 7 passing / 4 failing (report: 4 hidden installDocker) |
| 000236 chain (TS jest) | nop | 0 | 5 passing / 4 failing (report: 4 hidden snapshots) |
| 000553 chain (Go usercase) | nop | 0 | `no non-test Go files`, build failed (report: correct nop signal) |
| 000553 chain | oracle (upstream `main.go`) | 1 (423.8 s) | post-setup worktree: `mtime_distinct 1`, `mtime_newer_than_fixed_count 0` (agent log) — the mtime gate holds and the task still grades |
| 000007 chain (Solidity forge) | nop | 0 (60.2 s) | 94 passing / 2 failing (report: 2 hidden revert tests) |
| 000007 chain | oracle (fixed `OrderMixin.sol` file) | 1 (90.5 s) | raw commit diff did not apply (base drifted from fix parent); file overwrite works |

Rest of the fleet stays `candidate`: derivation proves the transforms apply
cleanly, not that each task grades correctly (per-task proof needs the locked
nop census, a morning job).

## 3. Static census (MEASURED over the pinned snapshot, no Docker)

`census.py` + `census_lib.py` (pure, unit-tested: `tests/test_code_harden_night.py`,
11 tests). Full tables: `census-tables.md`; rows: `census.json`.

- Language recount: fleet is **1,519** (Go 722, JS 388, TS 166, Unknown 130,
  Ruby 25, PHP 23, Java 22, C++ 13, C 7, Rust 7, Scala 6, Kotlin 3, Lua 3,
  Elixir 1, Swift 1, Svelte 1; Python 1,179 out of scope).
- Grading-time downloads (§2 table): unconditional fetchers break under the
  egress lock; conditional (cache-guarded) and offline-flag (`GOPROXY=off`,
  `mvn -o`, `cargo --offline`, `-mod=vendor`) invocations are hermetic.
  N tasks with ≥1 unconditional grading fetch: see table (prefetch-port
  candidates; dynamic truth needs the locked nop census).
- Rust PATH screen (§3): `path-missing-candidate` tasks listed (static
  candidates only — 000681-style confirmation needs a live probe).
- Unknown-130 triaged by grading runner (§4, per-runner buckets + task ids).

Limits: static only — a `go.mod` unconditional download may still pass if the
module cache is baked (hence "candidates"); unknown buckets are regex-based
(first match wins); `custom` bucket = unrecognized shell-compare harnesses.

## 4. Staged purge-port spec (no live probes; research-only)

`purge-port-spec.md`: per-runner forge payloads (jest/vitest/mocha/cargo/
phpunit/rspec/make/usercase/lint), the exact `evallab hack run --execute`
solution-override probe vehicle (matrix trials are unlocked — no egress field
on `ExperimentMatrix`; guard proof stays on locked `evallab run`), guard
design (`purge-planted-test-infra@2` untracked-deletes), and refusal cases
(tracked edits → `separate-verifier@1`, never purge). AUTOLOAD-class claims
are marked [INFERENCE] until probed.

## Environment notes for the parent

- `evallab run` uses PATH `harbor` (0.21.0 uv tool) → every MiMo local run
  dies in `_allowed_mounts` (`user_agent_dir`). Workaround above; shared fix
  belongs to whoever owns the tool pin.
- ValsClosure (purge-build-caches@1) and JudgeVariants (general/webdev
  transforms) own their `hardening.py` edits; this slice touches no source
  modules — no conflicts expected.
