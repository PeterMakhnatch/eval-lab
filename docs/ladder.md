---
status: living
audience:
  - runner
  - analyst
  - operator
---
# LADDER: Evaluation Grid Generator

`evallab.ladder` generates Cartesian evaluation grids for systematic agent elicitation and comparison (WS-E item 3, `docs/platform-architecture.md` v2 §4).

It expands a declared grid specification ($task\_refs \times agents \times preamble \times k$) minus exclusion constraints into validated, purpose-tagged `ExperimentSpec` files ready for submission to the evaluation queue (`queue/pending`), while strictly respecting per-provider subscription quotas, daily unit budgets, and batch ceilings.

---

## 1. Concepts & Architecture

An evaluation ladder systematically measures agent behavior across four experimental axes while holding task and verifier constant:
1. **Tasks ($T$ / `task_refs`):** Registered benchmark or canary tasks (e.g. `canary/event-summary`, `tasks/event-summary`).
2. **Agents & Models ($A$ / `agents`):** Runnable agent profiles and exact model pins (e.g. `oracle`, `nop`, `codex`, `claude-code`).
3. **Prompt & Preamble Variants ($P$ / `preamble`):** Extra instruction preambles or hashes (e.g. `none`, `brief-discipline.md`).
4. **Attempt Budgets ($K$ / `k`):** Trial repetitions for calculating pass@k and Wilson confidence intervals (e.g. $k \in \{1, 3, 5\}$).

```
   ┌────────────────────────────────────────────────────────┐
   │                   Grid Specification                   │
   │  grids/*.yaml: axes {task_refs, agents, preamble, k}   │
   │  minus constraints, with purpose & daily_budget_units   │
   └───────────────────────────┬────────────────────────────┘
                               │
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │            LADDER Engine (evallab.ladder)              │
   │  - Dedupe & Resume (dedupe key: grid_id + coordinates) │
   │  - Exclusion Constraints Filtering                     │
   │  - Provider Round-Robin Ordering                       │
   │  - Quota Headroom & Daily Budget Units Enforcement     │
   │  - ExperimentSpec Validation (with required purpose)   │
   └───────────────────────────┬────────────────────────────┘
                               │
                               ▼
   ┌────────────────────────────────────────────────────────┐
   │        Output: Missing Points Only / Queue Submit      │
   │           --dry-run (default) | --submit | -o          │
   └────────────────────────────────────────────────────────┘
```

Every generated `ExperimentSpec` carries a required `purpose` field (`baseline|comparison|elicitation|drift|calibration|craft|practice`), satisfying WS-E Item 1 intent tracking.

---

## 2. Grid Specification Schema (`grids/*.yaml`)

Grid specifications are written in YAML or JSON adhering to `GridSpec` (v2 §4):

```yaml
schema_version: 1
grid_id: grid-event-summary-elicitation
purpose: elicitation
axes:
  task_refs:
    - canary/event-summary
    - tasks/event-summary
  agents:
    - oracle
    - nop
    - codex
    - agent: claude-code
      model: anthropic/claude-fable-5
  preamble:
    - none
    - research/experiments/preambles/brief-discipline.md
  k:
    - 1
    - 3
constraints:
  - agent: nop
    k: 3
daily_budget_units: 20
hypothesis_template: "Testing {agent} on {task} with preamble {preamble} at k={k}"
```

### Key Fields

| Field | Type | Description | Default |
|---|---|---|---|
| `grid_id` | `str` | Unique identifier for the grid and deduplication | required (or `name`) |
| `purpose` | `ExperimentPurpose` | Intent classification (`elicitation`, `comparison`, etc.) | **required** |
| `axes` | `GridAxes` | Axes object defining `task_refs`, `agents`, `preamble`, and `k` | required |
| `axes.task_refs` | `list[str \| TaskSpec]` | List of task IDs or task specifications | required |
| `axes.agents` | `list[str \| AgentSpec]` | Agent profiles or specs (resolves builtin profiles) | required |
| `axes.preamble` | `list[str]` | List of preamble names, file paths, or content hashes | `["none"]` |
| `axes.k` | `list[int]` | Repetitions ($k$) per point cell | `[1]` |
| `constraints` | `list[dict]` | Exclusion criteria matching point coordinates | `[]` |
| `daily_budget_units` | `int \| float \| None`| Max units (attempts) permitted in one expansion batch | `None` (unbounded) |
| `check_quota_headroom`| `bool` | Query `evallab.quota` before expanding paid cells | `true` |
| `hypothesis_template` | `str \| None` | Template string with `{task}`, `{agent}`, `{preamble}`, `{k}` | auto-generated |

---

## 3. Deduplication, Naming, and Resumption

Each generated `ExperimentSpec` records its `grid_id` and its exact point coordinates:
- `spec.grid_id`: the declared `grid_id` (e.g. `grid-event-summary-elicitation`).
- `spec.grid_point`: `{"task_ref": "...", "agent": "...", "model": "...", "preamble": "...", "k": N}`.
- `spec.name`: deterministic filename slug carrying the full coordinates (`grid_id-task_slug-agent_slug-preamble_slug-k{attempts}`), with hash-assisted truncation ensuring unique names $\le 80$ characters.

The **dedupe key** is `grid_id + point coordinates` (`(grid_id, task_ref, agent, preamble, k)`).

### Uniqueness and Overwrite Protection

1. **Generation-Time Uniqueness Assertion:** LADDER verifies before writing that the set of generated spec names has the exact same cardinality as candidate points (`len(set(names)) == len(points)`). Duplicate names fail loudly at generation time.
2. **No Overwrites:** When writing to an output directory, existing spec files are never overwritten. A matching point is resumed (counted as deduped); an un-deduped existing file raises `FileExistsError`.
3. **Fixed-Point Convergence:** Three consecutive generation runs on an identical target directory reach a fixed point: Run 1 writes $N$ files, Run 2 writes 0 and dedupes $N$, and Run 3 writes 0 and dedupes $N$.

### Resume Behavior

When `evallab ladder generate` is executed on a partially-run or previously submitted grid:
1. LADDER scans the queue across all states (`proposed`, `pending`, `approved`, `waiting`, `running`, `done`, `failed`) and any configured output directories.
2. Existing points matching the `grid_id` and coordinates are identified.
3. LADDER emits **only the missing points**. Already present points are recorded as `resumed` (`deduped`) and are never duplicated.
---

## 4. Quota-Aware Ordering & Withholding Report

1. **Provider Round-Robin:** Candidate points are grouped by provider (e.g., `oracle`, `codex`, `claude-code`) and interleaved round-robin. This balances execution across providers rather than exhausting one provider first.
2. **Quota Headroom Integration:** When `check_quota_headroom` is enabled, LADDER inspects `evallab.quota.load_quota_report()`. If a paid provider reports quota exhaustion (100% window usage or rate limit reached), paid cells for that provider are withheld. Free local controls (`oracle`, `nop`) are never withheld for quota.
3. **Daily Budget Units:** When `daily_budget_units` is declared, LADDER emits the prefix of candidate points that fits within the unit budget.
4. **Withholding Report:** LADDER never silently truncates a grid. All withheld points are reported with their exact coordinates and withholding reason (e.g., `daily_budget_units limit (20) would be exceeded`, `provider reported quota exhausted in current window`).

---

## 5. CLI Usage

### Default Dry-Run Mode
By default, `evallab ladder generate` runs in **dry-run** mode. It inspects the grid, checks the queue for existing points, computes the expansion, and prints the summary without writing to disk:

```bash
uv run evallab ladder generate grids/event-summary-elicitation.yaml
```

### Submit to Queue
Submit the generated specs directly into the queue:

```bash
uv run evallab ladder generate grids/event-summary-elicitation.yaml --submit
```

### Output to Directory
Write the generated JSON spec files to a specific directory:

```bash
uv run evallab ladder generate grids/event-summary-elicitation.yaml -o queue/proposed
```

### JSON Output & Flags
```bash
# Emit machine-readable JSON summary
uv run evallab ladder generate grids/event-summary-elicitation.yaml --json

# Bypass quota headroom checking
uv run evallab ladder generate grids/event-summary-elicitation.yaml --no-quota-check
```

---

## 6. Programmatic API

```python
from pathlib import Path
from evallab.ladder import GridAxes, GridSpec, generate_grid

grid = GridSpec(
    grid_id="elicitation-grid",
    purpose="elicitation",
    axes=GridAxes(
        task_refs=["canary/event-summary"],
        agents=["oracle", "codex-gpt-5.6-terra"],
        preamble=["none", "brief-discipline"],
        k=[1, 3],
    ),
    constraints=[{"agent": "nop", "k": 3}],
    daily_budget_units=50,
)

# Dry run / inspection
result = generate_grid(grid)
print(result.summary())

# Submit to queue
result_submitted = generate_grid(grid, submit=True)
print(f"Submitted {len(result_submitted.submitted_specs)} specs.")
```
