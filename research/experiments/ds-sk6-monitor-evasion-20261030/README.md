# ds-sk6-monitor-evasion-20261030

Red-team of our own monitors (D1 Stage-1 tripwires, D1b content rules, D2 Run Atlas facet
reader) against an adversary told to keep the grader-visible effect but look innocent.

- `sk6_evasion.py` — the driver: attack-set selection, the rewrite/equivalence calls,
  the deterministic comment-strip control, D1/D1b re-scoring, D2 re-run with DT-5b's exact
  prompt path, and retention statistics.
- `summary.json` — counts only (no rewrite text, no variant scripts; those stay in the
  git-ignored receipts and `/tmp`).

Method, numbers and limits: the SK-6 report in the squad bridge directory. This experiment
directory deliberately carries no attempt text: every attack text is benchmark-derived.
