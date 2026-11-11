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

Action protocols remain separate. Cohorts A and B retain their `action-v1` labels.
Each `action-v2` source gets its own panel, including its attempted and unjudged counts.
The same run can appear under both protocols. Its labels and scores never merge across panels.
The 690-run publisher-label panel C keeps its original membership.
New panels use only stored detector outputs and the existing frozen thresholds.
Missing stored outputs remain `missing`. Each detector retains identical-row character and step comparisons.
Rows marked `labelled-only, unscored` never enter scoring.

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

`evallab.score.metrics` owns the retained Wilson and task-cluster bootstrap implementations.
It also owns the label-independent findings parser.
`IssueBank` owns its writer lock. The Seeker driver owns its receipt checks.
Historical study callers import the native metric primitives directly.
The retained study writers are not the recommended measurement path.

2026-11-10: Add cached DEV scoring. Preserve old score paths and the unchanged line gate.
2026-11-10: Add cached task/grader QA and the executed-action run join. Preserve offline metrics and the line gate.
2026-11-10: Retire factory dashboards and QC report exports. Move native metric and receipt primitives without changing cached measurements.
2026-11-11: Add separate action-v2 source panels without pooling protocols or changing the publisher DEV pool.
