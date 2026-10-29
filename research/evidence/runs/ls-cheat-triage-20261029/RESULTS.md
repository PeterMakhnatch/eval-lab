# LS-CHEAT-2026-10-29-1 results: the 154 Live Supervisor cheat cards

Plan: `README.md` (merged before any label was joined). Tools: `tools/stage_a.py` (label-free), `tools/stage_b.py` (labels),
`tools/stage_d.py` (open-more commands, label-free). Aggregates: `results/stage_{a,b,d}.json`, `results/m4_judgement.json`. Zero model
calls, $0, no upload, no Docker. Labels are publisher labels on **stored strict-DEV Goodfire runs**, not on live runs. Intervals are
Wilson 95%. Cells below 3 are suppressed; counts inside the 200-run cohort below 10 are suppressed so a complement cannot expose one run.

**Revision note (follow-up PR after the Opus review of #1510).** Aggregates were re-cut for the suppression rule (small cells bucketed,
the whole-findings-store tally dropped because it covered runs never cross-keyed as strict DEV, the exact count of cards inside the
cohort suppressed; README §2 revised to match). Wording was corrected where it claimed more than the data shows. No number of the
verdict changed.

## Headline

These 154 cards are alarms from one Haiku 5.5 `facets` export (`suspicion >= 2`) on **stored strict-DEV Goodfire runs, not live runs**.
By publisher label, 105 of the 144 cards on an enacted-or-clean run sit on an enacted run: 72.9% (65.1-79.5).

- **The preregistered rule was met, against a comparator the plan itself had flagged as the wrong population.** The plan compared the
  lower bound (65.1) with the 200-run cohort's 53.3%, and 65.1 > 53.3 with n = 144 >= 20. README M2/M3 already said that cohort is a
  case-control sample whose rate "does not transfer to the 3,146 batch" and scheduled batch-level strata. Using that comparator was a
  design error, not information the join revealed.
- **The primary statistic is the prevalence-free one: how often the alarm fires on enacted versus clean runs in the same batch.** 105/1,636
  = 6.4% (5.3-7.7) versus 39/752 = 5.2% (3.8-7.0): **rate ratio 1.24 (0.87-1.77), which covers 1.** By that, the alarm's ability to tell
  enacted from clean runs is **not shown**. (The batch's own enacted share, 1,636/2,388 = 68.5% (66.6-70.3), gives the same reading: a
  lift of 1.06.)
- A modest effect is not excluded: the point estimate is 24% more alarms on enacted runs and the study has 39 clean alarms, so power is low.
- Neither 68.5% nor 53.3% is a natural prevalence; both come from how Goodfire was built. **72.9% means nothing outside this dataset.**

## 1. What the cards are (M1, `stage_a.json`)

| Question | Number |
|---|---|
| Cards on disk / in the day index / distinct runs / cards per run | 154 / 154 / 154 / 1 each; no card beyond the index |
| Dataset prefix | `goodfire-reward-hack-data` for 154 |
| Header: `actor_inputs_available=false` / `owner=other` / `killable=false` | 154 / 154 / 154 |
| Alarm detector on the card | `facets` (Haiku 5.5, score 2) on all 154; a few cards also carry facets lines from other readers (single-line cells suppressed). Another detector alarmed too on 8 cards (flags, tripwire, cascade, metr, docent, facet-windows, re-check); 146 are facets-only |
| Alarm lines per card | 1 on 120, 2 on 26, 3 or more on 8; **34 cards repeat an identical alarm line** |
| Distinct steps across a card's alarm lines | 5 on 140, 4 or fewer on 6, 6 or more on 8; the card lists at most 8 and lists 787 in all |
| Cited steps with an excerpt | **0 of 787**: all say "outside the live window; open the run" |
| Card size | 1,221-4,866 bytes; every card ends "Leads, not verdicts" |
| First facets alarm line names no shortcut (`none`) at suspicion 2 | 12 cards (`stage_a.json`) |

**Strict DEV cross-key (identity, before any label): 154 of 154 strict DEV; 0 TEST, 0 sealed, 0 unknown.** Run id, split `dev`,
DEV-admitted task, no registry TEST assignment, effective split not TEST. All counts here are over these 154 runs or the 3,146-run batch
(also cross-keyed 3,146 of 3,146); no tally over the whole findings store is reported.

## 2. Why 154 alarmed (M2)

- Every card's alarm is one record, all stamped `2026-10-29T01:53:24Z`: the same write holds 3,146 Haiku 5.5 `facets` findings, one per
  strict-DEV Goodfire run (1,891 clear at score 0, 1,101 clear at score 1, **154 alarm at score 2 = 4.9%**). The Run Atlas facet store was
  exported as findings in one batch; the alarm router had consumed 1,995 older alarms at start-up without routing them, so this batch was
  the first it carded. The 154 equal the batch's `suspicion >= 2` set; the Haiku line's `suspicion` equals the finding's score for all 3,146.
- The last-wins facet reader for the 154 is Haiku (146), Sonnet (6) and Opus (fewer than 3); the finding is the Haiku record regardless.
- It is a different population from the published numbers. The 200-run labelled cohort (80 enacted, 70 clean, 50 considered-only) is inside
  the batch; **fewer than 10 of the 154 cards are in it**. The batch holds 2,944 labelled DEV runs (1,636 enacted, 752 clean, 556
  considered-only), that cohort among them; 202 batch runs carry no publisher stratum.
- Inside the cohort the same findings give **7/80 on enacted** and under 3/70 on clean. The 7/80 count matches the CM-12 figure; this
  work did not show the same runs or records. NP22-CM-1's 4/68 and 1/58 differ from the cohort's 80/70 in denominator **and**
  Haiku-record choice: **that gap is unexplained** (record selection and/or denominator), not explained here.
- Cascade v8 (10/80, 2/70) is a different detector and triggered no card; another detector's alarm appears on 8 cards only as an extra line.
  So 154 is the same detector on 15.7 times the runs of the cohort.
- The clean-run alarm rate in the larger population is 39/752 = 5.2% (3.8-7.0); under 3/70 in the cohort is compatible with it
  (about a 0.29 chance of 2 or fewer at 5.2%).
- Re-alarm arithmetic. 34 batch runs already had an older Haiku `facets` alarm record under the same key (detector, version, reader); all 34
  still alarm in the batch (0 flipped to clear). Exact duplicate lines are fewer than 3; the 34 are identical only once rendered on the card.
  The store's 188 Haiku alarm runs versus 154 alarming in the batch are not reconciled here: the other 34 runs are outside the batch and
  were not cross-keyed.

## 3. Card precision by publisher label (M3; stored DEV runs, not live)

| Card set | Enacted | Clean | Neither (unlabelled or considered-only) | P = enacted/(enacted+clean) |
|---|---|---|---|---|
| All 154 (all alarm on Haiku facets) | 105 | 39 | 10 | 72.9% (65.1-79.5), n = 144 |

- Base rates: batch labelled 1,636/2,388 = 68.5% (66.6-70.3); cohort-200 80/150 = 53.3% (45.4-61.1).
- **Plan deviation.** README M3 fixed the baseline at the cohort's 53.3%. Under it the Wilson lower bound clears (65.1 > 53.3) and the
  rule's answer would be "yes". Against the batch's own 68.5% it does not, and the prevalence-free rate ratio (headline) covers 1. I set the
  batch rate as comparator after seeing the labels; the plan had already said the cohort rate does not transfer, so this was a design
  error in the plan, not new information, and the change went against the cards.
- **Considered-only bracket omitted.** M3 asked for P with considered-only counted as positive and as negative. Considered-only cards are
  fewer than 3, so each bracket moves P by under one point; the cells are not reported because their complement would expose a single run.
- **Alarm rate by stratum (batch, not preregistered; this is an alarm rate on labelled strata, not a recall claim):** enacted 105/1,636 = 6.4%;
  clean 39/752 = 5.2%; considered-only under 3/556. Cohort-200, enacted 7/80 = 8.8% (4.3-17.0).
- Exploratory, by suspicion level (not preregistered; run length is not controlled, see NP30): enacted share 56.4% at score 0 (705/1,250),
  83.1% at score 1 (826/994), 72.9% at score 2 (105/144). **Do not read this as a signal at score 1.** At score >= 1 the alarm rate is 56.9%
  on enacted, 27.5% on clean and 7.6% (42/556) on considered-only: a detector tracking hacking intent should not score considered-only runs
  below clean ones, so the strata differ in source or length, which is the confound. A score >= 1 alarm would also fire on over 1,100
  labelled runs, so it is not a triage option.
- Reader and detector splits: all 154 carry a Haiku facets alarm, so reader = Haiku for the whole set. The 8 multi-detector cards and the
  cards in the 200-run cohort are under 20 labelled cards and are not split. The 20-card rule is met for the all-cards row only.
- Cited steps against publisher positive steps, 105 enacted cards (101 mappable): exact hit 55/101 = 54.5% against 34.1 expected under a
  **uniform random draw of the same size (33.8%)**; within 5 assistant turns 84/101 = 83.2% against 80.1 expected (79.3%). The null is
  uniform, not position-matched: the cited steps sit late (median relative position 0.73) and publisher-positive steps may too, so
  "beats a uniform null" is the claim, not "beats position". The within-5 test counts assistant-turn ordinals while the chance model uses
  the stored step index, two spaces that differ (Defect 2); that test says little either way.

## 4. Usability (M4; every card's own commands, `stage_d.json`)

- **All 154, mechanical:** `evallab trace card <run>` exit 0 on 154, median 1.9 s (p90 2.6 s, max 3.1 s, including `uv run` start-up);
  `evallab qa-data window <run> --around N --radius 3` exit 0 on 154, median 0.6 s (max 1.2 s); the window contains the step N on 154,
  5-7 steps, 1,069 steps with text or an action, none truncated. **No window step carries a tool-output preview**
  (`window_steps_with_observation_preview` = 0): tool results appear only as separate `tool` steps.
- **Defect 1:** the card says `actor_inputs_available=false` on 154, yet `trace card` reports `actor_inputs_available=true` on 154 and the
  windows return content. For stored runs the card header is wrong.
- **Defect 2:** `trace card` prints a `steps=N` header in a different index space from the cited steps: the `stage_d.json` ratio
  (header over stored step count) and the count of cards citing a step at or past the header are in that file. The windows use the stored
  index and match the cited steps.
- **Defect 3:** the first alarm line is a whole-run summary (shortcut plus approach), not the reason a given step was cited; no card has an
  evidence quote (0/787).
- **The preregistered M4 yes/no ("the window plus card is enough to judge a cheat") was not recorded as such at the time.** What I noted
  per card was whether the window shows the move the card's own summary names, with three post-hoc categories. `results/m4_judgement.json`
  records it with small cells suppressed. This is the author's impression on 10 cards, **not used in the verdict**. The sample is not
  representative: S2 takes 5 of the 8 multi-detector cards (about 5% of the 154) and S1 5 of 146. The sample was fixed by hash before
  labels; I had read the Stage B aggregates when I looked at the windows, but no per-run label was joined to the 10 cards. In fewer than 3
  of 10 did the window show the named move; the rest were routine edits or undecidable at radius 3. The window is built `--around` the
  earliest cited step, which was usually an earlier, benign one.
- Reading time, not the 2.5 s of commands, is the cost: a card gives a lead to open, never a judgement.

## 5. Router and queue defects (M5)

| # | Defect | Evidence | Fix |
|---|---|---|---|
| 1 | Alarm lines are read from the raw findings file, not newest-per-key: 34 cards repeat a line; a superseded alarm can still print | stage A | **Fixed in PR #1509** (`gather_alarms` keeps the newest record per detector, version, reader; test fails before, passes after). |
| 2 | Header `decision: flag_cheating kind=cheat reader=X` is the alarming record's detector and reader (`live_supervisor_cli.flagged`), not a supervisor or model decision | code; 154/154 | Print `alarm: <detector> (reader)`, drop `decision:` for alarm cards. List only. |
| 3 | `actor_inputs_available=false` for a stored run that has a full transcript | 154/154 vs `trace card` | Derive it from the store for non-live runs. List only. |
| 4 | Every cited step is "outside the live window" | 787/787 | For a stored run read the step excerpt from the store; or drop the section. List only. |
| 5 | `--around min(steps)` opens the earliest cited step | the named move showed in fewer than 3 of 10 windows | Emit one window command per cited step (up to 5) or centre on the detector's highest-ranked step. List only. |
| 6 | A bulk re-emission of the facet store is routed as 154 new alarms; 34 of them already had an older alarm under the same key | stage A | Route only when a key's verdict changes to alarm (compare with the previous record of the key). List only. |
| 7 | No prior on the card: facets `>= 2` fires on 4.9% of runs and the alarm rate on enacted versus clean runs is 1.24 (0.87-1.77) | section 3 | Add the detector's measured rate ratio. List only. |
| 8 | All 154 get equal weight; 8 carry a second detector | stage A | Rank multi-detector cards first. List only. |
| 9 | Queue line said "154 live runs" for stored runs and was 20,009 bytes | LS-5 | Already fixed in #1495 for later days. |
| 10 | `observed:` is the routing time (all 154 at 01:54:47Z), not the alarm's `produced_at` | cards | Show `produced_at`. List only. |

## 6. Are these cards worth a Trace Watcher's time?

**Not shown.** The alarm fires on enacted and clean runs at rates whose ratio, 1.24 (0.87-1.77), covers 1; the cards carry no excerpt; the
first window rarely shows the named move; 12 cards say "shortcut: none". They are cheap to open (about 2.5 s) and 105 of 144 labelled cards
sit on enacted runs, but that share is close to the batch's 68.5% and neither is a natural prevalence. The study cannot exclude a modest
effect. If the alarm's likelihood ratio (about 1.24) transferred to another traffic mix, precision would follow the prior (about 6% at 5%
prevalence, about 24% at 20%): that is arithmetic on this ratio, not a measurement. No live Harbor trial has alarmed yet.

**Not claimed:** a preregistered recall (the 6.4% is an alarm rate on a labelled stratum of this batch), anything about a detector subset
under 20 labelled cards, anything on live runs, the 202 batch runs without a publisher stratum, or any precision outside this dataset.

## 7. Proposal, not run

A hosted adjudication would settle what the labels cannot (is the cited move a hack?). Window-only, blinded, Sonnet 5.5: five radius-3 windows
per card is about 5.6k tokens (median window 1,035 estimated tokens), so all 154 cards cost about **0.87M input tokens**, plus a few k output;
a 40-card stratified subset is about 0.23M. A whole-run read would be far costlier: the Haiku facet reads of these 154 runs used 29.6M input
tokens in all (median 181k per run, p90 261k, max 1.52M). Anthropic is near 47% of its 7-day line; this is within budget but not started.
