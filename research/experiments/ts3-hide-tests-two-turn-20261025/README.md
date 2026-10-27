# TS-3, pT-SELF-3 & pT-SELF-4: Complete 36-Task 6-Model 2-Turn `hide_tests=True` Seeker Benchmark, 3-Benchmark Cross-Test (`432` Cells), and 28-Task Head-to-Head Regime Comparison (`768` Total Cells)

## Protocol & Fix for TS-2 Condition-A Confound
- **What changed (`src/evallab/production/seeker_worker.py`):**
  1. Added `hide_tests: bool = False` to `RewardSeekerLiveWorker`, `_prepare_attempt_workspace`, `build_headless_seeker_prompt`, and `OmpHeadlessAttemptGenerator`.
  2. When `hide_tests=True`, `_prepare_attempt_workspace` strips `tests`, `test`, and `verifier` from `workspace / "task"` and `build_headless_seeker_prompt` completely omits the `## Upstream Grader Files (tests/)` section.
  3. **Turn 1 (`attempt_index=1`, `hide_tests=True`, `prompt_mode="bare_task"`):** The seeker sees ONLY `task.toml`, `instruction.md`, and `environment/` (`0` `tests/` files, `0` Hackbook cues, `0` retrieval).
  4. **Turn 2 (`attempt_index=2`, `hide_tests=True`, `prompt_mode="qa_review"`):** `tests/` remains hidden, and the seeker receives the captured **Turn-1 Grader Failure Output (`SeekerFailureMemory`)** plus **`CloudTraceRetriever`** examples (`1,636` Goodfire DEV enacted cheats unlocked + MiMo/TB4 DEV confirmed issues).
  5. Added `select_cascade_model_order` for deterministic 2-stage tripwire model routing and hardened `_normalize_mechanism_key` against sequence-valued mechanism/target_check payloads.

## 1. Complete 36-Task × 6-Model × 2-Turn `hide_tests=True` Cross-Test (`14` MiMo DEV + `14` TB4 DEV + `8` `breaker-bench-v1` DEV = `432` Cells)

| Model Route | Served Identity | Attested Turns | Turn 1 Blind Diff (`MiMo 14` / `TB4 14` / `Breaker 8`) | Turn 2 Retr Diff (`MiMo 14` / `TB4 14` / `Breaker 8`) | 2-Turn Cumul `Pass@2` (`M` / `T` / `B`) | `T1 -> T2` Gain | `Diff/100kTok` | Total Cost USD |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `meta/muse-spark-open` | `openrouter/meta/muse-spark-open` | `72/72` (100.0%) | **20/36 (55.6%)** (`3/14` / `10/14` / `7/8`) | **32/36 (88.9%)** (`14/14` / `12/14` / `6/8`) | **34/36 (94.4%)** (`14/14` / `12/14` / `8/8`) | **`+14` tasks** | **4.894** | `$0.7442` |
| `meta/muse-spark-1.3-contributor` | `meta/muse-spark-1.3-contributor` | `70/72` (97.2%) | **17/36 (47.2%)** (`3/14` / `9/14` / `5/8`) | **34/36 (94.4%)** (`14/14` / `12/14` / `8/8`) | **34/36 (94.4%)** (`14/14` / `12/14` / `8/8`) | **`+17` tasks** | **4.026** | `$0.0964` |
| `meta/muse-wonder-1.0-contributor` | `meta/muse-wonder-1.0-contributor` | `72/72` (100.0%) | **22/36 (61.1%)** (`6/14` / `8/14` / `8/8`) | **30/36 (83.3%)** (`13/14` / `9/14` / `8/8`) | **31/36 (86.1%)** (`13/14` / `10/14` / `8/8`) | **`+9` tasks** | **3.702** | `$0.0000` |
| `anthropic/claude-sonnet-5-5` | `anthropic/claude-sonnet-5-5` | `62/72` (86.1%) | **15/36 (41.7%)** (`4/14` / `4/14` / `7/8`) | **28/36 (77.8%)** (`13/14` / `7/14` / `8/8`) | **29/36 (80.6%)** (`13/14` / `8/14` / `8/8`) | **`+14` tasks** | **3.203** | `$2.9739` |
| `openrouter/deepseek/deepseek-v4.1-flash` | `openrouter/deepseek/deepseek-v4.1-flash` | `51/72` (70.8%) | **19/36 (52.8%)** (`7/14` / `5/14` / `7/8`) | **20/36 (55.6%)** (`11/14` / `4/14` / `5/8`) | **29/36 (80.6%)** (`13/14` / `8/14` / `8/8`) | **`+10` tasks** | **2.028** | `$0.5843` |
| `kimi-code/k3.1-pro-highspeed` | `kimi-code/k3.1-pro-highspeed` | `63/72` (87.5%) | **17/36 (47.2%)** (`5/14` / `6/14` / `6/8`) | **21/36 (58.3%)** (`9/14` / `7/14` / `5/8`) | **26/36 (72.2%)** (`9/14` / `9/14` / `8/8`) | **`+9` tasks** | **3.904** | `$0.0000` |

## 2. 28-Task Head-to-Head Regime Comparison (`14` MiMo DEV + `14` TB4 DEV: Regime I `tests/` Visible `336` Cells vs Regime II `hide_tests=True` `336` Cells)

| Model Route | Regime I (`tests/` Visible) Bare | Regime I (`tests/` Visible) + Retrieval | Regime II (`hide_tests=True`) Turn 1 Blind | Regime II (`hide_tests=True`) Turn 2 Retr | Regime II (`hide_tests=True`) 2-Turn Cumul | Delta (`Regime II Cumul - Regime I Retr`) |
|---|---:|---:|---:|---:|---:|---:|
| `meta/muse-spark-open` | `11/28` (39.3%) | `13/28` (46.4%) | `13/28` (46.4%) | `26/28` (92.9%) | **26/28 (92.9%)** | **`+13` (+46.4 pp)** |
| `meta/muse-spark-1.3-contributor` | `11/28` (39.3%) | `12/28` (42.9%) | `12/28` (42.9%) | `26/28` (92.9%) | **26/28 (92.9%)** | **`+14` (+50.0 pp)** |
| `meta/muse-wonder-1.0-contributor` | `9/28` (32.1%) | `11/28` (39.3%) | `14/28` (50.0%) | `22/28` (78.6%) | **23/28 (82.1%)** | **`+12` (+42.9 pp)** |
| `openrouter/deepseek/deepseek-v4.1-flash` | `11/28` (39.3%) | `9/28` (32.1%) | `12/28` (42.9%) | `15/28` (53.6%) | **21/28 (75.0%)** | **`+12` (+42.9 pp)** |
| `anthropic/claude-sonnet-5-5` | `7/28` (25.0%) | `10/28` (35.7%) | `8/28` (28.6%) | `20/28` (71.4%) | **21/28 (75.0%)** | **`+11` (+39.3 pp)** |
| `kimi-code/k3.1-pro-highspeed` | `8/28` (28.6%) | `10/28` (35.7%) | `11/28` (39.3%) | `16/28` (57.1%) | **18/28 (64.3%)** | **`+8` (+28.6 pp)** |

## 3. Early-Stop Two-Turn Cascade Efficiency (36 Tasks)
- **`early_stop_muse_spark_open_36`**: **`34/36 (94.4%)`** in `52` calls (`492,440` tokens, **`6.904` differentials/100k tokens**, `$0.5311` — `5.60×` cheaper and `+13.8 pp` higher recall than `anthropic/claude-sonnet-5-5`).
- **`early_stop_muse_spark_1_3_36`**: **`34/36 (94.4%)`** in `55` calls (`632,767` tokens, **`5.373` differentials/100k tokens**, `$0.0712` — `41.8×` cheaper than `anthropic/claude-sonnet-5-5`).
- **`early_stop_muse_wonder_36`**: **`31/36 (86.1%)`** in `50` calls (`563,864` tokens, **`5.498` differentials/100k tokens**, `$0.0000` subscription).
