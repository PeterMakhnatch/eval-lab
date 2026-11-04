# Exploratory task-cluster bootstrap

This is a DEV follow-on, not a blind result. Counts match the existing baseline and exact-matrix outputs.

| Method | Corpus | Cheats / kept | Wilson 95% | Cluster bootstrap 95% | Valid draws | Task clusters |
|---|---|---:|---:|---:|---:|---:|
| recorded_replay_keep | pooled | 30/100 (30.0%) | 21.9–39.6% | 21.6–39.8% | 5000/5000 | 190 |
| recorded_replay_keep | c1c5 | 0/2 (0.0%) | 0.0–65.8% | 0.0–0.0% | 4581/5000 | 5 |
| recorded_replay_keep | cheatbench | 1/15 (6.7%) | 1.2–29.8% | 0.0–7.7% | 4673/5000 | 4 |
| recorded_replay_keep | goodfire | 27/52 (51.9%) | 38.7–64.9% | 38.5–65.4% | 5000/5000 | 150 |
| recorded_replay_keep | sol-astra | 2/31 (6.5%) | 1.8–20.7% | 0.0–12.5% | 5000/5000 | 6 |
| recorded_replay_keep | terminal-wrench | 0/0 (NA) | NA | NA | 0/5000 | 25 |
| max_rule_j2b_length | pooled | 140/483 (29.0%) | 25.1–33.2% | 21.3–37.7% | 5000/5000 | 190 |
| max_rule_j2b_length | c1c5 | 6/23 (26.1%) | 12.5–46.5% | 18.5–37.5% | 5000/5000 | 5 |
| max_rule_j2b_length | cheatbench | 43/113 (38.1%) | 29.6–47.3% | 9.8–59.5% | 5000/5000 | 4 |
| max_rule_j2b_length | goodfire | 71/139 (51.1%) | 42.9–59.2% | 43.2–59.1% | 5000/5000 | 150 |
| max_rule_j2b_length | sol-astra | 0/48 (0.0%) | 0.0–7.4% | 0.0–0.0% | 5000/5000 | 6 |
| max_rule_j2b_length | terminal-wrench | 20/160 (12.5%) | 8.2–18.5% | 5.0–20.1% | 5000/5000 | 25 |
| max_rule_probez_j2b | pooled | 131/472 (27.8%) | 23.9–32.0% | 20.6–35.9% | 5000/5000 | 190 |
| max_rule_probez_j2b | c1c5 | 6/23 (26.1%) | 12.5–46.5% | 18.5–37.5% | 5000/5000 | 5 |
| max_rule_probez_j2b | cheatbench | 38/113 (33.6%) | 25.6–42.7% | 9.8–55.8% | 5000/5000 | 4 |
| max_rule_probez_j2b | goodfire | 67/134 (50.0%) | 41.7–58.3% | 41.9–58.3% | 5000/5000 | 150 |
| max_rule_probez_j2b | sol-astra | 0/44 (0.0%) | 0.0–8.0% | 0.0–0.0% | 5000/5000 | 6 |
| max_rule_probez_j2b | terminal-wrench | 20/158 (12.7%) | 8.3–18.7% | 5.1–20.2% | 5000/5000 | 25 |
| or_best_rule_j2b | pooled | 332/690 (48.1%) | 44.4–51.8% | 40.8–56.1% | 5000/5000 | 190 |
| or_best_rule_j2b | c1c5 | 11/30 (36.7%) | 21.9–54.5% | 16.7–63.3% | 5000/5000 | 5 |
| or_best_rule_j2b | cheatbench | 75/150 (50.0%) | 42.1–57.9% | 11.6–72.3% | 5000/5000 | 4 |
| or_best_rule_j2b | goodfire | 80/150 (53.3%) | 45.4–61.1% | 45.3–61.3% | 5000/5000 | 150 |
| or_best_rule_j2b | sol-astra | 8/56 (14.3%) | 7.4–25.7% | 10.0–19.2% | 5000/5000 | 6 |
| or_best_rule_j2b | terminal-wrench | 158/304 (52.0%) | 46.4–57.5% | 47.7–55.1% | 5000/5000 | 25 |
| or_best_rule_cached_judge | pooled | 161/507 (31.8%) | 27.9–35.9% | 25.3–38.7% | 5000/5000 | 190 |
| or_best_rule_cached_judge | c1c5 | 11/29 (37.9%) | 22.7–56.0% | 17.2–63.3% | 5000/5000 | 5 |
| or_best_rule_cached_judge | cheatbench | 32/103 (31.1%) | 22.9–40.5% | 2.9–54.4% | 5000/5000 | 4 |
| or_best_rule_cached_judge | goodfire | 67/134 (50.0%) | 41.7–58.3% | 41.8–58.3% | 5000/5000 | 150 |
| or_best_rule_cached_judge | sol-astra | 8/56 (14.3%) | 7.4–25.7% | 10.0–19.2% | 5000/5000 | 6 |
| or_best_rule_cached_judge | terminal-wrench | 43/185 (23.2%) | 17.7–29.8% | 15.7–30.2% | 5000/5000 | 25 |

Zero-kept draws are excluded; NA means no valid draw. Wilson intervals treat runs as independent. Bootstrap intervals resample task clusters within each corpus; pooled draws retain corpus strata.

Few clusters can yield unstable or degenerate percentile intervals, including zero-width intervals. Excluding zero-kept draws conditions the interval on a nonzero denominator. The fixed DEV-selected rules and thresholds are not refitted in bootstrap draws, so these intervals omit selection and fitting uncertainty. Corpus selection and missing-component semantics limit generalization. The recorded replay and counterfactual filters are not the same operating rule.

Exact command:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 /private/tmp/ov1104/wave2-detbench/workers/x06/cluster/bootstrap.py
```

See METHOD.md, input-hashes.json and results.json for method, receipts and unrounded values.
