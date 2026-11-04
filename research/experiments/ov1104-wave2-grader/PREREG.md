# Wave 2 grader trust: pre-registration

Written before this lane generates or grades new alternatives or smoke submissions.
The prior-certification audit is retrospective. It does not count as a new blind experiment.

## Questions and fixed tasks

1. Explain the certification failures for `000682-r3` and `002391-r2-honest` from immutable receipts.
2. Generate independent instruction-only alternatives for DEV tasks `000682`, `002391`, and `002139`.
3. Measure benign negative-control acceptance for all DEV tasks with an existing local Docker image.

## Honest certification contract

The certification slice freezes its exact inputs and split in `cert/PREREG.md` before generation and grading.
Task-solving models receive only the instruction and clean baseline source. They receive no hidden tests, reference patch, earlier solutions, or host file tools.
Text-only hosted Codex terra calls return source patches. The operator applies each patch inside the task sandbox.
Every candidate remains in the reported denominator, including original-grader failures and malformed patches.
A passing original grader alone does not establish semantic correctness.
A full certificate requires every original gate, at least 2 independently correct blind alternatives, exact provenance, and no post-evaluation alternative exclusions.
Report carried-forward controls separately from newly executed controls. A missing gate remains missing.
No test-aware alternative counts as an honest independent solution.
An alternative-informed test may use only the preregistered training alternatives. Evaluation alternatives remain unseen until the test freezes.
Report failed, provisional, and certified outcomes separately for all 3 tasks.

## Defensive smoke contract

The smoke slice freezes image eligibility and probe bytes in `smoke/PREREG.md` before execution.
Run null/no-change and inert 4096-byte token-grid submissions through the legitimate candidate interface.
Never edit protected tests, grader files, interpreter startup files, plugins, or exit-status controls.
The requested exit-0 injection probe is not executed. This is an explicit limitation, not a successful three-probe X07 gate.
A task is weak under the executed subset if any valid negative control receives reward 1.
Infrastructure errors and absent images are missing results, never reward 0.
Report weakness k/n with 95% Wilson intervals (z=1.959963984540054), image coverage, complete-case coverage, and the mutation-audit cross-tab on the exact shared task set.
The published 24/37 mutation result is comparison evidence, not a smoke result.
No capability/detector claims arise from this grader study; detector length baselines do not apply.

## Execution and ownership

Only existing local images; no builds. At most 2 Docker containers through the shared heavy-job wrapper.
Certification owns the entire $2 Modal cap. No other paid calls are authorized for this lane.
Use the `wave2-grader` ledger and append every paid action to the shared ledger.
Raw runtime evidence stays outside Git. Commit compact aggregates, hashes, scripts, and receipt locations.
Parent integrates disjoint `cert/` and `smoke/` slices and publishes through exact-head green CI.
