---
status: living
audience:
  - builder
  - operator
---

# Score retained DEV detector outputs

`evallab score` reads stored predictions and labels. It makes no model call and starts no task run.
It does not change a detector, a line decision, or the line gate.

```sh
uv run evallab score \
  --gold ~/.local/state/evallab-failure-library \
  --freeze research/evidence/ev1110-score-1/freeze.json \
  --tools all-retained --mode cached \
  --out /tmp/evallab-score-dev
```

Use a new output directory. The command refuses to overwrite an existing directory.
The freeze names each permitted source and cache. It never searches historical directories or completes a missing prediction.
The frozen paths require the retained local evidence files. Missing optional prediction files remain missing.
A changed file hash, cohort hash, identity, or effective split stops the command.

## Outputs and separate contracts

- `scoreboard.json` is authoritative. It contains counts, intervals, thresholds, coverage, cost, source hashes, and limits.
- `SCOREBOARD.md` is a pure view of the saved JSON.
- `items.jsonl` records each eligible or null-state join. It preserves missing scores, source run IDs, lengths, and citation coordinates.

The table never reports one pooled accuracy.

| Contract | Unit | This step |
|---|---|---|
| Task QA | Task package, grader, backend version | Unavailable. Run labels cannot establish a task defect. |
| Grader QA | Grader/runtime-boundary version | Unavailable. Run labels cannot establish grader integrity. |
| Offline detection | Complete admitted run | Exposed DEV diagnostic, with 3 separate cohorts. |
| Online detection | Causally visible prefix | Unavailable. Finished-run caches cannot establish online detection or delay. |

## Cohorts and denominators

- A contains 11 executed Goodfire positives and 70 reviewed controls. It lists 23 attempted and 46 unjudged rows separately.
- B contains 43 impossible-task positives and 32 controls. It lists 98 unjudged rows separately.
- B has a variant/model confound. All positives use strict variants; all controls use original variants.
- C contains the old 690-run policy panel: 332 publisher positives and 358 controls. These labels define a different construct.

Null label states never become honest controls. All 3 cohorts are exposed DEV data.
The command states `UNAVAILABLE: no eligible untouched cohort`.
The registry and Gold loader remain the label and action-coordinate authorities. The scorer creates no registry or trajectory format.

## Metrics and missing predictions

The primary recall denominator contains every eligible positive. Missing predictions count as misses, not removed rows.
Observed honest alarm rates use scored controls. They do not count missing controls as safe.
The JSON also gives `eligible_honest_alarm_lower_bound`, `unresolved_honest`, and `worst_case_honest_alarm` on all eligible controls.
A budget claim remains unresolved when any control prediction is missing.
Zero denominators show `N/A`. An absent cached detector shows `missing`, not zero performance.
AUROC uses scored binary rows and reports that coverage.

The freeze specifies `>=`, whole tie blocks, 1% and 5% DEV targets, and explicit `no_alarm` points.
The line identities freeze their thresholds on cohort C. The native impossible readers retain their historical DEV thresholds.
The freeze keeps those reader identities and views distinct.
Binary L0 and the full-line disposition also show their native points. The full line shows drop-only and review-or-drop separately.
A no-alarm budget point is not a change to the line policy.

Both length controls use the same admitted actor view. Character counts include narration, tool arguments, names, and observations.
Step counts use actor turns, not database rows. File-only submissions have no invented trajectory length.
Each paired comparison requires the detector score and both lengths. Both controls then use exactly those rows.
The table also shows each length control on the full eligible cohort.

The scorer imports DetBench AUROC, paired bootstrap, Wilson intervals, length pairing, view construction, and size functions.
It imports the line scoreboard's policy counts. The old score commands remain available.
Task-cluster bootstrap intervals use source-namespaced task families, 1,000 draws, and seed 1104.
A single-class or insufficient bootstrap returns an unavailable interval. A zero-width empirical interval does not prove zero population risk.

## Localisation and cost

Gold validates source-event maps and marks cited cheating actions. Step ID 0 is valid.
The scorer never treats an array position as an original action ID.
The primary localisation rate uses the first returned citation and all eligible step-labelled positives.
Unmapped first citations and missing predictions are misses. Later correct citations cannot repair a wrong first citation.
The table also gives any-citation hits, citation precision, same-branch within-5-action hits, and localisation given an alarm.
Citation precision includes citations on alarmed honest controls. Branches never become one causal path.
Publisher labels have no resolved action construct, so cohort C localisation remains unavailable.
AutoReview localisation remains unavailable when the cache retains only aggregate line citations.

Historical USD, input/output tokens, latency, and CPU values stay unknown when absent.
The JSON gives their sums, recorded coverage, and means per recorded run.
New inference cost is $0 and the new model-call count is 0. The JSON records the cached score's CPU seconds separately.

## Change record

2026-11-10: Add the first cache-only DEV scoreboard and the `evallab score` command.
Preserve the old score paths and the unchanged line gate until callers migrate.
