# DQ-1 — importer truncation audit (DEV, descriptive)

Reads the 300 blind DEV run texts exactly as the DT-8/DT-9/DT-10 readers saw them and counts
the truncation the lab's own code inserts.

Reproduce:

```
python3 research/experiments/ds-dq1-truncation-audit-20261030/dq1_audit.py
```

Inputs (outside the repo): `/tmp/dt8/prompts/<blind_id>.txt`, DT-8 and DT-2 readings,
`/tmp/dt9/alerts.json`. Outputs: `receipts/truncation-census.json` (counts only, no run
text), `receipts/reader-effect.json`. Deterministic, no model calls, ~5 s.

Truncation constructs: `[... N characters elided ...]` from `run_atlas.bound()`
(12,000/4,000/2,500 chars; N known) and `…[truncated]` from `cheatbench._cap()`
(8,000/50,000 chars). A bare `…` is agent text and is not a marker.

Headline: 268/300 texts are truncated (2,516 markers, 10,207,737 characters dropped, 17.8% of
steps); agent reasoning is never cut; truncation tracks run length (Spearman 0.61–0.69) and
adds nothing to a length model (Δ AUROC +0.006 / +0.015, intervals span 0). Full report:
`raw/DQ-1/report.md` in the DeepSeek-squad bridge directory.
