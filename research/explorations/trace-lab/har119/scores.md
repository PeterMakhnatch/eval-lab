# HAR-119 part 2 scores (out of sample, 12 HAR-110 v2 runs)

first_failure: within ±2 steps (both null counts as agreement). loop onset: within ±5 steps when both see a loop. pass_copied: passes only (2 runs). `—` = the tool does not express the field. `_vs_agreed` scores only the run×field cells where the two raters agree.

| comparison | stop_reason | first_failure | blame | loop_present | loop_kind | loop_onset | pass_copied |
|---|---|---|---|---|---|---|---|
| rater_a_vs_rater_b | 12/12 | 9/12 | 11/12 | 11/12 | 11/12 | 6/6 | 2/2 |
| evallab_vs_rater_a | 12/12 | 5/12 | — | 7/12 | — | 2/4 | 2/2 |
| evallab_vs_rater_b | 12/12 | 5/12 | — | 8/12 | — | 3/5 | 2/2 |
| evallab_vs_agreed | 12/12 | 4/9 | — | 7/11 | — | 2/4 | 2/2 |
| scout_vs_rater_a | 12/12 | 2/12 | 11/12 | 7/12 | — | 1/6 | — |
| scout_vs_rater_b | 12/12 | 2/12 | 12/12 | 8/12 | — | 1/7 | — |
| scout_vs_agreed | 12/12 | 2/9 | 11/11 | 7/11 | — | 1/6 | — |
| docent_vs_rater_a | — | 5/12 | 12/12 | — | — | — | — |
| docent_vs_rater_b | — | 3/12 | 11/12 | — | — | — | — |
| docent_vs_agreed | — | 3/9 | 11/11 | — | — | — | — |
| loop_rule_vs_rater_a | — | — | — | 7/12 | 7/12 | 3/5 | — |
| loop_rule_vs_rater_b | — | — | — | 8/12 | 8/12 | 3/6 | — |
| loop_rule_vs_agreed | — | — | — | 7/11 | 7/11 | 3/5 | — |
