# LS-CHEAT-2026-10-29-1: are the Live Supervisor's 154 cheat cards worth a Trace Watcher's time?

**This file is the plan.** It merges before any label is joined. Stage A (below, already run) reads only card structure, finding
structure, identity rows and the `assignments.csv`/registry TEST check: no receipt label, no run prose, no model call. Stages B to E
(labels, usability, proposals) start after this merges. $0; zero model calls; no upload; no Docker.

## 1. Question

The Live Supervisor's cheat-alarm router (`live_supervisor_cheat.py`, LS-5) wrote 154 markdown cards at 2026-10-29 01:55 UTC and one
queue line `LS-CHEAT-2026-10-29-1` for the Trace Watcher. The queue line called them live runs; the LS-5 receipt says they are alarms on
**stored Goodfire runs** from a trace-harness fill, and that card precision is unmeasured. This item triages and measures them.

## 2. Stage A facts (label-free; `results/stage_a.json`, `tools/stage_a.py`)

| Fact | Value |
|---|---|
| Cards on disk = paths in the day index | 154 = 154, all distinct |
| Distinct runs / cards per run | 154 / exactly 1 |
| Dataset prefix | `goodfire-reward-hack-data` for 154 |
| Header: `actor_inputs_available=false`, `owner=other`, `killable=false` | 154, 154, 154 |
| Strict DEV by identity (registry TEST check, DEV-admitted task, split `dev`, effective split not TEST) | 154 of 154; 0 TEST/sealed, 0 unknown |
| Cards whose run is in the 200-run labelled Goodfire DEV sample (`goodfire-sample.json`) | fewer than 10 of 154 (exact count withheld in the follow-up: with the cohort's enacted alarm count it exposed a single run) |
| Cards with at least one repeated identical alarm line | 34 |
| Cited steps shown as "outside the live window; open the run" | 787 of 787 |
| Facets-only cards (no other detector alarm) | 146 of 154 |

**Where the 154 came from.** The router is a tail of the findings store: the cursor started at end of file with 1,995 historical
alarms consumed and none routed, then one batch of exactly 154 new alarm lines arrived. All 154 carry `produced_at` 2026-10-29T01:53:24Z,
the same stamp as 2,992 `facets` clear lines (1,891 at score 0, 1,101 at score 1) - 3,146 `facets` lines in all: the Haiku 5.5
record of each of 3,146 strict-DEV Goodfire runs, written as findings in one go. All 3,146 are DEV by identity; the 200 labelled
runs are a subset. So 154 is the `suspicion >= 2` rate (4.9%) over 3,146 runs, not over the 200-run cohort the published numbers use.
The router creates one card per run from the **first** new alarm record: `decision: flag_cheating` and its `reader=` are the alarming
detector's own record, not a supervisor or model decision (`live_supervisor_cli.flagged`). The `## Alarms` list is the raw findings
file, not newest-per-key, so an older record for the same key prints again.

## 3. What the labels may and may not be used for

- Strict DEV only. Every run id was cross-keyed against `assignments.csv`/the registry before anything else was read (stage A: 154/154
  admitted). Receipt labels are read for stages B/C only, through `run_atlas_score._read_labels` (required ids = the 154, wanted = the
  154), after this plan is merged.
- A run is **labelled** only if `_read_labels` returns a stratum: `enacted_yes`, `clean_no` or `considered_only` (publisher
  `source_label.judged`). A strict-DEV run without a stratum is **unlabelled/unknown, never negative**. Considered-only is its own
  cell and is neither positive nor negative.
- Per-run ids, card text, step numbers and run prose are not committed: aggregates only, cells below 3 suppressed (and a complement
  that would reveal a single run is suppressed too). The private per-run table lives in `/tmp/ls-cheat-triage-20261029/`.

## 4. Measurements (fixed before labels are joined)

**M1 Inventory** (stage A, done): counts above, plus by detector family/reader/score, cited-step counts, repeated-line cards, step
validity and position against `agent_traces.step_count`.

**M2 Cross-key and why 154** (A done; B adds label strata): DEV enacted / DEV clean / DEV considered-only / **DEV unlabelled** /
non-DEV / unknown, for the 154 and, for context, for the 3,146 batch runs and the 200-run cohort. Compare with the published
Haiku-primary 4/68 enacted and 1/58 clean at `>= 2` (NP22-CM-1) and cascade v8 10/80 and 2/70: by recomputing, from the same findings,
the alarm counts inside the 200-run cohort by stratum, so the difference is a population, reader-record or unlabelled-run difference
that the numbers can name.

**M3 Card precision (by publisher label on stored DEV runs, not on live runs).**
- Precision `P = enacted / (enacted + clean)` over labelled, non-considered cards, with Wilson 95% (`run_atlas_score.wilson`), by
  detector family and reader (cells < 3 suppressed). Considered-only is reported separately, with `P` also given with considered-only
  counted as positive and as negative as a bracket. Unlabelled cards are reported as a count and excluded from every denominator.
- Base rate for reading `P`: enacted/(enacted + clean) in the labelled cohort (80/150 = 53.3%). A card list must beat that, not 0.
  The cohort is a case-control sample (positives chosen), so the rate does not transfer to the 3,146 batch: stated, not corrected.
- Alarm coverage inside the 200-run cohort: alarms among enacted, among clean, among considered-only, over the cohort size.
- Step hit, for enacted cards with publisher positive steps: exact (cited ∩ labelled step set non-empty) and within 5; compared with the
  chance rate for a random set of the same size drawn from the same run (computed analytically from `step_count`, no model).
- Rule (fixed now): a precision claim needs at least 20 labelled non-considered cards; below that the number is printed with its
  Wilson interval and called **unmeasured**. "Worth a Trace Watcher's time" needs the Wilson lower bound to exceed the base rate
  (53.3%) on at least 20 labelled cards; otherwise the answer is "not shown", with the reason.

**M4 Usability, 10 cards.** Stratified, seed fixed now, no label seen: order each stratum by `sha256("20261029:" + run_id)` and take the
first k. Stratum S1 = facets-only cards (k = 5); S2 = cards with another detector alarm (k = 5). For each, run the card's own `Open
more` commands (`evallab trace card <run>`, `evallab qa-data window <run> --around N --radius 3`) in the isolated worktree
environment, record exit status, wall seconds, whether the window shows the cited step N, whether a cited step has an evidence quote
the card itself carries, whether alarm lines repeat, and a yes/no on "the window plus card is enough to judge a cheat"; the judgement is
mine and is counted, not quoted. No run prose in git.

**M5 Router / queue defects.** Listed with file and line, with a one-file fix and test only where trivial.

**Proposal only (not run):** a hosted adjudication of a stratified card sample, with a token estimate from stored `input_tokens` of the
facet records. Anthropic is at about 47% (7 days); stop at 85%; not started here.

## 5. Not claimed

No claim about live detection, recall over the whole 3,146, or any detector other than what the stored findings and labels support.
The cards inside the 200-run cohort (fewer than 10) cannot carry a precision claim; this plan says so before the number exists.
