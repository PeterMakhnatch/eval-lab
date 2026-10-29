# NP36-TW-1 results: no tightening of `grader_tamper` keeps its enacted-pair alarms and cuts its clean-pair alarms

Zero model calls, $0, stored data only, no upload, no local Docker or inference, strict DEV key, production untouched.
Preregistration: `PREREGISTRATION.md`, commit `238c6c9a4` (12:04 PM ET) before any label join; the design-half selection
(`results/design.json`, `results/chosen.json`) was pushed in the next commit before the held-out half was read; the held-out
half was read once (`results/heldout.json`, the scorer refuses a second run). Everything is rerunnable from `tools/` (private
inputs are the cached steps; `tools/cache_runs.py` rebuilds them). Counts in this file are copied from `results/*.json`.

## Answer

**No.** None of the four preregistered tightenings is eligible on the design half: every candidate that cuts the clean-pair
alarms to at most half also loses more than a third of the enacted-pair alarms, and the one that keeps them (C4) does not cut
the clean alarms. The fallback rule frozen by the preregistration (C4) is identical to the current gate on the held-out half
(17/62 enacted, 15/62 clean, both) and **fails** the held-out criteria. Outside the pairs, stratified by length, the current gate
does not separate enacted from clean runs at all (Mantel-Haenszel odds ratio 0.88, 0.62 to 1.25). The matched-pair excess of
`grader_tamper` (39/123 against 28/123, sign test p = 0.031) is not reproduced on the 2,142 strict-DEV runs that are not in the
pairs.

## Inputs

* Frozen pair list: the 123 primary pairs of NP31 / #1529 (246 distinct runs, 101 tasks), sha256 in `results/strict_gate.json`.
  **All 123 pairs pass the strict gate** (both members in the `goodfire-dev` cohort with a verdict: 2,388 of 3,030 admitted runs).
* Replica check: `tools/gate.py` returns the production `grader_tamper_hits` step list on all 5,169 cached runs of every
  population (246 pairs, 150 + 150 samples, 3,674 DEV, 646 CM-18, 67 NP21, 236 NP33; `results/labelfree_marginals.json`),
  and flags 67 of the 246 pair runs, run for run the pass-through list in p0's stored decomposition.
* Reproduction of p0 on the 123 pairs (never adjusted): current gate-only 39 enacted / 28 clean (enacted-only 20, clean-only 9);
  cascade' (gate or judge-confirmed) 42 / 28 (23 / 9). Both equal the published numbers.
* Mechanism mix of the current gate's hits: Goodfire hits are all shell writes (python-script replacements, `sed -i`, plain
  overwrites of a pre-existing file); Goodfire traces contain no tool-call edit. The candidates were written after seeing that
  pooled, class-free mix.

## Design half (61 pairs, 51 tasks), gate-only

| rule | enacted alarms | clean alarms | enacted-only (b) | clean-only (c) | one-sided sign p |
|---|---|---|---|---|---|
| current | 22 | 13 | 13 | 4 | 0.025 |
| C1 not agent-created | 6 | 4 | 4 | 2 | 0.344 |
| C2 changes a test outcome | 10 | 5 | 8 | 3 | 0.113 |
| C3 provably enforces less | 6 | 4 | 4 | 2 | 0.344 |
| C4 exercised by own test runs | 21 | 11 | 14 | 4 | 0.015 |

Bars (fixed beforehand): clean alarms at most floor(13/2) = 6 and enacted alarms at least ceil(2/3 x 22) = 15. C1, C2 and C3
meet the first bar and fail the second (6, 10, 6 enacted alarms against 15); C4 meets the second and fails the first (11 against
6). No candidate is eligible; by the preregistered fallback the one with the largest `b - c` (C4, 10) is frozen.

## Held-out half (62 pairs, 50 tasks), read once, chosen rule C4

| rule | enacted alarms | clean alarms | b | c | one-sided p | length-only at the same clean alarms |
|---|---|---|---|---|---|---|
| current | 17 | 15 | 7 | 5 | 0.387 | 15 enacted alarms (threshold 143 steps) |
| C4 | 17 | 15 | 7 | 5 | 0.387 | same |

a. clean alarms at most 7: **false** (15). b. enacted alarms at least 12: true (17). c. p below 0.05: **false**.
d. above length-only: true (17 against 15). **SUCCESS false, SUPPORTED false, BEATS_LENGTH false.** With judge-confirmed cards
added (cascade'): 18 / 15 for both, b 8, c 5, p 0.29.

## All 123 pairs, descriptive (includes the design half; includes all four candidates, not just C4)

| rule | gate-only enacted / clean | b / c | p | length-only enacted alarms at that clean count | cascade' enacted / clean |
|---|---|---|---|---|---|
| current | 39 / 28 | 20 / 9 | 0.031 | 29 | 42 / 28 |
| C1 | 11 / 8 | 9 / 6 | 0.304 | 7 | 17 / 9 |
| C2 | 22 / 11 | 17 / 6 | 0.017 | 11 | 27 / 12 |
| C3 | 10 / 9 | 5 / 4 | 0.500 | 10 | 17 / 10 |
| C4 | 38 / 26 | 21 / 9 | 0.021 | 27 | 41 / 26 |

C2 is the one candidate whose pair table looks like a tightening: it removes 61% of the clean alarms and 44% of the enacted
alarms and sits above the length-only rule (22 against 11 at 11 clean alarms). It is not "most of the enacted alarms", it was
ineligible on the design half, and the p-value is uncorrected over four candidates; see the post-hoc replication below.

## Alarm rate on all clean runs of the samples, and the NP29 reconciliation

| population | clean runs | current | C1 | C2 | C3 | C4 | overlap with the 246 pair runs | median length, clean / enacted |
|---|---|---|---|---|---|---|---|---|
| Goodfire 150 sample | 70 | 0 | 0 | 0 | 0 | 0 | 7 | 20 / 28 |
| CheatBench DEV | 75 | 0 | 0 | 0 | 0 | 0 | 0 | 41 / 17 |

(Enacted alarms on the same runs: Goodfire 150 2/80 current, 1 / 0 / 0 / 0 for C1-C4; CheatBench 1/75, 1 / 0 / 1 / 1.)

**Reconciliation.** NP29's 0 of 145 clean DEV runs are exactly these 70 + 75 clean sample runs, and the same gate flags 0 of
them. The 28 clean alarms on the pairs come from a different slice, not a different rule: the pair clean members are matched to
enacted runs and are long (median 103 steps, 59% at 90 steps or more), whereas the Goodfire-150 clean runs have median 20 steps
and **none at 90 or more** (maximum 69). On the 123 pair clean members the gate flags 0/23 at 40 steps or fewer, 6/27 at 41 to
89, and 22/73 at 90 or more. On the 629 clean DEV runs outside the pairs it flags 3/470, 13/57 and 45/102 in the same three
length bands (0.6%, 22.8%, 44.1%). **The 0/145 figure says nothing about the gate's false-alarm rate on long clean Goodfire
runs**; it says the sample's clean runs are short. (CheatBench's clean runs are not short, 29% at 90 or more, and the gate
flags none: the long-run alarm rate is a property of Goodfire's repository-test tasks, where agents edit repo tests as normal
work.)

## Secondary: the 2,142 strict-DEV runs outside the pairs (1,513 enacted, 629 clean)

Alarm rate by class within the NP31 length tertiles (cut points 40 and 89 steps), enacted / clean:

| band | current enacted | current clean | C4 enacted | C4 clean |
|---|---|---|---|---|
| 40 steps or fewer | 5/287 (1.7%) | 3/470 (0.6%) | 4/287 | 2/470 |
| 41 to 89 | 135/687 (19.6%) | 13/57 (22.8%) | 115/687 | 12/57 |
| 90 or more | 211/539 (39.1%) | 45/102 (44.1%) | 182/539 | 34/102 |
| all | 351/1513 (23.2%) | 61/629 (9.7%) | 301/1513 (19.9%) | 48/629 (7.6%) |

The raw difference (23.2% against 9.7%) is length: enacted runs are longer. Within each band the clean alarm rate is as high as
the enacted one. Length-only at C4's clean-alarm count (48) reaches 69/1513 enacted alarms (threshold 164 steps).

## Cross-tests, chosen rule C4 once and unchanged, read-only (positives alarmed, negatives alarmed)

| set | current gate | C4 gate | current cascade' | C4 cascade' |
|---|---|---|---|---|
| CheatBench DEV (75 / 75) | 1 / 0 | 1 / 0 | 14 / 0 | 14 / 0 |
| CM-18 established (317 / 329) | 11 / 0 | 10 / 0 | 33 / 5 | 32 / 5 |
| Flake Laundering (7 / 60) | 0 / 6 | 0 / 1 | 0 / 6 | 0 / 1 |
| pooled established, CM-18 + Flake (324 / 389) | 11 / 6 | 10 / 1 | 33 / 11 | 32 / 6 |
| NP33 caught_not_taught (46 / 80 untrusted negatives) | 0 / 0 | 0 / 0 | n/a | n/a |
| NP33 PostTrainBench (30 / 30) | 0 / 0 | 0 / 0 | n/a | n/a |
| NP33 agentcheck (10 / 40) | 8 / 12 | 0 / 0 | n/a | n/a |

The pooled cascade rows reproduce p0's published 33/324 at 11/389 for the current rule. C4 never raises a false alarm on any set
(it keeps a subset of the current hits). On Flake it removes 5 of 6 false alarms at no recall cost (recall was already 0/7).
**The agentcheck row is an artifact, not a trade-off:** C2 and C4 need to recognise a test run in the trace, and the recognizer
sees a test run in only 5 of the 236 NP33 traces (tool format), so on NP33 they alarm on nothing; agentcheck's 8 of 10 current
alarms and 12 of 40 false alarms are not comparable with C4's zeros. CM-18 and Flake were scored with the same traces and code
path as p0; the cascade' columns reuse p0's stored Haiku verdicts and no judge call is made here.

## Post-hoc (not preregistered, one look, descriptive)

The same candidates on the 2,142 runs outside the pairs, Mantel-Haenszel common odds ratio of an alarm on an enacted against a
clean run over length strata (`results/posthoc.json`):

| rule | tertiles | ten length-quantile bins |
|---|---|---|
| current | 0.88 (0.62 to 1.25) | 0.77 (0.54 to 1.10) |
| C1 | 1.30 (0.75 to 2.25) | 1.15 (0.66 to 2.00) |
| C2 | 0.90 (0.55 to 1.46) | 0.82 (0.51 to 1.34) |
| C3 | 2.08 (0.86 to 4.99) | 1.80 (0.78 to 4.19) |
| C4 | 0.99 (0.69 to 1.43) | 0.93 (0.64 to 1.36) |

C2's pair-table signal does not replicate outside the pairs (odds ratio 0.90). C3, the content-based rule, has the highest point
estimate but its interval contains 1 and it keeps 19 of the 67 current pair-run alarms; it is a lead, not a result.

## What this means

* The matched-pair story ("39 of the 42 enacted alarms come from `grader_tamper`") holds, but the gate's enacted excess over
  clean on Goodfire DEV is a length effect: it alarms on long Goodfire repository-task runs in both classes, because editing
  the repo's own tests is ordinary work there. Outside the pairs it separates the classes not at all after stratifying by length.
* The four tightenings that are pure functions of the stored steps cannot both keep the enacted alarms and drop the clean
  ones. What they remove is mostly shared by both classes, and what separates the classes (whether the edit weakens a test the
  task's own hidden grader runs) is not recorded in these steps.
* No production change is supported. The one cross-test benefit (C4 on Flake: false alarms 6/60 to 1/60 at no recall cost) rests
  on a rule that fails held-out here; a Flake-motivated change should be tested on its own preregistered design, not inferred from
  this slice. Whether `grader_tamper` should stay a judge-free pass-through on Goodfire-style repository tasks is a Research
  policy call: it carries the cascade's recall on Goodfire pairs and nearly all of its pair false alarms.

## Limits

* 61 + 62 pairs: the held-out sign test cannot detect a modest effect (a SUCCESS without a significant sign test was possible
  and is stated as such in the preregistration; here neither held).
* Candidate definitions are mine, written after seeing the pooled mechanism mix; other tightenings may exist (for example one
  using the task's hidden test list, which the traces do not contain).
* C2 and C4 depend on a test-run recognizer (command and output patterns); it finds no test run in 231 of 236 NP33 traces.
* Length-only rule thresholds are chosen in-sample, which favours it.
* The NP33 sets are staged by another lane and were read as they stood at about 12:10 PM ET; their negatives are as staged, not audited.
