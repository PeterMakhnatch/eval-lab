# PROGRAM.json schema (version 1)

Machine-readable research ledger. Validate with:

```bash
uv run python research/experiments/validate_program.py
```

Completion is never inferred from a filename. `status` must be one of:

`idea` · `designed` · `proposed` · `waiting` · `approved` · `running` ·
`completed` · `analyzed` · `stopped` · `superseded`

Each experiment object **requires** these keys:

| Key | Meaning |
| --- | --- |
| `id` | Stable experiment id (`EXP-…`) |
| `research_question` | What the experiment asks |
| `hypothesis` | Falsifiable claim |
| `primary_variable` | The single declared variable |
| `fixed_elicitation` | Held-fixed agent/model/preamble/tools/k notes |
| `task_cohort` | Task set / cohort definition |
| `agent` | Agent name |
| `model` | Model pin or `null` if adapter default |
| `profile` | Runtime profile (docker, …) |
| `k` | Attempts per task |
| `power_rationale` | Why this n/k is or is not enough |
| `status` | Enum above |
| `references` | Object: `spec`, `queue`, `jobs`, `analysis`, `cards` (lists of paths or empty) |
| `blocker` | Exact blocker or `none` |
| `next_action` | Exact next action |
| `predecessor` | Prior experiment id, discovery id, or `none` |
| `decision_rule` | How to decide from the evidence |
| `stopping_condition` | When to stop |
| `notes` | Citations and class labels (invalid harness vs scored) |

Optional: `n_tasks`.
