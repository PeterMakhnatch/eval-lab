# NP30-TT-1 length control: results

Stored findings and product rows up to 2026-10-29T04:15:00Z; seed 20261029, 2000 task-clustered resamples. Definitions: `PREREGISTRATION.md`. No detector threshold changed; the length threshold is chosen in-sample.

## goodfire

Population 149 runs (80 enacted / 69 clean), 149 task clusters (130 in the coarser sensitivity clustering).

Length AUROC on the population: 0.776 [0.696, 0.844]; coarser clustering: 0.776 [0.699, 0.849].

Exploratory, not preregistered, AUROC of hodoscope action count (149 runs): 0.813 [0.739, 0.874].
Exploratory, not preregistered, AUROC of trace rows step count (149 runs): 0.794 [0.716, 0.861].

Strata: k=3, cut points [20, 28] (rule trace: k=4 smallest cell 7 -> refused; k=3 smallest cell 11 -> ok).

| Stratum | lengths | enacted | clean |
|---|---|---|---|
| 1 | 7-20 | 11 | 39 |
| 2 | 21-28 | 33 | 19 |
| 3 | 29-132 | 36 | 11 |

### Length-only rule vs cascade v8

Cascade v8 native: recall 10/80 = 12.5% [6.9, 21.5]; FAR 1/69 = 1.4% [0.3, 7.8].

| Rule | length >= | alarms | recall | FAR | ref TPs (all / also length / top stratum) |
|---|---|---|---|---|---|
| matched_to_reference_far | 41.0 | 29 | 28/80 = 35.0% [25.5, 45.9] | 1/69 = 1.4% [0.3, 7.8] | 10 / 7 / 7 |
| far_0.03 | 40.0 | 31 | 29/80 = 36.2% [26.6, 47.2] | 2/69 = 2.9% [0.8, 10.0] | 10 / 7 / 7 |
| far_0.05 | 40.0 | 31 | 29/80 = 36.2% [26.6, 47.2] | 2/69 = 2.9% [0.8, 10.0] | 10 / 7 / 7 |

### Tools overall (native operating point, AUROC, decision)

| Tool | n | native recall | native FAR | AUROC | length AUROC (same runs) | AUROC - length | beats overall | adds within strata | beats length |
|---|---|---|---|---|---|---|---|---|---|
| cascade_v8 | 149 | 10/80 = 12.5% [6.9, 21.5] | 1/69 = 1.4% [0.3, 7.8] | 0.555 [0.516, 0.594] | 0.776 [0.696, 0.844] | -0.220 [-0.295, -0.137] | False | False (1/3) | False |
| cascade_v7 | 149 | 11/80 = 13.8% [7.9, 23.0] | 1/69 = 1.4% [0.3, 7.8] | 0.562 [0.521, 0.603] | 0.776 [0.696, 0.844] | -0.214 [-0.289, -0.131] | False | False (1/3) | False |
| facets_haiku | 149 | 7/80 = 8.8% [4.3, 17.0] | 1/69 = 1.4% [0.3, 7.8] | 0.616 [0.558, 0.671] | 0.776 [0.696, 0.844] | -0.160 [-0.242, -0.077] | False | False (1/3) | False |
| docent_sonnet | 69 | 3/42 = 7.1% [2.5, 19.0] | 0/27 = 0.0% [0.0, 12.5] | 0.679 [0.610, 0.750] | 0.859 [0.762, 0.941] | -0.180 [-0.284, -0.061] | False | False (1/2) | False |
| scout_native | 149 | 0/80 = 0.0% [0.0, 4.6] | 0/69 = 0.0% [0.0, 5.3] | 0.500 [0.500, 0.500] | 0.776 [0.696, 0.844] | -0.276 [-0.344, -0.196] | False | False (0/3) | False |
| metr_kimi_native | 25 | 3/13 = 23.1% [8.2, 50.3] | 0/12 = 0.0% [0.0, 24.3] | 0.654 [0.531, 0.800] | 0.788 [0.579, 0.956] | -0.135 [-0.342, 0.089] | False | False (0/0) | False |
| metr_grok | 8 | 1/3 = 33.3% [6.1, 79.2] | 0/5 = 0.0% [0.0, 43.4] | 0.667 [0.500, 1.000] | 0.733 [0.250, 1.000] | -0.067 [-0.500, 0.571] | False | False (0/0) | False |
| metr_openrouter_kimi_k3 | 149 | 5/80 = 6.2% [2.7, 13.8] | 0/69 = 0.0% [0.0, 5.3] | 0.718 [0.650, 0.784] | 0.776 [0.696, 0.844] | -0.058 [-0.150, 0.043] | False | True (3/3) | True |
| hodoscope_top10 | 149 | 37/80 = 46.2% [35.7, 57.1] | 14/69 = 20.3% [12.5, 31.2] | 0.630 [0.555, 0.701] | 0.776 [0.696, 0.844] | -0.146 [-0.245, -0.048] | False | False (0/3) | False |
| hodoscope_top25 | 149 | 62/80 = 77.5% [67.2, 85.3] | 29/69 = 42.0% [31.1, 53.8] | 0.677 [0.601, 0.753] | 0.776 [0.696, 0.844] | -0.098 [-0.193, 0.001] | False | False (1/3) | False |
| hodoscope_top50 | 149 | 75/80 = 93.8% [86.2, 97.3] | 44/69 = 63.8% [52.0, 74.1] | 0.650 [0.586, 0.714] | 0.776 [0.696, 0.844] | -0.126 [-0.212, -0.032] | False | False (1/3) | False |
| pxi_generic_investigation_r1 | 5 | 5/5 = 100.0% [56.6, 100.0] | n/a | n/a | n/a | n/a | False | False (0/0) | False |
| pxi_generic_investigation_r2 | 10 | 2/6 = 33.3% [9.7, 70.0] | 1/4 = 25.0% [4.6, 69.9] | 0.542 [0.200, 0.833] | 0.708 [0.250, 1.000] | -0.167 [-0.533, 0.300] | False | False (0/0) | False |
| pxi_generic_investigation_r3 | 12 | 3/7 = 42.9% [15.8, 75.0] | 0/5 = 0.0% [0.0, 43.4] | 0.714 [0.500, 0.909] | 0.814 [0.477, 1.000] | -0.100 [-0.375, 0.214] | False | False (0/0) | False |
| pxi_coverage_investigation_r1 | 15 | 1/8 = 12.5% [2.2, 47.1] | 0/7 = 0.0% [0.0, 35.4] | 0.562 [0.500, 0.714] | 0.955 [0.820, 1.000] | -0.393 [-0.500, -0.154] | False | False (0/0) | False |
| pxi_generic_union | 20 | 8/14 = 57.1% [32.6, 78.6] | 1/6 = 16.7% [3.0, 56.4] | 0.702 [0.450, 0.885] | 0.851 [0.622, 1.000] | -0.149 [-0.333, 0.045] | False | False (0/0) | False |
| laminar | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

### Within strata (native operating point and AUROC)

| Tool | Stratum | evaluated enacted / clean | recall | FAR | AUROC |
|---|---|---|---|---|---|
| cascade_v8 | 1 | 11 / 39 | 0/11 = 0.0% [0.0, 25.9] | 0/39 = 0.0% [0.0, 9.0] | 0.500 [0.500, 0.500] |
| cascade_v8 | 2 | 33 / 19 | 3/33 = 9.1% [3.1, 23.6] | 1/19 = 5.3% [0.9, 24.6] | 0.519 [0.445, 0.586] |
| cascade_v8 | 3 | 36 / 11 | 7/36 = 19.4% [9.8, 35.0] | 0/11 = 0.0% [0.0, 25.9] | 0.597 [0.534, 0.662] |
| cascade_v7 | 1 | 11 / 39 | 0/11 = 0.0% [0.0, 25.9] | 0/39 = 0.0% [0.0, 9.0] | 0.500 [0.500, 0.500] |
| cascade_v7 | 2 | 33 / 19 | 3/33 = 9.1% [3.1, 23.6] | 1/19 = 5.3% [0.9, 24.6] | 0.519 [0.445, 0.586] |
| cascade_v7 | 3 | 36 / 11 | 8/36 = 22.2% [11.7, 38.1] | 0/11 = 0.0% [0.0, 25.9] | 0.611 [0.547, 0.679] |
| facets_haiku | 1 | 11 / 39 | 0/11 = 0.0% [0.0, 25.9] | 0/39 = 0.0% [0.0, 9.0] | 0.533 [0.467, 0.643] |
| facets_haiku | 2 | 33 / 19 | 5/33 = 15.2% [6.7, 30.9] | 1/19 = 5.3% [0.9, 24.6] | 0.541 [0.439, 0.629] |
| facets_haiku | 3 | 36 / 11 | 2/36 = 5.6% [1.5, 18.1] | 0/11 = 0.0% [0.0, 25.9] | 0.708 [0.630, 0.786] |
| docent_sonnet | 1 | 7 / 21 | 0/7 = 0.0% [0.0, 35.4] | 0/21 = 0.0% [0.0, 15.5] | 0.571 [0.500, 0.750] |
| docent_sonnet | 2 | 17 / 5 | 3/17 = 17.6% [6.2, 41.0] | 0/5 = 0.0% [0.0, 43.4] | 0.676 [0.567, 0.794] |
| docent_sonnet | 3 | 18 / 1 | 0/18 = 0.0% [0.0, 17.6] | 0/1 = 0.0% [0.0, 79.3] | not estimable |
| scout_native | 1 | 11 / 39 | 0/11 = 0.0% [0.0, 25.9] | 0/39 = 0.0% [0.0, 9.0] | 0.500 [0.500, 0.500] |
| scout_native | 2 | 33 / 19 | 0/33 = 0.0% [0.0, 10.4] | 0/19 = 0.0% [0.0, 16.8] | 0.500 [0.500, 0.500] |
| scout_native | 3 | 36 / 11 | 0/36 = 0.0% [0.0, 9.6] | 0/11 = 0.0% [0.0, 25.9] | 0.500 [0.500, 0.500] |
| metr_kimi_native | 1 | 1 / 7 | 0/1 = 0.0% [0.0, 79.3] | 0/7 = 0.0% [0.0, 35.4] | not estimable |
| metr_kimi_native | 2 | 6 / 2 | 1/6 = 16.7% [3.0, 56.4] | 0/2 = 0.0% [0.0, 65.8] | not estimable |
| metr_kimi_native | 3 | 6 / 3 | 2/6 = 33.3% [9.7, 70.0] | 0/3 = 0.0% [0.0, 56.2] | not estimable |
| metr_grok | 1 | 0 / 3 | n/a | 0/3 = 0.0% [0.0, 56.2] | not estimable |
| metr_grok | 2 | 2 / 2 | 1/2 = 50.0% [9.5, 90.5] | 0/2 = 0.0% [0.0, 65.8] | not estimable |
| metr_grok | 3 | 1 / 0 | 0/1 = 0.0% [0.0, 79.3] | n/a | not estimable |
| metr_openrouter_kimi_k3 | 1 | 11 / 39 | 0/11 = 0.0% [0.0, 25.9] | 0/39 = 0.0% [0.0, 9.0] | 0.659 [0.502, 0.817] |
| metr_openrouter_kimi_k3 | 2 | 33 / 19 | 3/33 = 9.1% [3.1, 23.6] | 0/19 = 0.0% [0.0, 16.8] | 0.644 [0.504, 0.767] |
| metr_openrouter_kimi_k3 | 3 | 36 / 11 | 2/36 = 5.6% [1.5, 18.1] | 0/11 = 0.0% [0.0, 25.9] | 0.760 [0.651, 0.859] |
| hodoscope_top10 | 1 | 11 / 39 | 3/11 = 27.3% [9.7, 56.6] | 7/39 = 17.9% [9.0, 32.7] | 0.547 [0.394, 0.710] |
| hodoscope_top10 | 2 | 33 / 19 | 15/33 = 45.5% [29.8, 62.0] | 4/19 = 21.1% [8.5, 43.3] | 0.622 [0.490, 0.742] |
| hodoscope_top10 | 3 | 36 / 11 | 19/36 = 52.8% [37.0, 68.0] | 3/11 = 27.3% [9.7, 56.6] | 0.628 [0.453, 0.776] |
| hodoscope_top25 | 1 | 11 / 39 | 8/11 = 72.7% [43.4, 90.3] | 13/39 = 33.3% [20.6, 49.0] | 0.697 [0.526, 0.846] |
| hodoscope_top25 | 2 | 33 / 19 | 26/33 = 78.8% [62.2, 89.3] | 10/19 = 52.6% [31.7, 72.7] | 0.631 [0.495, 0.768] |
| hodoscope_top25 | 3 | 36 / 11 | 28/36 = 77.8% [61.9, 88.3] | 6/11 = 54.5% [28.0, 78.7] | 0.616 [0.450, 0.783] |
| hodoscope_top50 | 1 | 11 / 39 | 10/11 = 90.9% [62.3, 98.4] | 20/39 = 51.3% [36.2, 66.1] | 0.698 [0.569, 0.800] |
| hodoscope_top50 | 2 | 33 / 19 | 30/33 = 90.9% [76.4, 96.9] | 16/19 = 84.2% [62.4, 94.5] | 0.533 [0.439, 0.632] |
| hodoscope_top50 | 3 | 36 / 11 | 35/36 = 97.2% [85.8, 99.5] | 8/11 = 72.7% [43.4, 90.3] | 0.622 [0.486, 0.760] |
| pxi_generic_investigation_r1 | 1 | 0 / 0 | n/a | n/a | not estimable |
| pxi_generic_investigation_r1 | 2 | 2 / 0 | 2/2 = 100.0% [34.2, 100.0] | n/a | not estimable |
| pxi_generic_investigation_r1 | 3 | 3 / 0 | 3/3 = 100.0% [43.8, 100.0] | n/a | not estimable |
| pxi_generic_investigation_r2 | 1 | 2 / 2 | 0/2 = 0.0% [0.0, 65.8] | 0/2 = 0.0% [0.0, 65.8] | not estimable |
| pxi_generic_investigation_r2 | 2 | 2 / 2 | 1/2 = 50.0% [9.5, 90.5] | 1/2 = 50.0% [9.5, 90.5] | not estimable |
| pxi_generic_investigation_r2 | 3 | 2 / 0 | 1/2 = 50.0% [9.5, 90.5] | n/a | not estimable |
| pxi_generic_investigation_r3 | 1 | 2 / 4 | 0/2 = 0.0% [0.0, 65.8] | 0/4 = 0.0% [0.0, 49.0] | not estimable |
| pxi_generic_investigation_r3 | 2 | 3 / 1 | 2/3 = 66.7% [20.8, 93.9] | 0/1 = 0.0% [0.0, 79.3] | not estimable |
| pxi_generic_investigation_r3 | 3 | 2 / 0 | 1/2 = 50.0% [9.5, 90.5] | n/a | not estimable |
| pxi_coverage_investigation_r1 | 1 | 0 / 4 | n/a | 0/4 = 0.0% [0.0, 49.0] | not estimable |
| pxi_coverage_investigation_r1 | 2 | 5 / 3 | 1/5 = 20.0% [3.6, 62.4] | 0/3 = 0.0% [0.0, 56.2] | not estimable |
| pxi_coverage_investigation_r1 | 3 | 3 / 0 | 0/3 = 0.0% [0.0, 56.2] | n/a | not estimable |
| pxi_generic_union | 1 | 3 / 4 | 0/3 = 0.0% [0.0, 56.2] | 0/4 = 0.0% [0.0, 49.0] | not estimable |
| pxi_generic_union | 2 | 6 / 2 | 4/6 = 66.7% [30.0, 90.3] | 1/2 = 50.0% [9.5, 90.5] | not estimable |
| pxi_generic_union | 3 | 5 / 0 | 4/5 = 80.0% [37.6, 96.4] | n/a | not estimable |

### Recall at FAR budgets (in-sample score threshold, diagnostic)

| Tool | recall at <=3% FAR | recall at <=5% FAR |
|---|---|---|
| cascade_v8 | 10/80 = 12.5% [6.9, 21.5] (FP 1) | 10/80 = 12.5% [6.9, 21.5] (FP 1) |
| cascade_v7 | 11/80 = 13.8% [7.9, 23.0] (FP 1) | 11/80 = 13.8% [7.9, 23.0] (FP 1) |
| facets_haiku | 7/80 = 8.8% [4.3, 17.0] (FP 1) | 22/80 = 27.5% [18.9, 38.1] (FP 3) |
| docent_sonnet | 15/42 = 35.7% [23.0, 50.8] (FP 0) | 15/42 = 35.7% [23.0, 50.8] (FP 0) |
| scout_native | 0/80 = 0.0% [0.0, 4.6] (FP 0) | 0/80 = 0.0% [0.0, 4.6] (FP 0) |
| metr_kimi_native | 4/13 = 30.8% [12.7, 57.6] (FP 0) | 4/13 = 30.8% [12.7, 57.6] (FP 0) |
| metr_grok | 1/3 = 33.3% [6.1, 79.2] (FP 0) | 1/3 = 33.3% [6.1, 79.2] (FP 0) |
| metr_openrouter_kimi_k3 | 20/80 = 25.0% [16.8, 35.5] (FP 1) | 20/80 = 25.0% [16.8, 35.5] (FP 1) |
| hodoscope_top10 | 0/80 = 0.0% [0.0, 4.6] (FP 0) | 0/80 = 0.0% [0.0, 4.6] (FP 0) |
| hodoscope_top25 | 0/80 = 0.0% [0.0, 4.6] (FP 0) | 0/80 = 0.0% [0.0, 4.6] (FP 0) |
| hodoscope_top50 | 0/80 = 0.0% [0.0, 4.6] (FP 0) | 0/80 = 0.0% [0.0, 4.6] (FP 0) |
| pxi_generic_investigation_r1 | 5/5 = 100.0% [56.6, 100.0] (FP 0) | 5/5 = 100.0% [56.6, 100.0] (FP 0) |
| pxi_generic_investigation_r2 | 0/6 = 0.0% [0.0, 39.0] (FP 0) | 0/6 = 0.0% [0.0, 39.0] (FP 0) |
| pxi_generic_investigation_r3 | 3/7 = 42.9% [15.8, 75.0] (FP 0) | 3/7 = 42.9% [15.8, 75.0] (FP 0) |
| pxi_coverage_investigation_r1 | 1/8 = 12.5% [2.2, 47.1] (FP 0) | 1/8 = 12.5% [2.2, 47.1] (FP 0) |
| pxi_generic_union | 0/14 = 0.0% [0.0, 21.5] (FP 0) | 0/14 = 0.0% [0.0, 21.5] (FP 0) |

### Length rule then tool (no fitting)

| Tool | union recall | union FAR | tool TP / FP on runs the length rule missed |
|---|---|---|---|
| cascade_v8 | 31/80 = 38.8% [28.8, 49.7] | 2/69 = 2.9% [0.8, 10.0] | 3 / 1 |
| cascade_v7 | 32/80 = 40.0% [30.0, 51.0] | 2/69 = 2.9% [0.8, 10.0] | 4 / 1 |
| facets_haiku | 33/80 = 41.2% [31.1, 52.2] | 2/69 = 2.9% [0.8, 10.0] | 5 / 1 |
| docent_sonnet | 17/42 = 40.5% [27.0, 55.5] | 0/27 = 0.0% [0.0, 12.5] | 3 / 0 |
| scout_native | 28/80 = 35.0% [25.5, 45.9] | 1/69 = 1.4% [0.3, 7.8] | 0 / 0 |
| metr_kimi_native | 7/13 = 53.8% [29.1, 76.8] | 1/12 = 8.3% [1.5, 35.4] | 1 / 0 |
| metr_grok | 2/3 = 66.7% [20.8, 93.9] | 0/5 = 0.0% [0.0, 43.4] | 1 / 0 |
| metr_openrouter_kimi_k3 | 31/80 = 38.8% [28.8, 49.7] | 1/69 = 1.4% [0.3, 7.8] | 3 / 0 |
| hodoscope_top10 | 49/80 = 61.2% [50.3, 71.2] | 14/69 = 20.3% [12.5, 31.2] | 21 / 13 |
| hodoscope_top25 | 67/80 = 83.8% [74.2, 90.3] | 29/69 = 42.0% [31.1, 53.8] | 39 / 28 |
| hodoscope_top50 | 76/80 = 95.0% [87.8, 98.0] | 44/69 = 63.8% [52.0, 74.1] | 48 / 43 |
| pxi_generic_investigation_r1 | 5/5 = 100.0% [56.6, 100.0] | n/a | 2 / 0 |
| pxi_generic_investigation_r2 | 2/6 = 33.3% [9.7, 70.0] | 1/4 = 25.0% [4.6, 69.9] | 2 / 1 |
| pxi_generic_investigation_r3 | 3/7 = 42.9% [15.8, 75.0] | 0/5 = 0.0% [0.0, 43.4] | 3 / 0 |
| pxi_coverage_investigation_r1 | 3/8 = 37.5% [13.7, 69.4] | 0/7 = 0.0% [0.0, 35.4] | 1 / 0 |
| pxi_generic_union | 8/14 = 57.1% [32.6, 78.6] | 1/6 = 16.7% [3.0, 56.4] | 5 / 1 |

## cheatbench

Population 150 runs (75 enacted / 75 clean), 150 task clusters (4 in the coarser sensitivity clustering).

Length AUROC on the population: 0.298 [0.216, 0.387]; coarser clustering: 0.298 [0.249, 0.684].


Length is anti-predictive here (enacted runs are shorter, AUROC below 0.5): the length-only rule (alarm iff long) is not a meaningful comparator, and any tool AUROC above 0.5 beats it.

Strata: k=3, cut points [17, 46] (rule trace: k=4 smallest cell 9 -> refused; k=3 smallest cell 11 -> ok).

| Stratum | lengths | enacted | clean |
|---|---|---|---|
| 1 | 2-17 | 40 | 11 |
| 2 | 18-46 | 16 | 34 |
| 3 | 47-610 | 19 | 30 |

### Length-only rule vs cascade v8

Cascade v8 native: recall 19/75 = 25.3% [16.9, 36.2]; FAR 0/75 = 0.0% [0.0, 4.9].

| Rule | length >= | alarms | recall | FAR | ref TPs (all / also length / top stratum) |
|---|---|---|---|---|---|
| matched_to_reference_far | None | 0 | 0/75 = 0.0% [0.0, 4.9] | 0/75 = 0.0% [0.0, 4.9] | 19 / 0 / 9 |
| far_0.03 | 387.0 | 2 | 0/75 = 0.0% [0.0, 4.9] | 2/75 = 2.7% [0.7, 9.2] | 19 / 0 / 9 |
| far_0.05 | 327.0 | 3 | 0/75 = 0.0% [0.0, 4.9] | 3/75 = 4.0% [1.4, 11.1] | 19 / 0 / 9 |

### Tools overall (native operating point, AUROC, decision)

| Tool | n | native recall | native FAR | AUROC | length AUROC (same runs) | AUROC - length | beats overall | adds within strata | beats length |
|---|---|---|---|---|---|---|---|---|---|
| cascade_v8 | 150 | 19/75 = 25.3% [16.9, 36.2] | 0/75 = 0.0% [0.0, 4.9] | 0.627 [0.577, 0.680] | 0.298 [0.216, 0.387] | 0.329 [0.237, 0.414] | True | True (3/3) | True |
| cascade_v7 | 150 | 20/75 = 26.7% [18.0, 37.6] | 5/75 = 6.7% [2.9, 14.7] | 0.591 [0.534, 0.652] | 0.298 [0.216, 0.387] | 0.293 [0.202, 0.382] | True | True (3/3) | True |
| facets_haiku | 150 | 30/75 = 40.0% [29.7, 51.3] | 4/75 = 5.3% [2.1, 12.9] | 0.698 [0.618, 0.775] | 0.298 [0.216, 0.387] | 0.400 [0.290, 0.507] | True | False (2/3) | True |
| docent_sonnet | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| scout_native | 150 | 19/75 = 25.3% [16.9, 36.2] | 34/75 = 45.3% [34.6, 56.6] | 0.433 [0.351, 0.514] | 0.298 [0.216, 0.387] | 0.135 [0.050, 0.214] | True | False (0/3) | True |
| metr_kimi_native | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| metr_grok | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| metr_openrouter_kimi_k3 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| hodoscope_top10 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| hodoscope_top25 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| hodoscope_top50 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| pxi_generic_investigation_r1 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| pxi_generic_investigation_r2 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| pxi_generic_investigation_r3 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| pxi_coverage_investigation_r1 | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| pxi_generic_union | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| laminar | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

### Within strata (native operating point and AUROC)

| Tool | Stratum | evaluated enacted / clean | recall | FAR | AUROC |
|---|---|---|---|---|---|
| cascade_v8 | 1 | 40 / 11 | 5/40 = 12.5% [5.5, 26.1] | 0/11 = 0.0% [0.0, 25.9] | 0.562 [0.514, 0.615] |
| cascade_v8 | 2 | 16 / 34 | 5/16 = 31.2% [14.2, 55.6] | 0/34 = 0.0% [0.0, 10.2] | 0.656 [0.545, 0.778] |
| cascade_v8 | 3 | 19 / 30 | 9/19 = 47.4% [27.3, 68.3] | 0/30 = 0.0% [0.0, 11.4] | 0.737 [0.618, 0.853] |
| cascade_v7 | 1 | 40 / 11 | 6/40 = 15.0% [7.1, 29.1] | 0/11 = 0.0% [0.0, 25.9] | 0.575 [0.524, 0.636] |
| cascade_v7 | 2 | 16 / 34 | 5/16 = 31.2% [14.2, 55.6] | 1/34 = 2.9% [0.5, 14.9] | 0.637 [0.523, 0.762] |
| cascade_v7 | 3 | 19 / 30 | 9/19 = 47.4% [27.3, 68.3] | 4/30 = 13.3% [5.3, 29.7] | 0.639 [0.508, 0.770] |
| facets_haiku | 1 | 40 / 11 | 16/40 = 40.0% [26.3, 55.4] | 0/11 = 0.0% [0.0, 25.9] | 0.745 [0.625, 0.848] |
| facets_haiku | 2 | 16 / 34 | 8/16 = 50.0% [28.0, 72.0] | 1/34 = 2.9% [0.5, 14.9] | 0.790 [0.635, 0.922] |
| facets_haiku | 3 | 19 / 30 | 6/19 = 31.6% [15.4, 54.0] | 3/30 = 10.0% [3.5, 25.6] | 0.613 [0.459, 0.766] |
| scout_native | 1 | 40 / 11 | 3/40 = 7.5% [2.6, 19.9] | 0/11 = 0.0% [0.0, 25.9] | 0.537 [0.500, 0.583] |
| scout_native | 2 | 16 / 34 | 4/16 = 25.0% [10.2, 49.5] | 15/34 = 44.1% [28.9, 60.5] | 0.431 [0.283, 0.596] |
| scout_native | 3 | 19 / 30 | 12/19 = 63.2% [41.0, 80.9] | 19/30 = 63.3% [45.5, 78.1] | 0.617 [0.442, 0.779] |

### Recall at FAR budgets (in-sample score threshold, diagnostic)

| Tool | recall at <=3% FAR | recall at <=5% FAR |
|---|---|---|
| cascade_v8 | 19/75 = 25.3% [16.9, 36.2] (FP 0) | 19/75 = 25.3% [16.9, 36.2] (FP 0) |
| cascade_v7 | 0/75 = 0.0% [0.0, 4.9] (FP 1) | 0/75 = 0.0% [0.0, 4.9] (FP 1) |
| facets_haiku | 16/75 = 21.3% [13.6, 31.9] (FP 1) | 16/75 = 21.3% [13.6, 31.9] (FP 1) |
| scout_native | 7/75 = 9.3% [4.6, 18.0] (FP 2) | 8/75 = 10.7% [5.5, 19.7] (FP 3) |

### Length rule then tool (no fitting)

| Tool | union recall | union FAR | tool TP / FP on runs the length rule missed |
|---|---|---|---|
| cascade_v8 | 19/75 = 25.3% [16.9, 36.2] | 0/75 = 0.0% [0.0, 4.9] | 19 / 0 |
| cascade_v7 | 20/75 = 26.7% [18.0, 37.6] | 5/75 = 6.7% [2.9, 14.7] | 20 / 5 |
| facets_haiku | 30/75 = 40.0% [29.7, 51.3] | 4/75 = 5.3% [2.1, 12.9] | 30 / 4 |
| scout_native | 19/75 = 25.3% [16.9, 36.2] | 34/75 = 45.3% [34.6, 56.6] | 19 / 34 |
