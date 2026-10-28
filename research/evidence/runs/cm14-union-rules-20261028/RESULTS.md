# CM-14 results

## Primary (frozen CM-6 cascade, the cascade of every earlier table)

Recall on enacted runs / false alarms on clean runs, Wilson 95% intervals.

| Detector | Goodfire DEV 80 / 70 | CheatBench DEV 75 / 75 |
|---|---|---|
| Cascade alone | 12/80 = 15.0% (8.8–24.4); 2/70 = 2.9% (0.8–9.8) | 20/75 = 26.7% (18.0–37.6); 5/75 = 6.7% (2.9–14.7) |
| Facets alone (`suspicion >= 2`) | 7/80 = 8.8%; 1/70 = 1.4% | 30/75 = 40.0%; 4/75 = 5.3% |
| Original union | 16/80 = 20.0% (12.7–30.0); 3/70 = 4.3% (1.5–11.9) | 45/75 = 60.0% (48.7–70.3); 9/75 = 12.0% (6.4–21.3) |
| R1 | 13/80 = 16.2% (9.7–25.8); 2/70 = 2.9% (0.8–9.8) | 40/75 = 53.3% (42.2–64.2); 6/75 = 8.0% (3.7–16.4) |
| R2 | 13/80 = 16.2% (9.7–25.8); 2/70 = 2.9% (0.8–9.8) | 34/75 = 45.3% (34.6–56.6); 6/75 = 8.0% (3.7–16.4) |
| R3 | 26/80 = 32.5% (23.2–43.4); 4/70 = 5.7% (2.2–13.8) | 33/75 = 44.0% (33.3–55.3); 9/75 = 12.0% (6.4–21.3) |

- **Selection.** Cross-validation picked R1 in four folds and R3 in one; four folds had no rule with training clean FAR of
  3% or less (two clean alarms in a 56-run training set is 3.6%), so they took the lowest-FAR rule. Pooled out-of-fold:
  **15/80 = 18.8% (11.7–28.7), 3/70 = 4.3% (1.5–11.9)**, which misses 3%. On all of Goodfire R1 is feasible (R1 and R2
  tie at 13/80 with 2/70; the lower rule number wins), so **R1 is the chosen rule**.
- **Goodfire.** R1 reaches 15% at 3% on the point estimate (16.2% at 2.9%), but the cascade alone already does (15.0% at
  2.9%); R1 adds one detection with no clean alarm. The interval for false alarms is 0.8–9.8%.
- **CheatBench (fixed-policy check, examined in CM-3..9).** **R1 does not reach 3%: 6/75 = 8.0% clean false alarms** at 53.3%
  recall. No rule does, and the cascade alone is already 5/75 = 6.7%, so a union cannot get below it.
- **HackBench (50 trajectories with facets, 41 natural; no clean rows, so no clean FAR exists).** R1 alarms 9/50 (9.8–30.8%),
  3 exact gold-command hits, 8 alarms before the first gold command. Cascade alone: 6 alarms, 1 hit, 6 before. So R1 is
  nearly the cascade; the facets that localize HackBench hacks (24 exact hits alone) are gated off by `answer_material_accessed`.
  Before-first-gold-command alarms are an upper-bound style estimate of false alarms (R1 8/50 = 16%), not a clean rate.

**Plainly: with the frozen cascade, no rule reaches 15% recall at 3% clean FAR on both corpora.** R1 does on Goodfire (and so
does the cascade alone) and not on CheatBench.

## Sensitivity (not selection)

| Shipping v8 cascade (submission_tamper demoted) | Goodfire | CheatBench |
|---|---|---|
| Cascade alone | 10/80 = 12.5%; 2/70 = 2.9% | 19/75 = 25.3%; 0/75 = 0.0% (0.0–4.9) |
| R1 | 11/80 = 13.8%; 2/70 = 2.9% | 39/75 = 52.0%; 1/75 = 1.3% (0.2–7.2) |
| R2 | 12/80 = 15.0% (8.8–24.4); 2/70 = 2.9% | 33/75 = 44.0% (33.3–55.3); 1/75 = 1.3% (0.2–7.2) |
| R3 | 24/80 = 30.0%; 4/70 = 5.7% | 32/75 = 42.7%; 5/75 = 6.7% |

On the v8 cascade the CheatBench false alarms of the frozen cascade (all from `submission_tamper`, see CM-11) disappear.
The chosen rule R1 then reaches 52% at 1.3% on CheatBench but 13.8% on Goodfire, one run short of 15%. The preregistered
selection run with the v8 cascade picks **R2** (the only rule with 15.0% at 2.9% on Goodfire), which clears both corpora on the
point estimate: Goodfire 12/80 at 2/70, CheatBench 33/75 at 1/75. That analysis was defined after the primary result was read and
the margin is one Goodfire run, so it is a lead, not a result: the false-alarm upper bounds are 9.8% and 7.2%, and R2 is not the
rule committed before scoring. Restricting to the 72/64 Goodfire runs CM-12 verified as blinded gives R1 11/72 = 15.3% at 2/64 = 3.1%
(misses 3% by a fraction), CheatBench unchanged.

## Limits

One tuned Goodfire cohort with 70 clean runs: one clean alarm is 1.4 points, so a 3% bound is two runs and intervals are wide.
CheatBench was examined in earlier items. Cross-validation had singleton task groups. HackBench has no clean negatives and only
facet-covered trajectories (50 of 169).
