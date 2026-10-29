# OQ-7 Atlas-guided audit

**Question:** Do label-free Run Atlas signals prioritize historically keyed DEV tasks for audit better than random ordering, and does the association survive run-matched controls?

## Files and execution order

Run from the repository root with its Python environment and the local DEV database, split assignments, OQ-6 plan, and Atlas snapshot available. The defaults point to the original local paths; inspect them before running. In particular, `labels.py` has a fixed output path and can overwrite existing results.

1. `python research/experiments/oq7-atlas-guided-audit/labels.py` — build `A/labels.jsonl`.
2. `python research/experiments/oq7-atlas-guided-audit/features_facets.py --out "$OUT/B1"` — build facet `features.jsonl` (optional `--db` and `--atlas`).
3. `python research/experiments/oq7-atlas-guided-audit/features_context.py --output "$OUT/B2"` — build `features-context.jsonl` (optional `--db`).
4. `python research/experiments/oq7-atlas-guided-audit/evaluate.py --labels "$LABELS" --features "$OUT/B1/features.jsonl" --context "$OUT/B2/features-context.jsonl" --output "$OUT/C"` — rank and evaluate.
5. `python research/experiments/oq7-atlas-guided-audit/controls.py --labels "$LABELS" --features "$OUT/B1/features.jsonl" --context "$OUT/B2/features-context.jsonl" --output "$OUT/D"` — run matched controls.

Set `OUT` to an output directory and `LABELS` to the labels file from step 1. Each analysis writes `results.json` and a report; the original A–E reports are in `~/Developer/eval-lab-results/2026-10-29/oq7-atlas-guided-audit/`.

## Findings and limitations

C: Among 1,141 tasks, 70 were keyed; the combined ranking's yield@10 was 0.30 versus random expected yield 0.0613. Run count itself yielded 1.00 at 10 in this selected population.

D: Run-matched pooled normalized combined AP was 0.115 versus null 0.078 (nominal p=0.0199), but its effect interval crossed zero; MiMo tests-modified remained nominally predictive while run count also remained strong within coarse bins. No paired evidence establishes superiority to run-count ordering.

Unkeyed does not mean negative: keys are incomplete, and keyed tasks are selection-biased. These are retrospective historical-key retrieval results, not independent prospective defect detection; see `E/report.md` in the original results for the validity analysis.
