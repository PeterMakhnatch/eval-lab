# LADDER: Evaluation Grid Generator

`evallab.ladder` generates Cartesian evaluation grids for systematic agent elicitation and comparison (WS-E item 3, `docs/build-plan.md`).

It expands a declared grid specification ($tasks \times agents \times preambles \times k\text{ attempts}$) into validated, purpose-tagged `ExperimentSpec` files ready for submission to the evaluation queue (`queue/proposed/`), while strictly respecting per-provider subscription quotas and batch ceilings.

---

## 1. Concepts & Architecture

An evaluation ladder systematically measures agent behavior across four experimental axes while holding task and verifier constant:
1. **Tasks ($T$):** Registered benchmark or canary tasks (e.g. `tasks/event-summary`, `canary/transaction-reconciliation`).
2. **Agents & Models ($A$):** Runnable agent profiles and exact model pins (e.g. `oracle`, `nop`, `codex`, `claude-code`).
3. **Prompt & Preamble Variants ($P$):** Extra instruction preambles (e.g. `none`, `brief-discipline.md`).
4. **Attempt Budgets ($K$):** Trial repetitions for calculating pass@k and Wilson confidence intervals (e.g. $k \in \{1, 3, 5\}$).

```
   ┌────────────────────────────────────────────────────────┐
   │                   Grid Specification                   │
   │  tasks × agents × preambles × k [under quota limits]   │
   └───────────────────────────┬────────────────────────────┘
                               │
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │            LADDER Engine (evallab.ladder)              │
   │  - Quota Headroom Check (evallab.quota)                │
   │  - Batch and Per-Provider Budget Bounds                │
   │  - Slug Sanitization & Deterministic Naming            │
   │  - ExperimentSpec Validation (with required purpose)   │
   └───────────────────────────┬────────────────────────────┘
                               │
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │             Output ExperimentSpec Files                │
   │               queue/proposed/*.json                    │
   └────────────────────────────────────────────────────────┘
```

Every generated `ExperimentSpec` carries a required `purpose` field (`baseline|comparison|elicitation|drift|calibration|craft|practice`), satisfying WS-E Item 1 intent tracking.

---

## 2. Grid Specification Schema

Grid specifications are written in YAML or JSON adhering to `LadderGridSpec`:

```yaml
schema_version: 1
name: grid-event-summary-elicitation
purpose: elicitation
hypothesis_template: "Testing {agent} on {task} with preamble {preamble} at k={k}"

tasks:
  - canary/event-summary
  - task: canary/transaction-reconciliation
    task_path: tasks/transaction-reconciliation

agents:
  - oracle
  - nop
  - codex-gpt-5.6-terra
  - agent: claude-code
    model: anthropic/claude-fable-5

preambles:
  - none
  - research/experiments/preambles/brief-discipline.md

attempts:
  - 1
  - 3

environment: docker
jobs_dir: runs
concurrency: 1
timeout_seconds: 1800
submitted_by: ladder-generator
priority: 100

limits:
  max_specs: 50
  max_trials: 150
  max_cost_usd: 10.0
  per_provider:
    codex:
      max_specs: 10
      max_cost_usd: 4.0
    claude-code:
      max_specs: 10
      max_cost_usd: 4.0

check_quota_headroom: true
```

### Key Fields

| Field | Type | Description | Default |
|---|---|---|---|
| `name` | `str` | Name prefix for generated specs | required |
| `purpose` | `ExperimentPurpose` | Intent classification (`elicitation`, `comparison`, etc.) | `elicitation` |
| `tasks` | `list[str \| TaskSpec]` | List of task IDs or detailed task objects | required |
| `agents` | `list[str \| AgentSpec]` | Agent profiles or specs (resolves builtin profiles) | required |
| `preambles` | `list[str]` | List of preamble names or file paths | `["none"]` |
| `attempts` | `list[int] \| int` | Repetitions ($k$) per cell | `[1]` |
| `limits` | `GridLimits` | Batch limits (global and per-provider) | empty limits |
| `check_quota_headroom`| `bool` | Query `evallab.quota` before expanding paid cells | `true` |
| `hypothesis_template` | `str \| None` | Template string with `{task}`, `{agent}`, `{preamble}`, `{k}` | auto-generated |

---

## 3. Quota and Batch Limits Enforcement

To protect subscription headroom and prevent queue overflow:

1. **Free Local Controls:** `oracle` and `nop` are free controls ($0.00 cost) and never consume subscription quotas.
2. **Observed Headroom Integration:** When `check_quota_headroom` is enabled, LADDER inspects `evallab.quota.load_quota_report()`. If the provider reports rate limit exhaustion or 100% window usage, paid cells for that provider are automatically pruned with recorded reasons.
3. **Global Ceilings:** `limits.max_specs`, `limits.max_trials`, and `limits.max_cost_usd` cap the entire batch.
4. **Per-Provider Limits:** `limits.per_provider.<provider>` restricts individual adapters (e.g. limiting `codex` to $4.00 while allowing `oracle` to run unconstrained).

---

## 4. CLI Usage

### Generate Experiment Specs
```bash
# Generate specs and print human-readable summary
python -m evallab.ladder generate grid_spec.yaml

# Generate and write directly to queue directory
python -m evallab.ladder generate grid_spec.yaml -o queue/proposed

# Generate with JSON output
python -m evallab.ladder generate grid_spec.yaml --json

# Bypass quota headroom checking
python -m evallab.ladder generate grid_spec.yaml --no-quota-check
```

---

## 5. Programmatic API

```python
from pathlib import Path
from evallab.ladder import LadderGridSpec, generate_grid

# Define or load spec
grid = LadderGridSpec(
    name="elicitation-grid",
    purpose="elicitation",
    tasks=["canary/event-summary"],
    agents=["oracle", "codex-gpt-5.6-terra"],
    preambles=["none", "brief-discipline"],
    attempts=[1, 3],
)

# Expand into validated ExperimentSpecs
result = generate_grid(grid, output_dir=Path("queue/proposed"))

print(result.summary())
for spec in result.specs:
    print(f"Generated {spec.name} (purpose={spec.purpose})")
```
