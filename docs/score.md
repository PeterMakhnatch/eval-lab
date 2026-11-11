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

A freeze with `qa` adds task and grader contracts from pinned native receipts.
It keeps strict/original pairs, passed-action versions, selected original graders, and strict certificates separate.
Publisher bundles without grader digests get a separate source-scoped panel. Missing rewards never imply a pass.
Gold gates and mutation audit reuse label evidence; their agreement is not independent accuracy.
The external `qa-links.jsonl` joins every executed-positive Gold run to task/grader availability and cached tool outputs.
DEFECT, integrity controls, references, and alternatives use submission denominators, never grader denominators.
Fresh-alternative rejection is diagnostic unless independent correctness evidence exists.
The JSON includes family-macro rates, clustered intervals, unresolved alarm bounds, coverage, and recorded costs.
`qa.findings` requires an explicit DEV binding, exact task digest, and matching runtime boundary.
An absent binding stays missing. Scoring never starts QA tools or inspects protected row payloads.

2026-11-10: Add cached DEV scoring. Preserve old score paths and the unchanged line gate.
2026-11-10: Add cached task/grader QA and the executed-action run join. Preserve offline metrics and the line gate.
