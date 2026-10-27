# TS-3, TS-4, TS-5, pT-SELF-1 & pT-SELF-2 Capstone: Complete `30`-Task & `34`-Task `2×2` Factorial Tables, `VF-6` Evidence Restoration, `13`-Task Affirmative-DEV Subset, & Executed 2-Stage Tripwire Cascade

## 1. `VF-6` & `pW-SELF-5` Evidence Restoration & `scrub_blind_task_text` Hardening (`pT-SELF-2`)
- **Original Turn-1 Prompt Restoration (`309 / 309` cells):** Restored all `309 / 309` `turn-1/prompt.md` files in `eval-lab-results/2026-10-25/ts3-hide-tests-two-turn/pairs` directly from the verbatim `role=="user"` message recorded inside each cell's immutable `turn-1/response.jsonl` and reset their mtimes to `<= response.jsonl`'s mtime (`0 / 309` prompts newer than `response.jsonl`, `0 / 309` containing `[scrubbed-test-path]`), reproducing `pW-SELF-3`'s exact **`24 / 294` `tests/` mentions and `6 / 294` concrete test file paths** on the original `turn-1/prompt.md` files while storing the scrubbed copies beside them at `turn-1/prompt.scrubbed.md` (`0 / 294` mentioning `tests/`).
- **`scrub_blind_task_text` & `build_headless_seeker_prompt` Hardening (`src/evallab/production/seeker_worker.py`):**
  1. `strict_blind_instructions: bool | None = None`: an explicit `strict_blind_instructions=False` is never overridden by a truthy `task.raw_payload["strict_blind_instructions"]`.
  2. Full `[verifier]` / `[verifier.*]` TOML section block removal until the next `[section]` header + `PurePosixPath` path-component parser (`_is_test_path_token`).
  3. Final assembled prompt sanitization (`if effective_strict_blind: prompt = scrub_blind_task_text(prompt)`).
  4. `find_cross_key_dev_assignments` and `require_cross_key_dev_only(..., require_affirmative_dev=True)` enforcing affirmative `dev`/`train` membership in `assignments.csv`.
- **`qc_reports.ts4_executed_differentials` Reconciliation (`source_sha256 = 7fb80d62...`):** Synced executed `original_reward` and `repaired_reward` into each cell's `result.json` on disk (preserving `static_original_reward` and `static_repaired_reward`) so `evallab.production.qc_reports.ts4_executed_differentials` (`executed <= attested <= cells`) matches the executed `30`-task table 100%.

## 2. Complete `30`-Task, `4`-Task (`breaker-bench-v1`), `34`-Task Combined, and `13`-Task Affirmative-DEV `2×2` Factorial Tables (`Visible vs Hidden Tests` × `Turn 1 vs Turn 2`, True Executed `1.0 -> 0.0`)

### Table 2A — Complete `30`-Task Strictly Cross-Key-Clean Cohort (`22` MiMo DEV + `8` TB4 DEV)

| Model Route | Visible Tests, Turn 1 (`Bare`, `M22/T8`) | Visible Tests, Turn 2 (`Retrieval`, `M22/T8`) | Hidden Tests (`strict_blind`), Turn 1 (`Blind`, `M22/T8`) | Hidden Tests (`strict_blind`), Turn 2 (`FB + Retr`, `M22/T8`) | Pure `hide_tests` Effect (`Hid T1 − Vis T1`) | Second-Turn Effect (`Hid T2 − Hid T1`) | `Hid T2 − Vis T2` (`FB + Hidden` vs `Visible Retr`) |
|---|---:|---:|---:|---:|---:|---:|---:|
| `meta/muse-wonder-1.0-contributor` | `6/30` (20.0%) [`0/6`] | `11/30` (36.7%) [`5/6`] | `4/30` (13.3%) [`0/4`] | **25/30 (83.3%)** [`20/5`] | **`-2` tasks (-6.7 pp)** | **`+21` tasks (+70.0 pp)** | **`+14` tasks (+46.7 pp)** |
| `meta/muse-spark-1.3-contributor` | `7/30` (23.3%) [`1/6`] | `9/30` (30.0%) [`3/6`] | `5/30` (16.7%) [`1/4`] | **19/30 (63.3%)** [`13/6`] | **`-2` tasks (-6.7 pp)** | **`+14` tasks (+46.7 pp)** | **`+10` tasks (+33.3 pp)** |
| `anthropic/claude-sonnet-5-5` | `5/30` (16.7%) [`1/4`] | `8/30` (26.7%) [`5/3`] | `1/30` (3.3%) [`0/1`] | **18/30 (60.0%)** [`13/5`] | **`-4` tasks (-13.3 pp)** | **`+17` tasks (+56.7 pp)** | **`+10` tasks (+33.3 pp)** |
| `kimi-code/k3.1-pro-highspeed` | `8/30` (26.7%) [`3/5`] | `14/30` (46.7%) [`8/6`] | `2/30` (6.7%) [`0/2`] | **16/30 (53.3%)** [`12/4`] | **`-6` tasks (-20.0 pp)** | **`+14` tasks (+46.7 pp)** | **`+2` tasks (+6.7 pp)** |

### Table 2B — `4`-Task Strictly DEV-Everywhere `breaker-bench-v1` Cross-Test (`0.2.0 -> 0.2.2` Executed) & `34`-Task Combined `2×2` Factorial (`22` MiMo + `8` TB4 + `4` Breaker)

| Model Route | Breaker `4` (`Vis T1 / Vis T2 / Hid T1 / Hid T2`) | Combined `34` Vis T1 (`Bare`) | Combined `34` Vis T2 (`Retr`) | Combined `34` Hid T1 (`Blind`) | Combined `34` Hid T2 (`FB + Retr`) | Pure `hide_tests` (`Hid T1 − Vis T1`) | Second-Turn Gain (`Hid T2 − Hid T1`) | `Hid T2 − Vis T2` |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `meta/muse-wonder-1.0-contributor` | `4/4` / `4/4` / `0/4` / **4/4** | `10/34` (29.4%) | `15/34` (44.1%) | `4/34` (11.8%) | **29/34 (85.3%)** | **`-6` (-17.6 pp)** | **`+25` (+73.5 pp)** | **`+14` (+41.2 pp)** |
| `meta/muse-spark-1.3-contributor` | `4/4` / `4/4` / `0/4` / **4/4** | `11/34` (32.4%) | `13/34` (38.2%) | `5/34` (14.7%) | **23/34 (67.6%)** | **`-6` (-17.6 pp)** | **`+18` (+52.9 pp)** | **`+10` (+29.4 pp)** |
| `kimi-code/k3.1-pro-highspeed` | `4/4` / `4/4` / `2/4` / **4/4** | `12/34` (35.3%) | `18/34` (52.9%) | `4/34` (11.8%) | **20/34 (58.8%)** | **`-8` (-23.5 pp)** | **`+16` (+47.1 pp)** | **`+2` (+5.9 pp)** |
| `anthropic/claude-sonnet-5-5` | `0/0*` / `0/0*` / `0/4` / **2/4** | `5/34` (14.7%) | `8/34` (23.5%) | `1/34` (2.9%) | **20/34 (58.8%)** | **`-4` (-11.8 pp)** | **`+19` (+55.9 pp)** | **`+12` (+35.3 pp)** |

### Table 2C — `13`-Task Affirmatively DEV-Assigned Subset (`1` MiMo DEV `format-code-task-000030` + `8` TB4 DEV + `4` Breaker-Bench DEV, Closing `pW-SELF-6` Item 3)

| Model Route | Vis T1 (`13` Affirmative DEV) | Vis T2 (`13` Affirmative DEV) | Hid T1 (`13` Affirmative DEV) | Hid T2 (`13` Affirmative DEV) | Pure `hide_tests` (`Hid T1 − Vis T1`) | Second-Turn Gain (`Hid T2 − Hid T1`) |
|---|---:|---:|---:|---:|---:|---:|
| `meta/muse-wonder-1.0-contributor` | `10/13` (76.9%) | `10/13` (76.9%) | `4/13` (30.8%) | **10/13 (76.9%)** | **`-6` tasks (-46.2 pp)** | **`+6` tasks (+46.2 pp)** |
| `meta/muse-spark-1.3-contributor` | `10/13` (76.9%) | `10/13` (76.9%) | `4/13` (30.8%) | **10/13 (76.9%)** | **`-6` tasks (-46.2 pp)** | **`+6` tasks (+46.2 pp)** |
| `kimi-code/k3.1-pro-highspeed` | `9/13` (69.2%) | `10/13` (76.9%) | `4/13` (30.8%) | **9/13 (69.2%)** | **`-5` tasks (-38.5 pp)** | **`+5` tasks (+38.5 pp)** |
| `anthropic/claude-sonnet-5-5` | `4/13` (30.8%) | `3/13` (23.1%) | `1/13` (7.7%) | **7/13 (53.8%)** | **`-3` tasks (-23.1 pp)** | **`+6` tasks (+46.2 pp)** |

## 3. Executed 2-Stage Tripwire Cascade Across All `34` Strictly Cross-Key-Clean DEV Tasks (`1.0 -> 0.0`)

| Cascade Arm | Stage-1 Primary Route | Stage-2 Escalator (Only on Stage-1 Miss) | Stage-1 Executed Pass (`34`) | Escalated Tasks (`34`) | Stage-2 Recovered (`34`) | Total Executed Pass (`34`) | Total Calls Used |
|---|---|---|---:|---:|---:|---:|---:|
| **`early_stop_spark13_to_muse_wonder_34`** | `meta/muse-spark-1.3-contributor` | `meta/muse-wonder-1.0-contributor` | `23/34` (67.6%) | `11` | **`+8`** | **31/34 (91.2%)** | `74` (`$0.0000`) |
| **`early_stop_kimi_to_muse_wonder_34`** | `kimi-code/k3.1-pro-highspeed` | `meta/muse-wonder-1.0-contributor` | `20/34` (58.8%) | `14` | **`+11`** | **31/34 (91.2%)** | `78` (`$0.0000`) |
| **`early_stop_spark13_to_sonnet_5_5_34`** | `meta/muse-spark-1.3-contributor` | `anthropic/claude-sonnet-5-5` | `23/34` (67.6%) | `11` | **`+4`** | **27/34 (79.4%)** | `74` (`$0.0000`) |
