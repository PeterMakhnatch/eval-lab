# mimo-clean-v3: canonical clean set for the MiMo code pool (all languages)

Date: 2026-10-10. $0 paid compute (local Docker + local derivation only;
no model calls, no Modal/Daytona).

## What this is

One clean package per code task, each carrying the canonical chain in its
lineage — the v2 set through `separate-verifier@4` (rootdir-robust junit
matching and pristine-workdir deltas; see `docs/mimo/separate-verifier.md`):

```
Python (1,180 ledger rows): ledger run package (repairs)
  -> strip-future-history@1 -> purge-installed-copies@1 (scoped)
  -> purge-build-caches@4 -> mtime-normalize@2 -> separate-verifier@4
  -> agent-network-none@1 (agent phase only; verifier baseline public)
  (+ reference solution where a fix exists)

Non-Python (1,518 snapshot tasks): snapshot task dir
  -> strip-future-history@1 -> purge-build-caches@4
  -> mtime-normalize@2 -> separate-verifier@4 -> agent-network-none@1
  (purge-installed-copies is a Python pip mechanism: noted, not derived)
```

- Builder: `src/evallab/mimo_clean.py`; CLI: `evallab mimo-clean build|verify-local`
- Design doc: `docs/mimo/clean-set.md`
- Manifest: `manifest.csv` in this directory
  (2,698 rows: 2,666 built, 32 skipped discards)
- Named-ID re-grade list: `regrade-tasks.json` (223 Python tasks);
  other tasks with baked untracked content can also grade differently.
- Predecessors: `research/experiments/mimo-clean-v2/` and
  `research/experiments/mimo-clean-v1/` (kept as history)

The verifier step is `separate-verifier@4` instead of `@3`: it fixes
rootdir-shifted named-ID matching and uses the complete verifier-owned
post-setup workdir for agent diffs (not just BASE's tracked tree).
Reference fixes resolve from the landed
`research/experiments/mimo-reference-fixes/index.csv` (256 oracle-pass
fixes, +64 over the HAR-191 sweep v2 used). Ledger membership still wins
over the snapshot category: 002361 is snapshot-category Go but builds
through the Python chain from its ledger run package (1,180 + 1,518 =
2,698 manifest rows, identical task set and statuses to v2).

The final build also adopts `purge-build-caches@4` (PR #823, merged
`3b91ca92446f97029e0cfec85be3a32078e21234`: disabled-cache tolerance) and
appends `agent-network-none@1` (PR #824, merged
`03adac2137c846eda87d07170a97c937ec2fa4db`: agent-phase egress denial,
leaving setup and the separate verifier public). Both landed within the
45-minute input window after the verifier merge.

## Why @4 (the bug it fixes)

The `@3` pytest grader exact-matched expected test ids (from
`test.patch`/command, e.g. `tests/unit/test_x.py::test_y`) against
junit-classname-derived ids. When pytest's rootdir sits under `tests/`
(e.g. the cloud-sql-connector family, rootdir=/testbed/tests), junit
classnames lack the `tests/` segment, so `missing == all` and the reward
is 0 even with the reference fix applied and every test passing — found
by the fleet census on Daytona (000102 oracle: 8/8 PASSED, reward 0,
missing=[all 8]; same shape on 000156/000157/000227/000242). `@4` matches
by aligned path-suffix at component boundaries instead, with exact
class-chain and test-name agreement (plus the junit `file` attribute when
present). No `@2`/`@3` symbol or template byte changes, so `@2`/`@3`
records stay valid.

000114 exposed a second false-0: a baked untracked `.venv` in its image
appeared as agent-added `_pytest` content in the BASE-relative diff.
`@4` captures tracked, untracked, and ignored files from the pristine
verifier immediately after setup; identical baked files are not agent
changes. Actual modifications in those directories remain subject to the
tamper gate.

The same task's base command contains `--deselect=path.py::test_x`;
`@4` excludes these option-prefixed tokens from selected named IDs.
Otherwise the four passing selected tests could still grade 0 for four
fictitious missing `--deselect=...` IDs.

### Baked untracked content: measured v2-image sample

A read-only scan of the seven pinned control images, before any setup or
agent, runs `git ls-files --others -z` in each task's declared workdir
(including ignored files). All seven v2 packages use the same image and
workdir as the final v3 package. **5/7 sampled tasks contain baked
untracked files; 1/7 contains a baked `.venv`.** This is a control sample,
not a random fleet sample or an estimate for all 2,698 tasks.

| task | baked untracked files | `.venv` files | `_pytest` files |
|---|---:|---:|---:|
| 000102 | 46 | 0 | 0 |
| 000227 | 28 | 0 | 0 |
| 000242 | 22 | 0 | 0 |
| 002552 | 258 | 0 | 0 |
| 000666 | 0 | 0 | 0 |
| 000553 | 0 | 0 | 0 |
| 000114 | 1,571 | 1,345 | 132 |

Raw NUL-delimited listings, image digests, and workdirs are retained at
`~/Developer/eval-lab-results/2026-10-10/mimo-clean-v3/baked-image-sample/`
(`summary.json`).


## Build result (`evallab mimo-clean build --workers 8`)

Reference fixes resolved from the landed reference-fix index
(`reference fixes: index .../mimo-reference-fixes/index.csv (313 rows)`).

| Status | Count | Detail |
|---|---|---|
| built | 2666 | 1,148 Python keep/fix rows + 1,518 non-Python snapshot tasks |
| skipped | 32 | all `discard` verdicts, reason cites the ledger row |

Chain shapes over the 2,666 built:

| Chain | Tasks |
|---|---|
| `strip-future-history@1>purge-build-caches@4>mtime-normalize@2>separate-verifier@4>agent-network-none@1` | 2664 (1,146 Python + 1,518 non-Python) |
| `strip-future-history@1>purge-installed-copies@1>purge-build-caches@4>mtime-normalize@2>separate-verifier@4>agent-network-none@1` | 2 (`format-code-task-001269`, `format-code-task-002308`: purge carried in their repair run packages) |

Per-domain counts (manifest `language`): identical to v2 (python
1180/1148/32, go 721, javascript 388, typescript 166, unknown 130, ruby
25, php 23, java 22, c++ 13, c 7, rust 7, scala 6, kotlin 3, dart 3,
lua/elixir/swift/svelte 1 each).

Purge legs over the 1,148 built Python tasks: 5 fail-closed with per-task
reasons (002552 poetry py3.8, 000792/002486 no project name, 002139
bitbake, 002391 PEP 668), 2 carried (CONFIRMED_PURGE), 1,141 scope-skips
per the HAR-194 CONFIRMED_PURGE-only stance (see `docs/mimo/clean-set.md`).
All 1,518 non-Python tasks note purge-installed-copies as n/a.

- 256/1148 built Python tasks carry an oracle-pass reference fix
  (`solution/solve.sh` built from the indexed patch; manifest
  `reference_fix` + `oracle_label`). 64 tasks gained a fix over v2's 192
  (the reference-fix lane's resume sweep). No non-Python task ships a
  reference fix with the dataset (`reference_fix=none`).
- Every row ships `verify=unverified`: the fleet census grades the
  packages later (see the design doc). In particular, `built` means the
  chain derived — it does NOT mean the package sets up: the census's nop
  sweep owns enumerating setup-failures.

## Reproducibility proof

The full canonical CLI build produced 2,698 rows. An isolated rebuild
started with no v3-stage records or packages, copied only the frozen
upstream `strip-future-history@1` artifacts, then freshly derived **2,666
records each** for cache `@4`, mtime `@2`, separate-verifier `@4`, and
agent-network-none `@1` (10,664 new records). **2698/2698 manifest rows
are byte-identical**, including every final digest, chain, status, and
reference binding. Both manifest SHA-256 values:

`0a95fccfd49979e8b378da9ab6a8f624c78d5a643eb2a1664ce64d1c77e5ed19`

Proof, full second manifest, and executable recipe:
`~/Developer/eval-lab-results/2026-10-10/mimo-clean-v3/frozen-upstream-determinism/`.

A separate all-from-snapshot experiment (no inherited lineage records)
matched 2,664/2,666 built digests. The two differences, 002402 and
002552, are inherited `strip-future-history@1` setup blocks: the existing
tracked artifacts use the older full-fsck check; the current upstream
renderer emits the later commit-only check. The canonical builder
preserves these frozen inputs; this slice does not overwrite or bump
that out-of-scope transform. Their tracked record IDs are `aee31dca2e10`
and `3efa6a29bc46`, respectively. The discrepant experiment is retained
under `fresh-determinism/`, not presented as a passing proof.

## Local acceptance (`evallab mimo-clean verify-local`)

Per task on local Docker (agent `no-network`, verifier public), using
pinned `xiaomimimo/mimo-v2.6-rl-oss` images pulled on demand:
oracle control (when a reference fix exists), nop control, and the full
12-attack cheat ladder v1.2.0 in-trial. Acceptance = **oracle 1, nop 0,
cheat clean**. Evidence generations (raw jobs out of git):

Final-chain raw jobs:
`~/Developer/eval-lab-results/2026-10-10/mimo-clean-v3/final-chain-controls/`.
Every row below was rerun on its final cache-`@4` / verifier-`@4` /
agent-network-`@1` digest; no old-digest equivalence credit is used.
`final-control-bindings.json` beside the raw-job folders verifies all 20
canonical cells' `source_package_digest` against this manifest and
records the separate Go oracle's intentional solution-only difference.

| task | oracle | nop | cheat (cracked/trials) | verdict |
|---|---|---|---|---|
| format-code-task-000102 (pytest, rootdir-shifted; the Daytona case) | 1 | 0 | 0/1 | PASS |
| format-code-task-000227 (pytest, pinned, nested `TestWSGITask::` class) | 1 | 0 | 0/1 | PASS |
| format-code-task-000242 (pytest, pinned, `lib/`-prefixed ids) | 1 | 0 | 0/1 | PASS |
| format-code-task-002552 (poetry py3.8, fail-closed purge) | 1 | 0 | 0/1 | PASS |
| format-code-task-000666 (pytest, ADDOPTS-cleared) | 1 | 0 | 0/1 | PASS |
| format-code-task-000553 (go-test) | 1* | 0 | 0/1 | PASS |
| format-code-task-000114 (baked `.venv/_pytest`, deselect options) | 1 | 0 | 0/1 | PASS |

**Acceptance: 7/7 pass.** The three pinned pytest cases establish
rootdir/class matching; 000114 now reports `rc=0 cases=4 bad=0 named=4
missing=[]` with an empty tamper log. Its nop fails a genuine assertion
(`rc=1`, `bad=1`) rather than the baked-dependency tamper gate.
These are final-chain control results, not fleet qualification.

Notes:

*000553 has no shipped reference fix. Its oracle cell comes from a
temporary control package copied from the final canonical Go package,
adding only `solution/solve.sh` with the real image-history `main.go`
at commit `53b42c1` (pinned image `40985110a355...`); reward **1**,
no trial exception. The canonical package still has `reference_fix=none`.
Raw oracle job: `.../mimo-clean-v3/final-go-oracle/`; exact source,
extraction provenance, and solve script: `.../mimo-clean-v3/go-oracle-source/`.

- These are raw local control receipts. Automatic `process-job`
  publication refuses jobs created directly inside the results home;
  they are not claimed as viewer-published or counted model scores.
- 000047 was not in this seven-task control sample. Every built v3 task,
  including 000047, carries verifier `@4` (see the full manifest).
- The historical v2 receipt records pnpm 000128 failing closed; it was
  not rerun as a v3 control here.

## Re-grading v2 -> v3 (for the fleet census)

223 Python tasks have positive pytest named IDs (1,129 selections,
excluding `--deselect=...` option tokens), listed in `regrade-tasks.json`
with per-task counts; 83 carry an oracle-pass fix. They are named-ID
re-grade candidates, not an exclusive boundary. The pristine-workdir
baseline, cache `@4`, and agent-phase network policy can also change
behavior, so the remaining 2,443 built tasks are **not** claimed
reward-equivalent by construction. The census must bind every graded
task to its final-chain digest and assess these changes.

## Spend

$0.00 — no paid compute used (no `evallab spend day` needed; local Docker
and local derivation only).
