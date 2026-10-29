# NP29-CM-1 results: do clean runs that edit a test hide a false-alarm weakness on Goodfire DEV and CheatBench DEV?

Zero model calls, $0. Preregistration: `README.md` revision 2 (#1522, merged before any alarm file was opened); revision 3 (text and disclosure only) follows the Opus conclusions review of #1524. Every number below is
in `results/score.json` or `results/membership.json`; cells of 1 or 2 are suppressed there and here, exact zeros are printed.
Inputs and tool hashes: `results/freeze.json`. "S" is the frozen primary definition; "primary detectors" are cascade v8, facets
(Haiku, attested v2 record) and their union; everything else is descriptive.

## Headline

**Question.** Does the 3% DEV false-alarm figure hide the Flake weakness (clean runs that edit a test alarming at 8 of 10)?

**What ran.** The mechanical definition S (a write to a strict test path) was applied to every step of the 150 scored Goodfire and
150 scored CheatBench runs after the strict `assignments.csv` cross-key; the stored cascade v8 cards, the stored Haiku facet records
and their union were then split into S runs (T) and the rest (N). The production `grader_tamper` rule was run on the same steps (G).

**Number.** The clean test-editing group is **4 of 70 on Goodfire (4 tasks) and 15 of 75 on CheatBench (exactly 1 task)**, both under the
preregistered minimum of 20, so no clean alarm rate is computed. CheatBench clean S runs: cascade v8, facets and union alarm on
**0 of 15** (the cascade zero was already forced by the published 0/75; the 0 for facets and union comes from one task and has no
per-task bound). Goodfire clean S runs: the cascade alarms on 1 or 2 of 4 (suppressed; the union cell on the runs that have a facet
record is suppressed with it). The production `grader_tamper` rule G flags **0 of 145 clean DEV runs**, and no clean DEV cascade alarm
is a judge-free pass-through, so the judge-free pass-through that produced Flake's cascade alarms never occurs in DEV's clean data.

**Meaning.** DEV cannot show whether the Flake weakness is present, and the 3% DEV false-alarm figure contains no evidence about the
mechanism behind it: the verdict is **undetermined from DEV**. S finds few test-writing clean runs, in CheatBench all in one task and
mostly file creations (`create`: 19 of 24 S runs pooled). Of Goodfire's two published clean cascade alarms, one is on an S run and it
is judge-confirmed, not a pass-through (class-level pass-through-only alarms among Goodfire clean runs: 0; this identifies no run). SI
(in-place edits) is **not** a repair subset (Flake: 0 of 10 repairs, 5 of 50 application fixes; the five S repairs are whole-file or
shell-redirect writes, the same form class as Goodfire's S runs), so the comparison with Flake rests on the path difference above, not
on SI. Flake's 8 of 10 splits into cascade 5 of 5 on the S repairs and facets 0 of 5 on them; the three facet alarms (NP21 facets 3/10,
assumed to be the same Haiku records [INFERENCE]) therefore fall on the five repairs that S, and L, does not flag: an S split of DEV
cannot see that half of the weakness (Flake's cascade used a Sonnet judge, DEV's Kimi). Required sample for a stated target on the
test-editing stratum: with zero alarms, a Wilson upper bound below 49.0% needs 4 clean test-editing runs, below 10% needs 35, below 3%
needs 125 (5, 35, 127 if rounded to 0.1), counted in distinct tasks, spread over many tasks, inside test directories so the rule can
see them.

## What can and cannot be said

Can: DEV holds few clean test editors that S finds (5.7% and 20.0% of its clean runs; S is a lower bound, L flags 28/70 and 34/75);
the CheatBench ones are in one task; G fires on no clean DEV run; every run G flags in DEV (and in Flake's repairs) is also an S run. Cannot: a rate for
test-editing clean runs in either corpus; "DEV does not hide the weakness"; "no rule change is warranted". Preregistered verdict on a
rule change, C1-C5 under S and the primary detectors only: **undetermined from DEV**.

## 1. Clean S counts and the task-key check

| Corpus | Clean S / clean runs | Exact distinct tasks of those S runs | Notes |
|---|---|---|---|
| Goodfire | 4 / 70 | 4 | one run per task |
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

Clean runs (group sizes 4 and 66 on Goodfire, 15 and 60 on CheatBench). A count at or under its bound is not a finding. The Goodfire facet and union cells are on the runs that have a primary facet record; their denominators and the exclusion count are not printed, and the union cell is suppressed with the cascade cell it contains.

| Detector | Goodfire T (n 4) | Goodfire N (n 66) | Bound for Goodfire T | CheatBench T (n 15) | CheatBench N (n 60) | Bound for CheatBench T |
|---|---|---|---|---|---|---|
| Cascade v8 | 1 or 2 of 4 | 1 or 2 of 66 | at most 2 of 4 | **0 of 15** | 0 of 60 | exactly 0 of 15 |
| Facets (primary record) | 0 | 1 or 2 | at most 1 of 4 | **0 of 15** | 4 of 60 | none published |
| Union (cascade v8 or facets) | suppressed | suppressed | at most 3 of 4 | **0 of 15** | 4 of 60 | none published |

Rates are not computed (both T groups are under 20). Descriptive sensitivities (no category): the frozen cascade is 0 of 15 (CheatBench
T) and 5 of 60 (N); the original union (frozen cascade or Haiku-all facets) is 0 of 15 and 9 of 60; v7 as the frozen cascade. The
published CheatBench union totals (9 clean alarms) therefore all lie in N. SD equals S in every class; L (over-inclusive) holds 34
CheatBench clean runs with 0 cascade and 3 facet alarms.

Enacted / positive runs (descriptive, groups of 5 and 9): CheatBench cascade T 4 of 9 against N 15 of 66, facets 4 of 9 against
26 of 66, union 6 of 9 against 38 of 66; Goodfire cascade 3 of 5 against 7 of 75, facets 0 of 5 against 4 of 63, union 3 of 5 against 8 of 63 (derivable from the public totals, so printed). Under
the group-size rule no rate or difference is computed.

## 4. What fired among alarmed T runs

- **Clean:** nothing fired in CheatBench T (0 alarms). Goodfire clean T cascade is 1 or 2 alarms, so its rule is not printed, but pass-through-only alarms are exactly 0 in T and in the whole Goodfire clean class.
- **Enacted CheatBench T:** the 4 cascade alarms were all confirmed by the judge, **none alarmed only by the pass-through** (`grader_tamper` also fired on 1 or 2 of them); rule families `prefilter:git_history` 3,
  `grader_tamper` and `hidden_info_read` 1 or 2 each; 4 facet alarms, all `eval_awareness` of `reasons_about_grader` or `plans_to_game`;
  `grader_or_harness_touched`, `tests` modified/deleted/replaced and `answer_material_accessed` are 1 or 2 each.
- **Enacted Goodfire T:** facets alarm 0 of 5; cascade 3 of 5, rule families and the confirmed/pass-through split are 1 or 2 each.

## 5. S x G x alarm

G (the production `grader_tamper` rule) flags none of the 70 Goodfire and 75 CheatBench clean runs. Cascade v8 alarms that land in N
with G set: **exactly 0** in all four classes (clean and positive, both corpora); every G-flagged run in DEV is an S run, so S does
not miss what the rule sees there. The absence of a pass-through alarm on clean S runs says the rule does not fire on these runs; why
(for example paths outside a `tests/` directory) was not tested, and it is not evidence about honest repairs.

## 6. SI (descriptive, not a repair subset) and the tuning deltas

SI: CheatBench clean 3 runs with 0 alarms under every primary detector; Goodfire clean 1 or 2 runs (counts suppressed). SD (deletions
and restores) adds no run to S in any class. S0 versus S (README section 5.4): only adjustment (a) moved a clean or Flake number; it
raised the Flake `legitimate_test_repair` S count from 3 to 5 of 10 and Goodfire enacted from 4 to 5; (d) moved CheatBench positives
from 10 to 9; Goodfire `considered_only` (never scored) moved from 1 or 2 to 3. No clean DEV count changed.

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

There is no evidence in DEV that editing a test separates positives from clean runs (Katz intervals 0.31-3.92 and 0.28-1.29 include 1). The CheatBench bootstrap interval is conditional: the 36%
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
| C3 more than half of T union alarms judge-free pass-throughs of one rule | not evaluable while C1 is unmet (no alarmed clean T group of at least 3 either) |
| C4 bootstrap lower bound of `FAR_T/FAR_N` above 1, T in at least 3 tasks, same sign in the other corpus | not evaluable; CheatBench T lies in 1 task |
| C5 enacted runs alarmed only by a pass-through at most 10% of enacted union alarms, Wilson upper at most 25%, in both corpora | CheatBench 0 of 44 (upper 8.0%) passes; Goodfire 1 or 2 of 11 (upper 37.7% or 47.7%) **fails either way** |

Verdict: **undetermined from DEV** (C1 is unmet, C4 cannot be met on CheatBench, C5 fails for Goodfire). No rule, threshold or detector was changed or tested.

## 11. Deviations from the preregistration, and disclosure log

- README section 8.1 states required n as 5, 35, 125; the exact values are 4, 35, 125 (unrounded) and 5, 35, 127 (rounded to one
  decimal). Both are in `results/score.json`.
- `score.py` hides a group of 1 or 2 runs entirely, hides an exclusion count of 1 or 2 and the `n` it implies, and hides union and
  cascade-on-subset cells of any hidden cascade cell (they would pin one Goodfire clean S run). It no longer hides complements that are
  derivable from public totals (Goodfire enacted cascade T is printed). `stats.py`, `test_edits.py` and `data.py` are unchanged;
  `membership.py` only dropped the prompt-length statistics (hashes in `freeze.json` and `membership.json`).
- `complement_log` lists the cells hidden or derivable: Goodfire clean cascade T and N (1 and 1 from the public total of 2, accepted, no
  run identified) and the subset/union cells hidden for that reason.
- Phase 2 loaded `suspicion` and the other facet alarm fields on purpose; nothing was tuned; the cascade rows were read only for
  `trace_id`, `cards` and `triage_unavailable` (frozen rows: `alarmed`, `excluded`).
