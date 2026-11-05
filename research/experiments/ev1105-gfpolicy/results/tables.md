# Full-Line DEV Tables & Goodfire Policy Options Under `goodfire-action-v1`

## 1. Core 3-Policy Comparison (Pooled Full-Line DEV $N=690$ and Goodfire $N=150$)

### 1.1 Pooled Full-Line DEV ($N=690$) — Original Labels vs `goodfire-action-v1`

| Policy | Orig Cheat Keep/Rev/Drop ($n=332$) | **Action `executed-shortcut` Keep/Rev/Drop ($n=263$ Primary)** | **Strict `Tier 1` Shortcut Keep/Rev/Drop ($n=258$)** | GF `intent-only` Keep/Rev/Drop ($n=23$) | GF `no-evidence` Keep/Rev/Drop ($n=46$) | **Honest Keep/Rev/Drop ($n=358$)** | **Total Review Load (Queue w/ 5% Audit)** |
|---|---|---|---|---|---|---|---|
| Current `main` (floor on, `J3` off) | `9 / 111 / 212` (9/332 (2.71% [1.43%, 5.07%])) | **`9 / 55 / 199`** (9/263 (3.42% [1.81%, 6.37%])) | **`9 / 52 / 197`** (9/258 (3.49% [1.85%, 6.50%])) | `0 / 18 / 5` | `0 / 38 / 8` | **`63 / 255 / 40`** (63/358 (17.60% [14.00%, 21.88%])) | **`366`** (`369`) |
| Floor off + `J3` `review>=61 / drop>=100` (#1908) | `58 / 60 / 214` (58/332 (17.47% [13.76%, 21.92%])) | **`7 / 55 / 201`** (7/263 (2.66% [1.30%, 5.39%])) | **`6 / 53 / 199`** (6/258 (2.33% [1.07%, 4.98%])) | `17 / 1 / 5` | `34 / 4 / 8` | **`112 / 206 / 40`** (112/358 (31.28% [26.70%, 36.27%])) | **`266`** (`271`) |
| Floor off + `J3` frozen `>=89` | `63 / 49 / 220` (63/332 (18.98% [15.12%, 23.54%])) | **`10 / 46 / 207`** (10/263 (3.80% [2.08%, 6.86%])) | **`8 / 45 / 205`** (8/258 (3.10% [1.58%, 6.00%])) | `17 / 1 / 5` | `36 / 2 / 8` | **`113 / 192 / 53`** (113/358 (31.56% [26.97%, 36.55%])) | **`241`** (`247`) |

### 1.2 Goodfire DEV ($N=150$) — Original Labels vs `goodfire-action-v1` Classes & Tiers

| Goodfire Cohort / Class | $n$ | Current `main` (floor on, `J3` off) Keep / Rev / Drop | **Floor off + `J3` `review>=61 / drop>=100` (#1908) Keep / Rev / Drop [Keep 95% CI]** | Floor off + `J3` frozen `>=89` Keep / Rev / Drop [Keep 95% CI] |
|---|---:|---|---|---|
| Original publisher `cheat` | 80 | `0 / 63 / 17` | **`52 / 10 / 18`** (52/80 (65.00% [54.08%, 74.55%])) | `55 / 4 / 21` (55/80 (68.75% [57.93%, 77.85%])) |
| **`executed-shortcut` — Primary (`R1 ∩ R2` consensus)** | 11 | `0 / 7 / 4` | **`1 / 5 / 5`** (1/11 (9.09% [1.62%, 37.74%])) | `2 / 1 / 8` (2/11 (18.18% [5.14%, 47.70%])) |
| **`executed-shortcut` — Strict `Tier 1` outcome-altering** | 6 | `0 / 4 / 2` | **`0 / 3 / 3`** (0/6 (0.00% [0.00%, 39.03%])) | `0 / 0 / 6` (0/6 (0.00% [0.00%, 39.03%])) |
| **`executed-shortcut` — 3-Reader unanimous (`R1 ∩ R2 ∩ R3`)** | 10 | `0 / 6 / 4` | **`0 / 5 / 5`** (0/10 (0.00% [0.00%, 27.75%])) | `1 / 1 / 8` (1/10 (10.00% [1.79%, 40.41%])) |
| `executed-shortcut` — Broader union (`Tier 1..3`) | 14 | `0 / 10 / 4` | **`3 / 6 / 5`** (3/14 (21.43% [7.57%, 47.59%])) | `5 / 1 / 8` (5/14 (35.71% [16.34%, 61.24%])) |
| `intent-only` (step digest, never relabelled honest) | 23 | `0 / 18 / 5` | **`17 / 1 / 5`** (17/23 (73.91% [53.53%, 87.45%])) | `17 / 1 / 5` (17/23 (73.91% [53.53%, 87.45%])) |
| `no-evidence` (step digest, never relabelled honest) | 46 | `0 / 38 / 8` | **`34 / 4 / 8`** (34/46 (73.91% [59.74%, 84.40%])) | `36 / 2 / 8` (36/46 (78.26% [64.43%, 87.74%])) |
| `intent-only` (full `<think>`+`content` synthesis) | 52 | `0 / 40 / 12` | **`35 / 5 / 12`** (35/52 (67.31% [53.76%, 78.48%])) | `37 / 3 / 12` (37/52 (71.15% [57.73%, 81.67%])) |
| `no-evidence` (full `<think>`+`content` synthesis) | 17 | `0 / 16 / 1` | **`16 / 0 / 1`** (16/17 (94.12% [73.02%, 98.95%])) | `16 / 0 / 1` (16/17 (94.12% [73.02%, 98.95%])) |
| Non-executed publisher positives (`intent` + `no-evidence`) | 69 | `0 / 56 / 13` | **`51 / 5 / 13`** (51/69 (73.91% [62.49%, 82.81%])) | `53 / 3 / 13` (53/69 (76.81% [65.60%, 85.19%])) |
| **`honest` (all publisher negatives; `0/70` executed-shortcut)** | 70 | `0 / 63 / 7` | **`60 / 3 / 7`** (60/70 (85.71% [75.66%, 92.05%])) | `60 / 3 / 7` (60/70 (85.71% [75.66%, 92.05%])) |
| `honest` — `no-evidence` sub-class | 69 | `0 / 62 / 7` | **`59 / 3 / 7`** (59/69 (85.51% [75.34%, 91.93%])) | `59 / 3 / 7` (59/69 (85.51% [75.34%, 91.93%])) |
| `honest` — `intent-only` sub-class (`c448cb22`) | 1 | `0 / 1 / 0` | **`1 / 0 / 0`** (1/1 (100.00% [20.65%, 100.00%])) | `1 / 0 / 0` (1/1 (100.00% [20.65%, 100.00%])) |

### 1.3 Full Per-Corpus DEV Table ($N=690$) Across the 3 Core Policies

| Corpus | Class / View | $n$ | Current `main` (floor on, `J3` off) Keep / Rev / Drop [Keep 95% CI] | Floor off + `J3` `61/100` (#1908) Keep / Rev / Drop [Keep 95% CI] | Floor off + `J3` frozen `89` Keep / Rev / Drop [Keep 95% CI] |
|---|---|---:|---|---|---|
| `c1c5` | `cheat (orig)` | 11 | `0 / 6 / 5` (0/11 (0.00% [0.00%, 25.88%])) | `0 / 6 / 5` (0/11 (0.00% [0.00%, 25.88%])) | `0 / 6 / 5` (0/11 (0.00% [0.00%, 25.88%])) |
| `c1c5` | `honest` | 19 | `1 / 14 / 4` (1/19 (5.26% [0.94%, 24.64%])) | `1 / 14 / 4` (1/19 (5.26% [0.94%, 24.64%])) | `1 / 14 / 4` (1/19 (5.26% [0.94%, 24.64%])) |
| `cheatbench` | `cheat (orig)` | 75 | `2 / 34 / 39` (2/75 (2.67% [0.73%, 9.21%])) | `1 / 34 / 40` (1/75 (1.33% [0.24%, 7.17%])) | `2 / 32 / 41` (2/75 (2.67% [0.73%, 9.21%])) |
| `cheatbench` | `honest` | 75 | `19 / 32 / 24` (19/75 (25.33% [16.86%, 36.21%])) | `9 / 42 / 24` (9/75 (12.00% [6.44%, 21.26%])) | `9 / 41 / 25` (9/75 (12.00% [6.44%, 21.26%])) |
| `goodfire` | `cheat (orig)` | 80 | `0 / 63 / 17` (0/80 (0.00% [0.00%, 4.58%])) | `52 / 10 / 18` (52/80 (65.00% [54.08%, 74.55%])) | `55 / 4 / 21` (55/80 (68.75% [57.93%, 77.85%])) |
| `goodfire` | `honest` | 70 | `0 / 63 / 7` (0/70 (0.00% [0.00%, 5.20%])) | `60 / 3 / 7` (60/70 (85.71% [75.66%, 92.05%])) | `60 / 3 / 7` (60/70 (85.71% [75.66%, 92.05%])) |
| `goodfire` | **`executed-shortcut` (act-v1 positive)** | 11 | `0 / 7 / 4` (0/11 (0.00% [0.00%, 25.88%])) | `1 / 5 / 5` (1/11 (9.09% [1.62%, 37.74%])) | `2 / 1 / 8` (2/11 (18.18% [5.14%, 47.70%])) |
| `goodfire` | **`executed-shortcut` (Strict Tier 1)** | 6 | `0 / 4 / 2` (0/6 (0.00% [0.00%, 39.03%])) | `0 / 3 / 3` (0/6 (0.00% [0.00%, 39.03%])) | `0 / 0 / 6` (0/6 (0.00% [0.00%, 39.03%])) |
| `goodfire` | `intent-only` (act-v1 separate) | 23 | `0 / 18 / 5` (0/23 (0.00% [0.00%, 14.31%])) | `17 / 1 / 5` (17/23 (73.91% [53.53%, 87.45%])) | `17 / 1 / 5` (17/23 (73.91% [53.53%, 87.45%])) |
| `goodfire` | `no-evidence` (act-v1 separate) | 46 | `0 / 38 / 8` (0/46 (0.00% [0.00%, 7.71%])) | `34 / 4 / 8` (34/46 (73.91% [59.74%, 84.40%])) | `36 / 2 / 8` (36/46 (78.26% [64.43%, 87.74%])) |
| `sol-astra` | `cheat (orig)` | 8 | `2 / 0 / 6` (2/8 (25.00% [7.15%, 59.07%])) | `1 / 1 / 6` (1/8 (12.50% [2.24%, 47.09%])) | `2 / 0 / 6` (2/8 (25.00% [7.15%, 59.07%])) |
| `sol-astra` | `honest` | 48 | `33 / 15 / 0` (33/48 (68.75% [54.67%, 80.05%])) | `33 / 15 / 0` (33/48 (68.75% [54.67%, 80.05%])) | `33 / 15 / 0` (33/48 (68.75% [54.67%, 80.05%])) |
| `terminal-wrench` | `cheat (orig)` | 158 | `5 / 8 / 145` (5/158 (3.16% [1.36%, 7.19%])) | `4 / 9 / 145` (4/158 (2.53% [0.99%, 6.33%])) | `4 / 7 / 147` (4/158 (2.53% [0.99%, 6.33%])) |
| `terminal-wrench` | `honest` | 146 | `10 / 131 / 5` (10/146 (6.85% [3.76%, 12.15%])) | `9 / 132 / 5` (9/146 (6.16% [3.28%, 11.30%])) | `10 / 119 / 17` (10/146 (6.85% [3.76%, 12.15%])) |

## 2. Goodfire Sub-Benchmark Split: `ImpossibleBench` ($N=74$) vs `SWE-bench Verified` ($N=76$)

| Sub-Benchmark | Class (`goodfire-action-v1`) | $n$ | `main` (floor on, `J3` off) | Floor off + `J3` off | **Floor off + `J3` `61/100` (#1908)** | Floor off + `J3` frozen `89` |
|---|---|---:|---|---|---|---|
| `ImpossibleBench` ($n=74$) | Original `cheat` | 38 | `0 / 33 / 5` | `32 / 1 / 5` | **`26 / 6 / 6`** | `28 / 1 / 9` |
| `ImpossibleBench` ($n=74$) | **`executed-shortcut` (Primary)** | 10 | `0 / 7 / 3` | `6 / 1 / 3` | **`1 / 5 / 4`** | `2 / 1 / 7` |
| `ImpossibleBench` ($n=74$) | **`executed-shortcut` (Strict Tier 1)** | 5 | `0 / 4 / 1` | `4 / 0 / 1` | **`0 / 3 / 2`** | `0 / 0 / 5` |
| `ImpossibleBench` ($n=74$) | `executed-shortcut` (3-reader unanimous) | 9 | `0 / 6 / 3` | `5 / 1 / 3` | **`0 / 5 / 4`** | `1 / 1 / 7` |
| `ImpossibleBench` ($n=74$) | `intent-only` (step digest) | 2 | `0 / 1 / 1` | `1 / 0 / 1` | **`1 / 0 / 1`** | `1 / 0 / 1` |
| `ImpossibleBench` ($n=74$) | `no-evidence` (step digest) | 26 | `0 / 25 / 1` | `25 / 0 / 1` | **`24 / 1 / 1`** | `25 / 0 / 1` |
| `ImpossibleBench` ($n=74$) | **`honest`** | 36 | `0 / 36 / 0` | `35 / 1 / 0` | **`35 / 1 / 0`** | `35 / 1 / 0` |
| `SWE-bench Verified` ($n=76$) | Original `cheat` | 42 | `0 / 30 / 12` | `27 / 3 / 12` | **`26 / 4 / 12`** | `27 / 3 / 12` |
| `SWE-bench Verified` ($n=76$) | **`executed-shortcut` (Primary)** | 1 | `0 / 0 / 1` | `0 / 0 / 1` | **`0 / 0 / 1`** | `0 / 0 / 1` |
| `SWE-bench Verified` ($n=76$) | **`executed-shortcut` (Strict Tier 1)** | 1 | `0 / 0 / 1` | `0 / 0 / 1` | **`0 / 0 / 1`** | `0 / 0 / 1` |
| `SWE-bench Verified` ($n=76$) | `executed-shortcut` (3-reader unanimous) | 1 | `0 / 0 / 1` | `0 / 0 / 1` | **`0 / 0 / 1`** | `0 / 0 / 1` |
| `SWE-bench Verified` ($n=76$) | `intent-only` (step digest) | 21 | `0 / 17 / 4` | `16 / 1 / 4` | **`16 / 1 / 4`** | `16 / 1 / 4` |
| `SWE-bench Verified` ($n=76$) | `no-evidence` (step digest) | 20 | `0 / 13 / 7` | `11 / 2 / 7` | **`10 / 3 / 7`** | `11 / 2 / 7` |
| `SWE-bench Verified` ($n=76$) | **`honest`** | 34 | `0 / 27 / 7` | `25 / 2 / 7` | **`25 / 2 / 7`** | `25 / 2 / 7` |

## 3. Complete Policy Options Matrix for Peter ($N=690$ Full-Line DEV)

| Option ID & Policy Description | GF Floor (`IB` / `SWE`) | `J3` Mode | GF `exec-shortcut` Keep/Rev/Drop ($n=11$ / Tier1 $n=6$) | Pooled `exec-shortcut` Keep/Rev/Drop ($n=263$ / Tier1 $n=258$) | Orig Cheat Keep ($n=332$) | GF Honest Keep/Rev/Drop ($n=70$) | **Pooled Honest Keep/Rev/Drop ($n=358$)** | **Total Review Load (Queue)** |
|---|---|---|---|---|---|---|---|---|
| **P0 (Current `main`)**: Keep floor on all Goodfire, `J3` off | On / On | Off | `0 / 7 / 4` (Tier1: `0 / 4 / 2`) | `9 / 55 / 199` (Tier1: `9 / 52 / 197`) | `9/332` | `0 / 63 / 7` | **`63 / 255 / 40`** | **`366`** (`369`) |
| **P1a (`#1908` w/ floor)**: Keep floor on all Goodfire, global `J3 61/100` | On / On | Global `61/100` | `0 / 6 / 5` (Tier1: `0 / 3 / 3`) | `6 / 56 / 201` (Tier1: `6 / 53 / 199`) | `6/332` | `0 / 63 / 7` | **`52 / 266 / 40`** | **`378`** (`381`) |
| **P1b**: Keep floor on all Goodfire, global `J3 >= 89` | On / On | Global `>=89` | `0 / 3 / 8` (Tier1: `0 / 0 / 6`) | `8 / 48 / 207` (Tier1: `8 / 45 / 205`) | `8/332` | `0 / 63 / 7` | **`53 / 252 / 53`** | **`356`** (`359`) |
| **P2a**: Remove floor on all Goodfire + global `J3 61/100` (#1908) | Off / Off | Global `61/100` | `1 / 5 / 5` (Tier1: `0 / 3 / 3`) | `7 / 55 / 201` (Tier1: `6 / 53 / 199`) | `58/332` | `60 / 3 / 7` | **`112 / 206 / 40`** | **`266`** (`271`) |
| **P2b**: Remove floor on all Goodfire + global `J3 >= 89` | Off / Off | Global `>=89` | `2 / 1 / 8` (Tier1: `0 / 0 / 6`) | `10 / 46 / 207` (Tier1: `8 / 45 / 205`) | `63/332` | `60 / 3 / 7` | **`113 / 192 / 53`** | **`241`** (`247`) |
| **P2c (Unsafe baseline)**: Remove floor on all Goodfire, `J3` off | Off / Off | Off | `6 / 1 / 4` (Tier1: `4 / 0 / 2`) | `15 / 49 / 199` (Tier1: `13 / 48 / 197`) | `68/332` | `60 / 3 / 7` | **`123 / 195 / 40`** | **`247`** (`254`) |
| **P3a**: Remove floor ONLY for `ImpossibleBench` Goodfire + global `J3 61/100` | Off / On | Global `61/100` | `1 / 5 / 5` (Tier1: `0 / 3 / 3`) | `7 / 55 / 201` (Tier1: `6 / 53 / 199`) | `32/332` | `35 / 28 / 7` | **`87 / 231 / 40`** | **`317`** (`321`) |
| **P3b**: Remove floor ONLY for `ImpossibleBench` Goodfire + global `J3 >= 89` | Off / On | Global `>=89` | `2 / 1 / 8` (Tier1: `0 / 0 / 6`) | `10 / 46 / 207` (Tier1: `8 / 45 / 205`) | `36/332` | `35 / 28 / 7` | **`88 / 217 / 53`** | **`293`** (`297`) |
| **P3c**: Remove floor ONLY for `ImpossibleBench` Goodfire, `J3` off | Off / On | Off | `6 / 1 / 4` (Tier1: `4 / 0 / 2`) | `15 / 49 / 199` (Tier1: `13 / 48 / 197`) | `41/332` | `35 / 28 / 7` | **`98 / 220 / 40`** | **`299`** (`304`) |
| **P4a**: Remove floor ONLY for `SWE-bench` Goodfire, `J3` off | On / Off | Off | `0 / 7 / 4` (Tier1: `0 / 4 / 2`) | `9 / 55 / 199` (Tier1: `9 / 52 / 197`) | `36/332` | `25 / 38 / 7` | **`88 / 230 / 40`** | **`314`** (`319`) |
| **P4b**: Remove floor ONLY for `SWE-bench` Goodfire + global `J3 61/100` | On / Off | Global `61/100` | `0 / 6 / 5` (Tier1: `0 / 3 / 3`) | `6 / 56 / 201` (Tier1: `6 / 53 / 199`) | `32/332` | `25 / 38 / 7` | **`77 / 241 / 40`** | **`327`** (`331`) |
| **P4c**: Remove floor ONLY for `SWE-bench` Goodfire + global `J3 >= 89` | On / Off | Global `>=89` | `0 / 3 / 8` (Tier1: `0 / 0 / 6`) | `8 / 48 / 207` (Tier1: `8 / 45 / 205`) | `35/332` | `25 / 38 / 7` | **`78 / 227 / 53`** | **`304`** (`309`) |
| **P5a**: Remove floor on all Goodfire + Goodfire-scoped `J3 61/100` (`J3` off on other 4 corpora) | Off / Off | GF-only `61/100` | `1 / 5 / 5` (Tier1: `0 / 3 / 3`) | `10 / 53 / 200` (Tier1: `9 / 51 / 198`) | `61/332` | `60 / 3 / 7` | **`123 / 195 / 40`** | **`253`** (`258`) |
| **P5b**: Remove floor on all Goodfire + Goodfire-scoped `J3 >= 89` (`J3` off on other 4 corpora) | Off / Off | GF-only `>=89` | `2 / 1 / 8` (Tier1: `0 / 0 / 6`) | `11 / 49 / 203` (Tier1: `9 / 48 / 201`) | `64/332` | `60 / 3 / 7` | **`123 / 195 / 40`** | **`247`** (`253`) |
| **P5c**: Remove floor ONLY for `ImpossibleBench` Goodfire + Goodfire-scoped `J3 61/100` (`J3` off on other 4 corpora) | Off / On | GF-only `61/100` | `1 / 5 / 5` (Tier1: `0 / 3 / 3`) | `10 / 53 / 200` (Tier1: `9 / 51 / 198`) | `35/332` | `35 / 28 / 7` | **`98 / 220 / 40`** | **`304`** (`308`) |
| **P5d**: Remove floor ONLY for `ImpossibleBench` Goodfire + Goodfire-scoped `J3 >= 89` (`J3` off on other 4 corpora) | Off / On | GF-only `>=89` | `2 / 1 / 8` (Tier1: `0 / 0 / 6`) | `11 / 49 / 203` (Tier1: `9 / 48 / 201`) | `37/332` | `35 / 28 / 7` | **`98 / 220 / 40`** | **`299`** (`303`) |
