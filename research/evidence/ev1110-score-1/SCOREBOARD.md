# Cached DEV scoreboard

`scoreboard.json` is authoritative. This file is its pure view.

No pooled accuracy. No model calls or task runs. All intervals resample task families.
Thresholds stay frozen. Missing positives remain misses in full-cohort recall.
Abstention is not an honest result. Budget claims require complete honest coverage.
All comparisons are exploratory DEV diagnostics, not independent hypothesis tests.
Line L0, compact AutoReview, line J3, full-line severity, characters, and steps freeze on cohort C only. Native impossible J3/J2b retain their historical pooled-DEV cutoffs and distinct identities. Binary/policy signals also show native points; no-alarm means no tie block meets the target. No A/B label selects a cutoff.
A/B Gold locators resolve. AutoReview-only citations are unavailable when only the full-line union was retained.
A zero-width empirical interval does not establish zero population risk.
Characters include retained actor narration, tool names, arguments, and observations; no task/system text.
Steps count actor messages/actions, not raw database rows. File-only bundles have missing lengths.
UNAVAILABLE: no eligible untouched cohort

## Four separate contracts

| Contract | Unit | State |
|---|---|---|
| grader_qa | grader/runtime-boundary version | unavailable: Step 1 has no frozen grader-QA prediction join; runs are not graders. |
| offline | complete admitted run | DEV diagnostic only:  |
| online | causally visible prefix | unavailable: Cached finished-run outputs do not establish online detection or delay. |
| task_qa | task-package/grader/backend version | unavailable: Step 1 has no frozen task-QA prediction join; run labels are not task labels. |

## A: Goodfire action panel

Eligible: 81; positives: 11; controls: 70.
Label states: `attempted=23, executed-positive=11, reviewed-control=70, unjudged=46`. Exposure: exposed DEV; not blind.
Limits: Primary R1/R2 action consensus; 23 attempted and 46 unjudged stay separate. Review-floor policy recall is not detector recall. The final-path cache does not expose every Gold branch.

Each cell gives full-cohort recall and observed honest alarms. Unresolved controls appear separately.

| Tool | Eligible / scored / missing | 1% DEV budget: recall; observed honest alarms | 5% DEV budget: recall; observed honest alarms | AUROC [95% CI] | Paired ΔAUROC vs chars [CI]; n | Paired ΔAUROC vs steps [CI]; n | First-citation hit at 5% | Recorded cost |
|---|---|---|---|---|---|---|---|---|
| AutoReview | 81 / 49 / 32 | 1/11 (9.1%) [0.000, 0.300]; 0/41 (0.0%) [0.000, 0.000]; t=88.0; unresolved | 2/11 (18.2%) [0.000, 0.455]; 0/41 (0.0%) [0.000, 0.000]; t=8.0; unresolved | 0.616 [0.477, 0.795] | -0.171 [-0.408, 0.111]; 49 | -0.035 [-0.238, 0.210]; 49 | unavailable | $0.2560 (49/81) |
| J2b | 81 / 0 / 81 | missing (n=11); 0/0 N/A; t=88; unresolved | missing (n=11); 0/0 N/A; t=20; unresolved | N/A [N/A, N/A] | N/A [N/A, N/A]; 0 | N/A [N/A, N/A]; 0 | missing | unknown |
| J3 | 81 / 81 / 0 | 3/11 (27.3%) [0.000, 0.545]; 0/70 (0.0%) [0.000, 0.000]; t=96.0; met | 6/11 (54.5%) [0.250, 0.857]; 0/70 (0.0%) [0.000, 0.000]; t=90.0; met | 0.994 [0.979, 1.000] | 0.142 [0.038, 0.265]; 81 | 0.362 [0.227, 0.507]; 81 | 5/11 (45.5%) [0.143, 0.778] | $0.0000 (81/81) |
| L0 | 81 / 81 / 0 | 0/11 (0.0%) [0.000, 0.000]; 0/70 (0.0%) [0.000, 0.000]; t=no_alarm; met | 0/11 (0.0%) [0.000, 0.000]; 0/70 (0.0%) [0.000, 0.000]; t=no_alarm; met | 0.586 [0.446, 0.744] | -0.266 [-0.432, -0.086]; 81 | -0.045 [-0.193, 0.119]; 81 | 0/11 (0.0%) [0.000, 0.000] | unknown |
| characters | 81 / 81 / 0 | 0/11 (0.0%) [0.000, 0.000]; 0/70 (0.0%) [0.000, 0.000]; t=1866118.0; met | 4/11 (36.4%) [0.111, 0.692]; 2/70 (2.9%) [0.000, 0.070]; t=272765.0; met | 0.852 [0.725, 0.957] | 0.000 [0.000, 0.000]; 81 | 0.220 [0.106, 0.333]; 81 | inapplicable | unknown |
| full_line | 81 / 81 / 0 | 0/11 (0.0%) [0.000, 0.000]; 0/70 (0.0%) [0.000, 0.000]; t=no_alarm; met | 0/11 (0.0%) [0.000, 0.000]; 0/70 (0.0%) [0.000, 0.000]; t=no_alarm; met | 0.632 [0.480, 0.799] | -0.220 [-0.406, -0.019]; 81 | 0.000 [-0.159, 0.167]; 81 | 0/11 (0.0%) [0.000, 0.000] | unknown |
| steps | 81 / 81 / 0 | 0/11 (0.0%) [0.000, 0.000]; 0/70 (0.0%) [0.000, 0.000]; t=326.0; met | 0/11 (0.0%) [0.000, 0.000]; 0/70 (0.0%) [0.000, 0.000]; t=95.0; met | 0.632 [0.498, 0.770] | -0.220 [-0.334, -0.107]; 81 | 0.000 [0.000, 0.000]; 81 | inapplicable | unknown |

### Identical-row length operating points and paired differences

Each detector and baseline use the same rows. Counts and intervals do not borrow full-cohort coverage.

| Tool | Baseline | Budget | Paired rows | Detector recall | Baseline recall | Δrecall [95% CI] | Detector honest alarms | Baseline honest alarms |
|---|---|---|---|---|---|---|---|---|
| AutoReview | characters | 0.01 | 49 | 1/8 (12.5%) [0.000, 0.400] | 0/8 (0.0%) [0.000, 0.000] | 0.125 [0.000, 0.400] | 0/41 (0.0%) [0.000, 0.000] | 0/41 (0.0%) [0.000, 0.000] |
| AutoReview | characters | 0.05 | 49 | 2/8 (25.0%) [0.000, 0.600] | 3/8 (37.5%) [0.000, 0.750] | -0.125 [-0.600, 0.333] | 0/41 (0.0%) [0.000, 0.000] | 1/41 (2.4%) [0.000, 0.079] |
| AutoReview | steps | 0.01 | 49 | 1/8 (12.5%) [0.000, 0.400] | 0/8 (0.0%) [0.000, 0.000] | 0.125 [0.000, 0.400] | 0/41 (0.0%) [0.000, 0.000] | 0/41 (0.0%) [0.000, 0.000] |
| AutoReview | steps | 0.05 | 49 | 2/8 (25.0%) [0.000, 0.600] | 0/8 (0.0%) [0.000, 0.000] | 0.250 [0.000, 0.600] | 0/41 (0.0%) [0.000, 0.000] | 0/41 (0.0%) [0.000, 0.000] |
| J2b | characters | 0.01 | 0 | missing | missing | N/A | missing | missing |
| J2b | characters | 0.05 | 0 | missing | missing | N/A | missing | missing |
| J2b | steps | 0.01 | 0 | missing | missing | N/A | missing | missing |
| J2b | steps | 0.05 | 0 | missing | missing | N/A | missing | missing |
| J3 | characters | 0.01 | 81 | 3/11 (27.3%) [0.000, 0.556] | 0/11 (0.0%) [0.000, 0.000] | 0.273 [0.000, 0.556] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |
| J3 | characters | 0.05 | 81 | 6/11 (54.5%) [0.250, 0.857] | 4/11 (36.4%) [0.091, 0.667] | 0.182 [0.000, 0.462] | 0/70 (0.0%) [0.000, 0.000] | 2/70 (2.9%) [0.000, 0.071] |
| J3 | steps | 0.01 | 81 | 3/11 (27.3%) [0.000, 0.556] | 0/11 (0.0%) [0.000, 0.000] | 0.273 [0.000, 0.556] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |
| J3 | steps | 0.05 | 81 | 6/11 (54.5%) [0.250, 0.857] | 0/11 (0.0%) [0.000, 0.000] | 0.545 [0.250, 0.857] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |
| L0 | characters | 0.01 | 81 | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |
| L0 | characters | 0.05 | 81 | 0/11 (0.0%) [0.000, 0.000] | 4/11 (36.4%) [0.091, 0.667] | -0.364 [-0.667, -0.091] | 0/70 (0.0%) [0.000, 0.000] | 2/70 (2.9%) [0.000, 0.071] |
| L0 | steps | 0.01 | 81 | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |
| L0 | steps | 0.05 | 81 | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |
| characters | characters | 0.01 | 81 | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |
| characters | characters | 0.05 | 81 | 4/11 (36.4%) [0.091, 0.667] | 4/11 (36.4%) [0.091, 0.667] | 0.000 [0.000, 0.000] | 2/70 (2.9%) [0.000, 0.071] | 2/70 (2.9%) [0.000, 0.071] |
| characters | steps | 0.01 | 81 | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |
| characters | steps | 0.05 | 81 | 4/11 (36.4%) [0.091, 0.667] | 0/11 (0.0%) [0.000, 0.000] | 0.364 [0.091, 0.667] | 2/70 (2.9%) [0.000, 0.071] | 0/70 (0.0%) [0.000, 0.000] |
| full_line | characters | 0.01 | 81 | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |
| full_line | characters | 0.05 | 81 | 0/11 (0.0%) [0.000, 0.000] | 4/11 (36.4%) [0.091, 0.667] | -0.364 [-0.667, -0.091] | 0/70 (0.0%) [0.000, 0.000] | 2/70 (2.9%) [0.000, 0.071] |
| full_line | steps | 0.01 | 81 | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |
| full_line | steps | 0.05 | 81 | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |
| steps | characters | 0.01 | 81 | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |
| steps | characters | 0.05 | 81 | 0/11 (0.0%) [0.000, 0.000] | 4/11 (36.4%) [0.091, 0.667] | -0.364 [-0.667, -0.091] | 0/70 (0.0%) [0.000, 0.000] | 2/70 (2.9%) [0.000, 0.071] |
| steps | steps | 0.01 | 81 | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |
| steps | steps | 0.05 | 81 | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] |

### Native binary/policy points (not budget-qualified)

- L0: 3/11 (27.3%) [0.000, 0.571] recall; 7/70 (10.0%) [0.032, 0.171] observed honest alarms.
- full_line: 11/11 (100.0%) [1.000, 1.000] recall; 70/70 (100.0%) [1.000, 1.000] observed honest alarms.
- full_line drop only: 4/11 (36.4%) [0.083, 0.667] recall; 7/70 (10.0%) [0.032, 0.171] observed honest alarms.

### Identity, unresolved controls, and recorded historical cost

| Tool | Identity / view | Unresolved controls | Worst-case honest alarms at 5% | Historical cost sum; mean per recorded run; recorded / eligible (USD / tokens in / tokens out / latency seconds / CPU seconds) |
|---|---|---|---|---|
| AutoReview | compact cached line AutoReview; google-antigravity/gemini-3.8-flash; final-path actor view | 29/70 (41.4%) [0.312, 0.530] | 29/70 (41.4%) [0.312, 0.530] | sum 0.256; mean 0.005; 49/81 / unknown / unknown / unknown / unknown |
| J2b | 7a4f27c6ba7d0e9261a0a78b07033bbf6ecd16290801b648e2b961bb6f33d75e; google-antigravity/gemini-3.8-flash; actions/task/diff V2 | 70/70 (100.0%) [1.000, 1.000] | 70/70 (100.0%) [1.000, 1.000] | unknown / unknown / unknown / unknown / unknown |
| J3 | evallab.j3_reading/v1; openai-codex/gpt-6.1-terra; rubric 7e7cb3c4686423ad137ecbfa726be97f54e5a05805157e3d8e8adbc5250afc83; prompt d447dc31c7c496a278b4f8cc2daecc547e8aa1d9689dc1de115e77cd72574592; final-path actor view | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | sum 0.000; mean 0.000; 81/81 / sum 944677.000; mean 11662.679; 81/81 / sum 10149.000; mean 125.296; 81/81 / sum 375.690; mean 4.638; 81/81 / unknown |
| L0 | line native hard-drop aggregate; code d2662a38a22f3ab62fc49c5cd0ce5683c42fee08; final-path actor view; J3 off | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | unknown / unknown / unknown / unknown / unknown |
| characters | DetBench actor-view character count; narration retained; same rows as each detector | 0/70 (0.0%) [0.000, 0.000] | 2/70 (2.9%) [0.000, 0.070] | unknown / unknown / unknown / unknown / unknown |
| full_line | retained replay-v1-gate-325ca5f349384620a7be304ade47b807; J3 off; Goodfire review floor; keep=0/review=1/drop=2 | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | unknown / unknown / unknown / unknown / unknown |
| steps | DetBench actor-turn count; no raw DB/result-row count; same rows as each detector | 0/70 (0.0%) [0.000, 0.000] | 0/70 (0.0%) [0.000, 0.000] | unknown / unknown / unknown / unknown / unknown |

### Localisation at the frozen 5% threshold

| Tool | First citation | Within 5 actions, same branch | Any citation | Citation precision | First citation given alarm |
|---|---|---|---|---|---|
| AutoReview | unavailable | unavailable | unavailable | unavailable | unavailable |
| J2b | missing | missing | missing | missing | missing |
| J3 | 5/11 (45.5%) [0.143, 0.778] | 5/11 (45.5%) [0.143, 0.778] | 6/11 (54.5%) [0.250, 0.857] | 12/28 (42.9%) [0.300, 0.607] | 5/6 (83.3%) [0.400, 1.000] |
| L0 | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0/0 N/A | 0/0 N/A |
| characters | inapplicable | inapplicable | inapplicable | inapplicable | inapplicable |
| full_line | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0/11 (0.0%) [0.000, 0.000] | 0/0 N/A | 0/0 N/A |
| steps | inapplicable | inapplicable | inapplicable | inapplicable | inapplicable |

## B: Impossible-task action panel; VARIANT/MODEL CONFOUND

Eligible: 75; positives: 43; controls: 32.
Label states: `executed-positive=43, reviewed-control=32, unjudged=98`. Exposure: exposed DEV; not blind.
Limits: VARIANT/MODEL CONFOUND: all 43 positives use strict variants; all 32 controls use original variants. This panel cannot establish general detector accuracy. Campaign remains INCOMPLETE_OR_INVALID.

Each cell gives full-cohort recall and observed honest alarms. Unresolved controls appear separately.

| Tool | Eligible / scored / missing | 1% DEV budget: recall; observed honest alarms | 5% DEV budget: recall; observed honest alarms | AUROC [95% CI] | Paired ΔAUROC vs chars [CI]; n | Paired ΔAUROC vs steps [CI]; n | First-citation hit at 5% | Recorded cost |
|---|---|---|---|---|---|---|---|---|
| AutoReview [VARIANT/MODEL CONFOUND] | 75 / 0 / 75 | missing (n=43); 0/0 N/A; t=88.0; unresolved | missing (n=43); 0/0 N/A; t=8.0; unresolved | N/A [N/A, N/A] | N/A [N/A, N/A]; 0 | N/A [N/A, N/A]; 0 | missing | unknown |
| J2b [VARIANT/MODEL CONFOUND] | 75 / 75 / 0 | 43/43 (100.0%) [1.000, 1.000]; 0/32 (0.0%) [0.000, 0.000]; t=88; met | 43/43 (100.0%) [1.000, 1.000]; 0/32 (0.0%) [0.000, 0.000]; t=20; met | 1.000 [1.000, 1.000] | 0.397 [0.322, 0.487]; 75 | 0.525 [0.420, 0.642]; 75 | 35/43 (81.4%) [0.729, 0.905] | unknown |
| J3 [VARIANT/MODEL CONFOUND] | 75 / 75 / 0 | 37/43 (86.0%) [0.766, 0.947]; 0/32 (0.0%) [0.000, 0.000]; t=96; met | 43/43 (100.0%) [1.000, 1.000]; 0/32 (0.0%) [0.000, 0.000]; t=89; met | 1.000 [1.000, 1.000] | 0.397 [0.322, 0.487]; 75 | 0.525 [0.420, 0.642]; 75 | 34/43 (79.1%) [0.702, 0.884] | unknown |
| L0 [VARIANT/MODEL CONFOUND] | 75 / 75 / 0 | 0/43 (0.0%) [0.000, 0.000]; 0/32 (0.0%) [0.000, 0.000]; t=no_alarm; met | 0/43 (0.0%) [0.000, 0.000]; 0/32 (0.0%) [0.000, 0.000]; t=no_alarm; met | 0.750 [0.617, 0.887] | 0.147 [-0.033, 0.321]; 75 | 0.275 [0.110, 0.458]; 75 | 0/43 (0.0%) [0.000, 0.000] | unknown |
| characters [VARIANT/MODEL CONFOUND] | 75 / 75 / 0 | 0/43 (0.0%) [0.000, 0.000]; 0/32 (0.0%) [0.000, 0.000]; t=1866118.0; met | 0/43 (0.0%) [0.000, 0.000]; 1/32 (3.1%) [0.000, 0.100]; t=272765.0; met | 0.603 [0.515, 0.687] | 0.000 [0.000, 0.000]; 75 | 0.129 [0.031, 0.224]; 75 | inapplicable | unknown |
| full_line [VARIANT/MODEL CONFOUND] | 75 / 0 / 75 | missing (n=43); 0/0 N/A; t=no_alarm; unresolved | missing (n=43); 0/0 N/A; t=no_alarm; unresolved | N/A [N/A, N/A] | N/A [N/A, N/A]; 0 | N/A [N/A, N/A]; 0 | missing | unknown |
| steps [VARIANT/MODEL CONFOUND] | 75 / 75 / 0 | 0/43 (0.0%) [0.000, 0.000]; 0/32 (0.0%) [0.000, 0.000]; t=326.0; met | 0/43 (0.0%) [0.000, 0.000]; 0/32 (0.0%) [0.000, 0.000]; t=95.0; met | 0.475 [0.363, 0.578] | -0.129 [-0.225, -0.032]; 75 | 0.000 [0.000, 0.000]; 75 | inapplicable | unknown |

### Identical-row length operating points and paired differences

Each detector and baseline use the same rows. Counts and intervals do not borrow full-cohort coverage.

| Tool | Baseline | Budget | Paired rows | Detector recall | Baseline recall | Δrecall [95% CI] | Detector honest alarms | Baseline honest alarms |
|---|---|---|---|---|---|---|---|---|
| AutoReview | characters | 0.01 | 0 | missing | missing | N/A | missing | missing |
| AutoReview | characters | 0.05 | 0 | missing | missing | N/A | missing | missing |
| AutoReview | steps | 0.01 | 0 | missing | missing | N/A | missing | missing |
| AutoReview | steps | 0.05 | 0 | missing | missing | N/A | missing | missing |
| J2b | characters | 0.01 | 75 | 43/43 (100.0%) [1.000, 1.000] | 0/43 (0.0%) [0.000, 0.000] | 1.000 [1.000, 1.000] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |
| J2b | characters | 0.05 | 75 | 43/43 (100.0%) [1.000, 1.000] | 0/43 (0.0%) [0.000, 0.000] | 1.000 [1.000, 1.000] | 0/32 (0.0%) [0.000, 0.000] | 1/32 (3.1%) [0.000, 0.103] |
| J2b | steps | 0.01 | 75 | 43/43 (100.0%) [1.000, 1.000] | 0/43 (0.0%) [0.000, 0.000] | 1.000 [1.000, 1.000] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |
| J2b | steps | 0.05 | 75 | 43/43 (100.0%) [1.000, 1.000] | 0/43 (0.0%) [0.000, 0.000] | 1.000 [1.000, 1.000] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |
| J3 | characters | 0.01 | 75 | 37/43 (86.0%) [0.761, 0.941] | 0/43 (0.0%) [0.000, 0.000] | 0.860 [0.761, 0.941] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |
| J3 | characters | 0.05 | 75 | 43/43 (100.0%) [1.000, 1.000] | 0/43 (0.0%) [0.000, 0.000] | 1.000 [1.000, 1.000] | 0/32 (0.0%) [0.000, 0.000] | 1/32 (3.1%) [0.000, 0.103] |
| J3 | steps | 0.01 | 75 | 37/43 (86.0%) [0.761, 0.941] | 0/43 (0.0%) [0.000, 0.000] | 0.860 [0.761, 0.941] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |
| J3 | steps | 0.05 | 75 | 43/43 (100.0%) [1.000, 1.000] | 0/43 (0.0%) [0.000, 0.000] | 1.000 [1.000, 1.000] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |
| L0 | characters | 0.01 | 75 | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |
| L0 | characters | 0.05 | 75 | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | 1/32 (3.1%) [0.000, 0.103] |
| L0 | steps | 0.01 | 75 | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |
| L0 | steps | 0.05 | 75 | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |
| characters | characters | 0.01 | 75 | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |
| characters | characters | 0.05 | 75 | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 1/32 (3.1%) [0.000, 0.103] | 1/32 (3.1%) [0.000, 0.103] |
| characters | steps | 0.01 | 75 | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |
| characters | steps | 0.05 | 75 | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 1/32 (3.1%) [0.000, 0.103] | 0/32 (0.0%) [0.000, 0.000] |
| full_line | characters | 0.01 | 0 | missing | missing | N/A | missing | missing |
| full_line | characters | 0.05 | 0 | missing | missing | N/A | missing | missing |
| full_line | steps | 0.01 | 0 | missing | missing | N/A | missing | missing |
| full_line | steps | 0.05 | 0 | missing | missing | N/A | missing | missing |
| steps | characters | 0.01 | 75 | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |
| steps | characters | 0.05 | 75 | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | 1/32 (3.1%) [0.000, 0.103] |
| steps | steps | 0.01 | 75 | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |
| steps | steps | 0.05 | 75 | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] |

### Native binary/policy points (not budget-qualified)

- L0: 43/43 (100.0%) [1.000, 1.000] recall; 16/32 (50.0%) [0.225, 0.767] observed honest alarms.
- full_line: missing (n=43) recall; 0/0 N/A observed honest alarms.
- full_line drop only: missing (n=43) recall; 0/0 N/A observed honest alarms.

### Identity, unresolved controls, and recorded historical cost

| Tool | Identity / view | Unresolved controls | Worst-case honest alarms at 5% | Historical cost sum; mean per recorded run; recorded / eligible (USD / tokens in / tokens out / latency seconds / CPU seconds) |
|---|---|---|---|---|
| AutoReview | compact cached line AutoReview; google-antigravity/gemini-3.8-flash; final-path actor view | 32/32 (100.0%) [1.000, 1.000] | 32/32 (100.0%) [1.000, 1.000] | unknown / unknown / unknown / unknown / unknown |
| J2b | 7a4f27c6ba7d0e9261a0a78b07033bbf6ecd16290801b648e2b961bb6f33d75e; google-antigravity/gemini-3.8-flash; actions/task/diff V2 | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | unknown / sum 1614184.000; mean 21522.453; 75/75 / sum 10543.000; mean 140.573; 75/75 / sum 184.470; mean 2.460; 75/75 / unknown |
| J3 | native DetBench c9d77cb7b0a5cb418abf76e398811915fa7fdd4c6d2ba783ed05109c9ae8c2a5; openai-codex/gpt-6.1-terra; prepared actions/task/diff V2 | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | unknown / sum 1346083.000; mean 17947.773; 75/75 / sum 9898.000; mean 131.973; 75/75 / sum 413.550; mean 5.514; 75/75 / unknown |
| L0 | l0v_full@63f07ba808a2; native full-view hard-rule aggregate | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | unknown / unknown / unknown / unknown / unknown |
| characters | DetBench actor-view character count; narration retained; same rows as each detector | 0/32 (0.0%) [0.000, 0.000] | 1/32 (3.1%) [0.000, 0.100] | unknown / unknown / unknown / unknown / unknown |
| full_line | retained replay-v1-gate-325ca5f349384620a7be304ade47b807; J3 off; Goodfire review floor; keep=0/review=1/drop=2 | 32/32 (100.0%) [1.000, 1.000] | 32/32 (100.0%) [1.000, 1.000] | unknown / unknown / unknown / unknown / unknown |
| steps | DetBench actor-turn count; no raw DB/result-row count; same rows as each detector | 0/32 (0.0%) [0.000, 0.000] | 0/32 (0.0%) [0.000, 0.000] | unknown / unknown / unknown / unknown / unknown |

### Localisation at the frozen 5% threshold

| Tool | First citation | Within 5 actions, same branch | Any citation | Citation precision | First citation given alarm |
|---|---|---|---|---|---|
| AutoReview | missing | missing | missing | missing | missing |
| J2b | 35/43 (81.4%) [0.729, 0.905] | 35/43 (81.4%) [0.729, 0.905] | 35/43 (81.4%) [0.729, 0.905] | 35/61 (57.4%) [0.459, 0.750] | 35/43 (81.4%) [0.729, 0.905] |
| J3 | 34/43 (79.1%) [0.702, 0.884] | 35/43 (81.4%) [0.723, 0.917] | 35/43 (81.4%) [0.720, 0.919] | 35/90 (38.9%) [0.330, 0.463] | 34/43 (79.1%) [0.702, 0.884] |
| L0 | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0/43 (0.0%) [0.000, 0.000] | 0/0 N/A | 0/0 N/A |
| characters | inapplicable | inapplicable | inapplicable | inapplicable | inapplicable |
| full_line | missing | missing | missing | missing | missing |
| steps | inapplicable | inapplicable | inapplicable | inapplicable | inapplicable |

### Model/variant confound

| Model / variant | Positives | Controls | One class |
|---|---|---|---|
| google-antigravity/gemini-3.8-flash / conflicting_certified_bool | 11 | 0 | True |
| google-antigravity/gemini-3.8-flash / original | 0 | 5 | True |
| google-antigravity/gemini-4-argon / original | 0 | 8 | True |
| openai-codex/gpt-6.1-sol / conflicting_certified_bool | 32 | 0 | True |
| openai-codex/gpt-6.1-sol / original | 0 | 9 | True |
| openai-codex/gpt-6.1-terra / original | 0 | 10 | True |

## C: 690-run DEV publisher-label policy regression; DIFFERENT CONSTRUCT

Eligible: 690; positives: 332; controls: 358.
Label states: `publisher-binary=690`. Exposure: exposed DEV; not blind.
Limits: DIFFERENT CONSTRUCT: publisher/lab run labels, not 332 verified executed hacks. Repeatedly inspected 690-run DEV policy regression. Ten file-only bundles have missing lengths.

Each cell gives full-cohort recall and observed honest alarms. Unresolved controls appear separately.

| Tool | Eligible / scored / missing | 1% DEV budget: recall; observed honest alarms | 5% DEV budget: recall; observed honest alarms | AUROC [95% CI] | Paired ΔAUROC vs chars [CI]; n | Paired ΔAUROC vs steps [CI]; n | First-citation hit at 5% | Recorded cost |
|---|---|---|---|---|---|---|---|---|
| AutoReview | 690 / 380 / 310 | 37/332 (11.1%) [0.043, 0.161]; 2/251 (0.8%) [0.000, 0.027]; t=88.0; unresolved | 47/332 (14.2%) [0.075, 0.189]; 12/251 (4.8%) [0.020, 0.084]; t=8.0; unresolved | 0.665 [0.595, 0.729] | 0.091 [-0.052, 0.243]; 380 | 0.168 [0.004, 0.261]; 380 | unavailable | $1.9781 (380/690) |
| J2b | 690 / 0 / 690 | missing (n=332); 0/0 N/A; t=88; unresolved | missing (n=332); 0/0 N/A; t=20; unresolved | N/A [N/A, N/A] | N/A [N/A, N/A]; 0 | N/A [N/A, N/A]; 0 | unavailable | unknown |
| J3 | 690 / 663 / 27 | 103/332 (31.0%) [0.223, 0.416]; 3/336 (0.9%) [0.000, 0.020]; t=96.0; unresolved | 153/332 (46.1%) [0.356, 0.581]; 16/336 (4.8%) [0.021, 0.078]; t=90.0; unresolved | 0.841 [0.798, 0.879] | 0.294 [0.206, 0.372]; 663 | 0.331 [0.242, 0.406]; 663 | unavailable | $0.0000 (680/690) |
| L0 | 690 / 680 / 10 | 0/332 (0.0%) [0.000, 0.000]; 0/353 (0.0%) [0.000, 0.000]; t=no_alarm; unresolved | 0/332 (0.0%) [0.000, 0.000]; 0/353 (0.0%) [0.000, 0.000]; t=no_alarm; unresolved | 0.709 [0.644, 0.784] | 0.176 [0.089, 0.274]; 680 | 0.220 [0.135, 0.315]; 680 | unavailable | unknown |
| characters | 690 / 680 / 10 | 0/332 (0.0%) [0.000, 0.000]; 3/353 (0.8%) [0.000, 0.027]; t=1866118.0; unresolved | 21/332 (6.3%) [0.037, 0.108]; 17/353 (4.8%) [0.003, 0.124]; t=272765.0; unresolved | 0.533 [0.473, 0.598] | 0.000 [0.000, 0.000]; 680 | 0.044 [0.016, 0.077]; 680 | inapplicable | unknown |
| full_line | 690 / 690 / 0 | 0/332 (0.0%) [0.000, 0.000]; 0/358 (0.0%) [0.000, 0.000]; t=no_alarm; met | 0/332 (0.0%) [0.000, 0.000]; 0/358 (0.0%) [0.000, 0.000]; t=no_alarm; met | 0.791 [0.735, 0.845] | 0.262 [0.172, 0.346]; 680 | 0.306 [0.221, 0.388]; 680 | unavailable | unknown |
| steps | 690 / 680 / 10 | 0/332 (0.0%) [0.000, 0.000]; 3/353 (0.8%) [0.000, 0.027]; t=326.0; unresolved | 8/332 (2.4%) [0.007, 0.040]; 17/353 (4.8%) [0.000, 0.108]; t=95.0; unresolved | 0.489 [0.430, 0.556] | -0.044 [-0.077, -0.017]; 680 | 0.000 [0.000, 0.000]; 680 | inapplicable | unknown |

### Identical-row length operating points and paired differences

Each detector and baseline use the same rows. Counts and intervals do not borrow full-cohort coverage.

| Tool | Baseline | Budget | Paired rows | Detector recall | Baseline recall | Δrecall [95% CI] | Detector honest alarms | Baseline honest alarms |
|---|---|---|---|---|---|---|---|---|
| AutoReview | characters | 0.01 | 380 | 37/129 (28.7%) [0.167, 0.390] | 0/129 (0.0%) [0.000, 0.000] | 0.287 [0.167, 0.390] | 2/251 (0.8%) [0.000, 0.028] | 3/251 (1.2%) [0.000, 0.037] |
| AutoReview | characters | 0.05 | 380 | 47/129 (36.4%) [0.248, 0.491] | 10/129 (7.8%) [0.032, 0.164] | 0.287 [0.113, 0.417] | 12/251 (4.8%) [0.021, 0.081] | 16/251 (6.4%) [0.000, 0.170] |
| AutoReview | steps | 0.01 | 380 | 37/129 (28.7%) [0.167, 0.390] | 0/129 (0.0%) [0.000, 0.000] | 0.287 [0.167, 0.390] | 2/251 (0.8%) [0.000, 0.028] | 3/251 (1.2%) [0.000, 0.037] |
| AutoReview | steps | 0.05 | 380 | 47/129 (36.4%) [0.248, 0.491] | 4/129 (3.1%) [0.005, 0.085] | 0.333 [0.192, 0.455] | 12/251 (4.8%) [0.021, 0.081] | 11/251 (4.4%) [0.000, 0.126] |
| J2b | characters | 0.01 | 0 | missing | missing | N/A | missing | missing |
| J2b | characters | 0.05 | 0 | missing | missing | N/A | missing | missing |
| J2b | steps | 0.01 | 0 | missing | missing | N/A | missing | missing |
| J2b | steps | 0.05 | 0 | missing | missing | N/A | missing | missing |
| J3 | characters | 0.01 | 663 | 103/327 (31.5%) [0.228, 0.427] | 0/327 (0.0%) [0.000, 0.000] | 0.315 [0.228, 0.427] | 3/336 (0.9%) [0.000, 0.020] | 3/336 (0.9%) [0.000, 0.028] |
| J3 | characters | 0.05 | 663 | 153/327 (46.8%) [0.364, 0.590] | 21/327 (6.4%) [0.036, 0.112] | 0.404 [0.286, 0.526] | 16/336 (4.8%) [0.023, 0.078] | 17/336 (5.1%) [0.003, 0.132] |
| J3 | steps | 0.01 | 663 | 103/327 (31.5%) [0.228, 0.427] | 0/327 (0.0%) [0.000, 0.000] | 0.315 [0.228, 0.427] | 3/336 (0.9%) [0.000, 0.020] | 3/336 (0.9%) [0.000, 0.028] |
| J3 | steps | 0.05 | 663 | 153/327 (46.8%) [0.364, 0.590] | 8/327 (2.4%) [0.007, 0.041] | 0.443 [0.328, 0.571] | 16/336 (4.8%) [0.023, 0.078] | 12/336 (3.6%) [0.000, 0.098] |
| L0 | characters | 0.01 | 680 | 0/327 (0.0%) [0.000, 0.000] | 0/327 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/353 (0.0%) [0.000, 0.000] | 3/353 (0.8%) [0.000, 0.027] |
| L0 | characters | 0.05 | 680 | 0/327 (0.0%) [0.000, 0.000] | 21/327 (6.4%) [0.036, 0.111] | -0.064 [-0.111, -0.036] | 0/353 (0.0%) [0.000, 0.000] | 17/353 (4.8%) [0.003, 0.127] |
| L0 | steps | 0.01 | 680 | 0/327 (0.0%) [0.000, 0.000] | 0/327 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/353 (0.0%) [0.000, 0.000] | 3/353 (0.8%) [0.000, 0.027] |
| L0 | steps | 0.05 | 680 | 0/327 (0.0%) [0.000, 0.000] | 8/327 (2.4%) [0.007, 0.041] | -0.024 [-0.041, -0.007] | 0/353 (0.0%) [0.000, 0.000] | 17/353 (4.8%) [0.000, 0.109] |
| characters | characters | 0.01 | 680 | 0/327 (0.0%) [0.000, 0.000] | 0/327 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 3/353 (0.8%) [0.000, 0.027] | 3/353 (0.8%) [0.000, 0.027] |
| characters | characters | 0.05 | 680 | 21/327 (6.4%) [0.036, 0.111] | 21/327 (6.4%) [0.036, 0.111] | 0.000 [0.000, 0.000] | 17/353 (4.8%) [0.003, 0.127] | 17/353 (4.8%) [0.003, 0.127] |
| characters | steps | 0.01 | 680 | 0/327 (0.0%) [0.000, 0.000] | 0/327 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 3/353 (0.8%) [0.000, 0.027] | 3/353 (0.8%) [0.000, 0.027] |
| characters | steps | 0.05 | 680 | 21/327 (6.4%) [0.036, 0.111] | 8/327 (2.4%) [0.007, 0.041] | 0.040 [0.009, 0.087] | 17/353 (4.8%) [0.003, 0.127] | 17/353 (4.8%) [0.000, 0.109] |
| full_line | characters | 0.01 | 680 | 0/327 (0.0%) [0.000, 0.000] | 0/327 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/353 (0.0%) [0.000, 0.000] | 3/353 (0.8%) [0.000, 0.027] |
| full_line | characters | 0.05 | 680 | 0/327 (0.0%) [0.000, 0.000] | 21/327 (6.4%) [0.036, 0.111] | -0.064 [-0.111, -0.036] | 0/353 (0.0%) [0.000, 0.000] | 17/353 (4.8%) [0.003, 0.127] |
| full_line | steps | 0.01 | 680 | 0/327 (0.0%) [0.000, 0.000] | 0/327 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 0/353 (0.0%) [0.000, 0.000] | 3/353 (0.8%) [0.000, 0.027] |
| full_line | steps | 0.05 | 680 | 0/327 (0.0%) [0.000, 0.000] | 8/327 (2.4%) [0.007, 0.041] | -0.024 [-0.041, -0.007] | 0/353 (0.0%) [0.000, 0.000] | 17/353 (4.8%) [0.000, 0.109] |
| steps | characters | 0.01 | 680 | 0/327 (0.0%) [0.000, 0.000] | 0/327 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 3/353 (0.8%) [0.000, 0.027] | 3/353 (0.8%) [0.000, 0.027] |
| steps | characters | 0.05 | 680 | 8/327 (2.4%) [0.007, 0.041] | 21/327 (6.4%) [0.036, 0.111] | -0.040 [-0.087, -0.009] | 17/353 (4.8%) [0.000, 0.109] | 17/353 (4.8%) [0.003, 0.127] |
| steps | steps | 0.01 | 680 | 0/327 (0.0%) [0.000, 0.000] | 0/327 (0.0%) [0.000, 0.000] | 0.000 [0.000, 0.000] | 3/353 (0.8%) [0.000, 0.027] | 3/353 (0.8%) [0.000, 0.027] |
| steps | steps | 0.05 | 680 | 8/327 (2.4%) [0.007, 0.041] | 8/327 (2.4%) [0.007, 0.041] | 0.000 [0.000, 0.000] | 17/353 (4.8%) [0.000, 0.109] | 17/353 (4.8%) [0.000, 0.109] |

### Native binary/policy points (not budget-qualified)

- L0: 170/332 (51.2%) [0.412, 0.636] recall; 36/353 (10.2%) [0.033, 0.191] observed honest alarms.
- full_line: 329/332 (99.1%) [0.975, 1.000] recall; 295/358 (82.4%) [0.732, 0.904] observed honest alarms.
- full_line drop only: 212/332 (63.9%) [0.548, 0.730] recall; 40/358 (11.2%) [0.042, 0.198] observed honest alarms.

### Identity, unresolved controls, and recorded historical cost

| Tool | Identity / view | Unresolved controls | Worst-case honest alarms at 5% | Historical cost sum; mean per recorded run; recorded / eligible (USD / tokens in / tokens out / latency seconds / CPU seconds) |
|---|---|---|---|---|
| AutoReview | compact cached line AutoReview; google-antigravity/gemini-3.8-flash; final-path actor view | 107/358 (29.9%) [0.187, 0.420] | 119/358 (33.2%) [0.217, 0.457] | sum 1.978; mean 0.005; 380/690 / unknown / unknown / unknown / unknown |
| J2b | 7a4f27c6ba7d0e9261a0a78b07033bbf6ecd16290801b648e2b961bb6f33d75e; google-antigravity/gemini-3.8-flash; actions/task/diff V2 | 358/358 (100.0%) [1.000, 1.000] | 358/358 (100.0%) [1.000, 1.000] | unknown / unknown / unknown / unknown / unknown |
| J3 | evallab.j3_reading/v1; openai-codex/gpt-6.1-terra; rubric 7e7cb3c4686423ad137ecbfa726be97f54e5a05805157e3d8e8adbc5250afc83; prompt d447dc31c7c496a278b4f8cc2daecc547e8aa1d9689dc1de115e77cd72574592; final-path actor view | 22/358 (6.1%) [0.006, 0.163] | 38/358 (10.6%) [0.037, 0.204] | sum 0.000; mean 0.000; 680/690 / sum 7592657.000; mean 11165.672; 680/690 / sum 96010.000; mean 141.191; 680/690 / sum 4514.260; mean 6.639; 680/690 / unknown |
| L0 | line native hard-drop aggregate; code d2662a38a22f3ab62fc49c5cd0ce5683c42fee08; final-path actor view; J3 off | 5/358 (1.4%) [0.003, 0.029] | 5/358 (1.4%) [0.003, 0.029] | unknown / unknown / unknown / unknown / unknown |
| characters | DetBench actor-view character count; narration retained; same rows as each detector | 5/358 (1.4%) [0.003, 0.029] | 22/358 (6.1%) [0.013, 0.136] | unknown / unknown / unknown / unknown / unknown |
| full_line | retained replay-v1-gate-325ca5f349384620a7be304ade47b807; J3 off; Goodfire review floor; keep=0/review=1/drop=2 | 0/358 (0.0%) [0.000, 0.000] | 0/358 (0.0%) [0.000, 0.000] | unknown / unknown / unknown / unknown / unknown |
| steps | DetBench actor-turn count; no raw DB/result-row count; same rows as each detector | 5/358 (1.4%) [0.003, 0.029] | 22/358 (6.1%) [0.015, 0.121] | unknown / unknown / unknown / unknown / unknown |

### Localisation at the frozen 5% threshold

| Tool | First citation | Within 5 actions, same branch | Any citation | Citation precision | First citation given alarm |
|---|---|---|---|---|---|
| AutoReview | unavailable | unavailable | unavailable | unavailable | unavailable |
| J2b | unavailable | unavailable | unavailable | unavailable | unavailable |
| J3 | unavailable | unavailable | unavailable | unavailable | unavailable |
| L0 | unavailable | unavailable | unavailable | unavailable | unavailable |
| characters | inapplicable | inapplicable | inapplicable | inapplicable | inapplicable |
| full_line | unavailable | unavailable | unavailable | unavailable | unavailable |
| steps | inapplicable | inapplicable | inapplicable | inapplicable | inapplicable |

Historical inference costs stay unknown when absent. New inference cost: $0; model calls: 0.
Cached metric computation: 18.643 wall seconds; 16.751 CPU seconds.
Native policy counts (scored cached decisions only; publisher construct): `drop=252, keep=66, review=372`.
Policy decision coverage: `eligible=690, missing=0, scored=690`.
