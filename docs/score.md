---
status: living
audience:
  - builder
  - operator
---

# Cached DEV scoring

`evallab score` reads frozen caches. It starts no detector, model call, or task run.
See `evallab score --help` for options and [SCOREBOARD.md](../research/evidence/ev1110-score-1/SCOREBOARD.md) for results and metric limits.

The default output is `~/Developer/eval-lab-results/score/qa-v1/`.
`EVALLAB_RESULTS_HOME` changes the results home. Use `--out` for another new directory.
The command refuses to overwrite existing data.

```sh
uv run evallab score \
  --freeze ~/Developer/eval-lab-results/score/qa-v1/freeze.json \
  --out ~/Developer/eval-lab-results/score/qa-v1/recheck
```

The results directory holds `scoreboard.json`, `freeze.json`, `items.jsonl`, and the Markdown view.
`summary.json` contains aggregate metrics and SHA-256 hashes of the full receipts.
Only the Markdown table and compact summary belong in Git. Full receipts remain in the results home.
The freeze pins local sources, cache identities, thresholds, and cohorts. Changed hashes stop scoring.
Missing predictions stay missing. Gold owns labels and action coordinates.

2026-11-10: Add cached DEV scoring. Preserve old score paths and the unchanged line gate.
