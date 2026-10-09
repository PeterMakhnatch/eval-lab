# Route 4 receipt: mtime-normalize@1 fleet (MEASURED sample, candidate rest)

Date: 2026-10-09 UTC. $0 (derivation is file-local; validation in local
Docker, `--network none`).

## Fleet

`derive_mtime_fleet.py` (this directory) derived `mtime-normalize@1` for
every Python-ledger `usable` task (1,146) off its strip variant package
(snapshot fallback, never used — all 1,146 had strip packages):
**1,137 derived + 8 skipped (pre-existing mtime record) + 002361 resolved**
(see below) = full coverage. Parents are strip variants, so the runtime
order is strip → normalize; the block anchors on the ready sentinel, after
purge/cache blocks wherever those apply. All fleet records are `candidate`.

002361 (in the Python ledger; CodeHarden also derived it): their package
bytes proved byte-identical to the deterministic derive (same digest12
`303306960c8a`); their package was relocated to scratch, my record derived
off strip `479e731f2693`, and CodeHarden deleted their record on PR #785.
No store divergence.

## 10-task validation sample (music-PR pattern: 10 validated / rest candidate)

Full chains (strip → cache → mtime), validating composition:

| Task | mtime variant | mtime_distinct | Caches | Nop | Oracle |
|---|---|---|---|---|---|
| 002552 | c9cc3f569e5b | 1 | 0 | 0 | 1 (fix e88159fb) |
| 002402 | 13010a49de52 | 1 | 0 | 0 (5 failed) | 1 (5 passed, fix 56f63eb6) |
| 001269 | f18fa0a79ce5 | 1 | 0 | exit 1 (7 failed/1 passed) | n/a |

Strip → mtime fleet records:

| Task | mtime variant | mtime_distinct | Nop |
|---|---|---|---|
| 002938 | 4065fdf8d218 | 1 | 0 (7 failed) |
| 000666 | c9b5419311c0 | 1 | 0 (9 failed) |
| 002308 | 247bb6a78983 | 1 | 0 (5 failed/10 passed) |
| 000324 | a0505b464aff | 1 | 0 (collection error — hidden test imports a name base lacks; identical signal on the strip parent, so pre-existing and sound) |
| 002139 | d120c93e377a | 1 | 0 (11 failed) |
| 000905 | 38e931f7c9be | 1 | 0 (4 failed/11 passed/2 xfailed/2 xpassed) |
| 001809 | 2d97c1dd0e05 | 1 | 0 |

All 10 records marked `validated` with per-task evidence. Found while
sampling: 002938's pre-existing purge-chained mtime candidate cannot set up
(purge-installed-copies fails closed: cannot identify the project version),
so its sample validation uses the new strip-chained record instead; the
purge-chained record stays `candidate` (purge inapplicable there, per the
HAR-194 stance).

Residual: 1,142 candidate records await grading-with-purge-guard evidence
before training use; tasks whose nop needs a network-dependent grader are
unchanged by this transform (it touches no test or dependency bytes).
