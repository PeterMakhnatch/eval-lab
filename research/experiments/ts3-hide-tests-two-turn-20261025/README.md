# TS-3, TS-4, TS-5, pT-SELF-1, pT-SELF-2, pT-SELF-3 & pT-SELF-4 Capstone: 6-Arm Orthogonal Causal Ablation (`turn-1-visible`, `turn-2-visible`, `turn-1`, `turn-2-retrieval-only`, `turn-2-feedback-only`, `turn-2-runner-accurate`), `30`/`34`-Task `2×2` Factorial Bundle, & `breaker-bench-v1` Cross-Test

## 1. `pT-SELF-4` (4/6) — 6-Arm Orthogonal Unbundling (`hide_tests` × `Turn-1 Runner Feedback` × `Turn-2 CloudTraceRetriever`) & Schema Validation
- **6-Arm Orthogonal Cells Retained Inside `eval-lab-results/2026-10-25/ts3-hide-tests-two-turn/pairs` (`60` new cells across `6` Affirmative-DEV Tune tasks + `4` `breaker-bench-v1` Cross-Test tasks = `10` tasks × `3` models):** Added `turn-2-retrieval-only/` (`hide_tests: true, 0 FB, +Retr`) and `turn-2-feedback-only/` (`hide_tests: true, +FB, 0 Retr`) alongside `turn-1-visible/` (`hide_tests: false, 0 FB, 0 Retr`), `turn-2-visible/` (`hide_tests: false, 0 FB, +Retr`), `turn-1/` (`hide_tests: true, 0 FB, 0 Retr`), and `turn-2-runner-accurate/` (`hide_tests: true, +FB, +Retr`), so every single causal factor (`hide_tests`, `Turn-1 Runner Failure Feedback`, and `Turn-2 CloudTraceRetriever`) is orthogonally isolated with identical auxiliary inputs.
- **Complete Per-Cell Usage Backfill (`94` cells) & `_SeekerFactorialDifferentials` Schema Validator (`src/evallab/production/benchmark_reports.py`):** Backfilled `input_tokens`, `output_tokens`, `total_tokens`, and `cost_usd` from `response.jsonl` across all `94` remaining cells in `TS3_ROOT/pairs` so `_seeker_system` in `qc_reports.py` reports `0 of 18 rows evaluated fewer tasks than the cohort's 30` and `0 rows lack a priced cost`; added `_SeekerFactorialDifferentials` (`evallab.seeker_factorial_2x2_differentials/v1`) validation to `validate_benchmark_document`.

### Table 1A — 6-Arm Orthogonal Causal Ablation on `10` Affirmative-DEV Tasks (`6` Tune [`1` MiMo + `5` TB4] + `4` `breaker-bench-v1` Cross-Test, True Executed `1.0 -> 0.0`)

| Model Route | `turn-1-visible` (`hide=F, 0 FB, 0 Retr`, `[T6/B4]`) | `turn-2-visible` (`hide=F, 0 FB, +Retr`, `[T6/B4]`) | `turn-1` (`hide=T, 0 FB, 0 Retr`, `[T6/B4]`) | `turn-2-retrieval-only` (`hide=T, 0 FB, +Retr`, `[T6/B4]`) | `turn-2-feedback-only` (`hide=T, +FB, 0 Retr`, `[T6/B4]`) | `turn-2-runner-accurate` (`hide=T, +FB, +Retr`, `[T6/B4]`) | Pure `hide_tests` at T1 (`HidT1 − VisT1`) | Pure `hide_tests` w/ Retr (`HidT2_Retr − VisT2`) | Pure Retr Under Hidden (`HidT2_Retr − HidT1`) | Pure FB Under Hidden (`HidT2_FB − HidT1`) | Combined `FB + Retr` (`HidT2_Both − HidT1`) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `meta/muse-wonder-1.0-contributor` | `7/10` [`3/4`] | `7/10` [`3/4`] | `3/10` [`3/0`] | `6/10` [`2/4`] | **7/10** [`3/4`] | **7/10** [`3/4`] | **`-4` (-40 pp)** | **`-1` (-10 pp)** | **`+3` (+30 pp)** | **`+4` (+40 pp)** | **`+4` (+40 pp)** |
| `meta/muse-spark-1.3-contributor` | `7/10` [`3/4`] | `7/10` [`3/4`] | `3/10` [`3/0`] | `5/10` [`3/2`] | `7/10` [`3/4`] | **9/10** [`5/4`] | **`-4` (-40 pp)** | **`-2` (-20 pp)** | **`+2` (+20 pp)** | **`+4` (+40 pp)** | **`+6` (+60 pp)** |
| `kimi-code/k3.1-pro-highspeed` | `8/10` [`4/4`] | `7/10` [`3/4`] | `5/10` [`3/2`] | `3/10` [`1/2`] | `6/10` [`2/4`] | **8/10** [`4/4`] | **`-3` (-30 pp)** | **`-4` (-40 pp)** | **`-2` (-20 pp)** | **`+1` (+10 pp)** | **`+3` (+30 pp)** |

## 2. 5-Arm Unbundled Causal Decomposition Inside `TS3_ROOT/pairs` Across All `30` & `34` Strictly Cross-Key-Clean DEV Tasks (`source_sha256 = d14dc918494bae0655e2d06544964bac517cd86eb9b68e976f602d098080e032`)

### Table 2A — 5-Arm Unbundled Decomposition Across All `30` Strictly Cross-Key-Clean DEV Tasks (`8` `pytest` MiMo + `14` Non-`pytest` MiMo + `8` TB4 DEV)

| Model Route | `turn-1-visible` (`hide=False, 0 FB, 0 Retr`) | `turn-2-visible` (`hide=False, 0 FB, +Retr`) | `turn-1` (`hide=True, 0 FB, 0 Retr`) | `turn-2` (`hide=True, Orig pytest FB, +Retr`, `[Py8 / NonPy14 / TB8]`) | `turn-2-runner-accurate` (`hide=True, +Runner FB, +Retr`, `[Py8 / NonPy14 / TB8]`) | Pure `hide_tests` (`HidT1 − VisT1`) | Pure Retrieval Under Hidden (`NonPy14` in `turn-2`) | Incremental Runner-Accurate FB Gain (`HidT2_Runner − HidT2_Orig`) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `meta/muse-wonder-1.0-contributor` | `6/28/30` (20.0%) | `11/29/30` (36.7%) | `4/30/30` (13.3%) | `13/30/30` (43.3%) [`8 / 0 / 5`] | **25/30/30 (83.3%)** [`8 / 12 / 5`] | **`-2` (-6.7 pp)** | **`0/14` (0.0%)** | **`+12` tasks (+40.0 pp)** |
| `meta/muse-spark-1.3-contributor` | `7/26/30` (23.3%) | `9/28/30` (30.0%) | `5/30/30` (16.7%) | `14/30/30` (46.7%) [`8 / 0 / 6`] | **19/30/30 (63.3%)** [`8 / 5 / 6`] | **`-2` (-6.7 pp)** | **`0/14` (0.0%)** | **`+5` tasks (+16.7 pp)** |
| `anthropic/claude-sonnet-5-5` | `5/27/30` (16.7%) | `8/25/30` (26.7%) | `1/25/30` (3.3%) | `12/27/30` (40.0%) [`7 / 0 / 5`] | **18/28/30 (60.0%)** [`7 / 6 / 5`] | **`-4` (-13.3 pp)** | **`0/14` (0.0%)** | **`+6` tasks (+20.0 pp)** |
| `kimi-code/k3.1-pro-highspeed` | `8/28/30` (26.7%) | `14/30/30` (46.7%) | `2/24/30` (6.7%) | `9/28/30` (30.0%) [`5 / 0 / 4`] | **16/27/30 (53.3%)** [`5 / 7 / 4`] | **`-6` (-20.0 pp)** | **`0/14` (0.0%)** | **`+7` tasks (+23.3 pp)** |

### Table 2B — 5-Arm Unbundled Decomposition Across All `34` Strictly Cross-Key-Clean DEV Tasks (`22` MiMo + `8` TB4 + `4` Breaker-Bench DEV)

| Model Route | `turn-1-visible` (`34`) | `turn-2-visible` (`34`) | `turn-1` (`34`) | `turn-2` (`OrigFB + Retr`, `34`) | `turn-2-runner-accurate` (`RunnerFB + Retr`, `34`) | Pure `hide_tests` (`HidT1 − VisT1`) | Total Second-Turn Gain (`HidT2_Runner − HidT1`) | `HidT2_Runner − VisT2` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `meta/muse-wonder-1.0-contributor` | `10/34` (29.4%) | `15/34` (44.1%) | `4/34` (11.8%) | `17/34` (50.0%) | **29/34 (85.3%)** | **`-6` (-17.6 pp)** | **`+25` (+73.5 pp)** | **`+14` (+41.2 pp)** |
| `meta/muse-spark-1.3-contributor` | `11/34` (32.4%) | `13/34` (38.2%) | `5/34` (14.7%) | `18/34` (52.9%) | **23/34 (67.6%)** | **`-6` (-17.6 pp)** | **`+18` (+52.9 pp)** | **`+10` (+29.4 pp)** |
| `kimi-code/k3.1-pro-highspeed` | `12/34` (35.3%) | `18/34` (52.9%) | `4/34` (11.8%) | `13/34` (38.2%) | **20/34 (58.8%)** | **`-8` (-23.5 pp)** | **`+16` (+47.1 pp)** | **`+2` (+5.9 pp)** |
| `anthropic/claude-sonnet-5-5` | `5/34` (14.7%) | `8/34` (23.5%) | `1/34` (2.9%) | `14/34` (41.2%) | **20/34 (58.8%)** | **`-4` (-11.8 pp)** | **`+19` (+55.9 pp)** | **`+12` (+35.3 pp)** |
