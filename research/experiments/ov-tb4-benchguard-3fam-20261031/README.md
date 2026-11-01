# BenchGuard 3-Family + Interactive Tool-Using Investigator Evaluation (TB4 DEV Wave-3)

## Overview
This experiment tests the **BenchGuard 3-Family + Interactive Tool-Using Package Investigator** hypothesis (`L2-report.md` §1 Rank 2), extending `W3-TB4-FINISH-SCORE` across the 14 TB4 DEV tasks (45 causal grading keys).

### Model Families
1. **Family 1 (`sonnet_k8`)**: `openrouter/~anthropic/claude-sonnet-latest`, $k=8$, flat-bundle audit.
2. **Family 2 (`deepseek_k8`)**: `deepseek/deepseek-v4-pro`, $k=8$, flat-bundle audit.
3. **Family 3 (`interactive_gemini38_k8`)**: `google-antigravity/gemini-3.8-flash`, `--thinking high`, interactive tool-using investigator (`read,grep,glob`), strictly confined to each task's `task_package` directory, given the compact `PackageAnatomy` card + file tree manifest.

### Blinded Pooled Judge
All findings from the baseline (`oq6_sonnet_B`) and the 3 auditor families were pooled, anonymized, and deterministically shuffled per task (`random.Random(case_id).shuffle(order)`). Exactly 1 blinded pooled judge call per task (14 calls) was evaluated on `google-antigravity/gemini-3.8-flash` using the immutable OQ-6 `scorer.py` rubric.

## Primary Results (NOFLOOR-k8)

| Arm / Variant | Matched Keys (Recall) | 95% Wilson CI | Tasks w/ Hit | Eligible Findings | Strict Precision | Eval³ Dedup Precision | Recovered NIV Keys | McNemar vs Baseline | McNemar vs 2-Family Union |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `oq6_sonnet_B` (re-judged) | 7 / 45 (15.6%) | 7.7% – 28.8% | 7 / 14 | 29 | 7 / 29 (24.1%) | 7 / 29 (24.1%) | 0 / 10 | — | — |
| `sonnet_k8` (Family 1) | 8 / 45 (17.8%) | 9.3% – 31.3% | 6 / 14 | 48 | 8 / 48 (16.7%) | 8 / 48 (16.7%) | 1 / 10 | $b=2, c=3, p=1.000$ | $b=2, c=0, p=0.500$ |
| `deepseek_k8` (Family 2) | 9 / 45 (20.0%) | 10.9% – 33.8% | 7 / 14 | 63 | 9 / 63 (14.3%) | 9 / 63 (14.3%) | 1 / 10 | $b=1, c=3, p=0.625$ | $b=1, c=0, p=1.000$ |
| `interactive_gemini38_k8` (Family 3) | 8 / 45 (17.8%) | 9.3% – 31.3% | 7 / 14 | 36 | 8 / 36 (22.2%) | 8 / 36 (22.2%) | 0 / 10 | $b=2, c=3, p=1.000$ | $b=5, c=3, p=0.727$ |
| `union_2fam_top8` (Sonnet + DeepSeek) | 10 / 45 (22.2%) | 12.5% – 36.3% | 8 / 14 | 94 | 16 / 94 (17.0%) | 10 / 88 (11.4%) | 1 / 10 | $b=1, c=4, p=0.375$ | — |
| `union_2fam_all` (Sonnet + DeepSeek) | 10 / 45 (22.2%) | 12.5% – 36.3% | 8 / 14 | 111 | 17 / 111 (15.3%) | 10 / 104 (9.6%) | 1 / 10 | $b=1, c=4, p=0.375$ | — |
| **`union_3fam_top8` (BenchGuard Gated)** | **13 / 45 (28.9%)** | **17.7% – 43.4%** | **10 / 14** | **97** | **21 / 97 (21.6%)** | **13 / 89 (14.6%)** | **1 / 10** | **$b=1, c=7, p=0.070$** | **$b=0, c=3, p=0.250$** |
| `union_3fam_all` (Uncapped) | 13 / 45 (28.9%) | 17.7% – 43.4% | 10 / 14 | 147 | 25 / 147 (17.0%) | 13 / 135 (9.6%) | 1 / 10 | $b=1, c=7, p=0.070$ | $b=0, c=3, p=0.250$ |

## CONF >= 0.50 Variant

| Arm / Variant | Matched Keys (Recall) | 95% Wilson CI | Tasks w/ Hit | Eligible Findings | Strict Precision | Eval³ Dedup Precision | Recovered NIV Keys | McNemar vs Baseline | McNemar vs 2-Family Union |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `oq6_sonnet_B` (re-judged) | 4 / 45 (8.9%) | 3.5% – 20.7% | 4 / 14 | 11 | 4 / 11 (36.4%) | 4 / 11 (36.4%) | 0 / 10 | — | — |
| `sonnet_k8` (Family 1) | 3 / 45 (6.7%) | 2.3% – 17.9% | 3 / 14 | 14 | 3 / 14 (21.4%) | 3 / 14 (21.4%) | 1 / 10 | $b=3, c=2, p=1.000$ | $b=6, c=0, p=0.031$ |
| `deepseek_k8` (Family 2) | 9 / 45 (20.0%) | 10.9% – 33.8% | 7 / 14 | 54 | 9 / 54 (16.7%) | 9 / 54 (16.7%) | 1 / 10 | $b=0, c=5, p=0.063$ | $b=0, c=0, p=1.000$ |
| `interactive_gemini38_k8` (Family 3) | 8 / 45 (17.8%) | 9.3% – 31.3% | 7 / 14 | 36 | 8 / 36 (22.2%) | 8 / 36 (22.2%) | 0 / 10 | $b=0, c=4, p=0.125$ | $b=4, c=3, p=1.000$ |
| `union_2fam_top8` (Sonnet + DeepSeek) | 9 / 45 (20.0%) | 10.9% – 33.8% | 7 / 14 | 65 | 12 / 65 (18.5%) | 9 / 62 (14.5%) | 1 / 10 | $b=0, c=5, p=0.063$ | — |
| `union_2fam_all` (Sonnet + DeepSeek) | 9 / 45 (20.0%) | 10.9% – 33.8% | 7 / 14 | 68 | 12 / 68 (17.6%) | 9 / 65 (13.8%) | 1 / 10 | $b=0, c=5, p=0.063$ | — |
| **`union_3fam_top8` (BenchGuard Gated)** | **12 / 45 (26.7%)** | **16.0% – 41.0%** | **9 / 14** | **86** | **19 / 86 (22.1%)** | **12 / 79 (15.2%)** | **1 / 10** | **$b=0, c=8, p=0.0078$** | **$b=0, c=3, p=0.250$** |
| `union_3fam_all` (Uncapped) | 12 / 45 (26.7%) | 16.0% – 41.0% | 9 / 14 | 104 | 20 / 104 (19.2%) | 12 / 96 (12.5%) | 1 / 10 | $b=0, c=8, p=0.0078$ | $b=0, c=3, p=0.250$ |

## Key Findings
1. **Adding Family 3 (Interactive Tool-Using Gemini) improves recall by +3 net keys over 2-family union:**
   - 3-family union reached **13/45 (28.9%)** NOFLOOR and **12/45 (26.7%)** CONF0.5, compared to 10/45 (22.2%) and 9/45 (20.0%) for the 2-family union.
   - Tasks with $\ge 1$ hit increased to **10/14 (71.4%)**, up from 8/14 (2-fam) and 7/14 (baseline).
   - Under CONF0.5, the improvement over baseline is statistically significant ($b=0, c=8, p = 0.0078$).
2. **Interactive tools provide high-precision discovery on complex packages:**
   - On D0005 (`satb-audio-transcription`), neither Sonnet nor DeepSeek detected any causal defect; interactive Gemini found the causal key `pm-tb4:satb-audio-transcription:2770a8e37b`.
   - On D0011 (`gsea-proteomics`), interactive Gemini matched *both* causal keys (`8203fba3ef` and `4343745cf3`), achieving 100% key recall on the task.
3. **Strict Package Boundary Compliance:**
   - Programmatic audit of session `.jsonl` confirmed that across 684 total tool calls (`read`, `grep`, `glob`), exactly 0 calls accessed any path outside each task's `task_package` directory.
