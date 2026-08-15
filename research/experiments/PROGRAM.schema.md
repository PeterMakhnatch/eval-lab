# PROGRAM.json schema (version 2)

Machine-readable research ledger. Validate with:

```bash
uv run python research/experiments/validate_program.py
```

The validator rejects unknown fields at the root, experiment, references,
provenance, and decision-rule levels. Strings must be non-empty, `k`,
`n_tasks`, and `representative_attempts` must be positive integers, and every
path in `references` must exist inside the current repository checkout.

Completion is never inferred from a filename. `status` must be one of:

`idea` · `designed` · `proposed` · `waiting` · `approved` · `running` ·
`completed` · `analyzed` · `stopped` · `superseded`

Each experiment object requires:

| Key | Meaning |
| --- | --- |
| `id` | Stable experiment id beginning `EXP-` |
| `research_question` | The bounded question |
| `hypothesis` | Falsifiable claim, or explicit withdrawal for a stopped design |
| `primary_variable` | Single intended variable, or explicit `none` for a stopped design |
| `fixed_elicitation` | Held-fixed agent/model/preamble/tools/k notes |
| `task_cohort` | Task set / cohort definition |
| `agent` | Agent name |
| `model` | Model pin or `null` if adapter default |
| `profile` | Runtime profile |
| `k` | Intended attempts per task |
| `power_rationale` | Why the n/k design does or does not answer the question |
| `status` | Enum above |
| `references` | Exact object: `spec`, `queue`, `jobs`, `analysis`, `cards` |
| `evidence_provenance` | Exact object: `status`, `basis` |
| `blocker` | Exact blocker or `none` |
| `next_action` | Exact next action |
| `predecessor` | Prior experiment/discovery id or `none` |
| `decision_rule` | Exact object: `declared_k`, `representative_attempts`, `rule` |
| `stopping_condition` | When to stop |
| `notes` | Evidence boundary and class labels |

Optional positive integers: `n_tasks` and `representative_attempts`.
`representative_attempts` is used only when a policy/registration probe has a
different attempt count from the intended scientific `k`. It must match
`decision_rule.representative_attempts`, while `decision_rule.declared_k` must
equal `k`. Referenced spec attempts must contain the decision attempt count.

`evidence_provenance.status` must be one of:

- `reviewed_primary` — raw evidence was directly inspected and a retained
  non-journal analysis record carries its numeric extracts/digests;
- `mixed` — reviewed primary evidence and inherited/design-only state coexist;
- `inherited_unresolved` — execution claims survive only in a journal or removed
  worktree and are not primary evidence here;
- `design_only` — no result is claimed.

`references` contains only retained repository-relative paths. Runtime `runs/`
and `queue/` locations may be described as source locations in retained prose,
but they are not valid references in a clean checkout. Journal-only evidence
cannot be labeled `reviewed_primary`.

Active proposals are rejected if their agent-facing fields tell the agent to
run hidden verifier inputs. Linked analysis Markdown is also checked for the
known invalid inference that a verifier-created iframe/srcdoc wrapper identifies
the vector that bypassed a batched test.
