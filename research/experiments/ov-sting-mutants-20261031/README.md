# ov-sting-mutants-20261031 — reference-patch semantic mutants vs. MiMo graders

STING-style weak-test discovery: apply the DEV reference patch, introduce one semantic mutation on a
line the patch *added*, and run the task's own grader on Modal. A mutant that still scores 1.0 is a
container-executed proof that the hidden tests do not pin that behaviour. Implementation:
`src/evallab/mutant_auditor.py` (`evallab audit-mutants`), driver `run_audit.py` here.

Prereg (cohort, operators, gates) was written before execution:
`research-context/bridge/2026-10-31-overnight/fable/raw/W2-STING-MUTANTS/prereg.md`.

## Cohort

18 strictly-DEV `mimo-defect-map-v1` tasks (`CorpusRegistry`: dev split, no TEST assignment
anywhere), sha256(task_id) order, no backfill: 12 of the 30 `task_quality.repair_qualified` tasks
(`mimo-task-quality-30-20261024`; their repaired snapshots are the "repaired grader") and 6 tasks
outside the 30 from `mimo-v6-full-fleet/references.csv` (`results/selection.json`). Two inside-30 tasks
were skipped before sampling because their reference patch adds no Python line (TypeScript tasks).

## Cells (96 Modal sandboxes, est. $0.217, 1,719 sandbox-seconds, median 9.4 s per cell)

Per task: 1 probe (reference patch only = control, dumps post-patch sources), up to 3 mutants on the
original grader, and the same mutants on the repaired grader for the 12 qualified tasks. Operators
(deterministic AST, added lines only, `ast.parse` + AST-difference checked): `boundary_flip`,
`equality_flip`, `guard_drop`, `handler_drop`, `call_drop`, `keyword_drop`, `bool_flip`, `int_shift`.
14/18 tasks yielded 3 mutants, 2 yielded 2, 2 yielded 1 (48 mutant cells on original, 30 on repaired).

## Results (2026-11-01)

| question | number | 95% Wilson |
|---|---|---|
| reference control passes original grader | 17/18 | 74.2–99.0% |
| tasks with ≥1 surviving mutant on the **original** grader (control = 1.0) | **12/18 = 66.7%** | 43.7–83.7% |
| … inside-30 / outside-30 | 8/12 / 4/6 | — |
| mutant-level survival, original grader | 18/48 = 37.5% | 25.2–51.6% |
| **repaired** graders (12) accepting ≥1 of the same mutants | **4/12 = 33.3%** | 13.8–60.9% |
| … among the 8 repaired tasks whose original was weak | 4/8 | 21.5–78.5% |
| mutant-level survival, repaired grader | 4/30 = 13.3% | 5.3–29.7% |

Discovery cost (probes + original-grader mutant cells): $0.140 and 844 sandbox-seconds for 12 weak
graders = **$0.012 and 70 s per discovered weak grader**. Repaired-grader audit: $0.077 / 468 s.

Both pre-registered gates are met: discovery ≥ 6/18 at ≤ $0.10 per defect; repairs "witness-only"
threshold ≥ 4/12 reached exactly. Every repaired grader that still accepts a mutant accepts one the
original also accepted; the repairs killed 14 of the 18 original survivors and introduced no new ones.

`format-code-task-002212`: the reference patch itself scores 0.0 on the original grader (control
failed); its 3 mutants also scored 0.0 and it is counted as "no survivor" per prereg.

## Receipts

`results/selection.json`, `results/audit-summary.json` (per-cell rewards, applied markers, wall
seconds, cost estimates, evidence sha256), `results/cells.csv`, `results/survivor-diffs/` (the 18
original-grader survivors). Full trial directories (agent log, verifier output) live outside the repo
at `~/Developer/eval-lab-results/2026-11-01/w2-sting-mutants/tasks/<task>/<cell>/trial/`. Spend rows:
`~/.local/state/evallab-failure-library/spend.jsonl` (`job: w2-sting-mutants:*`, route modal); spend
gate ledger `w2-sting-mutants-ledger.json` (approval "Peter 2026-10-31 overnight factory
W2-STING-MUTANTS: Modal cap $1.75").

## Limits

Mutant equivalence is checked at the AST level only; `handler_drop` and `call_drop` survivors on
logging/tolerance paths prove an untested error path, which a task author may consider out of scope.
No LLM-targeted mutant was used (deterministic operators only). Rewards are single executions per
cell (no flake repeat); the control row and the applied marker guard each counted survivor.
