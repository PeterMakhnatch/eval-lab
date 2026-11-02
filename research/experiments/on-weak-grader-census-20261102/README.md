# on-weak-grader-census-20261102: Corpus-Scale Weak-Grader Census

STING-style weak-grader audit over the eligible gold universe of strictly-DEV MiMo packages.
Follows the methodology of `research/experiments/ov-sting-mutants-20261031/`.

## Summary Results

| Cohort / Metric | Count | Rate | 95% Wilson Interval |
|---|---|---|---|
| Total gold candidates | 50 | — | — |
| Ineligible packages | 18 | 36.0% | 24.1–49.9% |
| Eligible packages | 32 | 64.0% | 50.1–75.9% |
| Probed with passing reference control | 29 | 90.6% | 75.8–96.8% |
| Audited with mutants generated | 28 | 87.5% | 72.0–94.9% |
| Control failed | 0 | 0.0% | 0.0–12.1% |
| **Weak graders (original accepts $\ge 1$ mutant)** | **18 / 28** | **64.3%** | **45.8–79.3%** |
| Weak graders (all probed with control == 1.0) | 18 / 29 | 62.1% | 44.0–77.3% |
| Total mutants evaluated | 74 | — | — |
| **Mutant survival rate** | **22 / 74** | **29.7%** | **20.5–40.9%** |

## Pilot Replication (12 Overlapping Tasks)

On the 11 tasks that overlap the 18-task pilot (`ov-sting-mutants-20261031`) and were audited in this sweep, replication agreement was **100% (11/11)**:
- Both weak: 8 tasks (`000079`, `000081`, `000232`, `000682`, `000931`, `001267`, `001850`, `001985`)
- Neither weak: 3 tasks (`001205`, `002160`, `002609`)
- Exact McNemar paired test: discordant counts $b = 0, c = 0$, $p = 1.0$.
- In addition, survivor counts matched identically for all 11 audited overlapping tasks.

## Survival by Mutation Operator

| Operator | Generated | Applied | Survived | Survival Rate | 95% Wilson Interval |
|---|---|---|---|---|---|
| `boundary_flip` | 6 | 6 | 3 | 50.0% | 18.8–81.2% |
| `bool_flip` | 10 | 10 | 4 | 40.0% | 16.8–68.7% |
| `equality_flip` | 11 | 10 | 4 | 36.4% | 15.2–64.6% |
| `call_drop` | 19 | 19 | 6 | 31.6% | 15.4–54.0% |
| `handler_drop` | 4 | 4 | 1 | 25.0% | 4.6–69.9% |
| `keyword_drop` | 8 | 8 | 2 | 25.0% | 7.1–59.1% |
| `guard_drop` | 12 | 12 | 2 | 16.7% | 4.7–44.8% |
| `int_shift` | 4 | 4 | 0 | 0.0% | 0.0–49.0% |
| **Total** | **74** | **73** | **22** | **29.7%** | **20.5–40.9%** |

## Runner Breakdown

- `pytest`: 16 / 25 weak = 64.0% (95% CI: 44.5–79.8%)
- `unittest`: 2 / 3 weak = 66.7% (95% CI: 20.8–93.9%)

## Unaudited Tasks and Exclusions

- `format-code-task-000945`: Reference control passed (1.0), but no valid AST mutants could be formed on the added non-test Python lines under the 8 operators.
- `format-code-task-002680`, `format-code-task-002976`, `format-code-task-003007`: Stopped at spend gate reservation cap ($2.496 / $2.50 across 312 reservations).

## Spend and Infrastructure

- Backend: Modal (`ModalBackend`, shortcut mode).
- Approval: `"Peter 2026-11-02 overnight A2: Modal cap $2.50"`.
- Ledger Reservations: 312 reservations ($2.496 reserved).
- Measured Cost: **$0.2360** (106 cell executions, 1,428.2 sandbox wall seconds, median 9.5 s per cell).
- Stretch goal: Not triggered because remaining reservation was $0.004 (< $0.60 threshold).

## Files

- `run_census.py`: Driver script to audit tasks and export structured artifacts.
- `census.json`: Complete census with one row per task.
- `weak-tasks.json`: List of weak tasks sorted by number of surviving mutants.
- `operators.json`: Summary stats per mutation operator.
- `replication.json`: Task-by-task paired replication against the 18-task pilot.
- `summary.json`: Top-level aggregate metrics and spend ledger data.
