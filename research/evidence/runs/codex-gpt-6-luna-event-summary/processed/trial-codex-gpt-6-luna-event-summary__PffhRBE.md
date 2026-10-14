# Run report: `codex-gpt-6-luna-event-summary__PffhRBE`

## Decision

### Counts

Rendered from the HAR-78 counts field. Not computed here.
Verdict: `counted_pass`.
Reasons: none.
Evidence: none.
Task ledger: `unknown`. No canonical task ledger row; verdict uses legacy labels when present.

### Facts

Observed signals copied from the trial records. Not a reading of why it failed.
- Stop reason: `unknown`.
- Calls: `2`.
- Tokens: input `None`, output `None`; source `proxy_settled_ledger`, attribution `unavailable`; proxy ledger unreadable: provider usage calls are invalid.
- Fetch commands observed: no fetch command observed.
- Verifier: reward `1.0`.
- Nop crash the same way: unknown. No same-task nop crash comparison was recorded.
- What the task asked: # Event stream summary Read the newline-delimited JSON records in `/app/input/events.jsonl` and create `/app/output/summary.json`. The output must be one JSON object with exactly these fields: - `schema_version`: integer `1` - `total_events`: number of input records - `counts`: object mapping each event `kind` to its count, with keys in alphabetical order - `total_duration_ms`: sum of every `dura…
- Grader signal (literal checks only, not a semantic verdict): no named gap; verifier ran 4 tests, 0 failed. A nonzero test count alone never means the grader tests the instruction.

### Judgments

Opinions. Do not treat these as facts.
Calibration: trial_decision.classify_loop_kind (HAR-119 claim-vs-repetition rule); cohort HAR-119 part 2: 12 HAR-110 split-v2 runs (frozen 2026-10-01T00:00:02Z); page loop kind `7/11` vs agreed rater cells (eligible 11, 1 excluded on rater disagreement, 0 abstentions); rater agreement `11/12`; out-of-sample: yes. Method: research/explorations/trace-lab/har119/score_page.py. Out-of-sample for the loop rule. Loop kind: n=11 agreed cells. First failure: n=9 agreed cells with page coverage 1/12 (11 abstentions). Blame 11/11 is uninformative on this cohort: the raters say model on every failure, so a constant prior scores the same. Grader alignment stays opinion.

Additional loop-kind-only calibration, same predictor, separately denominated cohort HAR-128 part 2: 40 HAR-116 trials (frozen 2026-10-01T07:57:30Z): page loop kind `28/35` vs agreed rater cells (35 agreed of 40, 5 excluded on rater disagreement, 0 abstentions); rater agreement `35/40`. Loop kind only on this cohort: no first-failure/blame calibration, denominators never pooled. Method: research/explorations/trace-lab/har119/score_page.py --labels research/explorations/trace-lab/har128/labels_har116 --results-home <eval-lab-results> --output research/experiments/har117-results-home/har131-page-calibration-har116.json. Artifact: research/experiments/har117-results-home/har131-page-calibration-har116.json. Loop kind only: this cohort carries no first-failure/blame calibration. Blind scout-agent raters (L116A1-8/L116B1-8), not human ground truth; rater A on har116-a-000383-baseline__igrXg8R reports off_limits_opened (config.json seen via a broad grep, arm info unused). Denominators stay per-cohort, never pooled. The artifact exports scores and report hashes only, not trial content.

Additional loop-kind-only calibration, same predictor, separately denominated cohort HAR-128 G2 attempt 1: 20 HAR-120 trials (frozen 2026-10-01T09:14:45Z): page loop kind `12/17` vs agreed rater cells (17 agreed of 20, 3 excluded on rater disagreement, 0 abstentions); rater agreement `17/20`. Loop kind only on this cohort: no first-failure/blame calibration, denominators never pooled. Method: research/explorations/trace-lab/har119/score_page.py --published-cohort --cohort har128-g2-a1 --results-home <eval-lab-results> --output research/experiments/har117-results-home/har131-page-calibration-g2-a1.json. Artifact: research/experiments/har117-results-home/har131-page-calibration-g2-a1.json. Loop kind only: this cohort carries no first-failure/blame calibration. Blind scout-agent raters A (G2A1-4) / B (G2B1-4), not human ground truth; no reported off-limits openings. Denominators stay per-cohort, never pooled. The artifact exports scores and report hashes only, not trial content; inspection only, never training reflection.

Additional loop-kind-only calibration, same predictor, separately denominated cohort HAR-128 G2 re-run: 19 HAR-120 trials (frozen 2026-10-01T09:54:19Z): page loop kind `10/16` vs agreed rater cells (16 agreed of 19, 3 excluded on rater disagreement, 0 abstentions); rater agreement `16/19`. Loop kind only on this cohort: no first-failure/blame calibration, denominators never pooled. Method: research/explorations/trace-lab/har119/score_page.py --published-cohort --cohort har128-g2-r2 --labels <labels_g2_r2> --results-home <eval-lab-results> --output research/experiments/har117-results-home/har131-page-calibration-g2-r2.json. Artifact: research/experiments/har117-results-home/har131-page-calibration-g2-r2.json. Loop kind only: this cohort carries no first-failure/blame calibration. Blind scout-agent raters A (G3A1-4) / B (G3B1-4), not human ground truth; no reported off-limits openings. Denominators stay per-cohort, never pooled. The artifact exports scores and report hashes only, not trial content; inspection only, never training reflection.

Additional loop-kind-only calibration, same predictor, separately denominated cohort HAR-128 G2 tail: 3 HAR-120 trials (frozen 2026-10-01T10:21:18Z): page loop kind `1/3` vs agreed rater cells (3 agreed of 3, 0 excluded on rater disagreement, 0 abstentions); rater agreement `3/3`. Loop kind only on this cohort: no first-failure/blame calibration, denominators never pooled. Method: research/explorations/trace-lab/har119/score_page.py --published-cohort --cohort har128-g2-tail --results-home <eval-lab-results> --output research/experiments/har117-results-home/har131-page-calibration-g2-tail.json. Artifact: research/experiments/har117-results-home/har131-page-calibration-g2-tail.json. Loop kind only; no first-failure/blame calibration. Blind scout-agent raters G4A/G4B, not human ground truth; neither reports off-limits openings. Tiny cohort: three trials, all agreed labels completion-claim. Denominators stay per-cohort, never pooled. Scores and report hashes only, not trial content; never training reflection.
- First failure: none recorded. Opinion; HAR-119 part 2: 12 HAR-110 split-v2 runs: page `1/9` vs agreed rater cells (coverage 1/12, 11 abstentions).
- Blame: probe-03 `n/a` (`R-NONE-01`), page reading `none`. Opinion; HAR-119 part 2: 12 HAR-110 split-v2 runs: page `11/11` vs agreed rater cells (uninformative: raters near-constant model).
- Loop kind: `none`. 0 of 0 turns after the confirm prompt claimed completion. Opinion; HAR-119 part 2: 12 HAR-110 split-v2 runs: page `7/11` vs agreed rater cells; HAR-128 part 2: 40 HAR-116 trials: page `28/35`; HAR-128 G2 attempt 1: 20 HAR-120 trials: page `12/17`; HAR-128 G2 re-run: 19 HAR-120 trials: page `10/16`; HAR-128 G2 tail: 3 HAR-120 trials: page `1/3`.
- Grader alignment: `unknown`. no named gap; verifier ran 4 tests, 0 failed. A nonzero test count alone never means the grader tests the instruction. Opinion: a nonzero test count alone never means the grader tests the instruction.
- Upstream acquisition: not confirmed; failed/unknown attempts are non-deciding.
- Pass taint: `clean` (no reasons). A flag for counts, not a ruling.

## Record

- task: `local-lab/event-summary`; model: `gpt-6-luna`
- reward: `1.0` (scored=True, verifier_result)
- counts: `counted_pass` (none)
- stop reason: `unknown`
- stop category: `unknown` (how the run ended; the counts verdict above is unchanged)
- tokens: step sum `28166` (27774/392); Harbor native `27774/392`; proxy-settled `None/None` (unavailable; proxy ledger unreadable: provider usage calls are invalid); attempted ceiling `None` (settled usage plus unresolved reservations; same attribution)
- cost: `None` (proxy_ledger_x_pinned_price; proxy ledger unreadable: provider usage calls are invalid); estimate unavailable: not a self-hosted route trial
- steps: stitched 7 (agent 2, model 2, single_head)
- first failure: `none` (clean execution)
- outcome: `R-NONE-01` (n/a): scored pass with no outcome-relevant failure
- flags: `outcome:R-NONE-01:n/a`, `post_edit_input_share:51%`, `max_prompt:14109`
- job limit-hit share: `0.0%` (0/1 trials hit our limits; setup-limited: `false`)
- token flow (HAR-114):
  - last useful edit: step `6` (call 1, write-call; persists in final diff: `None` -- verifier/agent.diff unreadable)
  - tokens after last edit: `14109` in / `31` out (shares `0.508` / `0.0791` of agent_result)
  - loop onset: none (no repeat run detected)
  - prompt/call: first `13665`, max `14109` (step `7`), median `13887.0`, slope `444.0` tokens/call over `2` calls
  - summarisations: metadata `None` / 0 handoff events found
  - largest terminal outputs fed back: step `6`:281ch
