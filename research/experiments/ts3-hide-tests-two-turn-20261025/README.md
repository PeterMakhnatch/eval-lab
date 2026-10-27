# TS-3, TS-4, TS-5, pT-SELF-1, pT-SELF-2 & pT-SELF-3 Capstone: Retained `Visible-Tests` & `Hidden-Tests` `2×2` Factorial Bundle (`turn-1-visible`, `turn-2-visible`, `turn-1`, `turn-2`, `turn-2-runner-accurate`), 5-Arm Unbundled Decomposition, & `34`-Task Cross-Test

## 1. `VF-7` Item (3) Closure — Retained `turn-1-visible` & `turn-2-visible` Cells Inside `TS3_ROOT/pairs` (`pT-SELF-3`)
- **Materialized `368` `Visible-Tests` Cells Directly Inside `eval-lab-results/2026-10-25/ts3-hide-tests-two-turn/pairs/<dataset>/<task>/<model>/<turn-1-visible|turn-2-visible>/`:** Every clean task across `mimo-defect-map-v1` (`22` tasks), `tb4-audit-v1` (`8` tasks), and `breaker-bench-v1` / `breaker-bench-v1-clean` (`4` tasks) now retains both `turn-1-visible/` (`condition_a_bare`, `hide_tests: false`) and `turn-2-visible/` (`condition_b_retrieval`, `hide_tests: false`) directly inside `TS3_ROOT/pairs` alongside `turn-1/`, `turn-2/`, and `turn-2-runner-accurate/` (`hide_tests: true`), each with its `prompt.md`, `response.jsonl`, `submission/solve.sh`, and `result.json`.
- **100% `llm_stdout_sha256` & `prompt.md` Mtime Integrity Across All `1,243` `result.json` Files in `TS3_ROOT/pairs`:** Audited all `1,243` `result.json` files (`1,223` with `response.jsonl`), repairing the remaining `16` (`9` original + `7` copied) `llm_stdout_sha256` digests so that **`1,223 / 1,223 (100.0%)` match `sha256(response.jsonl)`** and **`0 / 1,243` `prompt.md` files are newer than their `response.jsonl`**.
- **Added `ts5_factorial_2x2_differentials` in `src/evallab/production/qc_reports.py`:** Counts all 5 conditions (`turn_1_visible`, `turn_2_visible`, `turn_1_hidden`, `turn_2_hidden_original_feedback`, `turn_2_hidden_runner_accurate_feedback`) directly from `TS3_ROOT/pairs`, enforcing `cell.get("hide_tests") is False` on the visible conditions and `cell.get("hide_tests") is True` on the hidden conditions.

## 2. 5-Arm Unbundled Causal Decomposition Inside `TS3_ROOT/pairs` (`Visible vs Hidden` × `Turn-1 Single-Shot vs Turn-2 Retrieval vs Turn-2 Runner-Accurate Failure Feedback`, Addressing `pW-SELF-4` & `VF-7`)

### Table 2A — 5-Arm Unbundled Decomposition Across All `30` Strictly Cross-Key-Clean DEV Tasks (`8` `pytest` MiMo + `14` Non-`pytest` MiMo + `8` TB4 DEV)

| Model Route | `turn-1-visible` (`hide=False, 0 FB, 0 Retr`) | `turn-2-visible` (`hide=False, 0 FB, +Retr`) | `turn-1` (`hide=True, 0 FB, 0 Retr`) | `turn-2` (`hide=True, Orig pytest FB, +Retr`, `[Py8 / NonPy14 / TB8]`) | `turn-2-runner-accurate` (`hide=True, +Runner FB, +Retr`, `[Py8 / NonPy14 / TB8]`) | Pure `hide_tests` (`HidT1 − VisT1`) | Pure Retrieval Under Hidden (`NonPy14` in `turn-2`) | Incremental Runner-Accurate FB Gain (`HidT2_Runner − HidT2_Orig`) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `meta/muse-wonder-1.0-contributor` | `6/30` (20.0%) | `11/30` (36.7%) | `4/30` (13.3%) | `13/30` (43.3%) [`8 / 0 / 5`] | **25/30 (83.3%)** [`8 / 12 / 5`] | **`-2` (-6.7 pp)** | **`0/14` (0.0%)** | **`+12` tasks (+40.0 pp)** |
| `meta/muse-spark-1.3-contributor` | `7/30` (23.3%) | `9/30` (30.0%) | `5/30` (16.7%) | `14/30` (46.7%) [`8 / 0 / 6`] | **19/30 (63.3%)** [`8 / 5 / 6`] | **`-2` (-6.7 pp)** | **`0/14` (0.0%)** | **`+5` tasks (+16.7 pp)** |
| `anthropic/claude-sonnet-5-5` | `5/30` (16.7%) | `8/30` (26.7%) | `1/30` (3.3%) | `12/30` (40.0%) [`7 / 0 / 5`] | **18/30 (60.0%)** [`7 / 6 / 5`] | **`-4` (-13.3 pp)** | **`0/14` (0.0%)** | **`+6` tasks (+20.0 pp)** |
| `kimi-code/k3.1-pro-highspeed` | `8/30` (26.7%) | `14/30` (46.7%) | `2/30` (6.7%) | `9/30` (30.0%) [`5 / 0 / 4`] | **16/30 (53.3%)** [`5 / 7 / 4`] | **`-6` (-20.0 pp)** | **`0/14` (0.0%)** | **`+7` tasks (+23.3 pp)** |

### Table 2B — 5-Arm Unbundled Decomposition Across All `34` Strictly Cross-Key-Clean DEV Tasks (`22` MiMo + `8` TB4 + `4` Breaker-Bench DEV)

| Model Route | `turn-1-visible` (`34`) | `turn-2-visible` (`34`) | `turn-1` (`34`) | `turn-2` (`OrigFB + Retr`, `34`) | `turn-2-runner-accurate` (`RunnerFB + Retr`, `34`) | Pure `hide_tests` (`HidT1 − VisT1`) | Total Second-Turn Gain (`HidT2_Runner − HidT1`) | `HidT2_Runner − VisT2` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `meta/muse-wonder-1.0-contributor` | `10/34` (29.4%) | `15/34` (44.1%) | `4/34` (11.8%) | `17/34` (50.0%) | **29/34 (85.3%)** | **`-6` (-17.6 pp)** | **`+25` (+73.5 pp)** | **`+14` (+41.2 pp)** |
| `meta/muse-spark-1.3-contributor` | `11/34` (32.4%) | `13/34` (38.2%) | `5/34` (14.7%) | `18/34` (52.9%) | **23/34 (67.6%)** | **`-6` (-17.6 pp)** | **`+18` (+52.9 pp)** | **`+10` (+29.4 pp)** |
| `kimi-code/k3.1-pro-highspeed` | `12/34` (35.3%) | `18/34` (52.9%) | `4/34` (11.8%) | `13/34` (38.2%) | **20/34 (58.8%)** | **`-8` (-23.5 pp)** | **`+16` (+47.1 pp)** | **`+2` (+5.9 pp)** |
| `anthropic/claude-sonnet-5-5` | `5/34` (14.7%) | `8/34` (23.5%) | `1/34` (2.9%) | `14/34` (41.2%) | **20/34 (58.8%)** | **`-4` (-11.8 pp)** | **`+19` (+55.9 pp)** | **`+12` (+35.3 pp)** |

## 3. Executed 2-Stage Tripwire Cascade Across All `34` Strictly Cross-Key-Clean DEV Tasks (`1.0 -> 0.0`)

| Cascade Arm | Stage-1 Primary Route | Stage-2 Escalator (Only on Stage-1 Miss) | Stage-1 Executed Pass (`34`) | Escalated Tasks (`34`) | Stage-2 Recovered (`34`) | Total Executed Pass (`34`) | Total Calls Used |
|---|---|---|---:|---:|---:|---:|---:|
| **`early_stop_spark13_to_muse_wonder_34`** | `meta/muse-spark-1.3-contributor` | `meta/muse-wonder-1.0-contributor` | `23/34` (67.6%) | `11` | **`+8`** | **31/34 (91.2%)** | `74` (`$0.0000`) |
| **`early_stop_kimi_to_muse_wonder_34`** | `kimi-code/k3.1-pro-highspeed` | `meta/muse-wonder-1.0-contributor` | `20/34` (58.8%) | `14` | **`+11`** | **31/34 (91.2%)** | `78` (`$0.0000`) |
| **`early_stop_spark13_to_sonnet_5_5_34`** | `meta/muse-spark-1.3-contributor` | `anthropic/claude-sonnet-5-5` | `23/34` (67.6%) | `11` | **`+4`** | **27/34 (79.4%)** | `74` (`$0.0000`) |
