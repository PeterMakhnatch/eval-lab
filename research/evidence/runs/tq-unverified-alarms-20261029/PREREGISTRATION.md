# TQ-UNVERIFIED preregistration (written before any model call and before any label join)

Written 2026-10-29 by the TQ-UNVERIFIED worker. At this point I have read only the stored, frozen gate cells
(flags, eligible-finding memberships, finding text and citations) and the *header row* of AutomationBench
`fixes.csv`/`tasks.csv`. I have not read any label, class membership or defect description.

## Question

For every Task QA gate alarm on a task with no injected defect, in the frozen OQ-10 (Sonnet 5.5) and R29-OQ-1
(Codex terra) cohorts, is the reported problem a real defect of the original task?

## Population (fixed by rule, no new gate call)

- Gate output: `study-v2/cells/sonnet/C*.json` and `r29-oq1-codex-cross-arm/cells/codex/C*.json`.
  A **gate alarm** is a cell with `flags.new_verified_0_5 == true` (the production gate, the headline of both
  reports). Its **eligible findings** are the parsed findings named in `memberships.new_verified_0_5`
  (member `C<n>` is the parsed finding of rank `n`). Cells that are `missing` or `source_unavailable` have no alarm
  and are reported as coverage gaps, never as zeros.
- **Unchanged tasks** are the tasks without an injected/verified defect: AutomationBench Verified tasks with no
  `fixes.csv` row, and PAIChecker Multilingual and SWE-Gym tasks labelled human-aligned. PAIChecker human-misaligned
  tasks are not injected defects and are not part of this question (not adjudicated, not calibration).
  Labels come from the frozen sealed scorer inputs (`scorer-only/` and the staged bundles), joined only after this
  file is committed.
- **Alarm unit**: one case id (task) with at least one alarm from either gate. All eligible findings of both gates
  on that task go into one adjudication call. Exactly identical findings across gates (same normalised mechanism
  text, or same direction and identical citation set) are collapsed to one candidate that carries both gate
  attributions. Gate attribution is kept in the private unit table only.

## Calibration (mixed in blind)

- **Matcher** (separate role, never an adjudicator): `openai-codex/gpt-6.1-terra` is shown the documented
  AutomationBench defect (`what_was_wrong`, `finding`, `what_the_fix_does` of the task's `fixes.csv` row) and the
  eligible findings of that task, and answers `match | partial | no_match` per finding. It never sees the task source.
- **Known-real calibration items**: findings with matcher verdict `match` on AutomationBench tasks with a verified
  defect. The tasks are ordered by `sha256("TQ-UNVERIFIED-20261029:cal:" + case_id)` and the first 20 tasks that
  have at least one matched finding are taken. All eligible findings of those tasks (both gates) go into the task's
  unit; matched findings are the known-real items, the others are **off-target** items (status unknown, reported
  descriptively only).
- **Off-target-only items**: up to 6 further defective tasks (same ordering) that were flagged but with no matched
  finding.
- If the unit budget requires it (cap below) the calibration counts shrink in this order: off-target-only first,
  then known-real, never below 10 known-real tasks. Below 10 the item is reported as blocked.
- The adjudicators never learn which units are calibration units. There is no known-false calibration item: the
  adjudicators' specificity is therefore not measured, and this is stated as a limit.

## Adjudication

- Adjudicators: `openai-codex/gpt-6.1-sol` (primary) and `anthropic/claude-opus-5-5` (second), each one call per
  unit, `omp -p --thinking high --no-tools`, with the controlled no-retry/no-fallback config of the OQ-10 study
  (`controlled-subscription-oq10.yml`, sha256 `0df78b7a1aa1aad2dc08e5bef3520d9236b9c8918f0d5bef9bc000ef09fb7fb9`).
- Input: the task package exactly as the gate displayed it (`inputs/C*/source.txt`, numbered lines; no historical
  diagnostics, no DEV examples, no gate prompt) plus the candidate reports with opaque ids `F1..Fk` in a seeded
  shuffled order: claimed direction, mechanism and citations. Not shown: gate, confidence, benchmark, split, label,
  case id, unit class. Prompt text and schema are frozen in `research/experiments/tq-unverified-alarms/prompts.py`.
- Output per candidate: `real_defect | not_a_defect | uncertain` with rationale and up to three cited lines.
  Evidence is checked mechanically against the displayed source (path exists, quote occurs in the cited file). A
  `real_defect` verdict with no verifiable evidence is downgraded to `uncertain` and counted.
- Processing order is `sha256("TQ-UNVERIFIED-20261029:order:" + unit_id)`, so a quota stop leaves a random subset.
- Call cap 150 per adjudicator (including retries); matcher cap 60. One retry for an unparseable answer; a second
  failure is a missing verdict. Served `provider/model` and fallback flag are recorded per call from the session log;
  a call whose served identity is not the requested route, or that used a tool, is invalid (counts as a call, gives
  no verdict). Stop a batch after 3 consecutive failures-to-answer (refusals or invalid calls).
- Quota gate: read `omp usage --json` before every call; no call if that provider's 7-day window is at or above 85%.
  A stop is reported as partial coverage, never filled.

## Scoring (once, after both adjudicators finish)

- **Final finding label**: both adjudicators return the same verdict (after downgrades) -> that verdict; otherwise,
  or if either is missing, `uncertain`. Both raw verdicts are always shown.
- **Alarm label** per (task, gate): `real` if at least one of that gate's eligible findings has final label
  `real_defect`; `not_a_defect` if all of them are `not_a_defect`; else `uncertain`.
- Tables per benchmark (AutomationBench; PAIChecker Multilingual; PAIChecker SWE-Gym) and gate (Sonnet, Terra):
  counts of real / not a defect / uncertain over alarms, Wilson 95% intervals on each share, shown for the headline
  rule, for Sol alone and for Opus alone. Finding-level tables as well. Cohen kappa (3 classes) between the two
  adjudicators over findings with two parsed verdicts.
- Calibration: recall of the known-real items (share with final label `real_defect`, and per adjudicator), and the
  share of off-target findings labelled real by each adjudicator.
- **Validity gate**: the adjudicators are treated as validated only if known-real calibration recall under the
  headline rule is at least 0.70. If it is lower, `not_a_defect` counts are reported as upper bounds on falseness and
  the corrected reading is stated as not validated.
- **Corrected reading** per gate and benchmark: with `R`, `F`, `U` the alarms labelled real, not a defect, uncertain
  and `n` the unchanged tasks scored, report real-alarm rate `R/n`, false-alarm rate `F/n` to `(F+U)/n`, precision of
  alarms on unchanged tasks `R/(R+F+U)` (and `R/(R+F)`), and for AutomationBench a corrected balanced accuracy with
  the `R` unchanged tasks that carry a real alarm relabelled defective and the other unflagged unchanged tasks still
  treated as clean (an unverified assumption), as a lower and upper bound over `U`.
- **Decision rule for the headline reading** (per benchmark and gate, only if the validity gate passes), with
  `p = R / (R+F+U)`: `p <= 0.25` -> the alarms on unchanged tasks are mostly not real defects and the "chance"
  reading stands; `p >= 0.60` -> most are real findings, the unchanged tasks are not clean negatives and the "chance"
  reading is wrong; between -> mixed.
- Secondary, descriptive, joined only after every verdict is frozen: for AutomationBench unchanged tasks, the
  `tasks.csv` `fix_status`/`issue_group`/`finding` fields, to see whether the benchmark itself recorded a review note.
  It does not enter the headline.

## Boundaries

Hosted models read the task sources for evaluation only. Nothing is uploaded to Laminar or Docent. No gate, prompt,
cohort or stored score is changed. Raw prompts, responses and sessions stay in
`~/.local/state/evallab-fleet/2026-10-29/TQ-UNVERIFIED/`; the committed evidence holds counts, verdict tables keyed by
opaque unit ids, and no task text, held-out task names or label contents. Subscription routes only, $0 paid.

## Pins

- `research/experiments/tq-unverified-alarms/prompts.py` sha256: b8b940b2a350d760231348a0a1bd2732b268eaa801572b6539819ef26ff77a87
