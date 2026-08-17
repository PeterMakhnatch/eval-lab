<!-- generated-by: lessons v1 -->
# Statistical Lessons & Aggregation Views

- **Generated at:** 2026-08-17 14:09:54Z
- **Statistical Gating:** Power threshold $n \ge 5$, Wilson 95% confidence interval
- **Corpus Summary:** 551 craft tasks, 92 trials, 25 observation records, 3 analysis sidecars
- **Findings Gate:** 40 statistically powered finding(s), 4 observation row(s) gated with `insufficient n`

---

## 1. Outcome by Verifier Type (`v_outcome_by_verifier_type`)

Cross-tabulation of task verifier architecture against trial pass rates, exceptions, duration, and cost.

| Source Repo | Verifier Type | n | Passed | Pass Rate | Wilson 95% CI | Exceptions | Exception Rate | Status | Finding |
|---|---|---:|---:|---:|---|---:|---:|---|---|
| eval-lab/library | golden_file | 67 | 62 | 92.5% | [83.7%, 96.8%] | 3 | 4.5% | `sufficient` | pass_rate=92.5% [95% CI: 83.7%-96.8%, n=67], exceptions=3 |
| eval-lab/library | pytest | 13 | 6 | 46.2% | [23.2%, 70.9%] | 7 | 53.8% | `sufficient` | pass_rate=46.2% [95% CI: 23.2%-70.9%, n=13], exceptions=7 |
| eval-lab/library | hybrid | 12 | 0 | 0.0% | [0.0%, 24.2%] | 6 | 50.0% | `sufficient` | pass_rate=0.0% [95% CI: 0.0%-24.2%, n=12], exceptions=6 |
| terminal-bench/terminal-bench | hybrid | 12 | 0 | 0.0% | [0.0%, 24.2%] | 6 | 50.0% | `sufficient` | pass_rate=0.0% [95% CI: 0.0%-24.2%, n=12], exceptions=6 |

## 2. Loop Rate by Environment Complexity (`v_loop_rate_by_env`)

Analysis of repetitive tool loops vs multi-container and environment complexity.

| Source Repo | Services | Container Mode | Env Files | n | Loops | Loop Rate | Wilson 95% CI | Avg Steps | Avg Tool Errors | Status | Finding |
|---|---:|---|---|---:|---:|---:|---|---:|---:|---|---|
| eval-lab/library | 1 | single | 1_to_5_files | 92 | 0 | 0.0% | [0.0%, 4.0%] | 2.8 | 0.0 | `sufficient` | loop_rate=0.0% [95% CI: 0.0%-4.0%, n=92] |
| terminal-bench/terminal-bench | 1 | single | 1_to_5_files | 12 | 0 | 0.0% | [0.0%, 24.2%] | 10.3 | 0.0 | `sufficient` | loop_rate=0.0% [95% CI: 0.0%-24.2%, n=12] |

## 3. Failure by Craft Facet (`v_failure_by_facet`)

Taxonomy breakdown of agent and infrastructure failures across structural task facets.

| Source Repo | Facet Name | Facet Value | Category | Validity | n | Failures | Failure Rate | Wilson 95% CI | Status | Finding |
|---|---|---|---|---|---:|---:|---:|---|---|---|
| eval-lab/library | base_image_pin | tag | none | passed | 68 | 0 | 0.0% | [0.0%, 5.3%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-5.3%, n=68] |
| eval-lab/library | base_image_pin | tag | exception | harness_failure | 16 | 16 | 100.0% | [80.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 80.6%-100.0%, n=16] |
| eval-lab/library | base_image_pin | tag | unscored_failure | valid_agent_attempt | 8 | 8 | 100.0% | [67.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 67.6%-100.0%, n=8] |
| eval-lab/library | dependency_pinning | unstated | none | passed | 62 | 0 | 0.0% | [0.0%, 5.8%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-5.8%, n=62] |
| eval-lab/library | dependency_pinning | unpinned | exception | harness_failure | 7 | 7 | 100.0% | [64.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 64.6%-100.0%, n=7] |
| eval-lab/library | dependency_pinning | pinned | unscored_failure | valid_agent_attempt | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| eval-lab/library | dependency_pinning | pinned | exception | harness_failure | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| eval-lab/library | dependency_pinning | unpinned | none | passed | 6 | 0 | 0.0% | [0.0%, 39.0%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-39.0%, n=6] |
| eval-lab/library | dependency_pinning | unstated | exception | harness_failure | 3 | 3 | 100.0% | [43.9%, 100.0%] | `insufficient n` | insufficient n |
| eval-lab/library | dependency_pinning | unstated | unscored_failure | valid_agent_attempt | 2 | 2 | 100.0% | [34.2%, 100.0%] | `insufficient n` | insufficient n |
| eval-lab/library | difficulty_mechanism | unclassified | none | passed | 68 | 0 | 0.0% | [0.0%, 5.3%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-5.3%, n=68] |
| eval-lab/library | difficulty_mechanism | unclassified | exception | harness_failure | 16 | 16 | 100.0% | [80.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 80.6%-100.0%, n=16] |
| eval-lab/library | difficulty_mechanism | unclassified | unscored_failure | valid_agent_attempt | 8 | 8 | 100.0% | [67.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 67.6%-100.0%, n=8] |
| eval-lab/library | env_container_mode | single_container | none | passed | 68 | 0 | 0.0% | [0.0%, 5.3%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-5.3%, n=68] |
| eval-lab/library | env_container_mode | single_container | exception | harness_failure | 16 | 16 | 100.0% | [80.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 80.6%-100.0%, n=16] |
| eval-lab/library | env_container_mode | single_container | unscored_failure | valid_agent_attempt | 8 | 8 | 100.0% | [67.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 67.6%-100.0%, n=8] |
| eval-lab/library | instruction_style | unclassified | none | passed | 68 | 0 | 0.0% | [0.0%, 5.3%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-5.3%, n=68] |
| eval-lab/library | instruction_style | unclassified | exception | harness_failure | 16 | 16 | 100.0% | [80.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 80.6%-100.0%, n=16] |
| eval-lab/library | instruction_style | unclassified | unscored_failure | valid_agent_attempt | 8 | 8 | 100.0% | [67.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 67.6%-100.0%, n=8] |
| eval-lab/library | verifier_type | golden_file | none | passed | 62 | 0 | 0.0% | [0.0%, 5.8%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-5.8%, n=62] |
| eval-lab/library | verifier_type | pytest | exception | harness_failure | 7 | 7 | 100.0% | [64.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 64.6%-100.0%, n=7] |
| eval-lab/library | verifier_type | hybrid | unscored_failure | valid_agent_attempt | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| eval-lab/library | verifier_type | hybrid | exception | harness_failure | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| eval-lab/library | verifier_type | pytest | none | passed | 6 | 0 | 0.0% | [0.0%, 39.0%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-39.0%, n=6] |
| eval-lab/library | verifier_type | golden_file | exception | harness_failure | 3 | 3 | 100.0% | [43.9%, 100.0%] | `insufficient n` | insufficient n |
| eval-lab/library | verifier_type | golden_file | unscored_failure | valid_agent_attempt | 2 | 2 | 100.0% | [34.2%, 100.0%] | `insufficient n` | insufficient n |
| terminal-bench/terminal-bench | base_image_pin | tag | unscored_failure | valid_agent_attempt | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| terminal-bench/terminal-bench | base_image_pin | tag | exception | harness_failure | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| terminal-bench/terminal-bench | dependency_pinning | pinned | unscored_failure | valid_agent_attempt | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| terminal-bench/terminal-bench | dependency_pinning | pinned | exception | harness_failure | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| terminal-bench/terminal-bench | difficulty_mechanism | unclassified | exception | harness_failure | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| terminal-bench/terminal-bench | difficulty_mechanism | unclassified | unscored_failure | valid_agent_attempt | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| terminal-bench/terminal-bench | env_container_mode | single_container | unscored_failure | valid_agent_attempt | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| terminal-bench/terminal-bench | env_container_mode | single_container | exception | harness_failure | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| terminal-bench/terminal-bench | instruction_style | unclassified | unscored_failure | valid_agent_attempt | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| terminal-bench/terminal-bench | instruction_style | unclassified | exception | harness_failure | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| terminal-bench/terminal-bench | verifier_type | hybrid | unscored_failure | valid_agent_attempt | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |
| terminal-bench/terminal-bench | verifier_type | hybrid | exception | harness_failure | 6 | 6 | 100.0% | [61.0%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 61.0%-100.0%, n=6] |

## Statistical Gating Rules

1. **Sample Size Floor ($n \ge 5$):** Rows with sample count $n < 5$ carry status `insufficient n` and render findings as `insufficient n`. They are preserved for evidence tracking but never reported as generalized findings.
2. **Confidence Intervals:** Every proportion is bounded by a two-sided 95% Wilson score interval with continuity correction.
3. **Deterministic Regeneration:** This file is generated by `evallab.lessons`; hand-edits are prohibited.
