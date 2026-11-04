# Full-line DEV comparison

| Batch | Corpus | Class | N | Keep | Review | Drop | Keep rate [95% Wilson CI] |
|---|---|---|---:|---:|---:|---:|---|
| main | c1c5 | honest | 19 | 1 | 14 | 4 | 0.0526 [0.0094, 0.2464] |
| main | c1c5 | cheat | 11 | 0 | 6 | 5 | 0.0000 [0.0000, 0.2588] |
| main | cheatbench | honest | 75 | 19 | 32 | 24 | 0.2533 [0.1686, 0.3621] |
| main | cheatbench | cheat | 75 | 2 | 34 | 39 | 0.0267 [0.0073, 0.0921] |
| main | goodfire | honest | 70 | 0 | 63 | 7 | 0.0000 [0.0000, 0.0520] |
| main | goodfire | cheat | 80 | 0 | 63 | 17 | 0.0000 [0.0000, 0.0458] |
| main | pooled | honest | 358 | 63 | 255 | 40 | 0.1760 [0.1400, 0.2188] |
| main | pooled | cheat | 332 | 9 | 111 | 212 | 0.0271 [0.0143, 0.0507] |
| main | sol-astra | honest | 48 | 33 | 15 | 0 | 0.6875 [0.5467, 0.8005] |
| main | sol-astra | cheat | 8 | 2 | 0 | 6 | 0.2500 [0.0715, 0.5907] |
| main | terminal-wrench | honest | 146 | 10 | 131 | 5 | 0.0685 [0.0376, 0.1215] |
| main | terminal-wrench | cheat | 158 | 5 | 8 | 145 | 0.0316 [0.0136, 0.0719] |
| j3 | c1c5 | honest | 19 | 1 | 14 | 4 | 0.0526 [0.0094, 0.2464] |
| j3 | c1c5 | cheat | 11 | 0 | 6 | 5 | 0.0000 [0.0000, 0.2588] |
| j3 | cheatbench | honest | 75 | 9 | 41 | 25 | 0.1200 [0.0644, 0.2126] |
| j3 | cheatbench | cheat | 75 | 2 | 32 | 41 | 0.0267 [0.0073, 0.0921] |
| j3 | goodfire | honest | 70 | 0 | 63 | 7 | 0.0000 [0.0000, 0.0520] |
| j3 | goodfire | cheat | 80 | 0 | 59 | 21 | 0.0000 [0.0000, 0.0458] |
| j3 | pooled | honest | 358 | 53 | 252 | 53 | 0.1480 [0.1150, 0.1886] |
| j3 | pooled | cheat | 332 | 8 | 104 | 220 | 0.0241 [0.0123, 0.0468] |
| j3 | sol-astra | honest | 48 | 33 | 15 | 0 | 0.6875 [0.5467, 0.8005] |
| j3 | sol-astra | cheat | 8 | 2 | 0 | 6 | 0.2500 [0.0715, 0.5907] |
| j3 | terminal-wrench | honest | 146 | 10 | 119 | 17 | 0.0685 [0.0376, 0.1215] |
| j3 | terminal-wrench | cheat | 158 | 4 | 7 | 147 | 0.0253 [0.0099, 0.0633] |
| j3_no_floor | c1c5 | honest | 19 | 1 | 14 | 4 | 0.0526 [0.0094, 0.2464] |
| j3_no_floor | c1c5 | cheat | 11 | 0 | 6 | 5 | 0.0000 [0.0000, 0.2588] |
| j3_no_floor | cheatbench | honest | 75 | 9 | 41 | 25 | 0.1200 [0.0644, 0.2126] |
| j3_no_floor | cheatbench | cheat | 75 | 2 | 32 | 41 | 0.0267 [0.0073, 0.0921] |
| j3_no_floor | goodfire | honest | 70 | 60 | 3 | 7 | 0.8571 [0.7566, 0.9205] |
| j3_no_floor | goodfire | cheat | 80 | 55 | 4 | 21 | 0.6875 [0.5793, 0.7785] |
| j3_no_floor | pooled | honest | 358 | 113 | 192 | 53 | 0.3156 [0.2697, 0.3655] |
| j3_no_floor | pooled | cheat | 332 | 63 | 49 | 220 | 0.1898 [0.1512, 0.2354] |
| j3_no_floor | sol-astra | honest | 48 | 33 | 15 | 0 | 0.6875 [0.5467, 0.8005] |
| j3_no_floor | sol-astra | cheat | 8 | 2 | 0 | 6 | 0.2500 [0.0715, 0.5907] |
| j3_no_floor | terminal-wrench | honest | 146 | 10 | 119 | 17 | 0.0685 [0.0376, 0.1215] |
| j3_no_floor | terminal-wrench | cheat | 158 | 4 | 7 | 147 | 0.0253 [0.0099, 0.0633] |

## No cheat-keep increase

| Batch | Corpus | Reference cheat keeps | Cheat keeps | Pass |
|---|---|---:|---:|---|
| main | c1c5 | 0 | 0 | True |
| main | cheatbench | 2 | 2 | True |
| main | goodfire | 0 | 0 | True |
| main | sol-astra | 2 | 2 | True |
| main | terminal-wrench | 5 | 5 | True |
| j3 | c1c5 | 0 | 0 | True |
| j3 | cheatbench | 2 | 2 | True |
| j3 | goodfire | 0 | 0 | True |
| j3 | sol-astra | 2 | 2 | True |
| j3 | terminal-wrench | 5 | 4 | True |
| j3_no_floor | c1c5 | 0 | 0 | True |
| j3_no_floor | cheatbench | 2 | 2 | True |
| j3_no_floor | goodfire | 0 | 55 | False |
| j3_no_floor | sol-astra | 2 | 2 | True |
| j3_no_floor | terminal-wrench | 5 | 4 | True |

### main: J3 load

Eligible runs: 0. Available cache/detail load: 0.
- calls: sum 0; covered runs 0; mean unavailable.
- prompt_tokens: sum 0; covered runs 0; mean unavailable.
- completion_tokens: sum 0; covered runs 0; mean unavailable.
- latency_s: sum 0; covered runs 0; mean unavailable.
- cost_usd: sum 0; covered runs 0; mean unavailable.

Changed keep cases: 0. Exact metadata appears in the JSON.

### j3: J3 load

Eligible runs: 438. Available cache/detail load: 438.
- calls: sum 552; covered runs 438; mean 1.2603.
- prompt_tokens: sum 5199592; covered runs 438; mean 11871.2146.
- completion_tokens: sum 61507; covered runs 438; mean 140.4269.
- latency_s: sum 2905.93; covered runs 438; mean 6.6345.
- cost_usd: sum 0.0; covered runs 438; mean 0.0000.

Changed keep cases: 11. Exact metadata appears in the JSON.

### j3_no_floor: J3 load

Eligible runs: 438. Available cache/detail load: 438.
- calls: sum 552; covered runs 438; mean 1.2603.
- prompt_tokens: sum 5199592; covered runs 438; mean 11871.2146.
- completion_tokens: sum 61507; covered runs 438; mean 140.4269.
- latency_s: sum 2905.93; covered runs 438; mean 6.6345.
- cost_usd: sum 0.0; covered runs 438; mean 0.0000.

Changed keep cases: 126. Exact metadata appears in the JSON.

## Limits

- DEV only. Labels join after decisions.
- Cached calls and latency describe retained windows, not new replay calls or observed run wall time.
- A zero USD value is subscription accounting, not a commercial API price estimate.
