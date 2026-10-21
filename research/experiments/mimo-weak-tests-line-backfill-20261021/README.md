# HAR-209: weak-test coverage and task-check exclusions

Date: 2026-10-21. Spend: **$0**. Local Docker only, one task per shared FIFO
acquisition; no model calls, Modal, cloud compute, or shared-service change.

**Status: execution checkpoint, not HAR-209 completion.** The 567-task primary
sweep and its adjudications are finished. The 265-task line backfill continues
under the existing single-lane controller. Confirmed defects now have consumed,
exact-package scientific-use exclusions; newly reported candidates still need
their own bound review. `summary.json` is a frozen checkpoint, not live progress.
`build.py` recomputes it from the atlas, reports, reviews and exclusion inventory.

## What is established

| Measure | Value | Source |
|---|---|---|
| Primary reports | 567/567 (338 frozen + 229 continuation) | `atlas.jsonl` |
| Primary verdicts | 197 pass, 278 fail, 92 unknown | `atlas.jsonl` |
| Primary statuses | 453 measured, 90 not applicable, 22 budget exhausted, 2 baseline mismatch | `atlas.jsonl` |
| Block candidates adjudicated | 278: 79 confirmed weak, 114 neutral, 50 out of scope, 35 uncertain; 0 unreviewed | `atlas.jsonl` |
| Line survivors adjudicated | 674 across 302 scans: 163 required gap, 62 non-positive, 301 equivalent/optional, 148 uncertain; 0 unreviewed | `atlas.jsonl` |
| Published frozen key | 114 confirmed weak tasks, 242 executable witnesses (79 block + 163 line) | `../../answer-keys/mimo-weak-tests-v2.1/summary.json` |

Frozen atlas SHA-256 `84cc524b654b944fcbf286f2fd2c189bff3ebf2c071e3d180fe0d311ca282450`.
`primary-custody.json` records this owner's SHA-256 check of all **1,133**
report/log bindings: none missing or mismatched, exactly 567 unique primary IDs.
`002940` has no archived grade-log or reference binding. A later reference
**does** exist: the resumed plan binds
`eval-lab-results/2026-10-09/mimo-reference-fixes/format-code-task-002940/solution.patch`,
SHA-256 `ccbf226a22de878040a9a1815881d4c3b2e599c008069cd5398e3f012a0e9c41`.
That does not retroactively fill the historical absence.

## Candidate adjudication (Done-when 2)

`exclusion-audit.json` reconciles the 43 cases named in the original brief and
the fleet scan against instruction text, hidden-test bytes, recorded controls
and the current manifests. Verdicts are claim-specific, not whole-task
cleanliness labels.

- **10 confirmed**, **29 refuted**, **4 unknown**. Unknowns stay unknown:
  `002661` (unfairness of the exact default/scope is not established — the
  pre-change default and assertion were already profile-only), `002402` and
  `000495` (narrower than reference), `candidate-1702-ml-inference` (suspected
  broken environment, no composed package or controls).
- Material corrections to the original brief: only `001694` is whole-grader
  vacuous; the other 27 whole-vacuity readings are refuted because type checking
  (`tsd`/`tsc`) or real unit tests *are* the contract. `000838` is confirmed but
  narrower than claimed (the "Your command" example is in the prompt; the
  unprompted `hide_command`/`--hide-command` pin is the defect). `002552`'s
  "three of four unfixed tests pass" claim is refuted. `002391`'s original
  never-merge claim is refuted while a different required alias gap is confirmed.
- 13 genuine base-phase regression gaps are confirmed as narrower observations
  (`000051`, `000118`, `000262`, `000749`, `001235`, `001308`, `001651`,
  `001782`, `002012`, `002570`, `002918`, `002959`, `002979`).

## Repair or exclusion (Done-when 3)

`scientific-exclusions.json` is a generated, **consumed** inventory, keyed by
full task ID and `registry.task_directory_digest`, not a component digest or
task-ID-wide rule. `summary.json` gives its exact checkpoint totals by
classification and claim status; package-pair counts are not unique-task counts.

| Disposition | Scope | Evidence |
|---|---|---|
| Demonstrated targeted historical repair | `001809`, `001985`, `002486`, `002391` | Versioned packages and recorded wrong-zero vectors; only the declared cases are qualified |
| Accepted wrong-patch originals excluded | All 114 supported frozen-atlas packages and every requirement-supported fresh wrong-patch package in the bound reviews | Atlas and fresh review hashes, package/reference/witness bytes, full/nop/accepted-wrong grade triplets |
| Static defects excluded | Compile-only `001694`; unfair `001833`, `000865`, `000803`, `000838`; terminal `candidate-0036-software-data-engineering`, `candidate-1789-security-appsec`; all 13 base-regression gaps | Claim-specific audit and exact source/current-package identities |
| Conservative holds, not established failures | Four unresolved original claims and scoped unmeasured child packages | Typed `unknown` / `unmeasured_on_package`; no invented failed controls |

Manifest reads overlay the veto without rewriting historical source bytes;
fresh writes, census combination/reporting, and direct admission cannot restore
scientific acceptance with clean controls or a pass override. Admission gate
**v5** checks exclusions before selectable steps. Diagnostic oracle/nop,
mutation, ref-subset and repair controls remain allowed.

The completed campaign's `pending-controls.json` is unchanged, including
`001833`'s historical `runnable: true`; its pinned hash and observations remain
historical, not scientific admission. No existing training/repair manifest or
answer key is expanded. A different child digest receives neither its parent's
veto nor inherited certification; ordinary controls still apply.

## Line-level backfill (Done-when 1)

The plan covers 265 unique cohort task IDs with no previous line scan.
`resume-plan-20261021.json` binds every actual package and reference; its SHA-256
is `91a5f1541c3c5a60d09d8b8a6df419848d3d4817888357e2692d3d7ceca3940a`.
Retries and older overwritten attempts never add another task to coverage.

```sh
uv run --no-sync evallab docker-lock run --timeout 900 har209/line-backfill/<task> -- \
  uv run --no-sync evallab hack ref-subset run <package> --patch <reference.patch> \
  --output-dir <root>/<task> --mutants 20 --memory 3g --max-grades 24
```

The first driver's **17 attempts** wrote to the shared output root and
overwrote per-task summaries. Their archived logs remain under
`_attempts/20261021-overwritten/`, `recovered-from-run-log.json`, and `run.log`;
they are **not** counted as 17 covered tasks. The continuation uses a unique
directory per task, preserves interrupted logs, validates completed identities,
and skips existing reports instead of rerunning them.

`summary.json` pins the measured/inapplicable/unavailable stage counts, usable
control pairs, reviewed and unreviewed survivor counts, and the exact remaining
task-ID frontier. It is not a completion claim while either the report frontier
or candidate-review frontier is nonempty. The frozen 265-task plan, not retry or
log-entry counts, defines the denominator.

Each frozen review file binds report/log/reference/witness hashes. Label
inheritance requires the exact same package, reference, minimal and dropped
patch bytes plus new usable confirmation controls — never just the task ID.
Recorded mutant grades come from `grades.log`, not the pre-mutation report
snapshot. Timeouts and generated-but-unattempted mutants remain explicit.
No new result is automatically ingested into the published answer key.

## Exercised consumer proof

`scientific-use-smoke.json` records actual acceptance predicates on explicitly
synthetic green census signals: all 114 supported original packages are
rejected, as are all 23 audited source identities (22 confirmed, one scoped
unmeasured hold). These synthetic signals are **not new benchmark grades**.
Real `tasks admit` CLI calls reject the new `000169` line-positive package and
the exact raw `000803` package, and route unknown `000495` to `needs_review`.
Only the preflight runs; no Docker, paid model or other admission step executes.

The source-identity regression failed before the producer stopped assuming a
manifest run digest identified the retained original, then passed after repair.
`include-counterexample.json` additionally executes exact archived helper bodies:
the reference loads a relative main recipe; two accepted mutants double its
directory and exit 1. This bounded helper proof is not a strengthened-grader
certificate or a full-project replay.

## PR #918's failed profile

`pr-918-profile.json` records the audit (`gh` reads only; no rerun).

| Fact | Value |
|---|---|
| PR | [#918](https://github.com/PeterMakhnatch/eval-lab/pull/918), merged 2026-10-15T18:00:37Z |
| Head / merge | `cd14f6d814c2…` / `2a62a5d5fe7d…` (tested merge `08a772f4ff5d…`) |
| Checks | 10 success, 1 failure on both the head and the merge revision |
| Failed check | `profile` (workflow `perf`), [run 38781094530](https://github.com/PeterMakhnatch/eval-lab/actions/runs/38781094530/job/116581308645) |
| Cause | harness succeeded; the budget ratchet exited 1 — projection median 58.603 ms and facts median 101.958 ms against a 37.5 ms ceiling |
| Postmerge push run | [run 38782058666](https://github.com/PeterMakhnatch/eval-lab/actions/runs/38782058666/job/116584733329) also failed (facts 44.147 ms) |
| Supersession | none found; neither exact revision is all-green under `agents/CHECKS.md` |

`quality-required` and `typecheck-required` passed, but CHECKS requires every
reported check to succeed. The merged-918 smoke receipt proves only an owned
arithmetic fixture (weak checks kept 5/7 survivors, boundary checks 0/7); it
neither repairs the profile nor certifies the corpus.

## Limits

- Adjudications are instruction-level source reviews over recorded
  `full=1`/`nop=0` controls on the bound package. They are not fresh replays,
  repaired-package certificates, or clean-task certificates.
- The line backfill and fresh candidate review remain incomplete at this
  checkpoint. Archived unavailable or overwritten attempts are not extra tasks.
- Labels bind to package digests. A rematerialized or repaired package needs its
  own controls; old grades must not be transferred.
- The positive-only key supplies no detector recall, false-alarm rate or
  benchmark prevalence.

## Rebuild and check

```sh
uv run --no-sync python research/experiments/mimo-weak-tests-line-backfill-20261021/build_exclusions.py
uv run --no-sync python research/experiments/mimo-weak-tests-line-backfill-20261021/build.py
uv run --no-sync python research/experiments/mimo-weak-tests-line-backfill-20261021/build.py --check
```

The producer reads the initial `backfill-reviews.json` plus sorted
`review-batches/*.json`; duplicate task IDs fail. Hash changes, unusable controls,
stale adjudications, and confirmed wrong-patch packages missing an exclusion
refuse receipt generation. Counts can change while the authorized controller
is still running.

