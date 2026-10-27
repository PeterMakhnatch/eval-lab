# TS-3, pT-SELF-3, pT-SELF-4 & pT-SELF-5: Complete 44-Task 6-Model 2-Turn `hide_tests=True` Seeker Benchmark, 3-Benchmark Cross-Test (`528` Cells), and 28-Task Head-to-Head Regime Comparison (`864` Total Cells)

## Protocol & Fix for TS-2 Condition-A Confound
- **What changed (`src/evallab/production/seeker_worker.py`):**
  1. Added `hide_tests: bool = False` to `RewardSeekerLiveWorker`, `_prepare_attempt_workspace`, `build_headless_seeker_prompt`, and `OmpHeadlessAttemptGenerator`.
  2. When `hide_tests=True`, `_prepare_attempt_workspace` strips `tests`, `test`, and `verifier` from `workspace / "task"` and `build_headless_seeker_prompt` completely omits the `## Upstream Grader Files (tests/)` section.
  3. **Turn 1 (`attempt_index=1`, `hide_tests=True`, `prompt_mode="bare_task"`):** The seeker sees ONLY `task.toml`, `instruction.md`, and `environment/` (`0` `tests/` files, `0` Hackbook cues, `0` retrieval).
  4. **Turn 2 (`attempt_index=2`, `hide_tests=True`, `prompt_mode="qa_review"`):** `tests/` remains hidden, and the seeker receives the captured **Turn-1 Grader Failure Output (`SeekerFailureMemory`)** plus **`CloudTraceRetriever`** examples (`1,636` Goodfire DEV enacted cheats unlocked + MiMo/TB4 DEV confirmed issues), with Turn-2 failure-token query enrichment and automatic `base_query` fallback.
  5. Added `select_cascade_model_order` for deterministic 2-stage tripwire model routing and hardened `_normalize_mechanism_key` against sequence-valued mechanism/target_check payloads.

## 1. Complete 44-Task × 6-Model × 2-Turn `hide_tests=True` Cross-Test (`22` MiMo DEV + `14` TB4 DEV + `8` `breaker-bench-v1` DEV = `528` Cells)

| Model Route | Served Identity | Attested Turns | Turn 1 Blind Diff (`MiMo 22` / `TB4 14` / `Breaker 8`) | Turn 2 Retr Diff (`MiMo 22` / `TB4 14` / `Breaker 8`) | 2-Turn Cumul `Pass@2` (`M` / `T` / `B`) | `T1 -> T2` Gain | `Diff/100kTok` | Total Cost USD |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `meta/muse-spark-1.3-contributor` | `meta/muse-spark-1.3-contributor` | `86/88` (97.7%) | **25/44 (56.8%)** (`11/22` / `9/14` / `5/8`) | **42/44 (95.5%)** (`22/22` / `12/14` / `8/8`) | **42/44 (95.5%)** (`22/22` / `12/14` / `8/8`) | **`+17` tasks** | **4.006** | `$0.1176` |
| `meta/muse-wonder-1.0-contributor` | `meta/muse-wonder-1.0-contributor` | `88/88` (100.0%) | **30/44 (68.2%)** (`14/22` / `8/14` / `8/8`) | **38/44 (86.4%)** (`21/22` / `9/14` / `8/8`) | **39/44 (88.6%)** (`21/22` / `10/14` / `8/8`) | **`+9` tasks** | **3.810** | `$0.0000` |
| `meta/muse-spark-open` | `openrouter/meta/muse-spark-open` | `77/88` (87.5%)* | **23/44 (52.3%)** (`6/22` / `10/14` / `7/8`) | **34/44 (77.3%)** (`16/22` / `12/14` / `6/8`) | **37/44 (84.1%; 37/39 = 94.9%)** (`17/22` / `12/14` / `8/8`) | **`+14` tasks** | **4.870** | `$0.8952` |
| `anthropic/claude-sonnet-5-5` | `anthropic/claude-sonnet-5-5` | `75/88` (85.2%) | **20/44 (45.5%)** (`9/22` / `4/14` / `7/8`) | **36/44 (81.8%)** (`20/22` / `8/14` / `8/8`) | **37/44 (84.1%)** (`20/22` / `9/14` / `8/8`) | **`+17` tasks** | **3.340** | `$3.6414` |
| `openrouter/deepseek/deepseek-v4.1-flash` | `openrouter/deepseek/deepseek-v4.1-flash` | `63/88` (71.6%) | **24/44 (54.5%)** (`12/22` / `5/14` / `7/8`) | **27/44 (61.4%)** (`18/22` / `4/14` / `5/8`) | **36/44 (81.8%)** (`20/22` / `8/14` / `8/8`) | **`+12` tasks** | **2.004** | `$0.6855` |
| `kimi-code/k3.1-pro-highspeed` | `kimi-code/k3.1-pro-highspeed` | `78/88` (88.6%) | **24/44 (54.5%)** (`12/22` / `6/14` / `6/8`) | **29/44 (65.9%)** (`17/22` / `7/14` / `5/8`) | **34/44 (77.3%)** (`17/22` / `9/14` / `8/8`) | **`+10` tasks** | **4.136** | `$0.0000` |

*\*Note: `openrouter/meta/muse-spark-open` completed `77/77 (100%)` attested turns across the first `39` tasks (`37/39 = 94.9%` cumulative differentials); on the final 5 tasks, OpenRouter's default `max_tokens=131072` pre-reservation exceeded the remaining `92,191`-token balance (`402`), and `_require_llm_attestation` strictly refused fallback attribution.*

## 2. 28-Task Head-to-Head Regime Comparison (`14` MiMo DEV + `14` TB4 DEV: Regime I `tests/` Visible `336` Cells vs Regime II `hide_tests=True` `336` Cells)

| Model Route | Regime I (`tests/` Visible) Bare | Regime I (`tests/` Visible) + Retrieval | Regime II (`hide_tests=True`) Turn 1 Blind | Regime II (`hide_tests=True`) Turn 2 Retr | Regime II (`hide_tests=True`) 2-Turn Cumul | Delta (`Regime II Cumul - Regime I Retr`) |
|---|---:|---:|---:|---:|---:|---:|
| `meta/muse-spark-open` | `11/28` (39.3%) | `13/28` (46.4%) | `13/28` (46.4%) | `26/28` (92.9%) | **26/28 (92.9%)** | **`+13` (+46.4 pp)** |
| `meta/muse-spark-1.3-contributor` | `11/28` (39.3%) | `12/28` (42.9%) | `12/28` (42.9%) | `26/28` (92.9%) | **26/28 (92.9%)** | **`+14` (+50.0 pp)** |
| `meta/muse-wonder-1.0-contributor` | `9/28` (32.1%) | `11/28` (39.3%) | `14/28` (50.0%) | `22/28` (78.6%) | **23/28 (82.1%)** | **`+12` (+42.9 pp)** |
| `openrouter/deepseek/deepseek-v4.1-flash` | `11/28` (39.3%) | `9/28` (32.1%) | `12/28` (42.9%) | `15/28` (53.6%) | **21/28 (75.0%)** | **`+12` (+42.9 pp)** |
| `anthropic/claude-sonnet-5-5` | `7/28` (25.0%) | `10/28` (35.7%) | `8/28` (28.6%) | `20/28` (71.4%) | **21/28 (75.0%)** | **`+11` (+39.3 pp)** |
| `kimi-code/k3.1-pro-highspeed` | `8/28` (28.6%) | `10/28` (35.7%) | `11/28` (39.3%) | `16/28` (57.1%) | **18/28 (64.3%)** | **`+8` (+28.6 pp)** |

## 3. Early-Stop Two-Turn Cascade Efficiency (44 Tasks)
- **`early_stop_muse_spark_1_3_44`**: **`42/44 (95.5%)`** in `63` calls (`742,661` tokens, **`5.655` differentials/100k tokens**, `$0.0815` — `44.7×` cheaper and `+11.4 pp` higher recall than `anthropic/claude-sonnet-5-5`).
- **`early_stop_muse_wonder_44`**: **`39/44 (88.6%)`** in `58` calls (`654,781` tokens, **`5.956` differentials/100k tokens**, `$0.0000` subscription).
- **`early_stop_muse_spark_open_44`**: **`37/44 (84.1%; 37/39 = 94.9%)`** in `65` calls (`536,945` tokens, **`6.891` differentials/100k tokens**, `$0.6630`).
