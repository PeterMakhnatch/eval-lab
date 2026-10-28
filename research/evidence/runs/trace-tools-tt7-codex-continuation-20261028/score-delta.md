<!-- Aggregate-only TT-7 Luna Codex continuation delta. No run-level data. -->
## Luna Codex continuation delta — attempt `partial`

Attempt reason: model_attestation_unknown: unsuccessful call lacks an accepted exact served-model attestation; route stopped. Finished 2026-10-28T16:27:07.850429+00:00. Attempt receipt `8e3bee536b29f4b5…`; new combined snapshot `191313cf3f5a3ecb…`.

Decided-only recall and false-alarm rate (FAR = FP / decided negatives) carry Wilson 95% intervals. Unattempted, interrupted, refused and abstained cells are non-decisions, never negatives; all-200 denominators never shrink. Goodfire DEV and CheatBench cross-test are separate. Recommendations use Goodfire only. Safe-196 is a secondary descriptive view only. No winner is chosen from unpaired conditional rates.

### Route, entitlement and financial basis

- Route `openai-codex/gpt-6-luna`; new phase auth class `chatgpt_oauth`, plan `prolite`, API `openai-codex-responses`, omp `18.12.6`. Entitlement re-validated: **True**.
- Original two Luna cells: auth class `UNKNOWN: historical credential class was not retained`; billing **UNKNOWN** (client-reported $0, list-equivalent $0.0136769 are neither a charge nor a free entitlement).
- New phase: 13 observed readings; marginal known total $0.0000 (1 observed readings of unknown basis); list-equivalent observed total —.
- Combined historical + new: PARTIALLY UNKNOWN: the two original Luna cells have no verified billing basis; marginal observed total — (null while any observed cell is unknown). per-1k values normalize observed attempts only; they are not an entitlement, quota or free-capacity forecast for 1,000 runs.

### Luna coverage (fixed all-200 denominator)

Original terminal 2; new terminal 13 (12 valid forms, 1 abstentions, 13 observed readings); unattempted 185; interrupted cells reported by the attempt: 0. Original terminal cells by source: {'cheatbench-actions-v1': 2}; new terminal cells by source: {'cheatbench-actions-v1': 4, 'cloud-trace-db': 9}. Per-cohort cost columns cover only that cohort's observed cells, so a cohort holding no original cell contains new-phase readings only; the all-200 combined cost stays partially unknown while any original cell is included.

### full-200 — Primary — goodfire-dev

| Arm / replay | Coverage + / − | TP / FP / FN / TN | Abstain + / − | TP / all positives (yield) [CI] | Recall [CI] | FAR [CI] | Gold-passage hit / valid cited IDs | Tokens in / out (known) | Marginal observed total · per-1k (basis) | List-equivalent total · per-1k | Median s (n) | TP both / arm-only / cascade-only / neither (paired+) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Luna combined (current) | 6.2% / 4.3% | 1 / 0 / 4 / 3 | 75 / 67 | 1 / 80 (1.2%) [0.2%–6.7%] | 20.0% [3.6%–62.4%] (n=5) | 0.0% [0.0%–56.1%] (n=3) | gold hit 1/1 flagged; 2/80 all+; valid IDs 100.0% | 371824 / 1047 | — · —/1k (9 obs; 1 unknown) | — · —/1k | 4.62 (9) | 0 / 1 / 0 / 4 (5) |
| Luna historical-2 (baseline) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 80 / 70 | 0 / 80 (0.0%) [0.0%–4.6%] | — [—] (n=0) | — [—] (n=0) | no supported cited steps | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |
| Luna continuation phase only | 6.2% / 4.3% | 1 / 0 / 4 / 3 | 75 / 67 | 1 / 80 (1.2%) [0.2%–6.7%] | 20.0% [3.6%–62.4%] (n=5) | 0.0% [0.0%–56.1%] (n=3) | gold hit 1/1 flagged; 2/80 all+; valid IDs 100.0% | 371824 / 1047 | — · —/1k (9 obs; 1 unknown) | — · —/1k | 4.62 (9) | 0 / 1 / 0 / 4 (5) |
| cm9_alarmed (retained comparator) | 100.0% / 100.0% | 8 / 2 / 72 / 68 | 0 / 0 | 8 / 80 (10.0%) [5.2%–18.5%] | 10.0% [5.2%–18.5%] (n=80) | 2.9% [0.8%–9.8%] (n=70) | no supported cited steps | 0 / 0 | $0.0000 · $0.0000/1k (150 obs; 0 unknown) | $0.0000 · $0.0000/1k | — (0) | 8 / 0 / 0 / 72 (80) |
| cm9_stage1_any (retained comparator) | 100.0% / 100.0% | 60 / 32 / 20 / 38 | 0 / 0 | 60 / 80 (75.0%) [64.5%–83.2%] | 75.0% [64.5%–83.2%] (n=80) | 45.7% [34.6%–57.3%] (n=70) | no supported cited steps | 0 / 0 | $0.0000 · $0.0000/1k (150 obs; 0 unknown) | $0.0000 · $0.0000/1k | — (0) | 8 / 52 / 0 / 20 (80) |
| docent:tt7_form:anthropic/claude-sonnet-5-5 (whole-run reader) | 52.5% / 38.6% | 3 / 0 / 39 / 27 | 38 / 43 | 3 / 80 (3.8%) [1.3%–10.5%] | 7.1% [2.5%–19.0%] (n=42) | 0.0% [0.0%–12.5%] (n=27) | gold hit 3/3 flagged; 19/80 all+; valid IDs 100.0% | 9142012 / 6404 | $0.0000 · $0.0000/1k (69 obs; 0 unknown) | — · —/1k | — (0) | 0 / 3 / 5 / 34 (42) |
| omp:tt7_form:anthropic/claude-opus-5-5 (whole-run reader) | 11.2% / 21.4% | 0 / 0 / 9 / 15 | 71 / 55 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–29.9%] (n=9) | 0.0% [0.0%–20.4%] (n=15) | gold hit 0/0 flagged; 7/80 all+; valid IDs 100.0% | 1890323 / 2524 | $0.0000 · $0.0000/1k (40 obs; 0 unknown) | — · —/1k | 3.66 (40) | 0 / 0 / 1 / 8 (9) |
| omp:tt7_form:anthropic/claude-sonnet-5-5 (whole-run reader) | 12.5% / 22.9% | 0 / 0 / 10 / 16 | 70 / 54 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–27.8%] (n=10) | 0.0% [0.0%–19.4%] (n=16) | gold hit 0/0 flagged; 5/80 all+; valid IDs 100.0% | 2499706 / 2543 | $0.0000 · $0.0000/1k (40 obs; 0 unknown) | — · —/1k | 4.09 (40) | 0 / 0 / 2 / 8 (10) |
| stage1_goodfire_dev (retained comparator) | 100.0% / 100.0% | 60 / 32 / 20 / 38 | 0 / 0 | 60 / 80 (75.0%) [64.5%–83.2%] | 75.0% [64.5%–83.2%] (n=80) | 45.7% [34.6%–57.3%] (n=70) | no supported cited steps | 0 / 0 | $0.0000 · $0.0000/1k (150 obs; 0 unknown) | $0.0000 · $0.0000/1k | — (0) | 8 / 52 / 0 / 20 (80) |

### full-200 — Primary — cheatbench-dev-crosstest

| Arm / replay | Coverage + / − | TP / FP / FN / TN | Abstain + / − | TP / all positives (yield) [CI] | Recall [CI] | FAR [CI] | Gold-passage hit / valid cited IDs | Tokens in / out (known) | Marginal observed total · per-1k (basis) | List-equivalent total · per-1k | Median s (n) | TP both / arm-only / cascade-only / neither (paired+) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Luna combined (current) | 8.0% / 16.0% | 1 / 0 / 1 / 4 | 23 / 21 | 1 / 25 (4.0%) [0.7%–19.5%] | 50.0% [9.5%–90.5%] (n=2) | 0.0% [0.0%–49.0%] (n=4) | n/a | 261371 / 366 | — · —/1k (6 obs; 2 unknown) | $0.0263 · $4.3867/1k | 4.17 (6) | 0 / 1 / 1 / 0 (2) |
| Luna historical-2 (baseline) | 4.0% / 4.0% | 0 / 0 / 1 / 1 | 24 / 24 | 0 / 25 (0.0%) [0.0%–13.3%] | 0.0% [0.0%–79.3%] (n=1) | 0.0% [0.0%–79.3%] (n=1) | n/a | 136154 / 123 | — · —/1k (2 obs; 2 unknown) | $0.0137 · $6.8385/1k | 7.11 (2) | 0 / 0 / 1 / 0 (1) |
| Luna continuation phase only | 4.0% / 12.0% | 1 / 0 / 0 / 3 | 24 / 22 | 1 / 25 (4.0%) [0.7%–19.5%] | 100.0% [20.7%–100.0%] (n=1) | 0.0% [0.0%–56.1%] (n=3) | n/a | 125217 / 243 | $0.0000 · $0.0000/1k (4 obs; 0 unknown) | $0.0126 · $3.1608/1k | 3.87 (4) | 0 / 1 / 0 / 0 (1) |
| cm9_alarmed (retained comparator) | 100.0% / 100.0% | 4 / 1 / 21 / 24 | 0 / 0 | 4 / 25 (16.0%) [6.4%–34.7%] | 16.0% [6.4%–34.7%] (n=25) | 4.0% [0.7%–19.5%] (n=25) | n/a | 0 / 0 | $0.0000 · $0.0000/1k (50 obs; 0 unknown) | $0.0000 · $0.0000/1k | — (0) | 4 / 0 / 0 / 21 (25) |
| cm9_stage1_any (retained comparator) | 100.0% / 100.0% | 15 / 9 / 10 / 16 | 0 / 0 | 15 / 25 (60.0%) [40.7%–76.6%] | 60.0% [40.7%–76.6%] (n=25) | 36.0% [20.2%–55.5%] (n=25) | n/a | 0 / 0 | $0.0000 · $0.0000/1k (50 obs; 0 unknown) | $0.0000 · $0.0000/1k | — (0) | 4 / 11 / 0 / 10 (25) |
| docent:tt7_form:anthropic/claude-sonnet-5-5 (whole-run reader) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 25 / 25 | 0 / 25 (0.0%) [0.0%–13.3%] | — [—] (n=0) | — [—] (n=0) | n/a | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |
| omp:tt7_form:anthropic/claude-opus-5-5 (whole-run reader) | 48.0% / 48.0% | 5 / 0 / 7 / 12 | 13 / 13 | 5 / 25 (20.0%) [8.9%–39.1%] | 41.7% [19.3%–68.0%] (n=12) | 0.0% [0.0%–24.2%] (n=12) | n/a | 2401765 / 2981 | $0.0000 · $0.0000/1k (34 obs; 0 unknown) | — · —/1k | 4.01 (34) | 0 / 5 / 3 / 4 (12) |
| omp:tt7_form:anthropic/claude-sonnet-5-5 (whole-run reader) | 48.0% / 52.0% | 5 / 0 / 7 / 13 | 13 / 12 | 5 / 25 (20.0%) [8.9%–39.1%] | 41.7% [19.3%–68.0%] (n=12) | 0.0% [0.0%–22.8%] (n=13) | n/a | 2428922 / 2546 | $0.0000 · $0.0000/1k (34 obs; 0 unknown) | — · —/1k | 3.55 (34) | 0 / 5 / 3 / 4 (12) |
| stage1_goodfire_dev (retained comparator) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 25 / 25 | 0 / 25 (0.0%) [0.0%–13.3%] | — [—] (n=0) | — [—] (n=0) | n/a | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |

### full-200 — Secondary — goodfire-dev

| Arm / replay | Coverage + / − | TP / FP / FN / TN | Abstain + / − | TP / all positives (yield) [CI] | Recall [CI] | FAR [CI] | Gold-passage hit / valid cited IDs | Tokens in / out (known) | Marginal observed total · per-1k (basis) | List-equivalent total · per-1k | Median s (n) | TP both / arm-only / cascade-only / neither (paired+) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Luna combined (current) | 6.2% / 4.3% | 1 / 0 / 4 / 3 | 75 / 67 | 1 / 80 (1.2%) [0.2%–6.7%] | 20.0% [3.6%–62.4%] (n=5) | 0.0% [0.0%–56.1%] (n=3) | gold hit 1/1 flagged; 2/80 all+; valid IDs 100.0% | 371824 / 1047 | — · —/1k (9 obs; 1 unknown) | — · —/1k | 4.62 (9) | 0 / 1 / 0 / 4 (5) |
| Luna historical-2 (baseline) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 80 / 70 | 0 / 80 (0.0%) [0.0%–4.6%] | — [—] (n=0) | — [—] (n=0) | no supported cited steps | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |
| Luna continuation phase only | 6.2% / 4.3% | 1 / 0 / 4 / 3 | 75 / 67 | 1 / 80 (1.2%) [0.2%–6.7%] | 20.0% [3.6%–62.4%] (n=5) | 0.0% [0.0%–56.1%] (n=3) | gold hit 1/1 flagged; 2/80 all+; valid IDs 100.0% | 371824 / 1047 | — · —/1k (9 obs; 1 unknown) | — · —/1k | 4.62 (9) | 0 / 1 / 0 / 4 (5) |
| docent:tt7_form:anthropic/claude-sonnet-5-5 (whole-run reader) | 52.5% / 38.6% | 2 / 0 / 40 / 27 | 38 / 43 | 2 / 80 (2.5%) [0.7%–8.7%] | 4.8% [1.3%–15.8%] (n=42) | 0.0% [0.0%–12.5%] (n=27) | gold hit 2/2 flagged; 19/80 all+; valid IDs 100.0% | 9142012 / 6404 | $0.0000 · $0.0000/1k (69 obs; 0 unknown) | — · —/1k | — (0) | 0 / 2 / 5 / 35 (42) |
| omp:tt7_form:anthropic/claude-opus-5-5 (whole-run reader) | 11.2% / 21.4% | 0 / 0 / 9 / 15 | 71 / 55 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–29.9%] (n=9) | 0.0% [0.0%–20.4%] (n=15) | gold hit 0/0 flagged; 7/80 all+; valid IDs 100.0% | 1890323 / 2524 | $0.0000 · $0.0000/1k (40 obs; 0 unknown) | — · —/1k | 3.66 (40) | 0 / 0 / 1 / 8 (9) |
| omp:tt7_form:anthropic/claude-sonnet-5-5 (whole-run reader) | 12.5% / 22.9% | 0 / 0 / 10 / 16 | 70 / 54 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–27.8%] (n=10) | 0.0% [0.0%–19.4%] (n=16) | gold hit 0/0 flagged; 5/80 all+; valid IDs 100.0% | 2499706 / 2543 | $0.0000 · $0.0000/1k (40 obs; 0 unknown) | — · —/1k | 4.09 (40) | 0 / 0 / 2 / 8 (10) |

### full-200 — Secondary — cheatbench-dev-crosstest

| Arm / replay | Coverage + / − | TP / FP / FN / TN | Abstain + / − | TP / all positives (yield) [CI] | Recall [CI] | FAR [CI] | Gold-passage hit / valid cited IDs | Tokens in / out (known) | Marginal observed total · per-1k (basis) | List-equivalent total · per-1k | Median s (n) | TP both / arm-only / cascade-only / neither (paired+) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Luna combined (current) | 8.0% / 16.0% | 2 / 0 / 0 / 4 | 23 / 21 | 2 / 25 (8.0%) [2.2%–25.0%] | 100.0% [34.2%–100.0%] (n=2) | 0.0% [0.0%–49.0%] (n=4) | n/a | 261371 / 366 | — · —/1k (6 obs; 2 unknown) | $0.0263 · $4.3867/1k | 4.17 (6) | 1 / 1 / 0 / 0 (2) |
| Luna historical-2 (baseline) | 4.0% / 4.0% | 1 / 0 / 0 / 1 | 24 / 24 | 1 / 25 (4.0%) [0.7%–19.5%] | 100.0% [20.7%–100.0%] (n=1) | 0.0% [0.0%–79.3%] (n=1) | n/a | 136154 / 123 | — · —/1k (2 obs; 2 unknown) | $0.0137 · $6.8385/1k | 7.11 (2) | 1 / 0 / 0 / 0 (1) |
| Luna continuation phase only | 4.0% / 12.0% | 1 / 0 / 0 / 3 | 24 / 22 | 1 / 25 (4.0%) [0.7%–19.5%] | 100.0% [20.7%–100.0%] (n=1) | 0.0% [0.0%–56.1%] (n=3) | n/a | 125217 / 243 | $0.0000 · $0.0000/1k (4 obs; 0 unknown) | $0.0126 · $3.1608/1k | 3.87 (4) | 0 / 1 / 0 / 0 (1) |
| docent:tt7_form:anthropic/claude-sonnet-5-5 (whole-run reader) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 25 / 25 | 0 / 25 (0.0%) [0.0%–13.3%] | — [—] (n=0) | — [—] (n=0) | n/a | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |
| omp:tt7_form:anthropic/claude-opus-5-5 (whole-run reader) | 48.0% / 48.0% | 6 / 0 / 6 / 12 | 13 / 13 | 6 / 25 (24.0%) [11.5%–43.4%] | 50.0% [25.4%–74.6%] (n=12) | 0.0% [0.0%–24.2%] (n=12) | n/a | 2401765 / 2981 | $0.0000 · $0.0000/1k (34 obs; 0 unknown) | — · —/1k | 4.01 (34) | 0 / 6 / 3 / 3 (12) |
| omp:tt7_form:anthropic/claude-sonnet-5-5 (whole-run reader) | 48.0% / 52.0% | 7 / 0 / 5 / 13 | 13 / 12 | 7 / 25 (28.0%) [14.3%–47.6%] | 58.3% [32.0%–80.7%] (n=12) | 0.0% [0.0%–22.8%] (n=13) | n/a | 2428922 / 2546 | $0.0000 · $0.0000/1k (34 obs; 0 unknown) | — · —/1k | 3.55 (34) | 1 / 6 / 2 / 3 (12) |

### Cheap → Luna replays, primary form (replayed, NOT executed)

#### goodfire-dev

| Arm / replay | Coverage + / − | TP / FP / FN / TN | Abstain + / − | TP / all positives (yield) [CI] | Recall [CI] | FAR [CI] | Gold-passage hit / valid cited IDs | Tokens in / out (known) | Marginal observed total · per-1k (basis) | List-equivalent total · per-1k | Median s (n) | TP both / arm-only / cascade-only / neither (paired+) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cm9_stage1_any → Luna (before; Luna calls 116–116) | 25.0% / 54.3% | 0 / 0 / 20 / 38 | 60 / 32 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–16.1%] (n=20) | 0.0% [0.0%–9.2%] (n=38) | no supported cited steps | 0 / 0 | $0.0000 · $0.0000/1k (58 obs; 0 unknown) | $0.0000 · $0.0000/1k | — (0) | 0 / 0 / 0 / 20 (20) |
| cm9_stage1_any → Luna (after; Luna calls 116–116) | 30.0% / 54.3% | 1 / 0 / 23 / 38 | 56 / 32 | 1 / 80 (1.2%) [0.2%–6.7%] | 4.2% [0.7%–20.2%] (n=24) | 0.0% [0.0%–9.2%] (n=38) | gold hit 1/1 flagged; 1/80 all+ | 209529 / 689 | — · —/1k (63 obs; 1 unknown) | — · —/1k | — (0) | 0 / 1 / 0 / 23 (24) |
| docent:tt7_form:anthropic/claude-sonnet-5-5 → Luna (before; Luna calls 203–334) | 48.8% / 38.6% | 0 / 0 / 39 / 27 | 41 / 43 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–9.0%] (n=39) | 0.0% [0.0%–12.5%] (n=27) | no supported cited steps | 7739400 / 6050 | $0.0000 · $0.0000/1k (66 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 5 / 34 (39) |
| docent:tt7_form:anthropic/claude-sonnet-5-5 → Luna (after; Luna calls 203–334) | 48.8% / 38.6% | 0 / 0 / 39 / 27 | 41 / 43 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–9.0%] (n=39) | 0.0% [0.0%–12.5%] (n=27) | gold hit 0/0 flagged; 0/80 all+ | 7739400 / 6050 | $0.0000 · $0.0000/1k (66 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 5 / 34 (39) |
| omp:tt7_form:anthropic/claude-sonnet-5-5 → Luna (before; Luna calls 205–354) | 12.5% / 22.9% | 0 / 0 / 10 / 16 | 70 / 54 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–27.8%] (n=10) | 0.0% [0.0%–19.4%] (n=16) | no supported cited steps | 2499706 / 2543 | $0.0000 · $0.0000/1k (40 obs; 0 unknown) | — · —/1k | 4.09 (40) | 0 / 0 / 2 / 8 (10) |
| omp:tt7_form:anthropic/claude-sonnet-5-5 → Luna (after; Luna calls 205–354) | 12.5% / 22.9% | 0 / 0 / 10 / 16 | 70 / 54 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–27.8%] (n=10) | 0.0% [0.0%–19.4%] (n=16) | gold hit 0/0 flagged; 0/80 all+ | 2499706 / 2543 | $0.0000 · $0.0000/1k (40 obs; 0 unknown) | — · —/1k | 4.09 (40) | 0 / 0 / 2 / 8 (10) |
| scout_metr:reward_hacking_scanner@ge5 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-xai-oauth-grok-4.3] → Luna (before; Luna calls 201–393) | 2.5% / 7.1% | 0 / 0 / 2 / 5 | 78 / 65 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–65.8%] (n=2) | 0.0% [0.0%–43.4%] (n=5) | no supported cited steps | 0 / 0 | $0.0000 · $0.0000/1k (7 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 2 (2) |
| scout_metr:reward_hacking_scanner@ge5 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-xai-oauth-grok-4.3] → Luna (after; Luna calls 201–393) | 2.5% / 7.1% | 0 / 0 / 2 / 5 | 78 / 65 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–65.8%] (n=2) | 0.0% [0.0%–43.4%] (n=5) | gold hit 0/0 flagged; 0/80 all+ | 0 / 0 | $0.0000 · $0.0000/1k (7 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 2 (2) |
| scout_metr:reward_hacking_scanner@ge7 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-xai-oauth-grok-4.3] → Luna (before; Luna calls 201–393) | 2.5% / 7.1% | 0 / 0 / 2 / 5 | 78 / 65 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–65.8%] (n=2) | 0.0% [0.0%–43.4%] (n=5) | no supported cited steps | 0 / 0 | $0.0000 · $0.0000/1k (7 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 2 (2) |
| scout_metr:reward_hacking_scanner@ge7 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-xai-oauth-grok-4.3] → Luna (after; Luna calls 201–393) | 2.5% / 7.1% | 0 / 0 / 2 / 5 | 78 / 65 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–65.8%] (n=2) | 0.0% [0.0%–43.4%] (n=5) | gold hit 0/0 flagged; 0/80 all+ | 0 / 0 | $0.0000 · $0.0000/1k (7 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 2 (2) |
| stage1_goodfire_dev → Luna (before; Luna calls 92–142) | 25.0% / 54.3% | 0 / 0 / 20 / 38 | 60 / 32 | 0 / 80 (0.0%) [0.0%–4.6%] | 0.0% [0.0%–16.1%] (n=20) | 0.0% [0.0%–9.2%] (n=38) | no supported cited steps | 0 / 0 | $0.0000 · $0.0000/1k (58 obs; 0 unknown) | $0.0000 · $0.0000/1k | — (0) | 0 / 0 / 0 / 20 (20) |
| stage1_goodfire_dev → Luna (after; Luna calls 92–142) | 30.0% / 54.3% | 1 / 0 / 23 / 38 | 56 / 32 | 1 / 80 (1.2%) [0.2%–6.7%] | 4.2% [0.7%–20.2%] (n=24) | 0.0% [0.0%–9.2%] (n=38) | gold hit 1/1 flagged; 1/80 all+ | 209529 / 689 | — · —/1k (63 obs; 1 unknown) | — · —/1k | — (0) | 0 / 1 / 0 / 23 (24) |

#### cheatbench-dev-crosstest

| Arm / replay | Coverage + / − | TP / FP / FN / TN | Abstain + / − | TP / all positives (yield) [CI] | Recall [CI] | FAR [CI] | Gold-passage hit / valid cited IDs | Tokens in / out (known) | Marginal observed total · per-1k (basis) | List-equivalent total · per-1k | Median s (n) | TP both / arm-only / cascade-only / neither (paired+) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| cm9_stage1_any → Luna (before; Luna calls 116–116) | 44.0% / 64.0% | 0 / 0 / 11 / 16 | 14 / 9 | 0 / 25 (0.0%) [0.0%–13.3%] | 0.0% [0.0%–25.9%] (n=11) | 0.0% [0.0%–19.4%] (n=16) | n/a | 107769 / 62 | — · —/1k (27 obs; 1 unknown) | $0.0108 · $0.4003/1k | — (0) | 0 / 0 / 1 / 10 (11) |
| cm9_stage1_any → Luna (after; Luna calls 116–116) | 48.0% / 72.0% | 1 / 0 / 11 / 18 | 13 / 7 | 1 / 25 (4.0%) [0.7%–19.5%] | 8.3% [1.5%–35.4%] (n=12) | 0.0% [0.0%–17.6%] (n=18) | n/a | 226265 / 249 | — · —/1k (30 obs; 1 unknown) | $0.0228 · $0.7584/1k | — (0) | 0 / 1 / 1 / 10 (12) |
| docent:tt7_form:anthropic/claude-sonnet-5-5 → Luna (before; Luna calls 203–334) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 25 / 25 | 0 / 25 (0.0%) [0.0%–13.3%] | — [—] (n=0) | — [—] (n=0) | n/a | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |
| docent:tt7_form:anthropic/claude-sonnet-5-5 → Luna (after; Luna calls 203–334) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 25 / 25 | 0 / 25 (0.0%) [0.0%–13.3%] | — [—] (n=0) | — [—] (n=0) | n/a | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |
| omp:tt7_form:anthropic/claude-sonnet-5-5 → Luna (before; Luna calls 205–354) | 28.0% / 52.0% | 0 / 0 / 7 / 13 | 18 / 12 | 0 / 25 (0.0%) [0.0%–13.3%] | 0.0% [0.0%–35.4%] (n=7) | 0.0% [0.0%–22.8%] (n=13) | n/a | 2184569 / 1972 | $0.0000 · $0.0000/1k (29 obs; 0 unknown) | — · —/1k | 3.58 (29) | 0 / 0 / 3 / 4 (7) |
| omp:tt7_form:anthropic/claude-sonnet-5-5 → Luna (after; Luna calls 205–354) | 32.0% / 52.0% | 1 / 0 / 7 / 13 | 17 / 12 | 1 / 25 (4.0%) [0.7%–19.5%] | 12.5% [2.2%–47.1%] (n=8) | 0.0% [0.0%–22.8%] (n=13) | n/a | 2208171 / 2150 | $0.0000 · $0.0000/1k (30 obs; 0 unknown) | — · —/1k | 3.70 (30) | 0 / 1 / 3 / 4 (8) |
| scout_metr:reward_hacking_scanner@ge5 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-xai-oauth-grok-4.3] → Luna (before; Luna calls 201–393) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 25 / 25 | 0 / 25 (0.0%) [0.0%–13.3%] | — [—] (n=0) | — [—] (n=0) | n/a | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |
| scout_metr:reward_hacking_scanner@ge5 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-xai-oauth-grok-4.3] → Luna (after; Luna calls 201–393) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 25 / 25 | 0 / 25 (0.0%) [0.0%–13.3%] | — [—] (n=0) | — [—] (n=0) | n/a | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |
| scout_metr:reward_hacking_scanner@ge7 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-xai-oauth-grok-4.3] → Luna (before; Luna calls 201–393) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 25 / 25 | 0 / 25 (0.0%) [0.0%–13.3%] | — [—] (n=0) | — [—] (n=0) | n/a | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |
| scout_metr:reward_hacking_scanner@ge7 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-xai-oauth-grok-4.3] → Luna (after; Luna calls 201–393) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 25 / 25 | 0 / 25 (0.0%) [0.0%–13.3%] | — [—] (n=0) | — [—] (n=0) | n/a | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |
| stage1_goodfire_dev → Luna (before; Luna calls 92–142) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 25 / 25 | 0 / 25 (0.0%) [0.0%–13.3%] | — [—] (n=0) | — [—] (n=0) | n/a | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |
| stage1_goodfire_dev → Luna (after; Luna calls 92–142) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 25 / 25 | 0 / 25 (0.0%) [0.0%–13.3%] | — [—] (n=0) | — [—] (n=0) | n/a | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |

Cheap arms with no decided gate (every pair is composed only of abstentions): docent:tt7_form:docent/glm-deferred, docent:tt7_form:google/gemini-3.8-flash, docent:tt7_form:openai/gpt-5.4-mini, laminar:tt7_form:laminar/flow-1, scout_metr:reward_hacking_scanner@ge5 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-kimi-code-k3.1-pro-highspeed], scout_metr:reward_hacking_scanner@ge7 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-kimi-code-k3.1-pro-highspeed].

### Paired Luna versus whole-run peers and retained comparators

Runs both decided only; first = Luna combined (current).

#### goodfire-dev

| Peer | Form | Paired + / − (unpaired + / −) | Luna TP/FN recall [CI] | Peer TP/FN recall [CI] | Luna FP/TN FAR [CI] | Peer FP/TN FAR [CI] | TP both / Luna-only / peer-only / neither |
|---|---|---|---|---|---|---|---|
| cm9_alarmed | primary | 5 / 3 (75 / 67) | 1/4 20.0% [3.6%–62.4%] | 0/5 0.0% [0.0%–43.4%] | 0/3 0.0% [0.0%–56.1%] | 0/3 0.0% [0.0%–56.1%] | 0 / 1 / 0 / 4 |
| cm9_stage1_any | primary | 5 / 3 (75 / 67) | 1/4 20.0% [3.6%–62.4%] | 4/1 80.0% [37.6%–96.4%] | 0/3 0.0% [0.0%–56.1%] | 0/3 0.0% [0.0%–56.1%] | 1 / 0 / 3 / 1 |
| docent:tt7_form:anthropic/claude-sonnet-5-5 | primary | 3 / 0 (77 / 70) | 1/2 33.3% [6.1%–79.2%] | 0/3 0.0% [0.0%–56.1%] | 0/0 — [—] | 0/0 — [—] | 0 / 1 / 0 / 2 |
| docent:tt7_form:anthropic/claude-sonnet-5-5 | secondary | 3 / 0 (77 / 70) | 1/2 33.3% [6.1%–79.2%] | 0/3 0.0% [0.0%–56.1%] | 0/0 — [—] | 0/0 — [—] | 0 / 1 / 0 / 2 |
| omp:tt7_form:anthropic/claude-opus-5-5 | primary | 4 / 2 (76 / 68) | 1/3 25.0% [4.6%–69.9%] | 0/4 0.0% [0.0%–49.0%] | 0/2 0.0% [0.0%–65.8%] | 0/2 0.0% [0.0%–65.8%] | 0 / 1 / 0 / 3 |
| omp:tt7_form:anthropic/claude-opus-5-5 | secondary | 4 / 2 (76 / 68) | 1/3 25.0% [4.6%–69.9%] | 0/4 0.0% [0.0%–49.0%] | 0/2 0.0% [0.0%–65.8%] | 0/2 0.0% [0.0%–65.8%] | 0 / 1 / 0 / 3 |
| omp:tt7_form:anthropic/claude-sonnet-5-5 | primary | 4 / 2 (76 / 68) | 1/3 25.0% [4.6%–69.9%] | 0/4 0.0% [0.0%–49.0%] | 0/2 0.0% [0.0%–65.8%] | 0/2 0.0% [0.0%–65.8%] | 0 / 1 / 0 / 3 |
| omp:tt7_form:anthropic/claude-sonnet-5-5 | secondary | 4 / 2 (76 / 68) | 1/3 25.0% [4.6%–69.9%] | 0/4 0.0% [0.0%–49.0%] | 0/2 0.0% [0.0%–65.8%] | 0/2 0.0% [0.0%–65.8%] | 0 / 1 / 0 / 3 |
| stage1_goodfire_dev | primary | 5 / 3 (75 / 67) | 1/4 20.0% [3.6%–62.4%] | 4/1 80.0% [37.6%–96.4%] | 0/3 0.0% [0.0%–56.1%] | 0/3 0.0% [0.0%–56.1%] | 1 / 0 / 3 / 1 |

#### cheatbench-dev-crosstest

| Peer | Form | Paired + / − (unpaired + / −) | Luna TP/FN recall [CI] | Peer TP/FN recall [CI] | Luna FP/TN FAR [CI] | Peer FP/TN FAR [CI] | TP both / Luna-only / peer-only / neither |
|---|---|---|---|---|---|---|---|
| cm9_alarmed | primary | 2 / 4 (23 / 21) | 1/1 50.0% [9.5%–90.5%] | 1/1 50.0% [9.5%–90.5%] | 0/4 0.0% [0.0%–49.0%] | 0/4 0.0% [0.0%–49.0%] | 0 / 1 / 1 / 0 |
| cm9_stage1_any | primary | 2 / 4 (23 / 21) | 1/1 50.0% [9.5%–90.5%] | 2/0 100.0% [34.2%–100.0%] | 0/4 0.0% [0.0%–49.0%] | 2/2 50.0% [15.0%–85.0%] | 1 / 0 / 1 / 0 |
| docent:tt7_form:anthropic/claude-sonnet-5-5 | primary | 0 / 0 (25 / 25) | 0/0 — [—] | 0/0 — [—] | 0/0 — [—] | 0/0 — [—] | 0 / 0 / 0 / 0 |
| docent:tt7_form:anthropic/claude-sonnet-5-5 | secondary | 0 / 0 (25 / 25) | 0/0 — [—] | 0/0 — [—] | 0/0 — [—] | 0/0 — [—] | 0 / 0 / 0 / 0 |
| omp:tt7_form:anthropic/claude-opus-5-5 | primary | 2 / 2 (23 / 23) | 1/1 50.0% [9.5%–90.5%] | 1/1 50.0% [9.5%–90.5%] | 0/2 0.0% [0.0%–65.8%] | 0/2 0.0% [0.0%–65.8%] | 1 / 0 / 0 / 1 |
| omp:tt7_form:anthropic/claude-opus-5-5 | secondary | 2 / 2 (23 / 23) | 2/0 100.0% [34.2%–100.0%] | 1/1 50.0% [9.5%–90.5%] | 0/2 0.0% [0.0%–65.8%] | 0/2 0.0% [0.0%–65.8%] | 1 / 1 / 0 / 0 |
| omp:tt7_form:anthropic/claude-sonnet-5-5 | primary | 2 / 2 (23 / 23) | 1/1 50.0% [9.5%–90.5%] | 1/1 50.0% [9.5%–90.5%] | 0/2 0.0% [0.0%–65.8%] | 0/2 0.0% [0.0%–65.8%] | 1 / 0 / 0 / 1 |
| omp:tt7_form:anthropic/claude-sonnet-5-5 | secondary | 2 / 2 (23 / 23) | 2/0 100.0% [34.2%–100.0%] | 2/0 100.0% [34.2%–100.0%] | 0/2 0.0% [0.0%–65.8%] | 0/2 0.0% [0.0%–65.8%] | 2 / 0 / 0 / 0 |
| stage1_goodfire_dev | primary | 0 / 0 (25 / 25) | 0/0 — [—] | 0/0 — [—] | 0/0 — [—] | 0/0 — [—] | 0 / 0 / 0 / 0 |

Peers with no decided runs: docent:tt7_form:docent/glm-deferred, docent:tt7_form:google/gemini-3.8-flash, docent:tt7_form:openai/gpt-5.4-mini, docent:tt7_form:openai/gpt-6.1-sol, omp:tt7_form:xai-oauth/grok-4.3.

### Latest METR snapshot intersection (separate campaign; descriptive only)

separate campaign with different detector semantics; never composed, never used for selection, never mixed into the main arm set or cascades. METR rows in the new snapshot: 1350.

#### goodfire-dev

| METR arm | Coverage + / − | TP / FP / FN / TN | Recall [CI] | FAR [CI] | Paired + / − with Luna | TP both / Luna-only / METR-only |
|---|---|---|---|---|---|---|
| scout_metr:reward_hacking_scanner@ge5 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-kimi-code-k3.1-pro-highspeed;projection-native-actor-v2] | 16.2% / 17.1% | 3 / 0 / 10 / 12 | 23.1% [8.2%–50.3%] | 0.0% [0.0%–24.2%] | 1 / 1 | 0 / 0 / 0 |
| scout_metr:reward_hacking_scanner@ge5 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-xai-oauth-grok-4.3] | 3.8% / 7.1% | 1 / 0 / 2 / 5 | 33.3% [6.1%–79.2%] | 0.0% [0.0%–43.4%] | 0 / 0 | 0 / 0 / 0 |
| scout_metr:reward_hacking_scanner@ge7 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-kimi-code-k3.1-pro-highspeed;projection-native-actor-v2] | 16.2% / 17.1% | 3 / 0 / 10 / 12 | 23.1% [8.2%–50.3%] | 0.0% [0.0%–24.2%] | 1 / 1 | 0 / 0 / 0 |
| scout_metr:reward_hacking_scanner@ge7 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-xai-oauth-grok-4.3] | 3.8% / 7.1% | 1 / 0 / 2 / 5 | 33.3% [6.1%–79.2%] | 0.0% [0.0%–43.4%] | 0 / 0 | 0 / 0 / 0 |

#### cheatbench-dev-crosstest

| METR arm | Coverage + / − | TP / FP / FN / TN | Recall [CI] | FAR [CI] | Paired + / − with Luna | TP both / Luna-only / METR-only |
|---|---|---|---|---|---|---|
| scout_metr:reward_hacking_scanner@ge5 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-kimi-code-k3.1-pro-highspeed;projection-native-actor-v2] | 0.0% / 0.0% | 0 / 0 / 0 / 0 | — [—] | — [—] | 0 / 0 | 0 / 0 / 0 |
| scout_metr:reward_hacking_scanner@ge5 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-xai-oauth-grok-4.3] | 0.0% / 0.0% | 0 / 0 / 0 / 0 | — [—] | — [—] | 0 / 0 | 0 / 0 / 0 |
| scout_metr:reward_hacking_scanner@ge7 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-kimi-code-k3.1-pro-highspeed;projection-native-actor-v2] | 0.0% / 0.0% | 0 / 0 / 0 / 0 | — [—] | — [—] | 0 / 0 | 0 / 0 / 0 |
| scout_metr:reward_hacking_scanner@ge7 [metr-scanners/0.1.11@c12893d1;scanner-v0;omp-xai-oauth-grok-4.3] | 0.0% / 0.0% | 0 / 0 / 0 / 0 | — [—] | — [—] | 0 / 0 | 0 / 0 / 0 |

METR arms without decisions: 5.

### safe-eligible-196 — secondary descriptive view, primary form

#### goodfire-dev

| Arm / replay | Coverage + / − | TP / FP / FN / TN | Abstain + / − | TP / all positives (yield) [CI] | Recall [CI] | FAR [CI] | Gold-passage hit / valid cited IDs | Tokens in / out (known) | Marginal observed total · per-1k (basis) | List-equivalent total · per-1k | Median s (n) | TP both / arm-only / cascade-only / neither (paired+) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Luna combined (current) | 6.2% / 4.3% | 1 / 0 / 4 / 3 | 75 / 66 | 1 / 80 (1.2%) [0.2%–6.7%] | 20.0% [3.6%–62.4%] (n=5) | 0.0% [0.0%–56.1%] (n=3) | gold hit 1/1 flagged; 2/80 all+; valid IDs 100.0% | 371824 / 1047 | — · —/1k (9 obs; 1 unknown) | — · —/1k | 4.62 (9) | 0 / 1 / 0 / 4 (5) |
| Luna historical-2 (baseline) | 0.0% / 0.0% | 0 / 0 / 0 / 0 | 80 / 69 | 0 / 80 (0.0%) [0.0%–4.6%] | — [—] (n=0) | — [—] (n=0) | no supported cited steps | 0 / 0 | — · —/1k (0 obs; 0 unknown) | — · —/1k | — (0) | 0 / 0 / 0 / 0 (0) |

#### cheatbench-dev-crosstest

| Arm / replay | Coverage + / − | TP / FP / FN / TN | Abstain + / − | TP / all positives (yield) [CI] | Recall [CI] | FAR [CI] | Gold-passage hit / valid cited IDs | Tokens in / out (known) | Marginal observed total · per-1k (basis) | List-equivalent total · per-1k | Median s (n) | TP both / arm-only / cascade-only / neither (paired+) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Luna combined (current) | 9.1% / 16.0% | 1 / 0 / 1 / 4 | 20 / 21 | 1 / 22 (4.5%) [0.8%–21.8%] | 50.0% [9.5%–90.5%] (n=2) | 0.0% [0.0%–49.0%] (n=4) | n/a | 261371 / 366 | — · —/1k (6 obs; 2 unknown) | $0.0263 · $4.3867/1k | 4.17 (6) | 0 / 1 / 1 / 0 (2) |
| Luna historical-2 (baseline) | 4.5% / 4.0% | 0 / 0 / 1 / 1 | 21 / 24 | 0 / 22 (0.0%) [0.0%–14.9%] | 0.0% [0.0%–79.3%] (n=1) | 0.0% [0.0%–79.3%] (n=1) | n/a | 136154 / 123 | — · —/1k (2 obs; 2 unknown) | $0.0137 · $6.8385/1k | 7.11 (2) | 0 / 0 / 1 / 0 (1) |

### Goodfire-only recommendation eligibility (Luna-involving replays only)

- primary / observed_arms: no_supported_winner; full-coverage Pareto candidates: none.
- primary / replayed_not_executed: no_supported_winner; full-coverage Pareto candidates: none.
- secondary / observed_arms: no_supported_winner; full-coverage Pareto candidates: none.
- secondary / replayed_not_executed: no_supported_winner; full-coverage Pareto candidates: none.

### Baseline reproduction

Committed baseline reproduced exactly: **True** (19 arm views, 12 Luna pairs compared).
