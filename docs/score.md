---
status: living
audience: [builder, operator]
---

# Frozen DEV scoring

`evallab score` runs deterministic local rules and reads stored model outputs. It performs no inference or task execution.
It requires an explicit freeze. Missing predictions remain missing. Gold owns labels and action coordinates.
The published scoreboard is `~/Developer/eval-lab-results/score/current/scoreboard.json`.
Its compact view is [SCOREBOARD.md](../research/evidence/ev1110-score-1/SCOREBOARD.md).

## Reproduce the published scoreboard

Run this command from current main. The output directory must not exist.

```sh
uv run evallab score --freeze ~/Developer/eval-lab-results/score/current/freeze.json --out ~/Developer/eval-lab-results/score/current/recheck
```

The published directory contains the freeze, `scoreboard.json`, `SCOREBOARD.md`, `items.jsonl`, `qa-links.jsonl`, and `summary.json`.
The compact summary records SHA-256 hashes and paths for the full receipts.
Only the compact Markdown view and summary belong in Git. Full receipts remain in the results home.
`EVALLAB_RESULTS_HOME` changes the results home. Explicit paths take precedence.
The freeze pins sources, cache identities, thresholds, and cohorts. Changed hashes stop scoring.

## Refresh code pins

After a merge changes pinned code, use the existing code-pin-only refresh procedure below.
It creates a new freeze. It never changes data, labels, cohorts, thresholds, or historical freezes.
Run it from the repository root. Choose a new output filename if this one already exists.
Review the source change before accepting its new code hashes. Refreshing pins does not prove unchanged metrics.

```sh
uv run python -c '
import json
from pathlib import Path
from evallab.score.inputs import digest, source
p = Path.home() / "Developer/eval-lab-results/score"
f = json.loads((p / "current/freeze.json").read_text())
for r in f["code_sources"]:
    q = Path(r["path"])
    if not q.is_absolute() and q.suffix == ".py":
        r["sha256"] = digest(Path.cwd() / q)
    else:
        source(r, Path.cwd())
with (p / "current-refreshed-freeze.json").open("x") as out:
    out.write(json.dumps(f, indent=2, sort_keys=True) + "\n")
'
```

Use the new freeze with `evallab score --freeze` and a new `--out` directory.
Never refresh a data hash to bypass a refusal. Preserve and investigate changed input bytes.

## Measurement boundaries

Action protocols remain separate. Cohorts A and B retain their action-v1 labels; C retains 690 publisher-labelled DEV runs.
Each action-v2 source has its own panel, including attempted and unjudged counts. The same run can occur under both protocols.
The freeze includes Terminal Wrench, CheatBench, C1-C5, and the all-null Goodfire action-v2 panel.
Source panels use stored outputs and unchanged thresholds. No binary label follows from attempted or unjudged states.
Rows marked `labelled-only, unscored` never enter scoring without a named unlock.

Gold exclusions live in `src/evallab/gold/exclusions.tsv`. IssueBank and the public Gold loader enforce them.
`gold.load_trajectory` refuses excluded sources before trajectory access, including calls with legacy rows.
The separate 690-run DEV policy regression pool does not change.

QA uses pinned task/grader receipts with exact subject and runtime identities.
Its action-v1 Gold joins stay frozen when additional offline source panels appear.
Strict/original pairs, passed-action versions, selected original graders, and certificates remain separate.
Publisher bundles without grader digests have a source-scoped panel. Missing rewards never imply a pass.
Gold gates and mutation audit reuse label evidence; agreement is not independent accuracy.
The external `qa-links.jsonl` records action-v1 executed-positive run links and task/grader availability.
Submission diagnostics use submission denominators, never grader denominators.
`qa.findings` requires an explicit DEV binding, exact task digest, and matching runtime boundary.
Absent bindings remain missing. Scoring never starts QA tools or inspects protected row payloads.

Online scoring replays causal prefixes through native rules. Models use exact-prefix caches only, without an inference callable.
The 3 model variants have no stored outputs on Gold prefixes. Missing coverage is not a negative prediction.
All panels use exposed DEV evidence. No untouched test cohort exists; every Goodfire DEV run has stored detector outputs.

## Retained research rules

Use the task as the evidence unit. Repeated attempts are not independent tasks.
State the population, missing outcomes, source identity, setup, and uncertainty.
Keep measured verifier outcomes separate from harness exits and capability eligibility.
Quote frozen preregistrations without changing decision rules after results.
The card generator and LADDER frontend are retired. Preserve their historical receipts; use native approved specs for new execution.

2026-11-11: Publish the combined current scoreboard. Keep hash guards, separate protocols, frozen QA populations, and operational limits.
