---
status: living
audience:
  - analyst
  - operator
---

# VerifierCheck: red/blue verifier audits

`evallab vcheck` rebuilds Tokenless EnvCheck's verifier-audit architecture inside
Eval Lab. It audits benchmark VERIFIERS (graders), not sandboxes or harnesses:
per-task red agents read the grader and construct wrong submissions that the
task's own verifier scores. A survivor — an incorrect submission that still
earns reward — is a candidate grader defect, never a certificate.

## Architecture

- **Orchestrator** (`evallab.vcheck.run_wave`): runs per-task red agents
  sequentially, collects hypotheses, then `synthesize_broadcast` turns
  confirmed patterns into hint strings for the next wave
  ("in this universe, verifiers of family F fail to check C — test whether this
  task's verifier also fails"). Only the orchestrator runs harness controls.
- **Red agent** (`evallab.vcheck_red`): reads the grader, launches hinted
  blue-solver probes, and authors wrong submissions. Budgets: max 30 steps, 25
  grades, 6 probes per task (`RED_BUDGETS`).
- **Blue solver**: the honest-submission channel. Blue probes test whether the
  task is solvable as stated; red never grades through blue's eyes.
- **Adjudicator** (`evallab.vcheck_judge`): blind review of survivors against
  the frozen requirement map; `confirm_hypothesis(hypothesis, verdict, actor)`
  promotes on confirmation. Only a human (`vcheck confirm`) or a re-grade
  closes a hypothesis.
- **Store** (`evallab.vcheck_store`): DuckDB hypothesis/finding tables with a
  hash-chained event log; `export_finding(run_dir, finding, cases)` writes
  `<run_dir>/findings/<slug>/` (`inputs/`, `expected.json`, `REPRODUCE.md`,
  `README.md`) plus a `findings.jsonl` record — the envcheck-findings-compatible
  layout.

Harness-owned controls per task (`run_controls`): baseline (oracle/reference,
expect 1), legit (honest solve where available, else oracle), negative (empty
submission, expect 0). Unless oracle scores 1 and negative scores 0 the task is
`task_broken`, which outranks any hackability claim; red agents can never call
controls directly.

All agents run GLM 5.3 (red may use Flash) at temperature 0.1, max 4096 tokens,
low reasoning effort. Model calls route through `evallab.vcheck_client`
(subscription endpoint first, metered fallback) with per-charge budget
accounting in `<run_dir>/spend.jsonl`.

## CLI

```bash
evallab vcheck run --manifest campaign.json --budget-usd 20 --approve "..."   # plan only
evallab vcheck run --manifest campaign.json --budget-usd 20 --approve "..." --execute [--workers 1]
evallab vcheck confirm H-001 --verdict confirm|reject [--run-dir R] [--json]
evallab vcheck export F-001 [--run-dir R] [--output-dir D]
evallab vcheck status <run-dir> [--json]
```

`vcheck run` refuses without both `--budget-usd` and `--approve` (exit 1); the
approval text is recorded in `vcheck-plan.json`. Without `--execute` it writes
the plan only and spends nothing. With `--json`, stdout is exactly one JSON
document for the admission gate:

```json
{
  "verdict": "plan|executed",
  "run_dir": "runs/.vcheck/pilot-<ulid>",
  "counts": {"tasks": 2, "hypotheses": 0, "survivors": 0},
  "estimated_total_usd": 0.94,
  "survivors": [],
  "hypotheses": []
}
```

`verdict` is `plan` until `--execute` runs the waves (`executed`). `survivors`
lists the incorrect-but-rewarded submissions; `hypotheses` carries the
requirement-map references, defect family/class, status, evidence, and history.

## Cost model

Per-task estimate before any spend (recomputed live in the plan):

```
per_task_usd = 30 steps × (6_000 in-toks × $in + 1_500 out-toks × $out) / 1e6
```

At GLM 5.3 prices ($1.40/$4.40 per Mtok in/out) that is ≈ $0.45/task; Flash
($0.15/$0.50) is ≈ $0.05/task. Blue-solver probes and control replays run
through free local Docker and cost nothing. The plan refuses when the estimate
exceeds `--budget-usd`. Actuals land per model call in `spend.jsonl`
(`{model, endpoint, input_tokens, output_tokens, usd, at}`).

## Limits

- Survivors are **candidates**, not findings: each must be read against the
  frozen requirement map (`instruction.md` equivalent) before quoting.
- **Equivalent mutants** are noise: edits that change no required behaviour
  (unreachable branches, no-op flags) survive without proving anything.
- A clean campaign is **not a certificate**: the red search is bounded (30
  steps, 6 probes) and compound or semantic wrong answers may lie outside it.
- `task_broken` (oracle ≠ 1 or negative ≠ 0) invalidates every number from that
  task path; fix the package before quoting hackability.
