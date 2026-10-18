# mimo-v6-full-fleet

Canonical source/package release for the **567 reference-bearing MiMo-v2.6-RL
code tasks**, using the `mimo-clean-v6` transform chain. Membership is exactly
`label == oracle:pass+nop:fail` in `../mimo-reference-fixes/index.csv`, not the
2,698-task source inventory. That historical label defines membership; it is
**not** current-package verification.

## Measured build, not fleet certification

The reviewed-helper consumer build produced the final manifest SHA256,
`f42bd0c91bd7e5b79c37e469575d86d2927143dafe5be19898f5ac3107df6bdb`.
The first two runs below preceded those edge fixes and shared the earlier
`e204b8bf…` manifest; their native observations do not transfer to the final bytes.

| Actual run | Built tasks | Wall seconds |
|---|---:|---:|
| Initial full build | 567 | 340.53 |
| Cached full rebuild | 567 | 305.29 |
| Reviewed-helper full build | 567 | 528.72 |

The emitted sidecar binds **3,263 canonical edges** and **3,438 distinct actual
records**, including 175 retained repair ancestors. Historical validation states
are preserved, including one rejected `env-keep-build-outputs@1` ancestor on
`002696`; this is not a rejected canonical edge or permission to call the new
package verified. New records remain candidates until separately qualified.

## Reproduce the packages and register their real lineage

The pinned source snapshot and the Python ledger's existing run packages must
already be in the shared task store. From the repository root:

```bash
uv run python research/experiments/mimo-v6-full-fleet/build_fleet.py --workers 8
```

This command vendors and hash-checks all 567 indexed references, applies the two
source recoveries below, derives the real ntfy environment repair, writes
`release-ledger.csv`, and invokes the authoritative `evallab mimo-clean build`
consumer. It refuses incomplete membership or skipped packages. It performs no
container execution, paid allocation, model call, or native qualification.

Outputs:

- `references.csv`, `references/*.patch`, `references.custody.json`: portable
  reference bytes, original-source SHA256s, original fix commits and the exact
  recovery applied. The historical index and frozen experiment inputs remain
  unchanged.
- `release-ledger.csv`: the fleet's Python roots; the ntfy root points to a real
  `mimo-v6-environment-repair@1` record rather than pretending an environment
  repair is part of the upstream reference patch.
- `manifest.csv`: all 567 final content-addressed packages.
- `manifest.lineage.json`: automatically emitted by the v6 builder, bound to the
  manifest SHA256. Every task includes full root/package/Harbor bindings and
  ordered, SHA256-bound paths to its actual `library/task-variants` records.

Python has six canonical edges:
`strip-future-history@1 > purge-installed-copies@2 > purge-build-caches@4 > mtime-normalize@2 > separate-verifier@10 > agent-network-none@1`.
Non-Python has five: the Python installed-copy purge is not applicable, and the
existing Node-fold tasks retain their declared `purge-build-caches@5` slot.
Pre-existing repair ancestry is retained in addition to the canonical edges.
A carried setup marker without a real recoverable edge is an explicit failure,
not permission to fabricate a shorter successful chain.

New records are candidates. Existing records/packages are immutable. Legacy
non-Python v10 records missing only the new, non-operative setup diagnostics can
be reused only after isolated real rederivation proves identical transform,
provenance, file changes and both endpoint digests; its hashed proof is retained
in the lineage sidecar. Wrong diagnostic values are not metadata-only
equivalence.

## Systemic repairs

- Setup helpers use `/usr/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/bin`,
  rather than accidentally selecting an image's Python 3.6. The v10 verifier
  prelude applies this only to the setup child when its bundle actually carries
  `purge-installed-copies@2`; the project test command is not rewritten.
  Cache selection binds the purge edge to its actual rendered setup SHA256,
  including the embedded helper implementation.
- Installed-copy ownership follows tracked package trees and explicit shared
  namespace boundaries, not the first import component. For example, repairing
  `google.cloud.logging_v2` must not delete the sibling `google.api_core` source
  or bytecode. Exact BASE files restore; fix-added files inside proven owned
  packages and adjacent bytecode are removed. Third-party namespace siblings,
  installation metadata and vendored nested copies are not purge targets.
  Source-less legacy and tagged/optimized bytecode is removed only through
  exact owned BASE source counterparts; verification rejects reintroduced
  bytecode. Unresolved namespace metadata refuses before mutation rather than
  broadening ownership. Ambiguous dotted-stem dependency caches are preserved.
- The standalone namespace smoke exercised the retained base helper and repaired
  helper against real isolated filesystem/import processes: both restored the
  owned project and removed its fix-added source/bytecode; only the repaired
  helper preserved the dependency's exact source/bytecode and working import.
  This is helper runtime proof, not an exact-task native control.

## Reference and environment recovery

| Task | Accepted failure / repair | Qualification boundary |
|---|---|---|
| `000081` (`xdoctest`) | Cleanup removes `.coveragerc`, causing the old atomic reference patch to reject before applying source. Retain every `xdoctest/runner.py` diff block and omit non-implementation files. | Recovered patch SHA256 `1007e5113f13068aeebc2ee1ce4416618ba98e678574319ad95b5ff595b7dd40`; applied successfully to the retained real baseline. Native controls are a separate requirement. |
| `000838` (`ntfy`) | Fix source typo `hide_commnad` to `hide_command`. Independently restore the missing YAML dependency through startup setup, which is bundled/replayed by the separate verifier. | Pin the real pure-Python `ruamel.yaml==0.16.13` wheel by URL/SHA256; use its legacy `yaml.safe_load` API without optional clib. Probe an absolute candidate for the observed Python 3.8 stdlib/unittest paths, then install through its resolved `sys.executable`. No claim that the candidate alias was observed historically. |
| `001373` (`python-logging`) | Keep the indexed reference unchanged. Older native logs expose over-broad shared-namespace purging; the ownership repair addresses that demonstrated failure mechanism. | Older-digest deletion evidence is not proof of the exact-v6 failure cause or proof of a new passing control. |

Recovery used actual read-only `codex exec`, retained passing rollouts, indexed
patches, local baseline sources and Git custody. Passing rollouts containing
test edits were not replayed wholesale. No task tests, grading or project test
commands are weakened. The ntfy setup uses the package's existing public
startup/verifier network policy and fails visibly if the pinned wheel or runner
probe is unavailable; it does not relax a lock or provide a fallback shim.

## Deterministic qualification census

`mimo-v6-intake-census.csv` freezes the old-package intake.
`mimo-v6-fleet-census.csv` describes the release's exact current packages.
Changed package bytes invalidate old controls; the final census adds only
new executed, exact-input pairs:

| Package census | `v6_verified` | `v6_unverified` | `broken_ref_or_env` | `missing_ref` |
|---|---:|---:|---:|---:|
| Retained intake | 34 | 530 | 3 | 0 |
| Rebuilt release, before new controls | 2 | 564 | 1 | 0 |
| Final reviewed release, after exact-source controls | 5 | 562 | 0 | 0 |

The final inventory deduplicates 397 native execution UUIDs across retained
generations; only full-package and Harbor-digest-matching evidence qualifies.

Generate the latter with explicitly retained evidence roots, for example:

```bash
uv run python research/experiments/mimo-v6-full-fleet/census.py \
  --repo-root . --package-root /path/to/shared/eval-lab \
  --reference-index research/experiments/mimo-v6-full-fleet/references.csv \
  --manifest research/experiments/mimo-v6-full-fleet/manifest.csv \
  --native-controls research/experiments/mimo-training-20261016/native-controls.json \
  --jobs-root /path/to/retained/native/jobs \
  --jobs-root /path/to/native/jobs \
  --output research/experiments/mimo-v6-full-fleet/mimo-v6-fleet-census.csv
```

Each `--jobs-root` is an explicit job-parent directory, not a recursive scan.
Include the current native `--jobs-dir` as well as retained evidence roots.

Qualification is fail-closed and scoped to reference/nop controls:

- `v6_verified`: actual indexed patch and embedded solution match; package bytes
  match the manifest; native oracle **1.0** and nop **0.0** both bind the same
  full package and Harbor digests, with executed native lock/config/staging
  agreement, positive test-case evidence, successful exits and no repeat exceptions.
- `v6_unverified`: reference/package exists but that exact-input pair is not
  proved. Reward-only custom-runner observations with unrecognized case
  execution remain unverified, not automatically broken.
- `broken_ref_or_env`: missing/mismatched package or embedded reference,
  accepted unresolved same-reference failure, or an exact-input failed control.
- `missing_ref`: indexed patch bytes are absent or fail their SHA256 binding.

Frozen wrappers marked `RUNNER=custom` can supply Python case evidence when the
resolved command directly invokes `python -m nose` or `python -m unittest`.
Admission also requires a completed, positive `Ran ... tests` summary and a named
non-loader case outcome or failure diagnostic. Interleaved application logs do
not erase a named `FAIL`/`ERROR` report.

The census also recognizes explicit Django `python manage.py test` invocations
(including `pipenv run python manage.py test`) and Twisted `trial` /
`python -m twisted.trial` invocations. Each output must have its own adjacent
resolved-command record; commands in another repeat do not establish provenance.
Django requires a named non-loader test and its `ok`/`FAIL`/`ERROR` outcome, even
when application logging separates them, followed by a positive counted
`Ran ... tests` and completed `OK`/`FAILED` suite. Twisted requires a named test
method under a non-loader class in its tree report, an `[OK]`/`[FAIL]`/`[ERROR]`
outcome, and a positive counted `Ran ... tests` with a counted `PASSED`/`FAILED`
completion. A nonfatal Python site startup warning does not erase those completed
cases.

Generic custom commands, bare exit codes or markers, arbitrary status words or
file counts, zero-case or all-skipped runs, truncated or merely started suites,
and loader-only import failures remain unmeasured. Pytest's zero-case collection
errors remain excluded. This recognizes execution; native identity, complete-pair,
reward, stability and exception gates still apply, and semantic correctness is
not inferred.

Native execution UUIDs deduplicate copied evidence only when every normalized
qualification fact agrees; conflicting copies refuse.
Native task checksums are retained diagnostically, not confused with the full
source-package digest. Old controls do not transfer across changed package
bytes, and repackaging does not erase an accepted same-reference failure. A
later exact-input passing pair can supersede an earlier unscored setup attempt.
An actually executed, error-free wrong repeat remains a failure even when another
repeat or the enclosing attempt failed. Historical issue reports
remain in each row even when a recovered source patch has a different digest.

Neither this census nor an oracle/nop pair is semantic-validity adjudication or
full cheat-ladder acceptance. `manifest.csv`'s full clean-set `verify` field is
not promoted merely because a reference/nop pair passes. Modal execution stays
inside this mission's separate **$4 total** cap; cost projections/reservations
are not provider billing receipts.

## Capped native controls

`run_controls.py` uses the first-party `Executor.execute_direct` / `RunRequest`
oracle and nop route, native Harbor 0.24.0 and Modal. The final `f42bd0c9…`
manifest's recovered/repaired targets completed six actual native calls:

| Task | Reference reward | NOP reward |
|---|---:|---:|
| `000081` | 1.0 | 0.0 |
| `000838` | 1.0 | 0.0 |
| `001373` | 1.0 | 0.0 |

All six have positive recognized case execution, no native exception and
verified executed-lock/config/staging identity bound to the current full package
and Harbor digests. The earlier six calls on `e204b8bf…` remain separate evidence
and are not transferred. These pairs qualify reference/nop controls only.

The default command is a provider-free plan, including for the full fleet.
For the three recovered reference/environment targets:

```bash
uv run --extra laminar python research/experiments/mimo-v6-full-fleet/run_controls.py \
  --package-root /path/to/shared/eval-lab \
  --jobs-dir /path/to/native/jobs \
  --task-id format-code-task-000081 \
  --task-id format-code-task-000838 \
  --task-id format-code-task-001373
```

Real execution additionally requires `--execute` and a recorded current
`--approve` authorization. Every request is preflight-validated before charging
the first-party `CloudSpendGate`; the cumulative `native-admissions.json`
reservation is atomically persisted before provider execution. Paid failures
are not refunded or silently retried. The mission ceiling is fixed at **$4**.
Proven preflight refusals with `provider_started=false` keep all historical
reservation units but have no provider exposure. Legacy bookkeeping migrates
explicitly; all paid and uncertain admissions retain their full chargeable units.
The lane uses the existing census billing namespace and `limit` resource policy,
does not exceed the tasks' declared 2-CPU/8-GiB caps, rejects GPUs, and bounds
native sandbox lifetimes at 900 seconds.

The final journal retains 12 executed native calls across the two manifests and
six proven zero-provider refusals: **$3.60 chargeable reservations / $5.40 total
historical reservations**, with the explicit v1-to-v2 accounting migration.
An actual provider-free plan for one further unverified pair proposes **$4.20**
and is refused by the unchanged $4 cap. Actual provider billing is unmeasured.

The measured fresh-ledger full-fleet plan requested **1,134 native executions,
6,804 allocation/preparation reservation units and $340.20**, so the default
cap refuses that admission. This deliberately conservative upper-bound plan is
neither a minimum certification cost nor a Modal bill. In particular, actual
zero-provider preflight refusals and native execution outcomes must not be
reported as measured spend or passing controls.

### October 17 reservation calibration

Fresh planning scopes use the shared Modal **allocation/preparation-unit**
estimate, `$0.0025`; three units per environment still reserve preparation and
both possible native creation attempts. The
[reference-subset calibration](../../../docs/task-quality.md) states the native
sample, metered-usage cross-check and mean-based safety margin. This is not a
900-second worst-case reservation: slow tasks, cold builds and larger resources
need an explicitly higher `--usd-per-unit`. Plans preserve unrounded exposure
and report the selected rate; sub-cent rounding never determines cap fit.

The historical `native-admissions.json` remains bound to `$0.05` and its original
`$4` cap. Calibration **does not reprice, refund, clear or migrate** that ledger
to the lower rate. Resuming its provider-free plan requires
`--usd-per-unit 0.05`; using the new default with it refuses the rate mismatch.
A fresh-scope preview can use an empty `--repo-root` with an explicit frozen
`--manifest` and `--package-root`, **without `--execute`**. That preview is not
authorization for a new ledger or fleet run: the unchanged `$4` mission ceiling
and a current approval still gate real execution.
