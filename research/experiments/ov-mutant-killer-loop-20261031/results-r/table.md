| task | kind | valid (ref/nop) | witnesses | R1 killed | R2 holdout (base -> upgraded) | test lines |
|---|---|---|---|---|---|---|
| 000079 | repaired | True (1.0/0.0) | 708968feb75c=0.0, cafc63d398af=0.0 | 1/1 | call_drop-L10 0.0->0.0; keyword_drop-L104 1.0->1.0 | 47 |
| 000232 | repaired | True (1.0/0.0) | 31986a3b6f17=0.0, c26266d288d8=0.0 | 1/1 | keyword_drop-L186 0.0->0.0; bool_flip-L179 0.0->0.0 | 59 |
| 001267 | repaired | True (1.0/0.0) | 904dbadbf41d=0.0, bbd9a15fc26c=0.0 | 1/1 |  | 39 |
| 001850 | repaired | True (1.0/0.0) | 11a3463f9035=0.0, d31f564c0179=0.0 | 1/1 | call_drop-L714 0.0->0.0; call_drop-L716 1.0->1.0 | 74 |
| 000682 | original | True (1.0/0.0) | - | 2/2 |  | 79 |
| 000931 | original | True (1.0/0.0) | - | 1/1 |  | 153 |
| 000484 | original | True (1.0/0.0) | - | 3/3 |  | 103 |
| 003005 | original | True (1.0/0.0) | - | 1/1 | handler_drop-L321 0.0->0.0; int_shift-L428 1.0->1.0 | 53 |

- g1_valid_tasks: 8/8 = 100.0% (67.6-100.0)
- g2_tasks_closed: 8/8 = 100.0% (67.6-100.0)
- g2_r1_mutants_killed: 11/11 = 100.0% (74.1-100.0)
- g3_witness_cells_rejected: 8/8 = 100.0% (67.6-100.0)
- g4_r2_killed_among_base_survivors: 0/3 = 0.0% (0.0-56.1)
- g4_r2_killed_among_applied: 5/8 = 62.5% (30.6-86.3)
- r2_base_survival: 3/8 = 37.5% (13.7-69.4)
- estimated_usd: 0.1393
- sandbox_seconds: 842.6
- executions: 51
- synthesis_usd: 0.4645
- synthesis_attested: 8/8 = 100.0% (67.6-100.0)
