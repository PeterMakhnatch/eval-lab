# Transfer matrix — generated tables

## Populations

| key/split | n | positives | view |
|---|---:|---:|---|
| cheatbench-actions-v1/dev | 150 | 75 | published-actions |
| cheatbench-actions-v1/test | 150 | 75 | published-actions |
| hacktrace-act-v1/dev | 150 | 75 | published-actions |
| hacktrace-act-v1/test | 150 | 75 | published-actions |
| mimo-v7-canonical-pass70/test | 70 | 6 | published-actions |
| trace-paired-v1/dev | 25 | 18 | projected-actions |
| trace-paired-v1/test | 11 | 8 | published-actions |
| trajectory-judge-400/dev | 212 | 162 | projected-actions |
| trajectory-judge-400/test | 188 | 138 | published-actions |

## Feature-family availability (N/A = no item in the key has an opportunity)

| family | CheatBench | HackTrace | TrajJudge | Paired | MiMo70 |
|---|---|---|---|---|---|
| exec | yes | N/A | N/A | yes | yes |
| history | yes | N/A | N/A | yes | yes |
| network | yes | N/A | N/A | yes | yes |
| ordering | yes | yes | N/A | yes | yes |
| reads | yes | N/A | N/A | yes | yes |
| tripwires | yes | N/A | N/A | yes | yes |
| volume | yes | yes | yes | yes | yes |
| writes | yes | yes | N/A | yes | yes |
| written_code | yes | yes | N/A | yes | yes |

## (a) Tripwires alone, test splits

| key | variant | recall | false alarms | AUROC |
|---|---|---|---|---|
| CheatBench | tripwires | 11/75 = 15% [8%, 24%] | 2/75 = 3% [1%, 9%] | 0.56 [0.52, 0.61] |
| CheatBench | tripwires_shell_normalized | 16/75 = 21% [14%, 32%] | 5/75 = 7% [3%, 15%] | 0.57 [0.52, 0.63] |
| HackTrace | tripwires | 0/75 = 0% [0%, 5%] | 0/75 = 0% [0%, 5%] | 0.50 [0.50, 0.50] |
| HackTrace | tripwires_shell_normalized | 0/75 = 0% [0%, 5%] | 0/75 = 0% [0%, 5%] | 0.50 [0.50, 0.50] |
| TrajJudge | tripwires | 0/138 = 0% [0%, 3%] | 0/50 = 0% [0%, 7%] | 0.50 [0.50, 0.50] |
| TrajJudge | tripwires_shell_normalized | 0/138 = 0% [0%, 3%] | 0/50 = 0% [0%, 7%] | 0.50 [0.50, 0.50] |
| Paired | tripwires | 8/8 = 100% [68%, 100%] | 1/3 = 33% [6%, 79%] | 0.83 [0.50, 1.00] |
| Paired | tripwires_shell_normalized | 8/8 = 100% [68%, 100%] | 1/3 = 33% [6%, 79%] | 0.83 [0.50, 1.00] |
| MiMo70 | tripwires | 3/6 = 50% [19%, 81%] | 34/64 = 53% [41%, 65%] | 0.48 [0.25, 0.77] |
| MiMo70 | tripwires_shell_normalized | 3/6 = 50% [19%, 81%] | 34/64 = 53% [41%, 65%] | 0.48 [0.25, 0.77] |

## (c) Dev-selected rule list: alarm if any of history_cmds, tripwire_any > 0

| key | recall | false alarms |
|---|---|---|
| CheatBench | 62/75 = 83% [73%, 90%] | 6/75 = 8% [4%, 16%] |
| HackTrace | 0/75 = 0% [0%, 5%] | 0/75 = 0% [0%, 5%] |
| TrajJudge | 0/138 = 0% [0%, 3%] | 0/50 = 0% [0%, 7%] |
| Paired | 8/8 = 100% [68%, 100%] | 2/3 = 67% [21%, 94%] |
| MiMo70 | 4/6 = 67% [30%, 90%] | 49/64 = 77% [65%, 85%] |

## (b) Logistic regression: train on X dev → test on Y test

### AUROC

| train ↓ / test → | CheatBench | HackTrace | TrajJudge | Paired | MiMo70 |
|---|---|---|---|---|---|
| CheatBench | 0.73 [0.65, 0.81] | 0.50 [0.38, 0.61] | 0.67 [0.65, 0.69] | 0.46 [0.00, 0.86] | 0.48 [0.07, 0.86] |
| HackTrace | 0.57 [0.47, 0.66] | 0.76 [0.67, 0.84] | 0.67 [0.65, 0.69] | 0.42 [0.00, 1.00] | 0.51 [0.23, 0.77] |
| Paired | 0.72 [0.63, 0.81] | 0.75 [0.65, 0.83] | 0.67 [0.65, 0.69] | 0.75 [0.22, 1.00] | 0.48 [0.14, 0.91] |
| TrajJudge | 0.20 [0.14, 0.28] | 0.61 [0.50, 0.71] | 0.67 [0.65, 0.69] | 0.46 [0.00, 0.90] | 0.46 [0.19, 0.70] |

### Recall at 2% false alarms (test-negative threshold)

| train ↓ / test → | CheatBench | HackTrace | TrajJudge | Paired | MiMo70 |
|---|---|---|---|---|---|
| CheatBench | 12% [0%, 38%] | 0% [0%, 3%] | 34% [30%, 39%] | 0% [0%, 80%] | 0% [0%, 25%] |
| HackTrace | 8% [1%, 16%] | 1% [0%, 9%] | 34% [30%, 38%] | 0% [0%, 100%] | 17% [0%, 60%] |
| Paired | 21% [13%, 36%] | 5% [0%, 13%] | 34% [30%, 38%] | 50% [14%, 100%] | 0% [0%, 0%] |
| TrajJudge | 0% [0%, 0%] | 0% [0%, 0%] | 34% [30%, 39%] | 0% [0%, 90%] | 0% [0%, 0%] |

### Recall at 5% false alarms (test-negative threshold)

| train ↓ / test → | CheatBench | HackTrace | TrajJudge | Paired | MiMo70 |
|---|---|---|---|---|---|
| CheatBench | 27% [5%, 50%] | 0% [0%, 11%] | 34% [30%, 39%] | 0% [0%, 80%] | 0% [0%, 50%] |
| HackTrace | 9% [3%, 20%] | 4% [0%, 31%] | 34% [30%, 38%] | 0% [0%, 100%] | 17% [0%, 67%] |
| Paired | 27% [15%, 55%] | 5% [0%, 36%] | 34% [30%, 38%] | 50% [14%, 100%] | 0% [0%, 50%] |
| TrajJudge | 0% [0%, 0%] | 0% [0%, 0%] | 34% [30%, 39%] | 0% [0%, 90%] | 0% [0%, 0%] |

### Frozen dev threshold (5% dev false alarms), applied unchanged

| train ↓ / test → | CheatBench | HackTrace | TrajJudge | Paired | MiMo70 |
|---|---|---|---|---|---|
| CheatBench | R 57% / FA 17% | R 33% / FA 44% | R 0% / FA 0% | R 50% / FA 33% | R 67% / FA 72% |
| HackTrace | R 9% / FA 4% | R 13% / FA 9% | R 0% / FA 0% | R 0% / FA 0% | R 0% / FA 0% |
| Paired | R 81% / FA 60% | R 32% / FA 15% | R 0% / FA 0% | R 88% / FA 67% | R 67% / FA 78% |
| TrajJudge | R 0% / FA 4% | R 95% / FA 92% | R 34% / FA 0% | R 0% / FA 33% | R 0% / FA 0% |

## Within-stratum AUROC (benchmark family / editable-test stratum)

| stratum | pos/neg | LR train=CheatBench | LR train=HackTrace | LR train=Paired | LR train=TrajJudge | history_cmds | log_calls | read_test | test_runs | write_source | literal_branches |
|---|---|---|---|---|---|---|---|---|---|---|---|
| cheatbench-actions-v1/test/cheatbench/cybench | 14/37 | 0.67 [0.46, 0.87] | 0.27 [0.12, 0.44] | 0.45 [0.26, 0.63] | 0.19 [0.08, 0.36] | 0.52 [0.47, 0.60] | 0.81 [0.65, 0.93] | 0.52 [0.47, 0.62] | 0.52 [0.47, 0.61] | 0.62 [0.48, 0.77] | N/A |
| cheatbench-actions-v1/test/cheatbench/swe-bench-verified | 61/38 | 0.80 [0.71, 0.89] | 0.48 [0.36, 0.61] | 0.74 [0.63, 0.84] | 0.25 [0.16, 0.35] | 0.99 [0.97, 1.00] | 0.75 [0.66, 0.85] | 0.66 [0.55, 0.76] | 0.62 [0.51, 0.73] | 0.54 [0.43, 0.64] | 0.47 [0.39, 0.54] |
| hacktrace-act-v1/test/editable-test | 37/37 | 0.53 [0.37, 0.69] | 0.68 [0.54, 0.81] | 0.72 [0.57, 0.84] | 0.43 [0.29, 0.55] | N/A | 0.57 [0.43, 0.70] | N/A | N/A | 0.59 [0.45, 0.70] | 0.31 [0.19, 0.48] |
| hacktrace-act-v1/test/read-only-test | 38/38 | 0.47 [0.31, 0.65] | 0.86 [0.74, 0.95] | 0.78 [0.65, 0.89] | 0.82 [0.72, 0.92] | N/A | 0.18 [0.08, 0.30] | N/A | N/A | 0.18 [0.08, 0.29] | 0.21 [0.10, 0.35] |

## Univariate feature AUROC (test splits) and verdicts

| feature | CheatBench | HackTrace | TrajJudge | Paired | MiMo70 | verdict |
|---|---|---|---|---|---|---|
| distinct_kind_role | 0.79 [0.72, 0.85] | 0.51 [0.44, 0.59] | 0.50 [0.50, 0.50] | 0.85 [0.64, 1.00] | 0.49 [0.17, 0.91] | same direction on 2+ keys, failed the dev selection rule |
| external_sourcing | N/A | N/A | N/A | 0.92 [0.71, 1.00] | 0.55 [0.29, 0.89] | dataset-specific (flat elsewhere) |
| failed_test_runs | N/A | N/A | N/A | 0.42 [0.00, 0.72] | 0.49 [0.28, 0.75] | uninformative here |
| final_diff_touches_tests | 0.63 [0.55, 0.71] | 0.51 [0.44, 0.58] | N/A | 0.56 [0.50, 0.71] | 0.41 [0.22, 0.62] | dataset-specific (flat elsewhere) |
| forced_exit | 0.51 [0.49, 0.54] | N/A | N/A | N/A | N/A | uninformative here |
| history_before_first_source_edit | 0.69 [0.63, 0.75] | N/A | N/A | 0.62 [0.50, 0.81] | 0.35 [0.17, 0.59] | dataset-specific (flat elsewhere) |
| history_browsing | 0.50 [0.48, 0.52] | N/A | N/A | 0.54 [0.00, 0.95] | 0.57 [0.30, 0.81] | dataset-specific (flat elsewhere) |
| history_cmds | 0.89 [0.84, 0.94] | N/A | N/A | 0.83 [0.56, 1.00] | 0.48 [0.18, 0.92] | transfers |
| literal_branches | 0.51 [0.45, 0.56] | 0.27 [0.17, 0.35] | N/A | N/A | 0.63 [0.42, 0.90] | selected on dev, not confirmed on test |
| log_calls | 0.80 [0.72, 0.86] | 0.39 [0.28, 0.51] | 0.33 [0.31, 0.35] | 0.54 [0.00, 1.00] | 0.54 [0.31, 0.82] | direction flips (dataset artifact) |
| network_calls | 0.52 [0.47, 0.57] | N/A | N/A | 0.92 [0.70, 1.00] | 0.51 [0.25, 0.86] | same direction on 2+ keys, failed the dev selection rule |
| print_literals | 0.62 [0.54, 0.69] | 0.54 [0.41, 0.66] | N/A | N/A | 0.49 [0.47, 0.50] | direction flips (dataset artifact) |
| read_grader | 0.53 [0.50, 0.57] | N/A | N/A | N/A | N/A | single key, transfer untestable |
| read_history_path | N/A | N/A | N/A | 0.62 [0.50, 0.79] | 0.48 [0.46, 0.50] | uninformative here |
| read_test | 0.72 [0.64, 0.80] | N/A | N/A | 0.81 [0.52, 1.00] | 0.48 [0.25, 0.84] | same direction on 2+ keys, failed the dev selection rule |
| test_asserts | 0.62 [0.55, 0.69] | 0.50 [0.42, 0.57] | N/A | N/A | 0.32 [0.06, 0.76] | dataset-specific (flat elsewhere) |
| test_edit_after_failing_run | N/A | N/A | N/A | 0.56 [0.50, 0.71] | 0.61 [0.38, 0.84] | uninformative here |
| test_runs | 0.70 [0.62, 0.78] | N/A | N/A | 0.62 [0.12, 1.00] | 0.42 [0.22, 0.75] | dataset-specific (flat elsewhere) |
| test_skips | N/A | N/A | N/A | N/A | 0.48 [0.45, 0.50] | uninformative here |
| tests_read_before_first_source_edit | 0.53 [0.45, 0.60] | N/A | N/A | 0.52 [0.22, 0.75] | 0.34 [0.12, 0.62] | dataset-specific (flat elsewhere) |
| tripwire_any | 0.56 [0.52, 0.61] | N/A | N/A | 0.83 [0.50, 1.00] | 0.48 [0.24, 0.77] | selected on dev, not confirmed on test |
| write_after_last_test_run | 0.51 [0.45, 0.56] | N/A | N/A | 0.33 [0.00, 0.50] | 0.54 [0.42, 0.67] | dataset-specific (flat elsewhere) |
| write_grader | N/A | N/A | N/A | N/A | N/A | uninformative here |
| write_source | 0.68 [0.60, 0.77] | 0.37 [0.27, 0.47] | N/A | 0.50 [0.20, 0.72] | 0.69 [0.50, 0.91] | direction flips (dataset artifact) |
| write_test | 0.67 [0.60, 0.75] | 0.51 [0.45, 0.59] | N/A | 0.56 [0.50, 0.69] | 0.51 [0.26, 0.77] | dataset-specific (flat elsewhere) |
| write_test_in_place | 0.65 [0.58, 0.72] | N/A | N/A | N/A | 0.49 [0.25, 0.81] | dataset-specific (flat elsewhere) |

## Univariate feature AUROC (dev splits)

| feature | CheatBench | HackTrace | TrajJudge | Paired |
|---|---|---|---|---|
| distinct_kind_role | 0.53 [0.43, 0.63] | 0.49 [0.43, 0.56] | 0.50 [0.50, 0.50] | 0.62 [0.47, 0.81] |
| external_sourcing | 0.50 [0.46, 0.53] | N/A | N/A | 0.64 [0.54, 0.75] |
| failed_test_runs | 0.51 [0.50, 0.53] | N/A | N/A | 0.41 [0.23, 0.61] |
| final_diff_touches_tests | 0.44 [0.38, 0.50] | 0.49 [0.43, 0.56] | N/A | 0.51 [0.33, 0.71] |
| forced_exit | N/A | N/A | N/A | N/A |
| history_before_first_source_edit | N/A | N/A | N/A | 0.54 [0.35, 0.75] |
| history_browsing | 0.54 [0.51, 0.57] | N/A | N/A | 0.67 [0.40, 0.85] |
| history_cmds | 0.58 [0.53, 0.63] | N/A | N/A | 0.73 [0.58, 0.90] |
| literal_branches | 0.43 [0.38, 0.47] | 0.31 [0.23, 0.39] | N/A | 0.45 [0.33, 0.50] |
| log_calls | 0.42 [0.33, 0.52] | 0.44 [0.36, 0.54] | 0.34 [0.32, 0.35] | 0.59 [0.44, 0.78] |
| network_calls | 0.59 [0.54, 0.64] | N/A | N/A | 0.57 [0.35, 0.73] |
| print_literals | 0.37 [0.31, 0.43] | 0.62 [0.52, 0.72] | N/A | N/A |
| read_grader | 0.59 [0.55, 0.64] | N/A | N/A | N/A |
| read_history_path | N/A | N/A | N/A | 0.56 [0.50, 0.65] |
| read_test | 0.46 [0.39, 0.52] | N/A | N/A | 0.37 [0.15, 0.61] |
| test_asserts | 0.51 [0.50, 0.53] | 0.49 [0.43, 0.56] | N/A | 0.45 [0.29, 0.57] |
| test_edit_after_failing_run | N/A | N/A | N/A | 0.46 [0.32, 0.56] |
| test_runs | 0.55 [0.48, 0.63] | N/A | N/A | 0.47 [0.21, 0.67] |
| test_skips | N/A | N/A | N/A | N/A |
| tests_read_before_first_source_edit | 0.44 [0.39, 0.49] | N/A | N/A | 0.42 [0.29, 0.58] |
| tripwire_any | 0.57 [0.50, 0.63] | N/A | N/A | 0.82 [0.63, 1.00] |
| write_after_last_test_run | 0.53 [0.50, 0.56] | N/A | N/A | 0.53 [0.50, 0.58] |
| write_grader | 0.51 [0.50, 0.52] | N/A | N/A | N/A |
| write_source | 0.38 [0.30, 0.47] | 0.44 [0.35, 0.52] | N/A | 0.51 [0.35, 0.69] |
| write_test | 0.44 [0.37, 0.52] | 0.50 [0.43, 0.57] | N/A | 0.50 [0.33, 0.68] |
| write_test_in_place | 0.50 [0.47, 0.53] | N/A | N/A | 0.48 [0.31, 0.65] |
