---
status: living
audience:
  - builder
  - operator
---

# Task admission gate (`evallab tasks admit`)

One fail-closed gate over a task package directory: offline static scan,
$0 oracle/nop controls, then the full 12-attack cheat ladder — in that
order, stopping at the first non-passing step, always writing a
digest-bound admission record (`evallab.task_admission/v1`).

## Usage

```bash
# print the 15-step plan without running Docker
uv run evallab tasks admit --task <package> --dry-run
# run the gate ($0, local Docker, n-concurrent 1)
uv run evallab tasks admit --task <package> --job-prefix admit --json
```

Exit 0 = admitted; 1 = rejected or not-admitted (infra). `--output`
overrides the record path (default
`<jobs-dir>/<prefix>-<slug>/admission-<digest12>.json`).
`--no-variant-status` skips the lineage update while still writing the
record. A `rejected` update finalizes the lineage record (validated and
rejected are both final) — pass `--no-variant-status` for trial runs
against records you do not want to flip.

## Steps (cheapest first)

| # | step | pass iff |
|---|---|---|
| 1 | `static-scan` (`hack scan` in-process) | the scan executes; findings are recorded as advisory evidence, never a veto (see below) |
| 2 | `oracle` (stability-job path, repeat 3) | every trial reward is exactly 1.0 (infra when the package ships no `solution/solve.sh` — the oracle agent has nothing to run) |
| 3 | `nop` (stability-job path, repeat 3) | every trial reward is exactly 0.0 |
| 4–15 | `cheat:<attack>` (one single-trial cheat job per attack, full 12-attack ladder) | reward < 1.0 (the lane rule: cracked iff reward >= 1.0) |

Each executed step records command, job dir, reward(s), and duration.
Infrastructure breakdowns (Harbor errors, missing jobs, unreadable
rewards, unregistered step kinds) yield `not_admitted` — fail closed,
distinct from the `rejected` task-failure verdict.

## Static-scan policy

The V1-V8 ledger states its own limits: a finding is a claim, never an
exploitation proof, and a clean scan is not a certificate. On the current
shared-container corpus the V1/V3/V7/V8 findings are
architecture-constant — fully validated `mtime-normalize@1` heads carry
the same ledger as their unhardened parents — so findings cannot
discriminate and do not veto. The dynamic proofs veto. Findings stay
first-class evidence in the record for later policy or human review.

## Records and lineage

The admission record binds `package_digest` (+ `harbor_digest`) and,
for lineage variants resolved through the existing `tasks lineage`
path, the record is cited as evidence in a `variant-status`
`validated`/`rejected` update — the `evallab.task_variant/v1` ledger is
reused, no second ledger is invented. Non-variant packages skip the
update. A final lineage verdict is never reflipped: a gate that
disagrees with an already-final record notes the collision and leaves
the record untouched. `not_admitted` never touches lineage status.
## First smoke (2026-10-09, Harbor 0.24.0, local Docker)

Hardened 002402 head `13010a49de52` (`mtime-normalize@1` over a fully
validated chain) vs its unhardened HF parent
(`FineEnvs__MiMo-V2.6-RL-harbor-code@5746e2f0c5c6/.../format-code-task-002402`):
both gates return **`not_admitted`** — fail closed, not certified either
way. Per-step evidence (records under
`<jobs-dir>/admit-smoke-<slug>/admission-<digest12>.json`):

* `static-scan` passes with the same 12 findings (V1/V3/V6/V7/V8) on both
  packages — the ledger is architecture-constant (see policy above).
* `oracle` is infra on both: neither package ships `solution/solve.sh`,
  so the oracle agent cannot run and solvability is unprovable. This is a
  task-shape fact, not harness flakiness — `tasks stability-run --agent
  oracle` fails the same way on these packages.
* The gate stops at oracle (early-stop); nop/cheat never run in-gate.

Out-of-gate Docker evidence, same day and image:

* Calibration: `cheat run --attacks skip_plant` against the hardened head
  cracks it (reward 1.0, 262 s) — shared-container grading stays
  tamper-permeable, so even past oracle this package would reject at the
  ladder.
* `stability-run --agent nop` on the hardened head (repeat 1): reward 0.0 —
  the nop half of the gate is viable on these packages; only the oracle
  half is blocked on the missing reference solution.

Path to a future admit: solution-injected variants (the MiMo lane's
`separate_verifier` derivation accepts a reference `solution_sh`) would
unblock the oracle proof; the separate-verifier grading would additionally
resist the tamper ladder. Neither exists as a committed lineage record
today.

## Extension: verifier mutation (design note)

The step list is data-driven: `default_steps()` owns the order and
`STEP_RUNNERS` maps each step kind to its runner
(`src/evallab/task_admission.py`). EnvCheck's forthcoming verifier
mutation `--json` result lands as one new registry entry plus one new
`StepDef` — no gate logic changes. No mutation step exists yet.

## First smoke (2026-10-09, Harbor 0.24.0, local Docker)

Hardened 002402 head `13010a49de52` (`mtime-normalize@1` over a fully
validated chain) vs its unhardened HF parent: see the admission records
for per-step timings and the verdict pair. Calibration note: `skip_plant`
alone cracks the hardened head (reward 1.0), so shared-container grading
stays tamper-permeable — the gate correctly rejects there.
