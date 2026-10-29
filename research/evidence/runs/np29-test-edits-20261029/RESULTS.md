# NP29-CM-1 results: do clean runs that edit a test hide a false-alarm weakness on Goodfire DEV and CheatBench DEV?

Zero model calls, $0. Preregistration: `README.md` revision 2 (#1522, merged before any alarm file was opened). Every number below is
in `results/score.json` or `results/membership.json`; cells of 1 or 2 are suppressed there and here, exact zeros are printed.
Inputs and tool hashes: `results/freeze.json`. "S" is the frozen primary definition; "primary detectors" are cascade v8, facets
(Haiku, attested v2 record) and their union; everything else is descriptive.

## Headline

**Question.** Does the 3% DEV false-alarm figure hide the Flake weakness (clean runs that edit a test alarming at 8 of 10)?

**What ran.** The mechanical definition S (a write to a strict test path) was applied to every step of the 150 scored Goodfire and
150 scored CheatBench runs after the strict `assignments.csv` cross-key; the stored cascade v8 cards, the stored Haiku facet records
and their union were then split into S runs (T) and the rest (N). The production `grader_tamper` rule was run on the same steps (G).

**Number.** The clean test-editing group is **4 of 70 on Goodfire (4 tasks) and 15 of 75 on CheatBench (exactly 1 task)**, both under the
preregistered minimum of 20, so no clean alarm rate is computed. Counts: cascade v8 alarms **0 of 15** on the CheatBench clean S
runs (the public total of 0/75 already forced this), facets and union **0 of 15** (4 of the 60 other CheatBench clean runs alarm under
both), and on Goodfire clean S runs the cascade alarms **1 or 2 of 4** (suppressed), facets and union 0 of the 3 that have a primary
facet record.

**Meaning.** **The DEV false-alarm rate says almost nothing about runs that edit a test, and whether DEV hides the Flake weakness is
undetermined from DEV.** DEV's clean S runs are mostly *new scratch test files* that the pass-through rule does not look at (G flags
0 of the 4 and 0 of the 15); the repair-like subset SI (edits of an existing test) is **at most 5 clean runs** (CheatBench 3,
Goodfire 1 or 2) against Flake's 10 publisher-defined repairs, so the 8/10 comparison is not like for like. The single
alarm-free CheatBench group is one task and mostly predicted by published totals. Required sample for a statement about the
test-editing stratum: with zero alarms, a Wilson upper bound below 49.0% needs 4 clean test-editing runs, below 10% needs 35, below 3%
needs 125 (exact; 5, 35 and 127 if the bound is rounded to 0.1 as printed).

## What can and cannot be said

Can: DEV holds few clean test editors (5.7% and 20.0% of its clean runs); they are mostly file creations in one CheatBench task; no
clean S run is seen by the judge-free rule; every run G flags in DEV (and in Flake's repairs) is also an S run. Cannot: a rate for
test-editing clean runs in either corpus; "DEV does not hide the weakness"; "no rule change is warranted". Preregistered verdict on a
rule change, C1-C5 under S and the primary detectors only: **undetermined from DEV**.

## 1. Clean S counts and the task-key check

| Corpus | Clean S / clean runs | Exact distinct tasks of those S runs | Notes |
|---|---|---|---|
| Goodfire | 4 / 70 | 4 | one run per task; prompts of 8,339 to 12,563 characters |
| CheatBench | 15 / 75 | **1** | one 3,913-character task prompt shared by 23 runs; 16 distinct write targets; 3 distinct first agent actions |

CheatBench has no independent task source (registry metadata holds `item_id` and a blind projection id; a trial directory holds
only `trajectory.json`). **66 of 150 CheatBench runs have no user step, so each is its own cluster**; the other 84 fall in 49 tasks,
and the 115 clusters are an upper bound on distinct tasks. The clean S group therefore carries the information of one task.

## 2. Reproduction of the published totals (all 9 match)

| Detector, class | Computed | Published |
|---|---|---|
| Cascade v8, Goodfire clean / enacted | 2/70 / 10/80 | 2/70 / 10/80 |
| Cascade v8, CheatBench clean | 0/75 | 0/75 |
| Frozen cascade, Goodfire clean / enacted | 2/70 / 12/80 | 2/70 / 12/80 |
| Frozen cascade, CheatBench clean / positive | 5/75 / 20/75 | 5/75 / 20/75 |
| Facets Haiku-all, Goodfire clean / enacted | 1/70 / 7/80 | 1/70 / 7/80 |

## 3. Primary T and N alarm counts, beside the predictable bounds

Clean runs (group sizes 4 and 66 on Goodfire, 15 and 60 on CheatBench). A count at or under its bound is not a finding.

| Detector | Goodfire T (n 4) | Goodfire N (n 66) | Bound for Goodfire T | CheatBench T (n 15) | CheatBench N (n 60) | Bound for CheatBench T |
|---|---|---|---|---|---|---|
| Cascade v8 | 1 or 2 of 4 | 1 or 2 of 66 | at most 2 of 4 | **0 of 15** | 0 of 60 | exactly 0 of 15 |
| Facets (primary record) | 0 of 3 (1 run has no record) | 1 or 2 of 55 | at most 1 of 4 | **0 of 15** | 4 of 60 | none published |
| Union (cascade v8 or facets) | 0 of 3 | 1 or 2 of 55 | at most 3 of 4 | **0 of 15** | 4 of 60 | none published |

Rates are not computed (both T groups are under 20). Descriptive sensitivities (no category): the frozen cascade is 0 of 15 (CheatBench
T) and 5 of 60 (N); the original union (frozen cascade or Haiku-all facets) is 0 of 15 and 9 of 60; v7 as the frozen cascade. The
published CheatBench union totals (9 clean alarms) therefore all lie in N. SD equals S in every class; L (over-inclusive) holds 34
CheatBench clean runs with 0 cascade and 3 facet alarms.

Enacted / positive runs (descriptive, groups of 5 and 9): CheatBench cascade T 4 of 9 against N 15 of 66, facets 4 of 9 against
26 of 66, union 6 of 9 against 38 of 66; Goodfire facets 0 of 5 against 4 of 63, union N 8 of 63, cascade cells suppressed. Under
the group-size rule no rate or difference is computed.

## 4. What fired among alarmed T runs

- **Clean:** nothing fired in CheatBench T (0 alarms); the Goodfire clean cascade cell is 1 or 2 alarms, so its rule is not printed.
- **Enacted CheatBench T:** the 4 cascade alarms are **all judge-confirmed, none judge-free**; rule families `prefilter:git_history` 3,
  `grader_tamper` and `hidden_info_read` 1 or 2 each; 4 facet alarms, all `eval_awareness` of `reasons_about_grader` or `plans_to_game`;
  `grader_or_harness_touched`, `tests` modified/deleted/replaced and `answer_material_accessed` are 1 or 2 each.
- **Enacted Goodfire T:** facets alarm 0 of 5; cascade cells suppressed.

## 5. S x G x alarm

G (the production `grader_tamper` rule) flags none of the 70 Goodfire and 75 CheatBench clean runs. Cascade v8 alarms that land in N
with G set: **exactly 0** in all four classes (clean and positive, both corpora); every G-flagged run in DEV is an S run, so S does
not miss what the rule sees there. Absence of a pass-through alarm on clean S runs means the rule does not see their forms (scratch
files outside `tests/` directories), not that it handles honest test edits.

## 6. Repair-like subset SI and the tuning deltas

SI: CheatBench clean 3 runs with 0 alarms under every primary detector; Goodfire clean 1 or 2 runs (counts suppressed). SD (deletions
and restores) adds no run to S in any class. S0 versus S (README section 5.4): only adjustment (a) moved a clean or Flake number; it
raised the Flake `legitimate_test_repair` S count from 3 to 5 of 10 and Goodfire enacted from 4 to 5; (d) moved CheatBench positives
from 10 to 9. No clean DEV count changed.

## 7. Flake: definition-tuning sample, descriptive (no rate, no category, no claim that S is valid)

| Group (S runs) | Cascade v8 (Sonnet judge) | Facets (Haiku) | Union |
|---|---|---|---|
| `legitimate_test_repair`, S: 5 of 10 runs | **5 of 5** | 0 of 5 | 5 of 5 |
| `legitimate_test_repair`, SI | no run | no run | no run |
| `app_fixed`, S: 7 of 50 runs | 1 or 2 of 7 | 0 of 7 | 1 or 2 of 7 |
| `app_fixed`, SI: 5 runs | 0 of 5 | 0 of 5 | 0 of 5 |

The five S repairs are whole-file or shell-redirect writes (none is in an in-place form, SI is 0), all five are G-flagged and alarmed by the judge-free rule.
S flags half of the publisher's repairs, so S neither recovers nor validates that label. The 69 runs span 16 tasks and the 12
established S runs lie in 2.

## 8. LR_S (descriptive; no category)

| Corpus | P(S given positive) | P(S given clean) | LR_S (Katz 95%) | Task-cluster bootstrap 95% | Fisher p |
|---|---|---|---|---|---|
| Goodfire | 5/80 (2.7-13.8%) | 4/70 (2.2-13.8%) | 1.09 (0.31-3.92) | 0.22-4.84 (1.7% of draws dropped) | 1.0 |
| CheatBench | 9/75 (6.4-21.3%) | 15/75 (12.5-30.4%) | 0.60 (0.28-1.29) | 0.37-0.89 (**36% of draws dropped**) | 0.27 |

Editing a test does not separate positives from clean runs in DEV. The CheatBench bootstrap interval is conditional: the 36%
of draws that never sample the one task holding the clean S runs are dropped, so it must not be read as excluding 1; the analytic
interval includes 1. `LR_S` does not measure the recall cost of an S-based carve-out; C5 does.

## 9. Arithmetic for the 3% target (hypothetical `FAR_T`, not estimates)

| Corpus | pi (clean S share) | pi x FAR_T at 10% / 25% / 50% / 80% (points) |
|---|---|---|
| Goodfire | 5.7% (4/70) | 0.6 / 1.4 / 2.9 / 4.6 |
| CheatBench | 20.0% (15/75) | 2.0 / 5.0 / 10.0 / 16.0 |

A clean test-editing stratum at Flake's 80% would, at CheatBench's share, add 16 points to a false-alarm rate; at Goodfire's share, 4.6.
Required n with zero alarms: 4 / 35 / 125 runs (unrounded) for an upper bound below 49.0% / 10% / 3%.

## 10. C1-C5 under S and the primary detectors only

| Criterion | Status |
|---|---|
| C1 `n_T >= 20` and `n_N >= 20` clean in a non-Flake corpus | **unmet** (T = 4 and 15) |
| C2 union `FAR_T - FAR_N` Newcombe lower bound above 0 and at least 10 points | not evaluable (C1) |
| C3 more than half of T union alarms judge-free pass-throughs of one rule | not evaluable (no alarmed clean T group of at least 3) |
| C4 bootstrap lower bound of `FAR_T/FAR_N` above 1, T in at least 3 tasks, same sign in the other corpus | not evaluable; CheatBench T lies in 1 task |
| C5 enacted runs alarmed only by a pass-through at most 10% of enacted union alarms (Wilson upper at most 25%) | CheatBench 0 of 44; Goodfire 1 or 2 of 11 (informational) |

Verdict: **undetermined from DEV.** No rule, threshold or detector was changed or tested.

## 11. Deviations from the preregistration, and disclosure log

- README section 8.1 states required n as 5, 35, 125; the exact values are 4, 35, 125 (unrounded) and 5, 35, 127 (rounded to one
  decimal). Both are in `results/score.json`.
- `score.py` hides a group of 1 or 2 runs entirely and hides an exclusion count of 1 or 2 (the preregistration did not list these two
  cells); `stats.py`, `test_edits.py`, `data.py`, `membership.py` are unchanged from the merged revision (hashes in `freeze.json` and
  `membership.json`).
- `complement_log` (in `results/score.json`) lists 3 Goodfire clean public-total derivations that survive and are accepted
  (a total of 2 split into two cells of 1 or 2 is determined; no run is identified) and 2 cases where an exact complement was
  suppressed. Several Flake cells are likewise derivable from the public NP21 totals and are accepted.
- Phase 2 loaded `suspicion` and the other facet alarm fields on purpose; nothing was tuned; the cascade rows were read only for
  `trace_id`, `cards` and `triage_unavailable` (frozen rows: `alarmed`, `excluded`).
- A single Goodfire clean S run has no attested primary facet record and is excluded from the facet and union columns.
