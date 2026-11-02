# Baselines (stored readings, no model call)

Every number is on the `report` half. Thresholds come from `tune` only. `k/n (lo-hi)` is a count with its 95% Wilson interval in percent. `n/a` = NOT MEASURED (the reader has no reading for the family, or no tune negatives).

## Coverage (cases with a stored reading / scored cases)

| reader | C1-C5 | CheatBench | deep_swe | Goodfire | Sol/Astra | Terminal Wrench |
|---|---|---|---|---|---|---|
| l0_tripwires | 16/16 | 150/150 | 100/100 | 150/150 | 56/56 | 304/304 |
| ar_g38 | 16/16 | 150/150 | 100/100 | 150/150 | 0/56 | 304/304 |
| ar_g38_fixed | 16/16 | 150/150 | 100/100 | 150/150 | 0/56 | 304/304 |
| ar_fable | 0/16 | 129/150 | 0/100 | 19/150 | 0/56 | 0/304 |
| dt15_masked | 0/16 | 150/150 | 100/100 | 150/150 | 0/56 | 0/304 |
| sol_astra_structured | 0/16 | 0/150 | 0/100 | 0/150 | 56/56 | 0/304 |
| sol_astra_flat | 0/16 | 0/150 | 0/100 | 0/150 | 56/56 | 0/304 |
| len_tokens | 16/16 | 150/150 | 100/100 | 150/150 | 56/56 | 304/304 |
| len_steps | 16/16 | 150/150 | 100/100 | 150/150 | 56/56 | 304/304 |

## AUROC on `report` (95% bootstrap interval)

| reader | CheatBench | Goodfire | Terminal Wrench | deep_swe | C1-C5 | Sol/Astra | pooled |
|---|---|---|---|---|---|---|---|
| l0_tripwires | 0.527 [0.46, 0.59] | 0.509 [0.45, 0.56] | 0.867 [0.81, 0.92] | 0.640 [0.50, 0.78] | 0.667 [0.50, 1.00] | 0.500 [0.50, 0.50] | 0.686 [0.65, 0.72] |
| ar_g38 | 0.727 [0.64, 0.81] | 0.525 [0.50, 0.56] | 0.840 [0.78, 0.89] | 0.716 [0.59, 0.84] | 0.767 [0.33, 1.00] | n/a | 0.730 [0.69, 0.77] |
| ar_g38_fixed | 0.689 [0.61, 0.77] | 0.500 [0.50, 0.50] | 0.777 [0.72, 0.83] | 0.540 [0.50, 0.60] | 0.733 [0.40, 1.00] | n/a | 0.665 [0.63, 0.70] |
| ar_fable | 0.874 [0.78, 0.95] | 0.646 [0.23, 1.00] | n/a | n/a | n/a | n/a | 0.847 [0.75, 0.93] |
| dt15_masked | 0.703 [0.61, 0.80] | 0.536 [0.48, 0.60] | n/a | 0.500 [0.50, 0.50] | n/a | n/a | 0.587 [0.54, 0.64] |
| sol_astra_structured | n/a | n/a | n/a | n/a | n/a | 1.000 [1.00, 1.00] | 1.000 [1.00, 1.00] |
| sol_astra_flat | n/a | n/a | n/a | n/a | n/a | 1.000 [1.00, 1.00] | 1.000 [1.00, 1.00] |
| len_tokens | 0.289 [0.18, 0.41] | 0.825 [0.73, 0.91] | 0.625 [0.54, 0.71] | 0.552 [0.39, 0.71] | 0.600 [0.13, 1.00] | 0.042 [0.00, 0.12] | 0.540 [0.48, 0.60] |
| len_steps | 0.227 [0.12, 0.34] | 0.749 [0.63, 0.86] | 0.628 [0.54, 0.71] | 0.430 [0.27, 0.60] | 0.733 [0.33, 1.00] | 0.021 [0.00, 0.06] | 0.480 [0.42, 0.54] |

## Recall and false alarms at a 0% false-alarm budget

| reader | CheatBench | Goodfire | Terminal Wrench | deep_swe | C1-C5 | Sol/Astra | pooled |
|---|---|---|---|---|---|---|---|
| l0_tripwires | R 0/37 (0-9)<br>FA 0/37 (0-9) | R 3/40 (3-20)<br>FA 2/35 (2-19) | R 0/79 (0-5)<br>FA 0/73 (0-5) | R 0/25 (0-13)<br>FA 0/25 (0-13) | R 0/3 (0-56)<br>FA 0/5 (0-43) | R 0/4 (0-49)<br>FA 0/24 (0-14) | R 0/188 (0-2)<br>FA 0/199 (0-2) |
| ar_g38 | R 18/37 (33-64)<br>FA 2/37 (2-18) | R 2/40 (1-16)<br>FA 0/35 (0-10) | R 49/79 (51-72)<br>FA 2/73 (1-9) | R 3/25 (4-30)<br>FA 1/25 (1-20) | R 0/3 (0-56)<br>FA 0/5 (0-43) | n/a | R 17/184 (6-14)<br>FA 1/175 (0-3) |
| ar_g38_fixed | R 14/37 (24-54)<br>FA 0/37 (0-9) | R 0/40 (0-9)<br>FA 0/35 (0-10) | R 46/79 (47-68)<br>FA 2/73 (1-9) | R 2/25 (2-25)<br>FA 0/25 (0-13) | R 0/3 (0-56)<br>FA 0/5 (0-43) | n/a | R 0/184 (0-2)<br>FA 0/175 (0-2) |
| ar_fable | R 19/35 (38-70)<br>FA 2/26 (2-24) | R 1/6 (3-56)<br>FA 1/4 (5-70) | n/a | n/a | n/a | n/a | R 19/41 (32-61)<br>FA 2/30 (2-21) |
| dt15_masked | R 0/37 (0-9)<br>FA 0/37 (0-9) | R 0/40 (0-9)<br>FA 0/35 (0-10) | n/a | R 0/25 (0-13)<br>FA 0/25 (0-13) | n/a | n/a | R 0/102 (0-4)<br>FA 0/97 (0-4) |
| sol_astra_structured | n/a | n/a | n/a | n/a | n/a | R 4/4 (51-100)<br>FA 0/24 (0-14) | R 4/4 (51-100)<br>FA 0/24 (0-14) |
| sol_astra_flat | n/a | n/a | n/a | n/a | n/a | R 0/4 (0-49)<br>FA 0/24 (0-14) | R 0/4 (0-49)<br>FA 0/24 (0-14) |
| len_tokens | R 0/37 (0-9)<br>FA 4/37 (4-25) | R 4/40 (4-23)<br>FA 0/35 (0-10) | R 1/79 (0-7)<br>FA 1/73 (0-7) | R 0/25 (0-13)<br>FA 0/25 (0-13) | R 1/3 (6-79)<br>FA 1/5 (4-62) | R 0/4 (0-49)<br>FA 2/24 (2-26) | R 0/188 (0-2)<br>FA 4/199 (1-5) |
| len_steps | R 0/37 (0-9)<br>FA 1/37 (0-14) | R 13/40 (20-48)<br>FA 1/35 (1-15) | R 1/79 (0-7)<br>FA 0/73 (0-5) | R 0/25 (0-13)<br>FA 2/25 (2-25) | R 0/3 (0-56)<br>FA 0/5 (0-43) | R 0/4 (0-49)<br>FA 7/24 (15-49) | R 0/188 (0-2)<br>FA 1/199 (0-3) |

## Recall and false alarms at a 1% false-alarm budget

| reader | CheatBench | Goodfire | Terminal Wrench | deep_swe | C1-C5 | Sol/Astra | pooled |
|---|---|---|---|---|---|---|---|
| l0_tripwires | R 0/37 (0-9)<br>FA 0/37 (0-9) | R 3/40 (3-20)<br>FA 2/35 (2-19) | R 0/79 (0-5)<br>FA 0/73 (0-5) | R 0/25 (0-13)<br>FA 0/25 (0-13) | R 0/3 (0-56)<br>FA 0/5 (0-43) | R 0/4 (0-49)<br>FA 0/24 (0-14) | R 0/188 (0-2)<br>FA 0/199 (0-2) |
| ar_g38 | R 18/37 (33-64)<br>FA 2/37 (2-18) | R 2/40 (1-16)<br>FA 0/35 (0-10) | R 49/79 (51-72)<br>FA 2/73 (1-9) | R 3/25 (4-30)<br>FA 1/25 (1-20) | R 0/3 (0-56)<br>FA 0/5 (0-43) | n/a | R 67/184 (30-44)<br>FA 3/175 (1-5) |
| ar_g38_fixed | R 14/37 (24-54)<br>FA 0/37 (0-9) | R 0/40 (0-9)<br>FA 0/35 (0-10) | R 46/79 (47-68)<br>FA 2/73 (1-9) | R 2/25 (2-25)<br>FA 0/25 (0-13) | R 0/3 (0-56)<br>FA 0/5 (0-43) | n/a | R 64/184 (28-42)<br>FA 3/175 (1-5) |
| ar_fable | R 19/35 (38-70)<br>FA 2/26 (2-24) | R 1/6 (3-56)<br>FA 1/4 (5-70) | n/a | n/a | n/a | n/a | R 19/41 (32-61)<br>FA 2/30 (2-21) |
| dt15_masked | R 0/37 (0-9)<br>FA 0/37 (0-9) | R 0/40 (0-9)<br>FA 0/35 (0-10) | n/a | R 0/25 (0-13)<br>FA 0/25 (0-13) | n/a | n/a | R 0/102 (0-4)<br>FA 0/97 (0-4) |
| sol_astra_structured | n/a | n/a | n/a | n/a | n/a | R 4/4 (51-100)<br>FA 0/24 (0-14) | R 4/4 (51-100)<br>FA 0/24 (0-14) |
| sol_astra_flat | n/a | n/a | n/a | n/a | n/a | R 0/4 (0-49)<br>FA 0/24 (0-14) | R 0/4 (0-49)<br>FA 0/24 (0-14) |
| len_tokens | R 0/37 (0-9)<br>FA 4/37 (4-25) | R 4/40 (4-23)<br>FA 0/35 (0-10) | R 1/79 (0-7)<br>FA 1/73 (0-7) | R 0/25 (0-13)<br>FA 0/25 (0-13) | R 1/3 (6-79)<br>FA 1/5 (4-62) | R 0/4 (0-49)<br>FA 2/24 (2-26) | R 1/188 (0-3)<br>FA 7/199 (2-7) |
| len_steps | R 0/37 (0-9)<br>FA 1/37 (0-14) | R 13/40 (20-48)<br>FA 1/35 (1-15) | R 1/79 (0-7)<br>FA 0/73 (0-5) | R 0/25 (0-13)<br>FA 2/25 (2-25) | R 0/3 (0-56)<br>FA 0/5 (0-43) | R 0/4 (0-49)<br>FA 7/24 (15-49) | R 1/188 (0-3)<br>FA 6/199 (1-6) |

## Recall and false alarms at a 2% false-alarm budget

| reader | CheatBench | Goodfire | Terminal Wrench | deep_swe | C1-C5 | Sol/Astra | pooled |
|---|---|---|---|---|---|---|---|
| l0_tripwires | R 0/37 (0-9)<br>FA 0/37 (0-9) | R 3/40 (3-20)<br>FA 2/35 (2-19) | R 0/79 (0-5)<br>FA 0/73 (0-5) | R 0/25 (0-13)<br>FA 0/25 (0-13) | R 0/3 (0-56)<br>FA 0/5 (0-43) | R 0/4 (0-49)<br>FA 0/24 (0-14) | R 0/188 (0-2)<br>FA 0/199 (0-2) |
| ar_g38 | R 18/37 (33-64)<br>FA 2/37 (2-18) | R 2/40 (1-16)<br>FA 0/35 (0-10) | R 57/79 (61-81)<br>FA 4/73 (2-13) | R 3/25 (4-30)<br>FA 1/25 (1-20) | R 0/3 (0-56)<br>FA 0/5 (0-43) | n/a | R 79/184 (36-50)<br>FA 8/175 (2-9) |
| ar_g38_fixed | R 14/37 (24-54)<br>FA 0/37 (0-9) | R 0/40 (0-9)<br>FA 0/35 (0-10) | R 46/79 (47-68)<br>FA 2/73 (1-9) | R 2/25 (2-25)<br>FA 0/25 (0-13) | R 0/3 (0-56)<br>FA 0/5 (0-43) | n/a | R 64/184 (28-42)<br>FA 3/175 (1-5) |
| ar_fable | R 19/35 (38-70)<br>FA 2/26 (2-24) | R 1/6 (3-56)<br>FA 1/4 (5-70) | n/a | n/a | n/a | n/a | R 19/41 (32-61)<br>FA 2/30 (2-21) |
| dt15_masked | R 0/37 (0-9)<br>FA 0/37 (0-9) | R 0/40 (0-9)<br>FA 0/35 (0-10) | n/a | R 0/25 (0-13)<br>FA 0/25 (0-13) | n/a | n/a | R 0/102 (0-4)<br>FA 0/97 (0-4) |
| sol_astra_structured | n/a | n/a | n/a | n/a | n/a | R 4/4 (51-100)<br>FA 0/24 (0-14) | R 4/4 (51-100)<br>FA 0/24 (0-14) |
| sol_astra_flat | n/a | n/a | n/a | n/a | n/a | R 0/4 (0-49)<br>FA 0/24 (0-14) | R 0/4 (0-49)<br>FA 0/24 (0-14) |
| len_tokens | R 0/37 (0-9)<br>FA 4/37 (4-25) | R 4/40 (4-23)<br>FA 0/35 (0-10) | R 1/79 (0-7)<br>FA 1/73 (0-7) | R 0/25 (0-13)<br>FA 0/25 (0-13) | R 1/3 (6-79)<br>FA 1/5 (4-62) | R 0/4 (0-49)<br>FA 2/24 (2-26) | R 8/188 (2-8)<br>FA 12/199 (3-10) |
| len_steps | R 0/37 (0-9)<br>FA 1/37 (0-14) | R 13/40 (20-48)<br>FA 1/35 (1-15) | R 3/79 (1-11)<br>FA 1/73 (0-7) | R 0/25 (0-13)<br>FA 2/25 (2-25) | R 0/3 (0-56)<br>FA 0/5 (0-43) | R 0/4 (0-49)<br>FA 7/24 (15-49) | R 2/188 (0-4)<br>FA 10/199 (3-9) |

## Recall and false alarms at a 5% false-alarm budget

| reader | CheatBench | Goodfire | Terminal Wrench | deep_swe | C1-C5 | Sol/Astra | pooled |
|---|---|---|---|---|---|---|---|
| l0_tripwires | R 0/37 (0-9)<br>FA 0/37 (0-9) | R 3/40 (3-20)<br>FA 2/35 (2-19) | R 59/79 (64-83)<br>FA 1/73 (0-7) | R 0/25 (0-13)<br>FA 0/25 (0-13) | R 0/3 (0-56)<br>FA 0/5 (0-43) | R 0/4 (0-49)<br>FA 0/24 (0-14) | R 0/188 (0-2)<br>FA 0/199 (0-2) |
| ar_g38 | R 18/37 (33-64)<br>FA 2/37 (2-18) | R 2/40 (1-16)<br>FA 0/35 (0-10) | R 57/79 (61-81)<br>FA 4/73 (2-13) | R 3/25 (4-30)<br>FA 1/25 (1-20) | R 0/3 (0-56)<br>FA 0/5 (0-43) | n/a | R 86/184 (40-54)<br>FA 10/175 (3-10) |
| ar_g38_fixed | R 14/37 (24-54)<br>FA 0/37 (0-9) | R 0/40 (0-9)<br>FA 0/35 (0-10) | R 46/79 (47-68)<br>FA 2/73 (1-9) | R 2/25 (2-25)<br>FA 0/25 (0-13) | R 0/3 (0-56)<br>FA 0/5 (0-43) | n/a | R 64/184 (28-42)<br>FA 3/175 (1-5) |
| ar_fable | R 19/35 (38-70)<br>FA 2/26 (2-24) | R 1/6 (3-56)<br>FA 1/4 (5-70) | n/a | n/a | n/a | n/a | R 19/41 (32-61)<br>FA 2/30 (2-21) |
| dt15_masked | R 0/37 (0-9)<br>FA 0/37 (0-9) | R 4/40 (4-23)<br>FA 1/35 (1-15) | n/a | R 0/25 (0-13)<br>FA 0/25 (0-13) | n/a | n/a | R 0/102 (0-4)<br>FA 0/97 (0-4) |
| sol_astra_structured | n/a | n/a | n/a | n/a | n/a | R 4/4 (51-100)<br>FA 0/24 (0-14) | R 4/4 (51-100)<br>FA 0/24 (0-14) |
| sol_astra_flat | n/a | n/a | n/a | n/a | n/a | R 4/4 (51-100)<br>FA 0/24 (0-14) | R 4/4 (51-100)<br>FA 0/24 (0-14) |
| len_tokens | R 0/37 (0-9)<br>FA 7/37 (9-34) | R 14/40 (22-50)<br>FA 1/35 (1-15) | R 6/79 (4-16)<br>FA 3/73 (1-11) | R 0/25 (0-13)<br>FA 2/25 (2-25) | R 1/3 (6-79)<br>FA 1/5 (4-62) | R 0/4 (0-49)<br>FA 2/24 (2-26) | R 14/188 (4-12)<br>FA 17/199 (5-13) |
| len_steps | R 0/37 (0-9)<br>FA 2/37 (2-18) | R 14/40 (22-50)<br>FA 1/35 (1-15) | R 4/79 (2-12)<br>FA 1/73 (0-7) | R 1/25 (1-20)<br>FA 4/25 (6-35) | R 0/3 (0-56)<br>FA 0/5 (0-43) | R 0/4 (0-49)<br>FA 7/24 (15-49) | R 10/188 (3-10)<br>FA 17/199 (5-13) |

## Length-matched negatives (each positive paired with the nearest-length negative, per family and split)

| reader | CheatBench AUROC | Goodfire AUROC | Terminal Wrench AUROC | deep_swe AUROC | C1-C5 AUROC | Sol/Astra AUROC | pooled AUROC |
|---|---|---|---|---|---|---|---|
| l0_tripwires | 0.527 [0.46, 0.59] | 0.500 [0.44, 0.56] | 0.856 [0.80, 0.91] | 0.640 [0.52, 0.76] | 0.667 [0.50, 1.00] | 0.500 [0.50, 0.50] | 0.675 [0.63, 0.71] |
| ar_g38 | 0.727 [0.64, 0.81] | 0.529 [0.50, 0.57] | 0.834 [0.77, 0.89] | 0.716 [0.58, 0.84] | 0.722 [0.17, 1.00] | n/a | 0.732 [0.69, 0.77] |
| ar_g38_fixed | 0.689 [0.62, 0.77] | 0.500 [0.50, 0.50] | 0.767 [0.71, 0.82] | 0.540 [0.50, 0.60] | 0.667 [0.33, 1.00] | n/a | 0.662 [0.62, 0.70] |
| ar_fable | 0.885 [0.79, 0.96] | 0.594 [0.19, 1.00] | n/a | n/a | n/a | n/a | 0.852 [0.75, 0.93] |
| dt15_masked | 0.703 [0.61, 0.80] | 0.543 [0.49, 0.60] | n/a | 0.500 [0.50, 0.50] | n/a | n/a | 0.593 [0.55, 0.64] |
| sol_astra_structured | n/a | n/a | n/a | n/a | n/a | 1.000 [1.00, 1.00] | 1.000 [1.00, 1.00] |
| sol_astra_flat | n/a | n/a | n/a | n/a | n/a | 1.000 [1.00, 1.00] | 1.000 [1.00, 1.00] |
| len_tokens | 0.289 [0.18, 0.41] | 0.800 [0.69, 0.90] | 0.595 [0.50, 0.69] | 0.552 [0.39, 0.72] | 0.667 [0.11, 1.00] | 0.250 [0.00, 0.75] | 0.508 [0.45, 0.57] |
| len_steps | 0.227 [0.12, 0.34] | 0.713 [0.58, 0.84] | 0.602 [0.51, 0.69] | 0.430 [0.28, 0.59] | 0.889 [0.67, 1.00] | 0.125 [0.00, 0.38] | 0.470 [0.41, 0.53] |

Recall and false alarms at a 2% budget, length-matched:

| reader | CheatBench | Goodfire | Terminal Wrench | deep_swe | C1-C5 | Sol/Astra | pooled |
|---|---|---|---|---|---|---|---|
| l0_tripwires | R 0/37 (0-9)<br>FA 0/37 (0-9) | R 2/35 (2-19)<br>FA 2/35 (2-19) | R 0/73 (0-5)<br>FA 0/73 (0-5) | R 0/25 (0-13)<br>FA 0/25 (0-13) | R 0/3 (0-56)<br>FA 0/3 (0-56) | R 0/4 (0-49)<br>FA 0/4 (0-49) | R 0/177 (0-2)<br>FA 0/177 (0-2) |
| ar_g38 | R 18/37 (33-64)<br>FA 2/37 (2-18) | R 2/35 (2-19)<br>FA 0/35 (0-10) | R 52/73 (60-80)<br>FA 4/73 (2-13) | R 3/25 (4-30)<br>FA 1/25 (1-20) | R 2/3 (21-94)<br>FA 1/3 (6-79) | n/a | R 74/173 (36-50)<br>FA 8/173 (2-9) |
| ar_g38_fixed | R 14/37 (24-54)<br>FA 0/37 (0-9) | R 0/35 (0-10)<br>FA 0/35 (0-10) | R 41/73 (45-67)<br>FA 2/73 (1-9) | R 2/25 (2-25)<br>FA 0/25 (0-13) | R 2/3 (21-94)<br>FA 1/3 (6-79) | n/a | R 59/173 (27-41)<br>FA 3/173 (1-5) |
| ar_fable | R 14/26 (35-71)<br>FA 2/26 (2-24) | R 0/4 (0-49)<br>FA 1/4 (5-70) | n/a | n/a | n/a | n/a | R 14/30 (30-64)<br>FA 2/30 (2-21) |
| dt15_masked | R 0/37 (0-9)<br>FA 0/37 (0-9) | R 0/35 (0-10)<br>FA 0/35 (0-10) | n/a | R 0/25 (0-13)<br>FA 0/25 (0-13) | n/a | n/a | R 0/97 (0-4)<br>FA 0/97 (0-4) |
| sol_astra_structured | n/a | n/a | n/a | n/a | n/a | R 4/4 (51-100)<br>FA 0/4 (0-49) | R 4/4 (51-100)<br>FA 0/4 (0-49) |
| sol_astra_flat | n/a | n/a | n/a | n/a | n/a | R 0/4 (0-49)<br>FA 0/4 (0-49) | R 0/4 (0-49)<br>FA 0/4 (0-49) |
| len_tokens | R 0/37 (0-9)<br>FA 4/37 (4-25) | R 0/35 (0-10)<br>FA 0/35 (0-10) | R 0/73 (0-5)<br>FA 1/73 (0-7) | R 0/25 (0-13)<br>FA 0/25 (0-13) | R 2/3 (21-94)<br>FA 1/3 (6-79) | R 0/4 (0-49)<br>FA 3/4 (30-95) | R 7/177 (2-8)<br>FA 11/177 (4-11) |
| len_steps | R 0/37 (0-9)<br>FA 1/37 (0-14) | R 8/35 (12-39)<br>FA 1/35 (1-15) | R 1/73 (0-7)<br>FA 1/73 (0-7) | R 0/25 (0-13)<br>FA 2/25 (2-25) | R 0/3 (0-56)<br>FA 0/3 (0-56) | R 0/4 (0-49)<br>FA 3/4 (30-95) | R 2/177 (0-4)<br>FA 8/177 (2-9) |

## Paired comparisons (exact McNemar on the shared covered `report` cases)

`b` = cases only reader A alerts on; `c` = cases only reader B alerts on. Each reader uses its own tune threshold at the stated budget. Positives test recall; negatives test false alarms.

| A | B | budget | scope | recall b/c | false alarms b/c | n pos | n neg |
|---|---|---|---|---|---|---|---|
| ar_g38 | len_tokens | 2% | pooled | 77/6 p=0.000 | 8/11 p=0.648 | 184 | 175 |
| ar_g38 | len_tokens | 2% | CheatBench | 18/0 p=0.000 | 2/4 p=0.688 | 37 | 37 |
| ar_g38 | len_tokens | 2% | Goodfire | 2/4 p=0.688 | 0/0 p=1.000 | 40 | 35 |
| ar_g38 | len_tokens | 2% | Terminal Wrench | 56/0 p=0.000 | 3/0 p=0.250 | 79 | 73 |
| ar_g38 | len_tokens | 2% | deep_swe | 3/0 p=0.250 | 1/0 p=1.000 | 25 | 25 |
| ar_g38 | len_tokens | 5% | pooled | 79/6 p=0.000 | 10/16 p=0.327 | 184 | 175 |
| ar_g38 | len_tokens | 5% | CheatBench | 18/0 p=0.000 | 2/7 p=0.180 | 37 | 37 |
| ar_g38 | len_tokens | 5% | Goodfire | 2/14 p=0.004 | 0/1 p=1.000 | 40 | 35 |
| ar_g38 | len_tokens | 5% | Terminal Wrench | 52/1 p=0.000 | 3/2 p=1.000 | 79 | 73 |
| ar_g38 | len_tokens | 5% | deep_swe | 3/0 p=0.250 | 1/2 p=1.000 | 25 | 25 |
| ar_g38 | l0_tripwires | 2% | pooled | 79/0 p=0.000 | 8/0 p=0.008 | 184 | 175 |
| ar_g38 | l0_tripwires | 2% | CheatBench | 18/0 p=0.000 | 2/0 p=0.500 | 37 | 37 |
| ar_g38 | l0_tripwires | 2% | Goodfire | 2/3 p=1.000 | 0/2 p=0.500 | 40 | 35 |
| ar_g38 | l0_tripwires | 2% | Terminal Wrench | 57/0 p=0.000 | 4/0 p=0.125 | 79 | 73 |
| ar_g38 | l0_tripwires | 2% | deep_swe | 3/0 p=0.250 | 1/0 p=1.000 | 25 | 25 |
| ar_g38 | l0_tripwires | 5% | pooled | 86/0 p=0.000 | 10/0 p=0.002 | 184 | 175 |
| ar_g38 | l0_tripwires | 5% | CheatBench | 18/0 p=0.000 | 2/0 p=0.500 | 37 | 37 |
| ar_g38 | l0_tripwires | 5% | Goodfire | 2/3 p=1.000 | 0/2 p=0.500 | 40 | 35 |
| ar_g38 | l0_tripwires | 5% | Terminal Wrench | 14/16 p=0.856 | 4/1 p=0.375 | 79 | 73 |
| ar_g38 | l0_tripwires | 5% | deep_swe | 3/0 p=0.250 | 1/0 p=1.000 | 25 | 25 |
| ar_g38 | ar_g38_fixed | 2% | pooled | 15/0 p=0.000 | 5/0 p=0.062 | 184 | 175 |
| ar_g38 | ar_g38_fixed | 2% | CheatBench | 4/0 p=0.125 | 2/0 p=0.500 | 37 | 37 |
| ar_g38 | ar_g38_fixed | 2% | Goodfire | 2/0 p=0.500 | 0/0 p=1.000 | 40 | 35 |
| ar_g38 | ar_g38_fixed | 2% | Terminal Wrench | 11/0 p=0.001 | 2/0 p=0.500 | 79 | 73 |
| ar_g38 | ar_g38_fixed | 2% | deep_swe | 1/0 p=1.000 | 1/0 p=1.000 | 25 | 25 |
| ar_g38 | ar_g38_fixed | 5% | pooled | 22/0 p=0.000 | 7/0 p=0.016 | 184 | 175 |
| ar_g38 | ar_g38_fixed | 5% | CheatBench | 4/0 p=0.125 | 2/0 p=0.500 | 37 | 37 |
| ar_g38 | ar_g38_fixed | 5% | Goodfire | 2/0 p=0.500 | 0/0 p=1.000 | 40 | 35 |
| ar_g38 | ar_g38_fixed | 5% | Terminal Wrench | 11/0 p=0.001 | 2/0 p=0.500 | 79 | 73 |
| ar_g38 | ar_g38_fixed | 5% | deep_swe | 1/0 p=1.000 | 1/0 p=1.000 | 25 | 25 |
| ar_g38 | ar_fable | 2% | pooled | 2/5 p=0.453 | 1/1 p=1.000 | 41 | 30 |
| ar_g38 | ar_fable | 2% | CheatBench | 2/5 p=0.453 | 1/1 p=1.000 | 35 | 26 |
| ar_g38 | ar_fable | 2% | Goodfire | 0/1 p=1.000 | 0/1 p=1.000 | 6 | 4 |
| ar_g38 | ar_fable | 5% | pooled | 2/5 p=0.453 | 1/1 p=1.000 | 41 | 30 |
| ar_g38 | ar_fable | 5% | CheatBench | 2/5 p=0.453 | 1/1 p=1.000 | 35 | 26 |
| ar_g38 | ar_fable | 5% | Goodfire | 0/1 p=1.000 | 0/1 p=1.000 | 6 | 4 |
| ar_g38 | dt15_masked | 2% | pooled | 20/0 p=0.000 | 2/0 p=0.500 | 102 | 97 |
| ar_g38 | dt15_masked | 2% | CheatBench | 18/0 p=0.000 | 2/0 p=0.500 | 37 | 37 |
| ar_g38 | dt15_masked | 2% | Goodfire | 2/0 p=0.500 | 0/0 p=1.000 | 40 | 35 |
| ar_g38 | dt15_masked | 2% | deep_swe | 3/0 p=0.250 | 1/0 p=1.000 | 25 | 25 |
| ar_g38 | dt15_masked | 5% | pooled | 27/0 p=0.000 | 4/0 p=0.125 | 102 | 97 |
| ar_g38 | dt15_masked | 5% | CheatBench | 18/0 p=0.000 | 2/0 p=0.500 | 37 | 37 |
| ar_g38 | dt15_masked | 5% | Goodfire | 1/3 p=0.625 | 0/1 p=1.000 | 40 | 35 |
| ar_g38 | dt15_masked | 5% | deep_swe | 3/0 p=0.250 | 1/0 p=1.000 | 25 | 25 |
| l0_tripwires | len_tokens | 2% | pooled | 0/8 p=0.008 | 0/12 p=0.000 | 188 | 199 |
| l0_tripwires | len_tokens | 2% | CheatBench | 0/0 p=1.000 | 0/4 p=0.125 | 37 | 37 |
| l0_tripwires | len_tokens | 2% | Goodfire | 2/3 p=1.000 | 2/0 p=0.500 | 40 | 35 |
| l0_tripwires | len_tokens | 2% | Terminal Wrench | 0/1 p=1.000 | 0/1 p=1.000 | 79 | 73 |
| l0_tripwires | len_tokens | 2% | deep_swe | 0/0 p=1.000 | 0/0 p=1.000 | 25 | 25 |
| l0_tripwires | len_tokens | 5% | pooled | 0/14 p=0.000 | 0/17 p=0.000 | 188 | 199 |
| l0_tripwires | len_tokens | 5% | CheatBench | 0/0 p=1.000 | 0/7 p=0.016 | 37 | 37 |
| l0_tripwires | len_tokens | 5% | Goodfire | 1/12 p=0.003 | 1/0 p=1.000 | 40 | 35 |
| l0_tripwires | len_tokens | 5% | Terminal Wrench | 53/0 p=0.000 | 1/3 p=0.625 | 79 | 73 |
| l0_tripwires | len_tokens | 5% | deep_swe | 0/0 p=1.000 | 0/2 p=0.500 | 25 | 25 |
