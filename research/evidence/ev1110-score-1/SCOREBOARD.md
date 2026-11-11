# Current frozen DEV scoreboard

Full authority: `~/Developer/eval-lab-results/score/current/scoreboard.json`.
This compact view covers offline detection, online detection, task QA, and grader QA.
No pooled accuracy. All panels use exposed DEV data. New model calls: 0. New inference cost: $0.
Thresholds, labels, and cohort membership remain unchanged. Missing predictions remain missing.
Intervals and paired comparisons are in the full scoreboard; `summary.json` retains compact aggregates.

## Full receipt hashes

| Receipt | SHA-256 |
|---|---|
| `freeze.json` | `7200f19904feeeb090ccfb48516313fd860005bd02236b7b9f31f31788b95e5c` |
| `scoreboard.json` | `151a8fb97949cce3c2e349f2b318947b97e2a555f3cae88188e04859d601265b` |
| `SCOREBOARD.md` | `56414c726629d1437ad475873ba7d32212f654aceaa493b84922abc8bbf538d6` |
| `items.jsonl` | `ab92959d05e700e02e53b978bdc88d346e69635c44cfb6e78e389842e2fd8d94` |
| `qa-links.jsonl` | `a7c3457d12f92416a9e2335abb17b7ddfe37526782be0d72858a2ee62076ecfc` |
| `summary.json` | `37f30bb4cede102e7bdd84552a90bcf9a412e472fbd97da25a59f8ef7630642a` |

## Frozen offline cohorts

| Cohort | Eligible | Positive | Control | Protocol |
|---|---:|---:|---:|---|
| A | 81 | 11 | 70 | action-v1 |
| B | 75 | 43 | 32 | action-v1 |
| C | 690 | 332 | 358 | publisher |
| action-v2/c1c5-action-v2 | 3 | 1 | 2 | action-v2 |
| action-v2/cheatbench-action-v2 | 80 | 13 | 67 | action-v2 |
| action-v2/goodfire-action-v2 | 0 | 0 | 0 | action-v2 |
| action-v2/terminal-wrench-action-v2 | 221 | 100 | 121 | action-v2 |

## Offline detection

The 5% column uses the frozen DEV threshold, not a new calibration. Unresolved controls are not negatives.

| Cohort | Tool | Eligible/scored/missing | AUROC | 5% recall | Observed control alarms | Unresolved controls |
|---|---|---|---:|---|---|---|
| A | AutoReview | 81/49/32 | 0.616 | 2/11 | 0/41 | 29/70 |
| A | J2b | 81/0/81 | N/A | missing/11 | missing/0 | 70/70 |
| A | J3 | 81/81/0 | 0.994 | 6/11 | 0/70 | 0/70 |
| A | L0 | 81/81/0 | 0.586 | 0/11 | 0/70 | 0/70 |
| A | characters | 81/81/0 | 0.852 | 4/11 | 2/70 | 0/70 |
| A | full_line | 81/81/0 | 0.632 | 0/11 | 0/70 | 0/70 |
| A | steps | 81/81/0 | 0.632 | 0/11 | 0/70 | 0/70 |
| B | AutoReview | 75/0/75 | N/A | missing/43 | missing/0 | 32/32 |
| B | J2b | 75/75/0 | 1.000 | 43/43 | 0/32 | 0/32 |
| B | J3 | 75/75/0 | 1.000 | 43/43 | 0/32 | 0/32 |
| B | L0 | 75/75/0 | 0.750 | 0/43 | 0/32 | 0/32 |
| B | characters | 75/75/0 | 0.603 | 0/43 | 1/32 | 0/32 |
| B | full_line | 75/0/75 | N/A | missing/43 | missing/0 | 32/32 |
| B | steps | 75/75/0 | 0.475 | 0/43 | 0/32 | 0/32 |
| C | AutoReview | 690/380/310 | 0.665 | 47/332 | 12/251 | 107/358 |
| C | J2b | 690/0/690 | N/A | missing/332 | missing/0 | 358/358 |
| C | J3 | 690/663/27 | 0.841 | 153/332 | 16/336 | 22/358 |
| C | L0 | 690/680/10 | 0.709 | 0/332 | 0/353 | 5/358 |
| C | characters | 690/680/10 | 0.533 | 21/332 | 17/353 | 5/358 |
| C | full_line | 690/690/0 | 0.791 | 0/332 | 0/358 | 0/358 |
| C | steps | 690/680/10 | 0.489 | 8/332 | 17/353 | 5/358 |
| action-v2/goodfire-action-v2 | AutoReview | 0/0/0 | N/A | missing/0 | missing/0 | 0/0 |
| action-v2/goodfire-action-v2 | J2b | 0/0/0 | N/A | missing/0 | missing/0 | 0/0 |
| action-v2/goodfire-action-v2 | J3 | 0/0/0 | N/A | missing/0 | missing/0 | 0/0 |
| action-v2/goodfire-action-v2 | L0 | 0/0/0 | N/A | missing/0 | missing/0 | 0/0 |
| action-v2/goodfire-action-v2 | characters | 0/0/0 | N/A | missing/0 | missing/0 | 0/0 |
| action-v2/goodfire-action-v2 | full_line | 0/0/0 | N/A | missing/0 | missing/0 | 0/0 |
| action-v2/goodfire-action-v2 | steps | 0/0/0 | N/A | missing/0 | missing/0 | 0/0 |
| action-v2/terminal-wrench-action-v2 | AutoReview | 221/153/68 | 0.833 | 24/100 | 0/117 | 4/121 |
| action-v2/terminal-wrench-action-v2 | J2b | 221/0/221 | N/A | missing/100 | missing/0 | 121/121 |
| action-v2/terminal-wrench-action-v2 | J3 | 221/221/0 | 0.958 | 83/100 | 4/121 | 0/121 |
| action-v2/terminal-wrench-action-v2 | L0 | 221/221/0 | 0.803 | 0/100 | 0/121 | 0/121 |
| action-v2/terminal-wrench-action-v2 | characters | 221/221/0 | 0.546 | 0/100 | 0/121 | 0/121 |
| action-v2/terminal-wrench-action-v2 | full_line | 221/221/0 | 0.909 | 0/100 | 0/121 | 0/121 |
| action-v2/terminal-wrench-action-v2 | steps | 221/221/0 | 0.582 | 0/100 | 0/121 | 0/121 |
| action-v2/cheatbench-action-v2 | AutoReview | 80/45/35 | 1.000 | 8/13 | 0/36 | 31/67 |
| action-v2/cheatbench-action-v2 | J2b | 80/0/80 | N/A | missing/13 | missing/0 | 67/67 |
| action-v2/cheatbench-action-v2 | J3 | 80/70/10 | 0.973 | 8/13 | 1/57 | 10/67 |
| action-v2/cheatbench-action-v2 | L0 | 80/80/0 | 0.482 | 0/13 | 0/67 | 0/67 |
| action-v2/cheatbench-action-v2 | characters | 80/80/0 | 0.403 | 0/13 | 14/67 | 0/67 |
| action-v2/cheatbench-action-v2 | full_line | 80/80/0 | 0.799 | 0/13 | 0/67 | 0/67 |
| action-v2/cheatbench-action-v2 | steps | 80/80/0 | 0.286 | 1/13 | 16/67 | 0/67 |
| action-v2/c1c5-action-v2 | AutoReview | 3/2/1 | N/A | 0/1 | 2/2 | 0/2 |
| action-v2/c1c5-action-v2 | J2b | 3/0/3 | N/A | missing/1 | missing/0 | 2/2 |
| action-v2/c1c5-action-v2 | J3 | 3/3/0 | 1.000 | 1/1 | 0/2 | 0/2 |
| action-v2/c1c5-action-v2 | L0 | 3/3/0 | 1.000 | 0/1 | 0/2 | 0/2 |
| action-v2/c1c5-action-v2 | characters | 3/3/0 | 0.000 | 0/1 | 0/2 | 0/2 |
| action-v2/c1c5-action-v2 | full_line | 3/3/0 | 0.750 | 0/1 | 0/2 | 0/2 |
| action-v2/c1c5-action-v2 | steps | 3/3/0 | 0.000 | 0/1 | 0/2 | 0/2 |

## Task and grader QA

Positive counts are detected defects. Negative counts are alarms. Each negative class keeps its own denominator.
Gold gates and mutation audit reuse label evidence. Their agreement is not independent accuracy.

| Contract | Panel | Tool | Eligible/scored/missing | Positive | Paired negative | Strong negative | Weak negative |
|---|---|---|---|---|---|---|---|
| task_qa | strict_pairs | envcheck | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | gold_G1 | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | gold_G2 | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | gold_G3 | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | gold_G4 | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | gold_G5_defect_nonempty | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | gold_G6_defect_nonempty | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | gold_G7_uninformed | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | gold_stock | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | gold_strict | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | hardening | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | mutation_audit | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | nop | 40/20/20 | 0/20 | 0/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | oracle | 40/40/0 | 20/20 | 0/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | qa_task_flagged | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| task_qa | strict_pairs | qa_task_verified | 40/0/40 | missing/20 | missing/20 | 0/0 | 0/0 |
| grader_qa | selected_originals | envcheck | 37/0/37 | missing/24 | 0/0 | 0/0 | missing/13 |
| grader_qa | selected_originals | gold_G1 | 37/0/37 | missing/24 | 0/0 | 0/0 | missing/13 |
| grader_qa | selected_originals | gold_G2 | 37/0/37 | missing/24 | 0/0 | 0/0 | missing/13 |
| grader_qa | selected_originals | gold_G3 | 37/0/37 | missing/24 | 0/0 | 0/0 | missing/13 |
| grader_qa | selected_originals | gold_G4 | 37/0/37 | missing/24 | 0/0 | 0/0 | missing/13 |
| grader_qa | selected_originals | gold_G5_defect_nonempty | 37/0/37 | missing/24 | 0/0 | 0/0 | missing/13 |
| grader_qa | selected_originals | gold_G6_defect_nonempty | 37/0/37 | missing/24 | 0/0 | 0/0 | missing/13 |
| grader_qa | selected_originals | gold_G7_uninformed | 37/0/37 | missing/24 | 0/0 | 0/0 | missing/13 |
| grader_qa | selected_originals | gold_stock | 37/0/37 | missing/24 | 0/0 | 0/0 | missing/13 |
| grader_qa | selected_originals | gold_strict | 37/0/37 | missing/24 | 0/0 | 0/0 | missing/13 |
| grader_qa | selected_originals | hardening | 37/0/37 | missing/24 | 0/0 | 0/0 | missing/13 |
| grader_qa | selected_originals | mutation_audit | 37/37/0 | 24/24 | 0/0 | 0/0 | 0/13 |
| grader_qa | selected_originals | nop | 37/32/5 | 0/24 | 0/0 | 0/0 | 0/13 |
| grader_qa | selected_originals | oracle | 37/37/0 | 0/24 | 0/0 | 0/0 | 1/13 |
| grader_qa | selected_originals | qa_task_flagged | 37/0/37 | missing/24 | 0/0 | 0/0 | missing/13 |
| grader_qa | selected_originals | qa_task_verified | 37/0/37 | missing/24 | 0/0 | 0/0 | missing/13 |
| task_qa | gold_tasks | envcheck | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_tasks | gold_G1 | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_tasks | gold_G2 | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_tasks | gold_G3 | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_tasks | gold_G4 | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_tasks | gold_G5_defect_nonempty | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_tasks | gold_G6_defect_nonempty | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_tasks | gold_G7_uninformed | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_tasks | gold_stock | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_tasks | gold_strict | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_tasks | hardening | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_tasks | mutation_audit | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_tasks | nop | 29/10/19 | 0/19 | 0/0 | 0/0 | 0/10 |
| task_qa | gold_tasks | oracle | 29/29/0 | 19/19 | 0/0 | 0/0 | 0/10 |
| task_qa | gold_tasks | qa_task_flagged | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_tasks | qa_task_verified | 29/0/29 | missing/19 | 0/0 | 0/0 | missing/10 |
| task_qa | gold_task_bundles | envcheck | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | gold_G1 | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | gold_G2 | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | gold_G3 | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | gold_G4 | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | gold_G5_defect_nonempty | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | gold_G6_defect_nonempty | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | gold_G7_uninformed | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | gold_stock | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | gold_strict | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | hardening | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | mutation_audit | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | nop | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | oracle | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | qa_task_flagged | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| task_qa | gold_task_bundles | qa_task_verified | 33/0/33 | missing/1 | 0/0 | 0/0 | missing/32 |
| grader_qa | gold_graders | envcheck | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | gold_G1 | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | gold_G2 | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | gold_G3 | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | gold_G4 | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | gold_G5_defect_nonempty | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | gold_G6_defect_nonempty | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | gold_G7_uninformed | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | gold_stock | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | gold_strict | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | hardening | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | mutation_audit | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | nop | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | oracle | 19/19/0 | 19/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | qa_task_flagged | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | gold_graders | qa_task_verified | 19/0/19 | missing/19 | 0/0 | 0/0 | 0/0 |
| grader_qa | certified_graders | envcheck | 3/0/3 | 0/0 | 0/0 | missing/3 | 0/0 |
| grader_qa | certified_graders | gold_G1 | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| grader_qa | certified_graders | gold_G2 | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| grader_qa | certified_graders | gold_G3 | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| grader_qa | certified_graders | gold_G4 | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| grader_qa | certified_graders | gold_G5_defect_nonempty | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| grader_qa | certified_graders | gold_G6_defect_nonempty | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| grader_qa | certified_graders | gold_G7_uninformed | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| grader_qa | certified_graders | gold_stock | 3/0/3 | 0/0 | 0/0 | missing/3 | 0/0 |
| grader_qa | certified_graders | gold_strict | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| grader_qa | certified_graders | hardening | 3/0/3 | 0/0 | 0/0 | missing/3 | 0/0 |
| grader_qa | certified_graders | mutation_audit | 3/0/3 | 0/0 | 0/0 | missing/3 | 0/0 |
| grader_qa | certified_graders | nop | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| grader_qa | certified_graders | oracle | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| grader_qa | certified_graders | qa_task_flagged | 3/0/3 | 0/0 | 0/0 | missing/3 | 0/0 |
| grader_qa | certified_graders | qa_task_verified | 3/0/3 | 0/0 | 0/0 | missing/3 | 0/0 |
| grader_qa | certification_coverage | envcheck | 5/0/5 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | gold_G1 | 5/4/1 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | gold_G2 | 5/4/1 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | gold_G3 | 5/4/1 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | gold_G4 | 5/4/1 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | gold_G5_defect_nonempty | 5/4/1 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | gold_G6_defect_nonempty | 5/4/1 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | gold_G7_uninformed | 5/4/1 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | gold_stock | 5/0/5 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | gold_strict | 5/4/1 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | hardening | 5/0/5 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | mutation_audit | 5/0/5 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | nop | 5/4/1 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | oracle | 5/4/1 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | qa_task_flagged | 5/0/5 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | certification_coverage | qa_task_verified | 5/0/5 | 0/0 | 0/0 | 0/0 | 0/0 |
| task_qa | certified_tasks | envcheck | 3/0/3 | 0/0 | 0/0 | missing/3 | 0/0 |
| task_qa | certified_tasks | gold_G1 | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| task_qa | certified_tasks | gold_G2 | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| task_qa | certified_tasks | gold_G3 | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| task_qa | certified_tasks | gold_G4 | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| task_qa | certified_tasks | gold_G5_defect_nonempty | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| task_qa | certified_tasks | gold_G6_defect_nonempty | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| task_qa | certified_tasks | gold_G7_uninformed | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| task_qa | certified_tasks | gold_stock | 3/0/3 | 0/0 | 0/0 | missing/3 | 0/0 |
| task_qa | certified_tasks | gold_strict | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| task_qa | certified_tasks | hardening | 3/0/3 | 0/0 | 0/0 | missing/3 | 0/0 |
| task_qa | certified_tasks | mutation_audit | 3/0/3 | 0/0 | 0/0 | missing/3 | 0/0 |
| task_qa | certified_tasks | nop | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| task_qa | certified_tasks | oracle | 3/3/0 | 0/0 | 0/0 | 0/3 | 0/0 |
| task_qa | certified_tasks | qa_task_flagged | 3/0/3 | 0/0 | 0/0 | missing/3 | 0/0 |
| task_qa | certified_tasks | qa_task_verified | 3/0/3 | 0/0 | 0/0 | missing/3 | 0/0 |
| grader_qa | historical_stock_packages | envcheck | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | gold_G1 | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | gold_G2 | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | gold_G3 | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | gold_G4 | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | gold_G5_defect_nonempty | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | gold_G6_defect_nonempty | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | gold_G7_uninformed | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | gold_stock | 8/7/1 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | gold_strict | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | hardening | 8/8/0 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | mutation_audit | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | nop | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | oracle | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | qa_task_flagged | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |
| grader_qa | historical_stock_packages | qa_task_verified | 8/0/8 | 0/0 | 0/0 | 0/0 | 0/0 |

## Online detection

Native rules replay causal prefixes. Model monitors use exact-prefix caches only; their Gold-prefix coverage remains missing.

| Cohort | Tool | Model | Eligible/scored/missing | Prefixes | Action-linked detection | Honest runs with alert | Unresolved controls |
|---|---|---|---|---:|---|---|---|
| A | live_watch | deterministic | 81/81/0 | 4240 | 0/11 | 5/70 | 0/70 |
| A | cascade_stream | kimi-code/k3.1-pro-highspeed | 81/27/54 | 4240 | 0/11 | 0/70 | 45/70 |
| A | cascade_stream_judge_only | kimi-code/k3.1-pro-highspeed | 81/27/54 | 4240 | 0/11 | 0/70 | 45/70 |
| A | cascade_stream | anthropic/claude-haiku-5-5 | 81/27/54 | 4240 | 0/11 | 0/70 | 45/70 |
| A | cascade_stream_judge_only | anthropic/claude-haiku-5-5 | 81/27/54 | 4240 | 0/11 | 0/70 | 45/70 |
| A | cascade_stream | anthropic/claude-sonnet-5-5 | 81/27/54 | 4240 | 0/11 | 0/70 | 45/70 |
| A | cascade_stream_judge_only | anthropic/claude-sonnet-5-5 | 81/27/54 | 4240 | 0/11 | 0/70 | 45/70 |
| B | live_watch | deterministic | 75/75/0 | 3189 | 2/43 | 1/32 | 0/32 |
| B | cascade_stream | kimi-code/k3.1-pro-highspeed | 75/9/66 | 3189 | 2/43 | 1/32 | 23/32 |
| B | cascade_stream_judge_only | kimi-code/k3.1-pro-highspeed | 75/8/67 | 3189 | 0/43 | 0/32 | 24/32 |
| B | cascade_stream | anthropic/claude-haiku-5-5 | 75/9/66 | 3189 | 2/43 | 1/32 | 23/32 |
| B | cascade_stream_judge_only | anthropic/claude-haiku-5-5 | 75/8/67 | 3189 | 0/43 | 0/32 | 24/32 |
| B | cascade_stream | anthropic/claude-sonnet-5-5 | 75/9/66 | 3189 | 2/43 | 1/32 | 23/32 |
| B | cascade_stream_judge_only | anthropic/claude-sonnet-5-5 | 75/8/67 | 3189 | 0/43 | 0/32 | 24/32 |

## Limits

- No untouched test cohort exists. Every Goodfire DEV run has stored detector outputs.
- A/B use action-v1 consensus. Action-v2 source panels remain separate, including all-null Goodfire.
- B retains the variant/model confound. C measures publisher labels, not verified executed cheating actions.
- Missing rewards, grader digests, and exact-version QA outputs remain unknown.
- The strict and historical certifiers remain separate; the strict frozen execution manifest is lost.
- Full QA links, 10 submission diagnostics, cost coverage, localization, and timing remain in the hashed receipts.
- Reproduction and code-pin refresh commands: `docs/score.md`.
