# Grader-trust scorecard (DEV tasks, receipts only)

Built by `build_scorecard.py` from existing receipts under `/private/tmp/eval-lab-study/research/experiments/` and `~/Developer/eval-lab-results/2026-11-02/`, plus `ov1104-grader` audit/certification receipts. Rule and columns: `../PREREG.md`. `null` = not measured.

- H1 ($0 existing receipts, original grader accepted >=1 executed broken STING/census-family mutant): **23/35 = 65.7% (49.2-79.2)**
- H1 with `ov1104-grader` audit extension (+`002680`, `002976`, `003007`): **24/37 = 64.9% (48.8-78.2)** (`on-gold-universe` `executable_audit` 31/31 census: **20/31 = 64.5% (46.9-78.9)** pooled, **19/31 = 61.3% (43.8-76.3)** A2+ov1104 only); sensitivity incl. A4B held-out mutants on the original: 30/37 = 81.1% (65.8-90.5)
- Status distribution (strict H3/H4): certified 0, near_certifiable 12, hardened_but_leaky 15, original_weak 5, original_no_weakness_found 5, no_executed_evidence 4
- H2 (near_certifiable or better): **12** (11 pre-run; `002139` upgraded from `hardened_but_leaky` after passing Gates 1-6 in `ov1104-grader`)
- H3/H4/H5 certification: **0/5** under strict blind synthesis (`H3/H4`); **2** tasks reach `certification.json.status == "certified"` (7/7 gates, 2/2 held-out alternatives accepted) under `H5` alternative-informed synthesis: `000682-r3` (`provisional: test-aware alternatives`, 4/4 total accepted) and `002391-r2-honest` (4/4 honest alternatives accepted, non-test-aware; `1/2` held-out when including the `HAR-202` cheat copy `real-1` that drops `published=None`)

| task | status | nop | ref | mutants killed by original k/n (Wilson 95%) | A4B held-out mutants killed by original k/n | mutants killed by hardened k/n | attacker vs original | attacker vs separate verifier | attacker vs hardened | alt solutions accepted | gold gates | missing for certification |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 002139 | near_certifiable | 0 | 1 | 3/3 (44-100%) | 4/6 | 5/8 | 5/7 | 0/3 | 1/3 | 0/3 (certify-gold upgraded package) | 6/7 (certify-gold) | 2 alternative solution(s) (0/3 accepted by certify-gold upgraded package) |
| 002391 | near_certifiable | 0 | 1 | 3/3 (44-100%) | 4/5 | 6/7 | 20/21 | 0/4 | 2/17 | 0/4 (certify-gold upgraded package) | 6/7 (certify-gold) | 2 alternative solution(s) (0/4 accepted by certify-gold upgraded package) |
| 000081 | near_certifiable | 0 | 1 | 2/3 (21-94%) | 3/6 | 10/10 | 17/19 | 0/1 | 2/18 | null | 6/7 (A4B Gold v1.1) | certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 000682 | near_certifiable | 0 | 1 | 0/2 (0-66%) | 0/1 | 6/6 | 15/17 | 0/12 | 2/17 | 0/4 (certify-gold upgraded package) | 6/7 (ov-gold-certifier-8) | certify-gold run (certification.json) not executed; 2 alternative solution(s) (0/4 accepted by certify-gold upgraded package) |
| 000838 | near_certifiable | 0 | 1 | 2/3 (21-94%) | 0/1 | 6/6 | 4/8 | 0/13 | 1/3 | 0/2 (certify-gold upgraded package) | 6/7 (A4B Gold v1.1) | certify-gold run (certification.json) not executed; 2 alternative solution(s) (0/2 accepted by certify-gold upgraded package) |
| 001267 | near_certifiable | 0 | 1 | 0/2 (0-66%) | null | 2/2 | 1/6 | 0/2 | 0/2 | null | 6/7 (ov-gold-certifier-8) | certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 001850 | near_certifiable | 0 | 1 | 2/3 (21-94%) | 1/5 | 3/11 | 15/17 | 0/1 | 2/16 | null | 6/7 (ov-gold-certifier-8) | certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 002552 | near_certifiable | 0 | 1 | 1/1 (21-100%) | 1/1 | 3/3 | 25/25 | 0/20 | 2/17 | 3/4 (A4B Gold v1.1 shipped test suite) | 5/7 (A4B Gold v1.1) | gate6: 0 of 1 held-out mutants accepted by the original grader, none left to test the hardened package; certify-gold run (certification.json) not executed |
| 000085 | near_certifiable | 0 | 1 | 3/3 (44-100%) | 4/6 | 7/8 | 9/10 | 0/13 | 1/10 | null | 5/7 (A4B Gold v1.1) | gate6: 1 held-out mutant(s) survive (1/2); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 000232 | near_certifiable | 0 | 1 | 2/3 (21-94%) | 3/6 | 8/10 | 11/17 | 0/12 | 2/17 | null | 5/7 (A4B Gold v1.1) | gate6: 2 held-out mutant(s) survive (1/3); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 000812 | near_certifiable | 0 | 1 | 1/1 (21-100%) | null | 2/2 | 10/11 | 0/1 | 1/10 | null | 5/7 (A4B Gold v1.1) | gate6: no held-out mutant generated; certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 002609 | near_certifiable | 0 | 1 | 3/3 (44-100%) | 3/6 | 6/9 | 14/18 | 0/1 | 2/17 | null | 5/7 (A4B Gold v1.1) | gate6: 3 held-out mutant(s) survive (0/3); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 000079 | hardened_but_leaky | 0 | 1 | 2/3 (21-94%) | 3/6 | 6/12 | 16/18 | 0/2 | 2/17 | null | 5/7 (ov-gold-certifier-8) | gate6: 1 held-out mutant(s) survive (3/4); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 000484 | hardened_but_leaky | 0 | 1 | 0/4 (0-49%) | null | 2/4 | null | null | null | null | 5/7 (ov-gold-certifier-8) | gate6: 2 held-out mutant(s) survive (0/2); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 002535 | hardened_but_leaky | 0 | 1 | 2/3 (21-94%) | 4/6 | 8/8 | 8/9 | 0/1 | 1/8 | null | 5/7 (A4B Gold v1.1) | gate4: failed; certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 003005 | hardened_but_leaky | null | 1 | 3/5 (23-88%) | null | 5/6 | null | null | null | null | 5/7 (ov-gold-certifier-8) | gate6: 1 held-out mutant(s) survive (2/3); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 000256 | hardened_but_leaky | 0 | 1 | 2/3 (21-94%) | 3/6 | 4/10 | 9/9 | 0/13 | 1/8 | null | 4/7 (A4B Gold v1.1) | gate5: train mutants killed 2/4, probe 2/2; gate6: 3 held-out mutant(s) survive (0/3); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 000322 | hardened_but_leaky | 0 | 1 | 2/3 (21-94%) | 5/6 | 5/7 | 7/8 | 0/12 | 1/8 | null | 4/7 (A4B Gold v1.1) | gate5: train mutants killed 3/4, probe 2/2; gate6: 1 held-out mutant(s) survive (0/1); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 000847 | hardened_but_leaky | 0 | 1 | 2/3 (21-94%) | 3/6 | 5/10 | 8/9 | 0/13 | 1/8 | null | 4/7 (A4B Gold v1.1) | gate5: train mutants killed 3/4, probe 2/2; gate6: 3 held-out mutant(s) survive (0/3); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 000931 | hardened_but_leaky | 0 | 1 | 0/1 (0-79%) | 0/1 | 0/4 | 15/17 | 0/2 | 2/16 | null | 4/7 (ov-gold-certifier-8) | gate5: train mutants killed 0/1; gate6: 0 held-out mutant(s) survive (0/0); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 001431 | hardened_but_leaky | 0 | 1 | 2/3 (21-94%) | 6/6 | 5/7 | 6/9 | 0/1 | 1/8 | null | 4/7 (A4B Gold v1.1) | gate5: train mutants killed 3/4, probe 2/2; gate6: 0 of 6 held-out mutants accepted by the original grader, none left to test the hardened package; certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 001804 | hardened_but_leaky | 0 | 1 | 1/3 (6-79%) | 1/3 | 1/9 | 9/10 | 0/1 | 1/9 | null | 4/7 (A4B Gold v1.1) | gate5: train mutants killed 1/5, probe 0/0; gate6: 2 held-out mutant(s) survive (0/2); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 001809 | hardened_but_leaky | 0 | 1 | 2/3 (21-94%) | 2/2 | 5/7 | 11/12 | 0/4 | 1/8 | 0/2 (A4B Gold v1.1 shipped test suite) | 4/7 (A4B Gold v1.1) | gate5: train mutants killed 3/4, probe 2/2; gate6: 0 of 2 held-out mutants accepted by the original grader, none left to test the hardened package; certify-gold run (certification.json) not executed; 2 alternative solution(s) (0/2 accepted by A4B Gold v1.1 shipped test suite (S0)) |
| 001985 | hardened_but_leaky | 0 | 1 | 2/3 (21-94%) | 2/6 | 7/11 | 12/16 | 0/1 | 2/16 | null | 4/7 (A4B Gold v1.1) | gate5: train mutants killed 4/4, probe 1/2; gate6: 3 held-out mutant(s) survive (1/4); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 002545 | hardened_but_leaky | 0 | 1 | 1/3 (6-79%) | 3/6 | 8/10 | 7/8 | null | 1/8 | null | 4/7 (A4B Gold v1.1) | gate5: train mutants killed 3/4, probe 1/1; gate6: 1 held-out mutant(s) survive (2/3); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 002680 | hardened_but_leaky | 0 | 1 | 3/3 (44-100%) | 4/6 | 5/8 | 13/17 | 0/1 | 2/16 | null | 4/7 (A4B Gold v1.1) | gate5: train mutants killed 4/4, probe 1/2; gate6: 2 held-out mutant(s) survive (0/2); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 002976 | hardened_but_leaky | 0 | 1 | 3/3 (44-100%) | 5/6 | 5/7 | 0/4 | null | null | null | 3/7 (A4B Gold v1.1) | gate4: failed; gate5: train mutants killed 4/4, probe 1/2; gate6: 1 held-out mutant(s) survive (0/1); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured) |
| 001338 | original_weak | 0 | 1 | 2/3 (21-94%) | null | 3/3 | 4/8 | null | 1/8 | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured); hardened package: original grader accepts 1 executed mutant(s) |
| 001426 | original_weak | 0 | 1 | 1/2 (9-91%) | null | 2/2 | 4/9 | 0/1 | 1/8 | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured); hardened package: original grader accepts 1 executed mutant(s) |
| 001793 | original_weak | null | 1 | 0/3 (0-56%) | null | null | null | null | null | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured); hardened package: original grader accepts 3 executed mutant(s) |
| 001954 | original_weak | null | 1 | 2/3 (21-94%) | null | null | null | null | null | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured); hardened package: original grader accepts 1 executed mutant(s) |
| 003007 | original_weak | 0 | 1 | 0/3 (0-56%) | null | null | 9/9 | 0/1 | 1/8 | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured); hardened package: original grader accepts 3 executed mutant(s) |
| 000212 | original_no_weakness_found | 0 | 1 | 3/3 (44-100%) | null | 3/3 | 8/9 | 0/13 | 1/8 | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured) |
| 000581 | original_no_weakness_found | null | 1 | 3/3 (44-100%) | null | null | null | null | null | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured) |
| 001205 | original_no_weakness_found | 0 | 1 | 1/1 (21-100%) | null | 1/1 | 15/17 | 0/3 | 2/16 | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured) |
| 002160 | original_no_weakness_found | 0 | 1 | 3/3 (44-100%) | null | 3/3 | 1/4 | null | null | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured) |
| 002212 | original_no_weakness_found | null | 0 | 3/3 (44-100%) | null | null | null | null | null | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured); reference control scored 0.0 on the original package |
| 000945 | no_executed_evidence | 0 | 1 | 0/0 | null | null | 7/9 | 0/13 | 1/8 | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured) |
| 000603 | no_executed_evidence | 0 | 1 | null | null | null | 3/7 | 0/1 | 0/3 | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured); executed mutant audit of the original grader |
| 000872 | no_executed_evidence | 0 | 1 | null | null | null | 0/6 | 0/2 | 0/2 | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured); executed mutant audit of the original grader |
| 001514 | no_executed_evidence | 0 | 1 | null | null | null | 3/7 | 0/3 | 0/3 | null | 0/7 | gold run: gates 1-7 unmeasured; 2 alternative solutions (none measured); executed mutant audit of the original grader |

Attacker columns pool every executed attack cell across receipts (success/attempts); per-receipt counts are in `scorecard.json`.

## Closest to certifiable

1. `format-code-task-002139` (near_certifiable, gold gates 6/7): 2 alternative solution(s) (0/3 accepted by certify-gold upgraded package)
2. `format-code-task-002391` (near_certifiable, gold gates 6/7): 2 alternative solution(s) (0/4 accepted by certify-gold upgraded package)
3. `format-code-task-000081` (near_certifiable, gold gates 6/7): certify-gold run (certification.json) not executed; 2 alternative solutions (none measured)
4. `format-code-task-000682` (near_certifiable, gold gates 6/7): certify-gold run (certification.json) not executed; 2 alternative solution(s) (0/4 accepted by certify-gold upgraded package)
5. `format-code-task-000838` (near_certifiable, gold gates 6/7): certify-gold run (certification.json) not executed; 2 alternative solution(s) (0/2 accepted by certify-gold upgraded package)
6. `format-code-task-001267` (near_certifiable, gold gates 6/7): certify-gold run (certification.json) not executed; 2 alternative solutions (none measured)
7. `format-code-task-001850` (near_certifiable, gold gates 6/7): certify-gold run (certification.json) not executed; 2 alternative solutions (none measured)
8. `format-code-task-002552` (near_certifiable, gold gates 5/7): gate6: 0 of 1 held-out mutants accepted by the original grader, none left to test the hardened package; certify-gold run (certification.json) not executed
9. `format-code-task-000085` (near_certifiable, gold gates 5/7): gate6: 1 held-out mutant(s) survive (1/2); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured)
10. `format-code-task-000232` (near_certifiable, gold gates 5/7): gate6: 2 held-out mutant(s) survive (1/3); certify-gold run (certification.json) not executed; 2 alternative solutions (none measured)

## Worked example: `format-code-task-000682` original grader accepts a broken mutant

Mutant `call_drop` at `c7n/resources/sns.py:316` (STING operator: drop the call the reference patch added). Receipt: `/private/tmp/eval-lab-study/research/experiments/ov-sting-mutants-20261031/results/audit-summary.json` cell `original-m0-call_drop-L316`; diff file `/private/tmp/eval-lab-study/research/experiments/ov-sting-mutants-20261031/results/survivor-diffs/format-code-task-000682-call_drop-L316.diff`.

```diff
--- a/c7n/resources/sns.py
+++ b/c7n/resources/sns.py
@@ -314,8 +314,4 @@
 
         for r in resources:
-            sns.set_topic_attributes(
-                TopicArn=r['TopicArn'],
-                AttributeName='KmsMasterKeyId',
-                AttributeValue=key
-            )
+            pass
         return resources
```

- original grader reward: **1.0** (STING, `aa393fc69dea`); A2 census re-execution also 1.0 (`/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A2/census.json`).
- task-quality repaired snapshot reward: 0.0 (STING repaired cell); R3 repaired cell: 0.0 (`/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/R3-REPAIR-VS-GOLD/repair_vs_gold.json`).
- mutation-guided repair + full-hardening reward: 0.0 (`/private/tmp/eval-lab-study/research/experiments/ov-mutant-killer-loop-20261031/results-r/summary.json`, r1 cell; reference 1.0, nop 0.0).
- A4B Gold v1.1 shipped package: A2 survivors killed 2/2, but gate 4 (exit-0 `.pth`) = False (`/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/A4B/tasks/format-code-task-000682/a4b-r1.json`).
