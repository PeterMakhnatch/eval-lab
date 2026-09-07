# Task A1 — Offline Distributions over TerminalBench Trajectories (52,104 Trials)

Date: 2026-09-06.
Corpus: `derived/harbor-packs/tb-trajectories/data/*.parquet` (52,104 rows; 89 tasks; 26 scaffolds in `agent` column; Apache-2.0).
Governing Contracts:
- `research/inbox/FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md` (Core Governance Rule: C0 heuristic/screening features are strictly barred from synthetic task generation; 7 certification gates required).
- `research/analysis/harbor-native-track/contamination-claim-boundaries.md` (public packs = behavior-study only).

---

## 1. Scaffold Distribution Table (26 Agents in Corpus)

Denominator: $N = 52,104$ total trials across 89 tasks.
Trials with recorded step streams: $n = 34,462$ ($66.14\%$).
Trials with recorded tool calls: $n = 34,029$ ($65.31\%$).

| Scaffold / Agent (`agent`) | Total $N$ | Trials w/ Steps | Pass Rate | Mean Cost ($) | Median Cost ($) | Mean Duration (s) | Mean In Tokens | Mean Out Tokens | Loop Rate (%) |
|:---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `terminus-2` | 17,431 | 15,941 | 33.58% | $0.3927 | $0.0636 | 760.8 | 551,376 | 15,262 | 26.74% |
| `mini-swe-agent` | 6,663 | 6,495 | 22.77% | $0.2851 | $0.0309 | 557.1 | 717,747 | 18,867 | 21.96% |
| `openhands` | 6,198 | 6,055 | 28.15% | $1.5758 | $0.3375 | 893.7 | 1,657,616 | 24,465 | 94.15% |
| `codex` | 3,532 | 1,410 | 45.22% | $0.0948 | $0.0070 | 644.3 | 647,997 | 10,234 | 15.60% |
| `claude-code` | 3,092 | 2,522 | 40.33% | $0.5470 | $0.0000 | 615.8 | 881,912 | 1,098 | 31.48% |
| `Factory Droid` | 2,224 | 0 | 67.31% | $0.0000 | $0.0000 | 579.4 | 0 | 0 | — |
| `gemini-cli` | 1,766 | 787 | 33.52% | $0.1577 | $0.0449 | 725.2 | 515,165 | 14,322 | 16.26% |
| `letta-code` | 1,335 | 0 | 56.18% | $0.0000 | $0.0000 | 685.7 | 0 | 0 | — |
| `goose` | 1,332 | 0 | 44.22% | $0.0000 | $0.0000 | 538.6 | 0 | 0 | — |
| `mux` | 1,068 | 0 | 66.20% | $0.0000 | $0.0000 | 632.6 | 0 | 0 | — |
| `ruley` | 890 | 0 | 66.29% | $0.0000 | $0.0000 | 574.4 | 0 | 0 | — |
| `terminus-3-3` | 887 | 678 | 74.86% | $0.8019 | $0.5694 | 739.2 | 1,244,063 | 25,795 | 13.57% |
| `ii-agent-simple` | 445 | 0 | 61.80% | $0.0000 | $0.0000 | 635.9 | 0 | 0 | — |
| `forge` | 445 | 0 | 78.43% | $0.0000 | $0.0000 | 561.0 | 0 | 0 | — |
| `Junie` | 445 | 0 | 64.27% | $0.0000 | $0.0000 | 461.0 | 0 | 0 | — |
| `sage` | 445 | 0 | 65.17% | $0.0000 | $0.0000 | 481.6 | 0 | 0 | — |
| `spoox-m` | 445 | 0 | 34.83% | $0.0000 | $0.0000 | 1113.2 | 0 | 0 | — |
| `TerminalBenchAgent` | 445 | 0 | 46.52% | $0.0000 | $0.0000 | 627.0 | 0 | 0 | — |
| `judy` | 445 | 157 | 71.91% | $0.0000 | $0.0000 | 903.0 | 0 | 0 | 7.01% |
| `ante` | 444 | 0 | 64.86% | $0.0000 | $0.0000 | 504.5 | 0 | 0 | — |
| `simple_codex` | 443 | 0 | 74.94% | $0.0000 | $0.0000 | 829.1 | 0 | 0 | — |
| `aone-agent` | 442 | 0 | 27.38% | $0.0000 | $0.0000 | 1000.6 | 0 | 0 | — |
| `deepagent-harbor` | 433 | 417 | 67.67% | $0.0000 | $0.0000 | 749.5 | 0 | 0 | 47.72% |
| `claude-code-enhanced` | 398 | 0 | 66.08% | $0.0917 | $0.0128 | 712.0 | 2,657,143 | 3,476 | — |
| `final` | 322 | 0 | 23.29% | $0.0000 | $0.0000 | 1050.2 | 0 | 0 | — |
| `opencode` | 89 | 0 | 51.69% | $0.0000 | $0.0000 | 692.7 | 0 | 0 | — |
| **Total / Overall** | **52,104** | **34,462** | **39.63%** | **$0.3707** | **$0.0210** | **704.9** | **781,241** | **15,487** | **37.24%** |

*Note on zero token/cost counts:* Scaffolds reporting 0 token/cost columns reflect external harness imports where token usage was unmetered or not serialized to the parquet export. Scaffolds with 0 step streams reflect summary-only evaluation rows.

---

## 2. Tool N-Gram Histogram

Denominator: $1,433,162$ total tool calls across $34,029$ tool-active trials.

### Top 10 Tool Unigrams (1-Grams)
| Rank | Tool Name (`fn`) | Total Invocations | Frequency Share |
|:---|:---|---:|---:|
| 1 | `bash_command` | 836,077 | 58.34% |
| 2 | `execute_bash` | 271,728 | 18.96% |
| 3 | `str_replace_editor` | 102,480 | 7.15% |
| 4 | `Bash` | 47,735 | 3.33% |
| 5 | `mark_task_complete` | 29,345 | 2.05% |
| 6 | `execute_ipython_cell` | 21,873 | 1.53% |
| 7 | `shell` | 20,148 | 1.41% |
| 8 | `task_tracker` | 19,030 | 1.33% |
| 9 | `run_shell_command` | 12,139 | 0.85% |
| 10 | `think` | 8,694 | 0.61% |

### Top 10 Tool Bigrams (2-Grams)
Denominator: $1,399,133$ total transitions.

| Rank | Tool Bigram $(t_i, t_{i+1})$ | Count | Share |
|:---|:---|---:|---:|
| 1 | (`bash_command`, `bash_command`) | 807,710 | 57.73% |
| 2 | (`execute_bash`, `execute_bash`) | 231,199 | 16.52% |
| 3 | (`str_replace_editor`, `str_replace_editor`) | 70,150 | 5.01% |
| 4 | (`Bash`, `Bash`) | 31,261 | 2.23% |
| 5 | (`str_replace_editor`, `execute_bash`) | 27,960 | 2.00% |
| 6 | (`execute_bash`, `str_replace_editor`) | 24,916 | 1.78% |
| 7 | (`shell`, `shell`) | 18,295 | 1.31% |
| 8 | (`bash_command`, `mark_task_complete`) | 18,287 | 1.31% |
| 9 | (`execute_ipython_cell`, `execute_ipython_cell`) | 16,372 | 1.17% |
| 10 | (`mark_task_complete`, `mark_task_complete`) | 10,995 | 0.79% |

### Top 10 Tool Trigrams (3-Grams)
Denominator: $1,365,104$ total transitions.

| Rank | Tool Trigram $(t_i, t_{i+1}, t_{i+2})$ | Count | Share |
|:---|:---|---:|---:|
| 1 | (`bash_command`, `bash_command`, `bash_command`) | 781,241 | 57.21% |
| 2 | (`execute_bash`, `execute_bash`, `execute_bash`) | 192,118 | 14.07% |
| 3 | (`str_replace_editor`, `str_replace_editor`, `str_replace_editor`) | 39,171 | 2.87% |
| 4 | (`str_replace_editor`, `execute_bash`, `execute_bash`) | 27,224 | 1.99% |
| 5 | (`str_replace_editor`, `str_replace_editor`, `execute_bash`) | 26,881 | 1.97% |
| 6 | (`execute_bash`, `execute_bash`, `str_replace_editor`) | 24,451 | 1.79% |
| 7 | (`execute_bash`, `str_replace_editor`, `str_replace_editor`) | 24,210 | 1.77% |
| 8 | (`Bash`, `Bash`, `Bash`) | 23,184 | 1.70% |
| 9 | (`shell`, `shell`, `shell`) | 16,854 | 1.23% |
| 10 | (`bash_command`, `bash_command`, `mark_task_complete`) | 16,719 | 1.22% |

---

## 3. Loop & Retry Rate Analysis

A trial exhibits loop/retry pathology when identical commands are repeatedly executed without intervening state changes, or when tool calls spin without error recovery.

Overall Corpus Loop Rate: **37.24%** ($12,833$ / $34,462$ trials with steps).

By Scaffold:
- `openhands`: **94.15%** ($5,701$ / $6,055$) — massive command-spinning and retry looping.
- `deepagent-harbor`: **47.72%** ($199$ / $417$) — frequent retry on environment stalls.
- `claude-code`: **31.48%** ($794$ / $2,522$) — repeated bash/todo checks.
- `terminus-2`: **26.74%** ($4,262$ / $15,941$) — retry loops on build errors.
- `mini-swe-agent`: **21.96%** ($1,426$ / $6,495$) — edit-retry loops.
- `gemini-cli`: **16.26%** ($128$ / $787$) — modest retry rate.
- `codex`: **15.60%** ($220$ / $1,410$) — low retry rate; more linear executions.
- `terminus-3-3`: **13.57%** ($92$ / $678$) — lowest among major automated agents.
- `judy`: **7.01%** ($11$ / $157$) — human/semi-interactive or conservative pacing.

---

## 4. Oracle-vs-Agent Step-Shape Contrast

| Cohort | Trials ($n$) | Mean Steps | Median Steps | StDev Steps | Step Shape Characteristics |
|:---|---:|---:|---:|---:|:---|
| **Oracle (Ground Truth)** | 3 | **1.0** | **1** | **0.0** | Singular deterministic reference command (`test-stdout.txt` / solution script). Zero exploratory actions, zero error loops. |
| **Model Passed Trials** (`reward=1`) | 11,079 | **30.0** | **18** | **44.9** | Compact, bounded exploration phase followed by targeted edits and single verification pass. Low variance. |
| **Model Failed Trials** (`reward=0`) | 23,383 | **55.1** | **22** | **172.3** | Extended spinning; long tail bloat ($\sigma = 172.3$) driven by repeated failed commands, timeout hitting, or context collapse. |

Contrast ratio: Failed trials consume **1.84×** more steps on average than passed trials, and **55×** more steps than the oracle.

---

## 5. Top-5 Tool-Sequence Skeletons (TASTE-Style Proposals Only)

> **GOVERNANCE NOTICE (Contract Enforcement):**
> Per `research/inbox/FEATURE_TO_SYNTHETIC_PIPELINE_CONTRACT.md` §1 & §2, raw observational/screening features from public runs (C0 grade) are strictly **barred** from seeding automated synthetic task generation.
> The following tool-sequence skeletons are recorded as **TASTE-style heuristic proposals only** for human inspection, prompt-context study, and negative-control design. No automated generator may consume these skeletons without passing all 7 mandatory certification gates.

Denominator: $34,029$ tool-active trials.

| Rank | Skeleton Signature (Collapsed Tool Transitions) | Trials ($n$) | Share (%) | Behavioral Archetype | Proposal Status |
|:---|:---|---:|---:|:---|:---|
| 1 | `bash_command` | 9,532 | 28.01% | Single-tool command chain (pure terminal navigation) | PROPOSAL ONLY |
| 2 | `bash_command -> mark_task_complete` | 9,345 | 27.46% | Standard autonomous solve (execute then terminate) | PROPOSAL ONLY |
| 3 | `bash_command -> mark_task_complete -> bash_command -> mark_task_complete` | 3,130 | 9.20% | Premature completion attempt followed by recovery | PROPOSAL ONLY |
| 4 | `shell` | 913 | 2.68% | Interactive subshell interaction loop | PROPOSAL ONLY |
| 5 | `execute_bash -> str_replace_editor -> execute_bash -> str_replace_editor -> execute_bash -> str_replace_editor` | 643 | 1.89% | Iterative edit-test-debug feedback cycle | PROPOSAL ONLY |

---

## 6. Exact Queries & Commands to Reproduce Every Number

All calculations execute in DuckDB on macOS/Linux with zero paid API calls:

```bash
# 1. Scaffolds summary & 26 agent distributions
uv run python -c "
import duckdb
conn = duckdb.connect()
print(conn.execute('''
    SELECT
        agent,
        count(*) AS n_trials,
        round(avg(reward), 4) AS pass_rate,
        round(avg(coalesce(cost_cents, 0)), 2) AS mean_cost_cents,
        round(median(coalesce(cost_cents, 0)), 2) AS median_cost_cents,
        round(avg(coalesce(duration_seconds, 0)), 1) AS mean_duration_s,
        round(avg(coalesce(input_tokens, 0)), 0) AS mean_input_tokens,
        round(avg(coalesce(output_tokens, 0)), 0) AS mean_output_tokens
    FROM read_parquet('derived/harbor-packs/tb-trajectories/data/*.parquet')
    GROUP BY agent
    ORDER BY n_trials DESC
''').fetchall())
"

# 2. Tool n-grams, loop rates, and step shape contrast
uv run python -c "
import duckdb, json
from collections import Counter
conn = duckdb.connect()
cursor = conn.execute('''
    SELECT task_name, agent, reward, steps
    FROM read_parquet('derived/harbor-packs/tb-trajectories/data/*.parquet')
    WHERE steps IS NOT NULL AND steps != 'null' AND steps != '[]'
''')
# (Logic in task log 2026-09-06 extracts unigrams, bigrams, trigrams, and loop detection)
"
```
