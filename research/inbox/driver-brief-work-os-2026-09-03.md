---
source_type: internal
---

# Driver brief: the work OS (board + ledger + cost + scoreboard)

Proposed by wK:p7 (Fable 5.1, Analyst) for Peter to place on the board. One driver pane owns it; Peter approves stage gates only.

## Why
Peter's real question is "which model/harness for which kind of work, at what cost" — a measurement problem. Databricks answered it internally by logging every agent interaction first, then building a benchmark from their own merged PRs with held-out tests (no LLM judge), and found: 3 capability tiers; token price is a poor predictor of task cost; harness choice moves cost 2x at equal quality. We are one person and ~20 panes; the same shape at 1/1000 scale is the ledger, instrumented.

## Non-goals (do not build)
Router. Linear/Plane/amux integration. Harness switch. New benchmark suite. Tab bonfire. Anything that needs a server.

## Stages (each claimable; each has a bar; later stages only if earlier ones leave a real question open)

### S0 — stop the paging (5 min, Peter's "go")
`~/.omp/agent/RULES.md` + `APPEND_SYSTEM.md`: replace "Peer-First Delegation" with: subagents for own work; propose to board backlog; page only for claim rounds (keeper), completions needing Peter, urgency to Peter.
Bar: 24h with zero unsolicited peer pages in `tasks` dashboard.

### S1 — ledger becomes a dataset (one day)
Ledger line schema, strict: `pane | model | harness | lane | task | mark(✓/~/✗) | reviewer | cost_usd | turns | wall_min | head/digest`.
Script `scripts/ledger_cost.py`: pulls cost/turns per claim from OMP session JSONL (`usage.cost` is already recorded per message) keyed by pane + claim window. No manual cost entry ever.
Bar: every DONE since Sep 3 has a cost figure that matches the session file to the cent; `ledger.md` parses with zero malformed lines.

### S2 — scheduled rounds (half a day)
Claim rounds run on a clock (launchd/cron -> page keeper "round"), default every 2h during Peter's day and once when the backlog file changes. Round template unchanged. Keeper posts one summary line per round to the board.
Bar: two consecutive days of rounds with no Peter-initiated round; every open pane either claimed or passed-with-reason each round.

### S3 — scoreboard (one day)
`scripts/scoreboard.py`: ledger -> table lane x model: n, ✓-rate with Wilson 95% (reuse `evallab.cohort.wilson_interval`), median cost per ✓, median turns. Rendered at top of `board.md` under `## ON PETER` (review queue) and `## SCOREBOARD`. Cells with n<5 print `insufficient n`, same rule as lessons.md.
Bar: regenerates byte-identically from the ledger; Peter can answer "who should sit in the architect tab" by reading one table.

### S4 — reviewer lane (process, not code)
Every DONE gets one adversarial review claim (the p7/p9 pattern from Track B: exact head, defect-or-clean) before it reaches Peter. Peter reviews the review. Reviewer mark goes in the ledger too, so reviewers are scored.
Bar: Peter's median review time per DONE drops below 5 min over a week.

### S5 — golden replay (only if S3 leaves a question open)
Every ✓ task with a digest-bound acceptance output becomes a replayable task: seal git history (Databricks guardrail), new model runs it cold, held-out check decides. This is the Databricks method with eval-lab's own tooling (Harbor task shape, verifier, CAS).
Bar: 10 golden tasks replayed on one new model with pass/fail and cost per task; no LLM judge.

## The mixed experiment (runs alongside, one week)
Two lanes push (Peter assigns), two lanes pull (board). Same bars. Compare completion rate, review-pass rate, cost per ✓, and Peter's own minutes. Decide the flip on that, not on vibes.

## Predictions to falsify (Analyst's, on record)
1. Task-spec quality will dominate model choice: well-specified items (exact head, bar, non-goals) get ✓ from every model; vague items produce essays from every model.
2. The scoreboard's first money finding will be tiering, not ranking: most lane work lands on the cheap tier at equal ✓-rate (Databricks found the same).
3. Lane-holder panes will out-claim floaters on their own lane at lower cost — context locality, not talent. Floaters are the clean model measurement; lane-holders are model+context.

## References
- Databricks, "Benchmarking Coding Agents on Databricks' Multi-Million Line Codebase" (Jul 2026): https://www.databricks.com/blog/benchmarking-coding-agents-databricks-multi-million-line-codebase
- Steve Yegge, beads (`bd`): git-backed, dependency-aware issue tracker built for agents to pull from — same shape as our board, heavier.
- Practitioner scale: https://allaboutcoding.ghinda.com/one-week-of-coding-and-reviewing-with-llm-agents/
