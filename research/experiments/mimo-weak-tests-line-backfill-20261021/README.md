# HAR-209: weak-test coverage and task-check exclusions

Date: 2026-10-21. Spend: **$0**. Local Docker only, one task per shared FIFO
acquisition; no model calls, Modal, cloud compute, or shared-service change.

**Status: partial delivery, not completion.** The 567-task primary sweep is
finished and every reported candidate has an adjudication. The line-level
backfill of 265 block-only tasks is not finished, and three confirmed defect
groups still lack a repair or an exclusion. `summary.json` is the
machine-readable receipt; `build.py` recomputes it from the frozen atlas and the
line-backfill output root.

## What is established

| Measure | Value | Source |
|---|---|---|
| Primary reports | 567/567 (338 frozen + 229 continuation) | `atlas.jsonl` |
| Primary verdicts | 197 pass, 278 fail, 92 unknown | `atlas.jsonl` |
| Primary statuses | 453 measured, 90 not applicable, 22 budget exhausted, 2 baseline mismatch | `atlas.jsonl` |
| Block candidates adjudicated | 278: 79 confirmed weak, 114 neutral, 50 out of scope, 35 uncertain; 0 unreviewed | `atlas.jsonl` |
| Line survivors adjudicated | 674 across 302 scans: 163 required gap, 62 non-positive, 301 equivalent/optional, 148 uncertain; 0 unreviewed | `atlas.jsonl` |
| Published key | 114 confirmed weak tasks, 242 executable witnesses (79 block + 163 line) | `../answer-keys/mimo-weak-tests-v2.1/summary.json` |

Frozen atlas SHA-256 `84cc524b654b944fcbf286f2fd2c189bff3ebf2c071e3d180fe0d311ca282450`.
The 567 primary reports, their grade logs, reference patches and packages were
re-hashed here: 1,133 report/log bindings matched, 0 missing, 0 mismatched, and
all 567 reference patches and packages are present. `format-code-task-002940` is
the one task with no archived reference patch or grade-log binding, so a
line-level pass cannot run for it until a reference exists.

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

| Disposition | Tasks | Evidence |
|---|---|---|
| Demonstrated targeted repair | `001809`, `001985`, `002486`, `002391` | separately versioned packages with recorded wrong-zero vectors; not full semantic certification |
| Explicit exclusion (literal v5 `excluded` row + `qa.finding/v1`) | `001694`, `001833`, `000865`, `000803`, `000838` | `mimo-clean-v5-census/verify-overrides.csv` (SHA-256 `7a23e3b7…`), `findings/*.json`, `docs/mimo/README.md` |
| Historical hold, defect now unknown | `002661` | v5 row retained; do not quote as established unfairness |
| Confirmed, **no repair and no exclusion** | `candidate-0036-software-data-engineering`, `candidate-1789-security-appsec`, the 13 base-gap tasks | open |
| Not applicable to the current generation | plain v6/v7 manifests | 0 literal per-task holds; generic `unverified` gating only |

The current-generation control inventory also lists `format-code-task-001833` as
`runnable: true` while the three prompt-misaligned tasks with the same kind of
inherited hold are `runnable: false`. The hidden non-idempotent
`django.contrib.messages` append is a property of the test command, not of the
package bytes, so that queue entry contradicts the recorded exclusion. It was
**recorded, not edited**: `pending-controls.json` is a completed campaign's
input inventory whose SHA-256 is pinned in `modal-readiness.json`.

## Line-level backfill (Done-when 1)

The plan is the 265 cohort tasks with a block report and no line scan; its
identity hash is `ea014420f1103be8a72805fb217643a3915e0013079e668c0f37ba83cb1ef3c5`
(`plan.tsv` in the output root). Each task runs as

```sh
uv run --no-sync evallab docker-lock run --timeout 900 har209/line-backfill/<task> -- \
  uv run --no-sync evallab hack ref-subset run <package> --patch <reference.patch> \
  --output-dir <root>/<task> --mutants 20 --memory 3g --max-grades 24
```

Executed today, measured from the CLI's own grade lines: **17 tasks**, each with
`full = 1` and `nop = 0`; 11 of them kept at least one surviving one-edit mutant.
Those 17 runs are **not counted as coverage**: the first driver passed the shared
root as `--output-dir`, and `hack ref-subset run` writes into that directory
without appending the task id, so the per-task `report.json` files overwrote each
other. The surviving artifacts are preserved under `_attempts/20261021-overwritten/`
with `recovered-from-run-log.json`; the grade lines remain in `run.log`. The
driver now writes one directory per task.

Bound per-task reports in this receipt: **3** tasks (`000169` measured pass with
6 of 17 mutants surviving, `000341` measured fail with 5 of 17, `000587`
measured fail with 2 of 5; every control pair is `full = 1`, `nop = 0` and every
package digest and reference SHA-256 matches the archived atlas). Remaining
without a line scan: **262** tasks, listed in
`summary.json` (`line_coverage.remaining_task_ids`); 261 are executable and
`format-code-task-002940` has no reference patch.

New survivors are `unreviewed` by construction: a surviving edit is a candidate,
not a defect, until a semantic review batch accepts it. No new scan is ingested
into a published answer key, so no key bytes change here.

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
- The line-level backfill is incomplete (see above); the 17 recovered attempts
  carry grade-line evidence only.
- Labels bind to package digests. A rematerialized or repaired package needs its
  own controls; old grades must not be transferred.
- The positive-only key supplies no detector recall, false-alarm rate or
  benchmark prevalence.

## Rebuild and check

```sh
uv run python research/experiments/mimo-weak-tests-line-backfill-20261021/build.py --check
```
