# HAR-109 item-5 scoring map (frozen before the Docent analysis runs)

Compares the item-5 Docent reading outputs against Peter's frozen hand labels
for the 10 HAR-104 Python runs. Readings shortlist; hand labels decide.

## Reading output fields (see `har109_docent_analysis.py`)

- `first_failure_window` {start_step, end_step, evidence}: step window only.
- `approach`: free text, not scored.
- `task_fair` boolean + `fairness_evidence`: task-fairness extraction, not scored.
- `attribution` (model | harness | task_or_grader | unclear) + evidence: scored.

## Agreement rules

1. **Attribution agreement**: exact match on the four-way `attribution`
   against the hand label. Report raw agreement + per-class precision/recall.
   Known prior (HAR-81 blind): judges blame the model on every failure, so
   expect high recall / weak precision on model; sample all non-model
   predictions by hand.
2. **First-failure step window**: a hit iff the hand-labelled first-failure
   step lies inside [`start_step`, `end_step`]. Never score exact-step
   matches (Who&When prior: ~14% exact-step accuracy; judges cite early
   trivia on half the comparable runs). Report window-hit rate + median
   window width; sample misses.
3. **Fairness / approach**: extraction only. Report the distribution; do not
   score against hand labels (no hand field exists for these).
4. **Deterministic first**: the deterministic first-failure rule and the
   Eval Lab `report run` stop reason outrank any reading on conflicts.

## Freeze

sha256 recorded in `HAR109_PLAN.md`. Any prompt/schema change after freezing
invalidates the freeze: update the hash and re-record it before running.
