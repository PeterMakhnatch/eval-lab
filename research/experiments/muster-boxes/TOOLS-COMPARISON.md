# muster, tidegauge, harbor-sextant, harbor-control-gate: what each does, and which to keep

Written 2026-11-20 for Peter, who wants fewer tools. OBSERVED means I read the code or ran a command.
INFERRED means my reasoning.

## What each tool does (OBSERVED)

| | muster | tidegauge | harbor-sextant | harbor-control-gate |
|---|---|---|---|---|
| Question | Who earns the reward in a multi-seat task? What did each seat do and read? | How far can a score move? Is a difference real? | The same as tidegauge | Was a comparison fixed before the runs? Do the controls behave as declared? |
| Commands | `matrix`, `attribute`, `run`, `regrade`, `events` | `audit`, `score`, `compare`, `rank`, `plan`, `gate`, `context`, `decompose` | `summary`, `compare`, `rank`, `flaky`, `power`, `health`, `profile` | `harbor-study` (`freeze`, `materialize`, `record`, `report`, ...), `harbor-gate` |
| Multi-seat trials | Yes, built for them | Yes (since 11-19) | No | No |
| Starts trials | Yes (`matrix`, `run`) | No | No | Yes (`materialize`, needs Harbor 0.26.0) |
| Also has | World kit, 4 task families, pre-registration, findings F1 to F13 | 2 error bars, exact tests, an independent statistics review, calibration | Multiple-comparison correction, flaky tasks, task health from oracle and nop, a GitHub Action | Plan freezing and recovery of interrupted studies |
| Python lines, tests | 4,731, 289 | 4,206, 203 | 5,399, 208 | 2,740, 199 |
| Last commit | 11-20 | 11-19 | 11-18 | 11-18 |
| GitHub on 11-20, 3:15 PM ET | Private repository exists | `gh` cannot find it | `gh` cannot find it | `gh` cannot find it |

All 4 tools are standard-library Python, deterministic, and use no model. The local checkouts of all 4 are
clean. For tidegauge, harbor-sextant and harbor-control-gate, the local checkout is the only copy that I can see.

## Where they overlap (OBSERVED)

| Capability | Tools that have it |
|---|---|
| Score with an interval | tidegauge `score`, sextant `summary` |
| Is the difference between 2 runs real, and a CI gate | tidegauge `compare` and `gate`, sextant `compare` and its Action |
| Ranks that survive a rerun | tidegauge `rank`, sextant `rank` |
| Trials needed to see a change | tidegauge `plan`, sextant `power` |
| Context and token use from trajectories | tidegauge `context`, sextant `profile` |
| Controls with oracle and nop | muster `matrix` (seat tasks), sextant `health` (single-agent tasks), control-gate `harbor-gate` |
| A plan fixed before the runs | control-gate `freeze` (enforced by code), muster `PREREGISTRATION.md` (by hand) |

tidegauge and harbor-sextant answer the same questions on the same files. About 80% of their commands overlap.

## Recommendation (INFERRED)

Keep 2 tools, with 1 job each:

1. **muster** for multi-seat work: controls, replay grading, message measures, the task families.
2. **tidegauge** for statistics on any Harbor job folder. Keep it over harbor-sextant, because it reads
   multi-seat trials, has `decompose`, and has the independent statistics review. Later, port 2 small ideas
   from harbor-sextant if they fit the 4,000-line budget: `flaky` and the GitHub Action.

Drop harbor-sextant (it duplicates tidegauge) and harbor-control-gate (muster's matrix covers the controls for
seat tasks, and a frozen plan can be a `muster` command later if hand-written pre-registration is not enough).

If you deleted the tidegauge repository on purpose, muster loses its statistics. Then the smallest fix is
to move tidegauge's kernel and `decompose` into muster as 1 package. That needs about 2,000 lines, and the
4,000-line budget of tidegauge would then apply to muster.
