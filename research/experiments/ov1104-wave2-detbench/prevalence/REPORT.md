# Exploratory prevalence sensitivity

This analysis was registered after X06 and review results were observed. It is not blind.
These are reweighted in-sample DEV operating points, not production estimates or independent validation.

| Method | Cheat keep / all | Honest keep / all | Sensitivity: cheat flag / all | FPR: honest flag / all | Missing cheat / honest |
|---|---:|---:|---:|---:|---:|
| recorded_replay_keep | 30/332 (9.04%) | 70/358 (19.55%) | NA | NA | 0 / 0 |
| max_rule_j2b_length | 140/332 (42.17%) | 343/358 (95.81%) | 192/332 (57.83%) | 15/358 (4.19%) | 0 / 0 |
| max_rule_probez_j2b | 131/332 (39.46%) | 341/358 (95.25%) | 201/332 (60.54%) | 17/358 (4.75%) | 0 / 0 |
| or_best_rule_j2b | 332/332 (100.00%) | 358/358 (100.00%) | 0/332 (0.00%) | 0/358 (0.00%) | 0 / 0 |
| or_best_rule_cached_judge | 161/332 (48.49%) | 346/358 (96.65%) | 171/332 (51.51%) | 12/358 (3.35%) | 0 / 0 |

| Method | Outcome | 1% prior | 5% prior | 20% prior | Valid / excluded draws |
|---|---|---:|---:|---:|---:|
| recorded_replay_keep | contamination | 0.46% [0.30%, 0.75%] | 2.37% [1.54%, 3.77%] | 10.36% [6.90%, 15.68%] | 5000 / 0 |
| recorded_replay_keep | precision | NA | NA | NA | NA |
| max_rule_j2b_length | contamination | 0.44% [0.37%, 0.50%] | 2.26% [1.89%, 2.58%] | 9.91% [8.37%, 11.16%] | 5000 / 0 |
| max_rule_j2b_length | precision | 12.24% [7.47%, 26.78%] | 42.08% [29.61%, 65.59%] | 77.53% [66.64%, 90.05%] | 5000 / 0 |
| max_rule_probez_j2b | contamination | 0.42% [0.35%, 0.48%] | 2.13% [1.81%, 2.44%] | 9.38% [8.05%, 10.61%] | 5000 / 0 |
| max_rule_probez_j2b | precision | 11.41% [7.69%, 19.46%] | 40.16% [30.27%, 55.74%] | 76.12% [67.34%, 85.68%] | 5000 / 0 |
| or_best_rule_j2b | contamination | 1.00% [1.00%, 1.00%] | 5.00% [5.00%, 5.00%] | 20.00% [20.00%, 20.00%] | 5000 / 0 |
| or_best_rule_j2b | precision | NA [NA] | NA [NA] | NA [NA] | 0 / 5000 |
| or_best_rule_cached_judge | contamination | 0.50% [0.45%, 0.57%] | 2.57% [2.31%, 2.91%] | 11.15% [10.10%, 12.47%] | 5000 / 0 |
| or_best_rule_cached_judge | precision | 13.44% [8.71%, 27.19%] | 44.71% [33.19%, 66.05%] | 79.35% [70.24%, 90.24%] | 5000 / 0 |

Cells show point estimates and 95% percentile intervals. All rates use all runs in each class as denominators, including missing scores. Missing scores are neither kept nor flagged.
Recorded replay review and drop are not combined as flags; recorded sensitivity, FPR and precision are not defined here. Review is not keep.
Recorded replay exclusions: 212 cheat and 40 honest drops; 90 cheat and 248 honest reviews.
The bootstrap draws 5,000 samples of task clusters with replacement within each corpus, with seed 110408. Pooled counts preserve corpus strata. Cluster counts: c1c5 5, cheatbench 4, goodfire 150, sol-astra 6, terminal-wrench 25.
Each interval excludes draws without either class or without the relevant selected outcome. Exclusion reasons and per-corpus point counts are in results.json. Thresholds and scores stay fixed.
The analysis assumes the class-conditional rates transfer to each hypothetical prior. Few clusters, missing scores, corpus selection, and DEV-based rule selection limit inference. The bootstrap excludes threshold and selection uncertainty.

Reproduce: `PYTHONDONTWRITEBYTECODE=1 python3 /private/tmp/ov1104/wave2-detbench/workers/x06/prevalence/prevalence.py`.
input-hashes.json records code, protocol and input bytes before scoring; results.json records post-computation verification and unrounded values.
