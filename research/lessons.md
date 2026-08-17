<!-- generated-by: lessons v1 -->
# Statistical Lessons & Aggregation Views

- **Generated at:** 2026-08-17 04:40:08Z
- **Statistical Gating:** Power threshold $n \ge 5$, Wilson 95% confidence interval
- **Corpus Summary:** 5 craft tasks, 25 trials, 25 observation records, 3 analysis sidecars
- **Findings Gate:** 18 statistically powered finding(s), 14 observation row(s) gated with `insufficient n`

---

## 1. Outcome by Verifier Type (`v_outcome_by_verifier_type`)

Cross-tabulation of task verifier architecture against trial pass rates, exceptions, duration, and cost.

| Source Repo | Verifier Type | n | Passed | Pass Rate | Wilson 95% CI | Exceptions | Exception Rate | Status | Finding |
|---|---|---:|---:|---:|---|---:|---:|---|---|
| local-lab/library | golden_file | 12 | 7 | 58.3% | [32.0%, 80.7%] | 3 | 25.0% | `sufficient` | pass_rate=58.3% [95% CI: 32.0%-80.7%, n=12], exceptions=3 |
| local-lab/library | pytest | 7 | 1 | 14.3% | [2.6%, 51.3%] | 3 | 42.9% | `sufficient` | pass_rate=14.3% [95% CI: 2.6%-51.3%, n=7], exceptions=3 |
| local-lab/library | hybrid | 6 | 0 | 0.0% | [0.0%, 39.0%] | 3 | 50.0% | `sufficient` | pass_rate=0.0% [95% CI: 0.0%-39.0%, n=6], exceptions=3 |

## 2. Loop Rate by Environment Complexity (`v_loop_rate_by_env`)

Analysis of repetitive tool loops vs multi-container and environment complexity.

| Source Repo | Services | Container Mode | Env Files | n | Loops | Loop Rate | Wilson 95% CI | Avg Steps | Avg Tool Errors | Status | Finding |
|---|---:|---|---|---:|---:|---:|---|---:|---:|---|---|
| local-lab/library | 1 | single | 1_to_5_files | 25 | 0 | 0.0% | [0.0%, 13.3%] | 1.2 | 0.0 | `sufficient` | loop_rate=0.0% [95% CI: 0.0%-13.3%, n=25] |

## 3. Failure by Craft Facet (`v_failure_by_facet`)

Taxonomy breakdown of agent and infrastructure failures across structural task facets.

| Source Repo | Facet Name | Facet Value | Category | Validity | n | Failures | Failure Rate | Wilson 95% CI | Status | Finding |
|---|---|---|---|---|---:|---:|---:|---|---|---|
| local-lab/library | base_image_pin | tag | exception | harness_failure | 9 | 9 | 100.0% | [70.1%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 70.1%-100.0%, n=9] |
| local-lab/library | base_image_pin | tag | unscored_failure | valid_agent_attempt | 8 | 8 | 100.0% | [67.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 67.6%-100.0%, n=8] |
| local-lab/library | base_image_pin | tag | none | passed | 8 | 0 | 0.0% | [0.0%, 32.4%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-32.4%, n=8] |
| local-lab/library | dependency_pinning | unstated | none | passed | 7 | 0 | 0.0% | [0.0%, 35.4%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-35.4%, n=7] |
| local-lab/library | dependency_pinning | pinned | unscored_failure | valid_agent_attempt | 3 | 3 | 100.0% | [43.9%, 100.0%] | `insufficient n` | insufficient n |
| local-lab/library | dependency_pinning | pinned | exception | harness_failure | 3 | 3 | 100.0% | [43.9%, 100.0%] | `insufficient n` | insufficient n |
| local-lab/library | dependency_pinning | unpinned | exception | harness_failure | 3 | 3 | 100.0% | [43.9%, 100.0%] | `insufficient n` | insufficient n |
| local-lab/library | dependency_pinning | unpinned | unscored_failure | valid_agent_attempt | 3 | 3 | 100.0% | [43.9%, 100.0%] | `insufficient n` | insufficient n |
| local-lab/library | dependency_pinning | unstated | exception | harness_failure | 3 | 3 | 100.0% | [43.9%, 100.0%] | `insufficient n` | insufficient n |
| local-lab/library | dependency_pinning | unstated | unscored_failure | valid_agent_attempt | 2 | 2 | 100.0% | [34.2%, 100.0%] | `insufficient n` | insufficient n |
| local-lab/library | dependency_pinning | unpinned | none | passed | 1 | 0 | 0.0% | [0.0%, 79.3%] | `insufficient n` | insufficient n |
| local-lab/library | difficulty_mechanism | unclassified | exception | harness_failure | 9 | 9 | 100.0% | [70.1%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 70.1%-100.0%, n=9] |
| local-lab/library | difficulty_mechanism | unclassified | unscored_failure | valid_agent_attempt | 8 | 8 | 100.0% | [67.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 67.6%-100.0%, n=8] |
| local-lab/library | difficulty_mechanism | unclassified | none | passed | 8 | 0 | 0.0% | [0.0%, 32.4%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-32.4%, n=8] |
| local-lab/library | env_container_mode | single_container | exception | harness_failure | 9 | 9 | 100.0% | [70.1%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 70.1%-100.0%, n=9] |
| local-lab/library | env_container_mode | single_container | unscored_failure | valid_agent_attempt | 8 | 8 | 100.0% | [67.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 67.6%-100.0%, n=8] |
| local-lab/library | env_container_mode | single_container | none | passed | 8 | 0 | 0.0% | [0.0%, 32.4%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-32.4%, n=8] |
| local-lab/library | instruction_style | unclassified | exception | harness_failure | 9 | 9 | 100.0% | [70.1%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 70.1%-100.0%, n=9] |
| local-lab/library | instruction_style | unclassified | unscored_failure | valid_agent_attempt | 8 | 8 | 100.0% | [67.6%, 100.0%] | `sufficient` | failure_rate=100.0% [95% CI: 67.6%-100.0%, n=8] |
| local-lab/library | instruction_style | unclassified | none | passed | 8 | 0 | 0.0% | [0.0%, 32.4%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-32.4%, n=8] |
| local-lab/library | verifier_type | golden_file | none | passed | 7 | 0 | 0.0% | [0.0%, 35.4%] | `sufficient` | failure_rate=0.0% [95% CI: 0.0%-35.4%, n=7] |
| local-lab/library | verifier_type | golden_file | exception | harness_failure | 3 | 3 | 100.0% | [43.9%, 100.0%] | `insufficient n` | insufficient n |
| local-lab/library | verifier_type | hybrid | unscored_failure | valid_agent_attempt | 3 | 3 | 100.0% | [43.9%, 100.0%] | `insufficient n` | insufficient n |
| local-lab/library | verifier_type | hybrid | exception | harness_failure | 3 | 3 | 100.0% | [43.9%, 100.0%] | `insufficient n` | insufficient n |
| local-lab/library | verifier_type | pytest | exception | harness_failure | 3 | 3 | 100.0% | [43.9%, 100.0%] | `insufficient n` | insufficient n |
| local-lab/library | verifier_type | pytest | unscored_failure | valid_agent_attempt | 3 | 3 | 100.0% | [43.9%, 100.0%] | `insufficient n` | insufficient n |
| local-lab/library | verifier_type | golden_file | unscored_failure | valid_agent_attempt | 2 | 2 | 100.0% | [34.2%, 100.0%] | `insufficient n` | insufficient n |
| local-lab/library | verifier_type | pytest | none | passed | 1 | 0 | 0.0% | [0.0%, 79.3%] | `insufficient n` | insufficient n |

## Statistical Gating Rules

1. **Sample Size Floor ($n \ge 5$):** Rows with sample count $n < 5$ carry status `insufficient n` and render findings as `insufficient n`. They are preserved for evidence tracking but never reported as generalized findings.
2. **Confidence Intervals:** Every proportion is bounded by a two-sided 95% Wilson score interval with continuity correction.
3. **Deterministic Regeneration:** This file is generated by `evallab.lessons`; hand-edits are prohibited.
