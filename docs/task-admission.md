---
status: living
audience:
  - builder
  - operator
---

# Task admission gate (`evallab tasks admit`)

One fail-closed gate over a task package directory: offline static scan,
$0 oracle/nop controls, then the full 14-attack cheat ladder, then a
verifier-mutation probe — in that order, stopping at the first
non-passing step, always writing a digest-bound admission record
(`evallab.task_admission/v1`).

## Usage

```bash
# print the 18-step plan without running Docker
uv run evallab tasks admit --task <package> --dry-run
# run the gate ($0, local Docker, n-concurrent 1)
uv run evallab tasks admit --task <package> --job-prefix admit --json
# explicit oracle reference (receipt or raw patch); "none" skips the lookup
uv run evallab tasks admit --task <package> --reference auto
# fewer mutants, or skip the mutation step (the skip is recorded)
uv run evallab tasks admit --task <package> --max-mutants 6
uv run evallab tasks admit --task <package> --skip-mutation
```

Exit 0 = admitted; 1 = any other verdict (`needs_review` included — it
routes to a human, never auto-admits). `--output` overrides the
record path (default
`<jobs-dir>/<prefix>-<slug>/admission-<digest12>.json`).
`--no-variant-status` skips the lineage update while still writing the
record. A `rejected` update finalizes the lineage record (validated and
rejected are both final) — pass `--no-variant-status` for trial runs
against records you do not want to flip.

## Steps (cheapest first)

| # | step | pass iff |
|---|---|---|
| 1 | `static-scan` (`hack scan` in-process) | the scan executes; findings are recorded as advisory evidence, never a veto (see below) |
| 2 | `oracle` (stability-job path, repeat 3) | every trial reward is exactly 1.0 — via the shipped `solution/solve.sh`, or via the HAR-191 reference (see below); `unproven` when neither exists |
| 3 | `nop` (stability-job path, repeat 3) | every trial reward is exactly 0.0 |
| 4–17 | `cheat:<attack>` (one single-trial cheat job per attack, full 14-attack ladder) | reward < 1.0 (the lane rule: cracked iff reward >= 1.0) |
| 18 | `mutation` (`hack mutate` audit, `--max-mutants` 12) | every scored mutant is killed (see below) |

Each executed step records command, job dir, reward(s), duration, and
(for oracle/mutation) the reference provenance. Verdicts: `admitted`
(all pass), `rejected` (a step proved the task bad), `needs_review` (a
mutant of the fix kept reward — a survivor hypothesis for a human, never
an auto-flip), `not_admitted` (infrastructure broke before proof either
way — fail closed), `unproven` (no oracle reference covers the task, so
solvability cannot be shown — distinct from infra).

## Oracle reference (HAR-191 path, no new store)

MiMo code tasks ship no `solution/solve.sh`, so the oracle agent alone
cannot prove solvability. The gate reuses the existing history-oracle
reference instead of inventing one:

1. The committed `research/experiments/python-task-ledger/oracle_sweep.csv`
   selects the proven row (`oracle:pass+nop:fail`) by bare task id.
2. The recorded sweep receipt is validated: extraction `ok`, solution
   patch present with matching sha, recorded arms 1.0/0.0.
3. The gate stages the package, plants a `solve.sh` applying that patch
   in the agent worktree (the mtime-validation mechanism), and grades
   the fixed tree with the oracle agent for fresh Docker proof.

`--reference` takes `auto` (the above), `none`, or an explicit sweep
receipt (`.json`) / raw patch path. The step records fix commit, patch
sha, receipt path, recorded arms, and exact/task-level digest binding
(receipt `run_digest` vs tested package).

## Records and lineage

The admission record binds `package_digest` (+ `harbor_digest`) and,
for lineage variants, the record is cited as evidence in a
`variant-status` `validated`/`rejected` update — the
`evallab.task_variant/v1` ledger is reused, no second ledger is
invented. Resolution is targeted (one record file by task slug plus
package digest), never a 15k-file corpus scan. Non-variant packages
skip the update. A final lineage verdict is never reflipped: a gate
that disagrees with an already-final record notes the collision and
leaves the record untouched. `needs_review`, `not_admitted`, and
`unproven` never touch lineage status.

## Smokes (2026-10-09, Harbor 0.24.0, local Docker, $0)

Reference `56f63eb6` / patch `sha256:9d9d297c…` (receipt
`HAR-191-oracle-sweep/receipts/format-code-task-002402.json`,
recorded 1.0/0.0, task-level binding for variant packages):

* Unhardened HF parent → **rejected**. Reference oracle [1.0, 1.0, 1.0]
  (29.9 s), nop [0.0, 0.0, 0.0] (27.1 s), recon attacks clean, then
  `cheat:skip_plant` cracks (reward 1.0, 20.7 s) and the gate stops.
  Record `admit2-format-code-task-002402/admission-5ca085f112ab.json`.
* `separate-verifier@2` variant `a4727ccce904` (derived for this smoke on
  the validated mtime head, marker `test_scan_plated_uniform`) →
  **admitted**. Oracle [1.0 × 3] (52.5 s), nop [0.0 × 3] (52.1 s), all
  12 ladder attacks resisted (40–83 s each, `skip_plant` 0.0 included).
  Record `admit2-a4727ccce904/admission-a4727ccce904.json`; lineage
  flipped to validated with the record as evidence. No hole: sv2 holds
  where shared grading falls.

Job dirs: `/private/tmp/admit2-jobs`, `/private/tmp/admit2sv-jobs`.
Earlier calibration: `skip_plant` cracks the shared-grading mtime head
(reward 1.0), confirming the ladder bites before sv2.

Mutation probe (2026-10-09, gate `tasks.admit/v2`, `--max-mutants 6`,
job dir `/private/tmp/admit-mut-jobs`): the same sv2 variant
`a4727ccce904` re-passes scan, oracle [1.0 × 3] (63.5 s), nop [0.0 × 3]
(80.5 s), and all 12 ladder attacks, then the mutation step targets
`/testbed/numpyro/distributions/batch_util.py` (the file the reference
patch changes) and grades 6 mutants (436.9 s): capture-oracle 1.0,
capture-base 0.0, blank 0.0, and **6/6 survived** (score 0%) →
`needs_review`, lineage untouched, survivor patches listed on the
record. The verifier is insensitive to every sampled edit of the fix —
a review hypothesis, not an auto-reject. Total gate wall time ~29.5 min.

## Startup cost (2026-10-09)

CLI wall time was ~150 s against 0.0–0.2 s step times. Cause: lineage
resolution parsed all 15,245 records (`load_records`, 81 s) plus ~15 s
of interpreter/uv/import overhead. Fixed by targeted resolution above;
residual startup is harness-wide import cost, not gate code.


## Verifier mutation (post-ladder probe)

After the ladder passes, the gate runs the `evallab hack mutate` audit
(`evallab.verifier_mutation.report/v1`) against the same staged package
the oracle step graded, with `--max-mutants` (default 12, one Docker
trial per mutant) and `--skip-mutation` as an explicitly recorded
operator override (`mutation_skipped: true`, step listed under
`skipped_steps`).

Targets: with a HAR-191 reference, the source files the reference patch
changes, as absolute container paths (workdir + `+++ b/<path>`), so
mutants are edits of the fix. With a shipped `solve.sh`, the package's
declared artifacts (the `hack mutate` default).

Verdict mapping (`survived + partial > 0` routes to review, never rejects):

* `task_broken` → step `fail`, gate `rejected`. The capture controls
  re-prove oracle=1/base=0 on the exact staged package, so an objective
  mis-score is the same evidence class as a control-step failure: the
  harness ran fine, the verifier is demonstrably wrong.
* any survivor or partial → step `review`, gate `needs_review`. Lineage
  stays untouched (only `admitted`/`rejected` finalize it); the record
  lists the survivor patch paths (`survivor_patches`,
  `inputs/<id>/<id>.patch` under the mutation run dir).
* all killed → step `pass` (the score is recorded in the step detail).
* nothing informative (`no_mutants`, `unscored`, harness breakage, or no
  targets at all) → step `infra`, gate `not_admitted`. Fail-closed and
  stated plainly in the step detail: the gate cannot show verifier
  sensitivity, so admission is blocked.
