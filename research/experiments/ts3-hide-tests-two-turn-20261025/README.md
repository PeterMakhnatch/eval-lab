# TS-3, TS-4 & TS-5 Capstone: Strict-Blind Instruction Scrubbing (`strict_blind_instructions=True`), 2×2 Factorial Table (`Visible vs Hidden Tests` × `Turn 1 vs Turn 2`), & Strictly DEV-Everywhere `breaker-bench-v1` Executed Differentials

## 1. TS-5 Part (1a): Strict-Blind Instruction Scrubbing (`strict_blind_instructions=True`, Closing `pW-SELF-3`)
- Added `scrub_blind_task_text(text)` and `strict_blind_instructions: bool = False` to `build_headless_seeker_prompt` and `OmpHeadlessAttemptGenerator` in `src/evallab/production/seeker_worker.py`.
- Scrubs `[verifier]` table headers, maintainer comments mentioning `tests/`, `test.sh`, `test_*.py`, `pytest`, or `verifier`, and inline `tests/<path>` / `test_*.py` / `.pytest_cache` tokens from `task.toml`, `instruction.md`, and `environment/*`.
- Across all `294` Turn-1 prompts in `eval-lab-results/2026-10-25/ts3-hide-tests-two-turn/pairs`:
  - **Before (`pW-SELF-3`):** `24/294` Turn-1 prompts (`4` TB4 tasks × `6` models: `distributed-dedup`, `html-js-filter`, `live-database-cutover`, `vba-userform-port`) mentioned `tests/`, and `6` (`vba-userform-port`) named `tests/test.sh` (`6` on `html-js-filter` named `tests/Dockerfile`).
  - **After (`strict_blind_instructions=True`):** **`0 / 294` Turn-1 prompts** mention `tests/` or any concrete test file path; re-running Turn 1 with `strict_blind_instructions=True` on the `3` cross-key-clean TB4 DEV tasks (`distributed-dedup`, `html-js-filter`, `vba-userform-port`) produced **`10/10 (100%)` identical Turn-1 differential outcomes** across all attested runs.

## 2. TS-5 Part (1b): Clean 2×2 Factorial Table (`Visible vs Hidden Tests` × `Turn 1 Single-Shot vs Turn 2 Runner-Feedback + Retrieval`, Closing `pW-SELF-4`)

### Table 2A — 16-Task Strictly Cross-Key-Clean Cohort (`8` MiMo DEV + `8` TB4 DEV = `384` Cells): Static Mechanism Match (`pW-SELF-4` Uncontaminated Recompute)

| Model Route | Visible Tests, Turn 1 (`Bare`) | Visible Tests, Turn 2 (`Retrieval`) | Hidden Tests (`strict_blind`), Turn 1 (`Blind`) | Hidden Tests (`strict_blind`), Turn 2 (`FB + Retr`) | Pure `hide_tests` Effect (`Hid T1 − Vis T1`) | Second-Turn Effect (`Hid T2 − Hid T1`) |
|---|---:|---:|---:|---:|---:|---:|
| `meta/muse-spark-open` | `6/16` (37.5%) | `7/16` (43.8%) | `6/16` (37.5%) | `14/16` (87.5%) | **`+0` tasks (+0.0 pp)** | **`+8` tasks (+50.0 pp)** |
| `meta/muse-spark-1.3-contributor` | `6/16` (37.5%) | `7/16` (43.8%) | `6/16` (37.5%) | `14/16` (87.5%) | **`+0` tasks (+0.0 pp)** | **`+8` tasks (+50.0 pp)** |
| `meta/muse-wonder-1.0-contributor` | `6/16` (37.5%) | `6/16` (37.5%) | `9/16` (56.2%) | `13/16` (81.2%) | **`+3` tasks (+18.8 pp)** | **`+4` tasks (+25.0 pp)** |
| `openrouter/deepseek/deepseek-v4.1-flash` | `7/16` (43.8%) | `4/16` (25.0%) | `8/16` (50.0%) | `10/16` (62.5%; Cum `13/16`) | **`+1` task (+6.2 pp)** | **`+2` tasks (+5 Cumul)** |
| `kimi-code/k3.1-pro-highspeed` | `5/16` (31.2%) | `7/16` (43.8%) | `6/16` (37.5%) | `10/16` (62.5%) | **`+1` task (+6.2 pp)** | **`+4` tasks (+25.0 pp)** |
| `anthropic/claude-sonnet-5-5` | `4/16` (25.0%) | `4/16` (25.0%) | `4/16` (25.0%) | `12/16` (75.0%) | **`+0` tasks (+0.0 pp)** | **`+8` tasks (+50.0 pp)** |

### Table 2B — 16-Task Strictly Cross-Key-Clean Cohort (`8` MiMo DEV + `8` TB4 DEV = `384` Cells): True Executed Paired-Grader Differential (`1.0 -> 0.0`)

| Model Route | Visible Tests, Turn 1 (`Bare`) | Visible Tests, Turn 2 (`Retrieval`) | Hidden Tests (`strict_blind`), Turn 1 (`Blind`) | Hidden Tests (`strict_blind`), Turn 2 (`FB + Retr`) | Pure `hide_tests` Effect (`Hid T1 − Vis T1`) | Second-Turn Effect (`Hid T2 − Hid T1`) |
|---|---:|---:|---:|---:|---:|---:|
| `meta/muse-spark-open` | `7/16` (43.8%) | `12/16` (75.0%) | `4/16` (25.0%) | `11/16` (68.8%) | **`-3` tasks (-18.8 pp)** | **`+7` tasks (+43.8 pp)** |
| `meta/muse-spark-1.3-contributor` | `7/16` (43.8%) | `9/16` (56.2%) | `4/16` (25.0%) | `12/16` (75.0%) | **`-3` tasks (-18.8 pp)** | **`+8` tasks (+50.0 pp)** |
| `meta/muse-wonder-1.0-contributor` | `6/16` (37.5%) | `8/16` (50.0%) | `4/16` (25.0%) | `12/16` (75.0%) | **`-2` tasks (-12.5 pp)** | **`+8` tasks (+50.0 pp)** |
| `openrouter/deepseek/deepseek-v4.1-flash` | `9/16` (56.2%) | `5/16` (31.2%) | `4/16` (25.0%) | `9/16` (56.2%; Cum `11/16`) | **`-5` tasks (-31.2 pp)** | **`+5` tasks (+7 Cumul)** |
| `kimi-code/k3.1-pro-highspeed` | `6/16` (37.5%) | `10/16` (62.5%) | `2/16` (12.5%) | `9/16` (56.2%) | **`-4` tasks (-25.0 pp)** | **`+7` tasks (+43.8 pp)** |
| `anthropic/claude-sonnet-5-5` | `4/16` (25.0%) | `5/16` (31.2%) | `1/16` (6.2%) | `10/16` (62.5%) | **`-3` tasks (-18.8 pp)** | **`+9` tasks (+56.2 pp)** |

- **Causal Decomposition:** On executed paired graders (`1.0 -> 0.0`), hiding `tests/` on Turn 1 (`Hid T1 − Vis T1`) **reduces** single-shot differential discovery by `-2` to `-5` tasks (`-12.5` to `-31.2` pp) because models cannot see the test runner on Turn 1; **100% of the two-turn gain comes from Turn 2 (`Runner-Accurate Failure Feedback + CloudTraceRetriever`)**, which adds **`+5` to `+9` tasks (`+31.2` to `+56.2` pp)** on the 16-task cohort and **`+7` to `+21` tasks (`+23.3` to `+70.0` pp)** across all `30` strictly cross-key-clean DEV tasks (`22` MiMo DEV + `8` TB4 DEV).

## 3. TS-5 Part (2): Strictly DEV-Everywhere `breaker-bench-v1` (Terminal Wrench) Audit & 2-Turn Executed-Differential Seeker (`0.2.0` vs `0.2.2`)
- Across all `19` `assignments.csv` keys (`item_id`, `task`, `unit`), **`29 / 214` pairs in `breaker-bench-v1/pairs.jsonl`** (`4` `terminal-bench-2`, `4` `skillsbench`, `21` `swebench-verified`) and **`25 / 192` `terminal_wrench_holdout` items** are strictly DEV-everywhere (`0` `test`/`heldout` in any `key_id`), whereas `17 / 21` `terminal-bench-2` DEV pairs in `breaker-bench-v1` are `test`/`heldout` in `tb2-to-tb21-v1` or `delphik-open-defect-tb2`.
- All **`4` strictly DEV-everywhere `terminal-bench-2` pairs** (`merge-diff-arc-agi-task`, `sparql-university`, `vulnerable-secret`, `write-compressor`) have local `0.2.0` (`tb20`) and `0.2.2` (`tb21`) Harbor packages and were evaluated with our 2-turn strict-blind executed-differential seeker:

| Model Route | Turn 1 Strict-Blind (`0.2.0 -> 0.2.2` Executed) | Turn 2 FB + Retrieval (`0.2.0 -> 0.2.2` Executed) | Cumul `Pass@2` (`4` Breaker DEV) | Combined `34`-Task Strictly DEV-Everywhere Executed (`T1 -> T2`, `M22 / T8 / B4`) |
|---|---:|---:|---:|---:|
| `meta/muse-wonder-1.0-contributor` | `0/4` (0.0%) | **4/4 (100.0%)** | **4/4 (100.0%)** | `4/34 (11.8%)` $\to$ **29/34 (85.3%)** (`20/22` / `5/8` / `4/4`) |
| `meta/muse-spark-1.3-contributor` | `0/4` (0.0%) | **4/4 (100.0%)** | **4/4 (100.0%)** | `5/34 (14.7%)` $\to$ **23/34 (67.6%)** (`13/22` / `6/8` / `4/4`) |
| `kimi-code/k3.1-pro-highspeed` | **2/4 (50.0%)** | **4/4 (100.0%)** | **4/4 (100.0%)** | `4/34 (11.8%)` $\to$ **20/34 (58.8%)** (`12/22` / `4/8` / `4/4`) |
| `anthropic/claude-sonnet-5-5` | `0/4` (0.0%) | **2/4 (50.0%)** | **2/4 (50.0%)** | `1/34 (2.9%)` $\to$ **20/34 (58.8%)** (`13/22` / `5/8` / `2/4`) |
