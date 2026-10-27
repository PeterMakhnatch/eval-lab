# TS-3 & pT-SELF-3: Complete 6-Model 2-Turn `hide_tests=True` Seeker Benchmark & Cross-Test Across `mimo-defect-map-v1` DEV, `tb4-audit-v1` DEV, and `breaker-bench-v1` (Terminal Wrench) DEV Pairs (`288` Cells)

## Protocol & Fix for TS-2 Condition-A Confound
- **What changed (`src/evallab/production/seeker_worker.py`):**
  1. Added `hide_tests: bool = False` to `RewardSeekerLiveWorker`, `_prepare_attempt_workspace`, `build_headless_seeker_prompt`, and `OmpHeadlessAttemptGenerator`.
  2. When `hide_tests=True`, `_prepare_attempt_workspace` strips `tests`, `test`, and `verifier` from `workspace / "task"` and `build_headless_seeker_prompt` completely omits the `## Upstream Grader Files (tests/)` section.
  3. **Turn 1 (`attempt_index=1`, `hide_tests=True`, `prompt_mode="bare_task"`):** The seeker sees ONLY `task.toml`, `instruction.md`, and `environment/` (`0` `tests/` files, `0` Hackbook cues, `0` retrieval).
  4. **Turn 2 (`attempt_index=2`, `hide_tests=True`, `prompt_mode="qa_review"`):** `tests/` remains hidden, and the seeker receives the captured **Turn-1 Grader Failure Output (`SeekerFailureMemory`)** plus **`CloudTraceRetriever`** examples (`1,636` Goodfire DEV enacted cheats unlocked + MiMo/TB4 DEV confirmed issues).
  5. Added `select_cascade_model_order` for deterministic 2-stage tripwire model routing.

## Complete 6-Model × 24-Task × 2-Turn Cross-Test (`8` MiMo DEV + `8` TB4 DEV + `8` `breaker-bench-v1` DEV × `6` Models × `2` Turns = `288` Cells)

| Model Route | Served Identity (`48/48` calls) | Attested Turns | Turn 1 Blind Diff (`MiMo` / `TB4` / `Breaker`) | Turn 1 Script / Reasoning Probe | Turn 2 Retr Diff (`MiMo` / `TB4` / `Breaker`) | Turn 2 Script / Reasoning Probe | 2-Turn Cumul `Pass@2` (`M` / `T` / `B`) | `T1 -> T2` Gain | `Diff/100kTok` | Total Cost USD |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `meta/muse-spark-open` | `openrouter/meta/muse-spark-open` | `48/48` (100.0%) | **12/24 (50.0%)** (`0/8` / `5/8` / `7/8`) | `0/24` (0.0%) / `24/24` (100%) | **21/24 (87.5%)** (`8/8` / `7/8` / `6/8`) | `16/24` (66.7%) / `24/24` (100%) | **23/24 (95.8%)** (`8/8` / `7/8` / `8/8`) | **`+11` tasks** | **5.222** | `$0.4588` |
| `meta/muse-spark-1.3-contributor` | `meta/muse-spark-1.3-contributor` | `46/48` (95.8%) | **10/24 (41.7%)** (`0/8` / `5/8` / `5/8`) | `0/24` (0.0%) / `22/24` (91.7%) | **23/24 (95.8%)** (`8/8` / `7/8` / `8/8`) | `12/24` (50.0%) / `24/24` (100%) | **23/24 (95.8%)** (`8/8` / `7/8` / `8/8`) | **`+13` tasks** | **4.247** | `$0.0628` |
| `anthropic/claude-sonnet-5-5` | `anthropic/claude-sonnet-5-5` | `48/48` (100.0%) | **10/24 (41.7%)** (`0/8` / `3/8` / `7/8`) | `1/24` (4.2%) / `24/24` (100%) | **22/24 (91.7%)** (`7/8` / `7/8` / `8/8`) | `7/24` (29.2%) / `24/24` (100%) | **22/24 (91.7%)** (`7/8` / `7/8` / `8/8`) | **`+12` tasks** | **3.222** | `$2.2993` |
| `meta/muse-wonder-1.0-contributor` | `meta/muse-wonder-1.0-contributor` | `48/48` (100.0%) | **11/24 (45.8%)** (`0/8` / `3/8` / `8/8`) | `0/24` (0.0%) / `19/24` (79.2%) | **19/24 (79.2%)** (`7/8` / `4/8` / `8/8`) | `11/24` (45.8%) / `24/24` (100%) | **19/24 (79.2%)** (`7/8` / `4/8` / `8/8`) | **`+8` tasks** | **3.449** | `$0.0000` |
| `openrouter/deepseek/deepseek-v4.1-flash` | `openrouter/deepseek/deepseek-v4.1-flash` | `31/48` (64.6%) | **9/24 (37.5%)** (`1/8` / `1/8` / `7/8`) | `2/24` (8.3%) / `24/24` (100%) | **13/24 (54.2%)** (`6/8` / `2/8` / `5/8`) | `9/24` (37.5%) / `24/24` (100%) | **18/24 (75.0%)** (`7/8` / `3/8` / `8/8`) | **`+9` tasks** | **1.987** | `$0.3655` |
| `kimi-code/k3.1-pro-highspeed` | `kimi-code/k3.1-pro-highspeed` | `45/48` (93.8%) | **10/24 (41.7%)** (`0/8` / `4/8` / `6/8`) | `0/24` (0.0%) / `24/24` (100%) | **13/24 (54.2%)** (`3/8` / `5/8` / `5/8`) | `8/24` (33.3%) / `24/24` (100%) | **17/24 (70.8%)** (`3/8` / `6/8` / `8/8`) | **`+7` tasks** | **3.700** | `$0.0000` |

## Early-Stop Two-Turn Cascade Efficiency
- **`early_stop_two_turn_muse_spark_open`** (run Turn 2 only on the 12 tasks where Turn 1 did not confirm): **`23/24 (95.8%)`** in `36` calls, `324,114` tokens, **`7.096` differentials/100k tokens**, `$0.3460` (`6.65×` cheaper and `2.20×` more token-efficient than `anthropic/claude-sonnet-5-5`).
- **`early_stop_two_turn_muse_spark_1_3`** (run Turn 2 only on the 14 tasks where Turn 1 did not confirm): **`23/24 (95.8%)`** in `38` calls, `426,699` tokens, **`5.390` differentials/100k tokens**, `$0.0486` (`47.3×` cheaper than `anthropic/claude-sonnet-5-5`).
