# TS-3 & pT-SELF-3..6 Capstone: Complete 50-Task 6-Model 2-Turn `hide_tests=True` Seeker Benchmark, 3-Benchmark Cross-Test (`600` Cells), and 28-Task Head-to-Head Regime Comparison (`936` Total Cells)

## Protocol & Fix for TS-2 Condition-A Confound
- **What changed (`src/evallab/production/seeker_worker.py`):**
  1. Added `hide_tests: bool = False` to `RewardSeekerLiveWorker`, `_prepare_attempt_workspace`, `build_headless_seeker_prompt`, and `OmpHeadlessAttemptGenerator`.
  2. When `hide_tests=True`, `_prepare_attempt_workspace` strips `tests`, `test`, and `verifier` from `workspace / "task"` and `build_headless_seeker_prompt` completely omits the `## Upstream Grader Files (tests/)` section.
  3. **Turn 1 (`attempt_index=1`, `hide_tests=True`, `prompt_mode="bare_task"`):** The seeker sees ONLY `task.toml`, `instruction.md`, and `environment/` (`0` `tests/` files, `0` Hackbook cues, `0` retrieval).
  4. **Turn 2 (`attempt_index=2`, `hide_tests=True`, `prompt_mode="qa_review"`):** `tests/` remains hidden, and the seeker receives the captured **Turn-1 Grader Failure Output (`SeekerFailureMemory`)** plus **`CloudTraceRetriever`** examples (`1,636` Goodfire DEV enacted cheats unlocked + MiMo/TB4 DEV confirmed issues), with Turn-2 failure-token query enrichment and automatic `base_query` fallback.
  5. Added `select_cascade_model_order` for deterministic 2-stage tripwire model routing and hardened `_normalize_mechanism_key` against sequence-valued mechanism/target_check payloads.

## 1. Complete 50-Task × 6-Model × 2-Turn `hide_tests=True` Cross-Test (`28` MiMo DEV + `14` TB4 DEV + `8` `breaker-bench-v1` DEV = `600` Cells)

| Model Route | Served Identity | Attested Turns | Turn 1 Blind Diff (`MiMo 28` / `TB4 14` / `Breaker 8`) | Turn 2 Retr Diff (`MiMo 28` / `TB4 14` / `Breaker 8`) | 2-Turn Cumul `Pass@2` (`M` / `T` / `B`) | `T1 -> T2` Gain | `Diff/100kTok` | Total Cost USD |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `meta/muse-spark-1.3-contributor` | `meta/muse-spark-1.3-contributor` | `98/100` (98.0%) | **30/50 (60.0%)** (`16/28` / `9/14` / `5/8`) | **48/50 (96.0%)** (`28/28` / `12/14` / `8/8`) | **48/50 (96.0%)** (`28/28` / `12/14` / `8/8`) | **`+18` tasks** | **3.996** | `$0.1341` |
| `meta/muse-wonder-1.0-contributor` | `meta/muse-wonder-1.0-contributor` | `100/100` (100.0%) | **36/50 (72.0%)** (`20/28` / `8/14` / `8/8`) | **44/50 (88.0%)** (`27/28` / `9/14` / `8/8`) | **45/50 (90.0%)** (`27/28` / `10/14` / `8/8`) | **`+9` tasks** | **3.869** | `$0.0000` |
| `anthropic/claude-sonnet-5-5` | `anthropic/claude-sonnet-5-5` | `87/100` (87.0%) | **26/50 (52.0%)** (`15/28` / `4/14` / `7/8`) | **42/50 (84.0%)** (`26/28` / `8/14` / `8/8`) | **43/50 (86.0%)** (`26/28` / `9/14` / `8/8`) | **`+17` tasks** | **3.361** | `$4.2189` |
| `openrouter/deepseek/deepseek-v4.1-flash` | `openrouter/deepseek/deepseek-v4.1-flash` | `72/100` (72.0%) | **29/50 (58.0%)** (`17/28` / `5/14` / `7/8`) | **31/50 (62.0%)** (`22/28` / `4/14` / `5/8`) | **42/50 (84.0%)** (`26/28` / `8/14` / `8/8`) | **`+13` tasks** | **2.102** | `$0.7493` |
| `kimi-code/k3.1-pro-highspeed` | `kimi-code/k3.1-pro-highspeed` | `88/100` (88.0%) | **29/50 (58.0%)** (`17/28` / `6/14` / `6/8`) | **34/50 (68.0%)** (`22/28` / `7/14` / `5/8`) | **40/50 (80.0%)** (`23/28` / `9/14` / `8/8`) | **`+11` tasks** | **4.287** | `$0.0000` |
| `meta/muse-spark-open` | `openrouter/meta/muse-spark-open` | `77/77` (100%)* | **23/39 (59.0%; 23/50)** (`6/17` / `10/14` / `7/8`) | **34/39 (87.2%; 34/50)** (`16/17` / `12/14` / `6/8`) | **37/39 (94.9%; 37/50 = 74.0%)** (`17/17` / `12/14` / `8/8`) | **`+14` tasks** | **4.870** | `$0.8952` |

*\*Note: `openrouter/meta/muse-spark-open` completed `77/77 (100%)` attested turns across the first `39` tasks (`37/39 = 94.9%` cumulative differentials; `17/17 = 100%` on completed MiMo DEV tasks); on the remaining 11 MiMo DEV tasks, OpenRouter's server-side `max_tokens=131072` pre-reservation exceeded the `92,191`-token credit balance (`402`), and `_require_llm_attestation` strictly refused fallback attribution.*

## 2. 28-Task Head-to-Head Regime Comparison (`14` MiMo DEV + `14` TB4 DEV: Regime I `tests/` Visible `336` Cells vs Regime II `hide_tests=True` `336` Cells)

| Model Route | Regime I (`tests/` Visible) Bare | Regime I (`tests/` Visible) + Retrieval | Regime II (`hide_tests=True`) Turn 1 Blind | Regime II (`hide_tests=True`) Turn 2 Retr | Regime II (`hide_tests=True`) 2-Turn Cumul | Delta (`Regime II Cumul - Regime I Retr`) |
|---|---:|---:|---:|---:|---:|---:|
| `meta/muse-spark-open` | `11/28` (39.3%) | `13/28` (46.4%) | `13/28` (46.4%) | `26/28` (92.9%) | **26/28 (92.9%)** | **`+13` (+46.4 pp)** |
| `meta/muse-spark-1.3-contributor` | `11/28` (39.3%) | `12/28` (42.9%) | `12/28` (42.9%) | `26/28` (92.9%) | **26/28 (92.9%)** | **`+14` (+50.0 pp)** |
| `meta/muse-wonder-1.0-contributor` | `9/28` (32.1%) | `11/28` (39.3%) | `14/28` (50.0%) | `22/28` (78.6%) | **23/28 (82.1%)** | **`+12` (+42.9 pp)** |
| `openrouter/deepseek/deepseek-v4.1-flash` | `11/28` (39.3%) | `9/28` (32.1%) | `12/28` (42.9%) | `15/28` (53.6%) | **21/28 (75.0%)** | **`+12` (+42.9 pp)** |
| `anthropic/claude-sonnet-5-5` | `7/28` (25.0%) | `10/28` (35.7%) | `8/28` (28.6%) | `20/28` (71.4%) | **21/28 (75.0%)** | **`+11` (+39.3 pp)** |
| `kimi-code/k3.1-pro-highspeed` | `8/28` (28.6%) | `10/28` (35.7%) | `11/28` (39.3%) | `16/28` (57.1%) | **18/28 (64.3%)** | **`+8` (+28.6 pp)** |

## 3. Early-Stop Two-Turn Cascade Efficiency (50 Tasks)
- **`early_stop_muse_spark_1_3_50`**: **`48/50 (96.0%)`** in `70` calls (`838,341` tokens, **`5.726` differentials/100k tokens**, `$0.0913` — `31.7×` cheaper than early-stop `anthropic/claude-sonnet-5-5` and `46.2×` cheaper than 2-turn `anthropic/claude-sonnet-5-5`, while beating `Sonnet 5.5`'s `43/50 = 86.0%` by `+10.0 pp`).
- **`early_stop_muse_wonder_50`**: **`45/50 (90.0%)`** in `64` calls (`721,568` tokens, **`6.236` differentials/100k tokens**, `$0.0000` subscription).
- **`early_stop_sonnet_5_5_50`**: **`43/50 (86.0%)`** in `74` calls (`870,958` tokens, **`4.937` differentials/100k tokens**, `$2.8955`).
