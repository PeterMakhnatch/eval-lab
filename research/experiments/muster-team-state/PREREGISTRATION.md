# Draft pre-registration: team state

2026-11-25. Before any model trial of these families. Peter must approve the draft before any paid run.
Family versions: `depot` v4.1, `dossier` v3.4, `inquiry` v2.5 (generator seeds `depot-v4.1`, `dossier-v3.4`, `inquiry-v2.5`; designs in `DESIGN-*.md` in this directory).
Every family has a committed `card.json` that `muster card FAMILY --verify` checks in the gate. `REVIEWS.md` lists every review.

## Question

Do agents maintain shared state when the world does not enforce the necessary protocol?
Can a control arm remove the loss without changing the underlying items or reports?
Does an agent notice that a teammate's reply does not answer its question, and ask for the right correction?

## Families and arms

| Family | Facet class | Facets |
|---|---|---|
| depot | calibration | calm, clash |
| depot | hypothesis | limbo, takeover, skew, storm |
| dossier | calibration | share, scarce, gone |
| dossier | hypothesis | cascade, dispute, mix |
| inquiry | calibration | plain |
| inquiry | hypothesis | month, part, range, silent, mixed |

| Family | Arms and purpose |
|---|---|
| depot | `probe`: one real clerk, scripted peers, no guard. |
| depot | `guard`: same staged work, with world checks on writes and explicit instructions. |
| depot | `solo`: one clerk holds open tickets, with the total write budget. Faults stay. |
| depot | `team`: real concurrent clerks with a message channel. |
| dossier | `probe`: one real writer and reader with correct scripted peers. |
| dossier | `reader`: correct register fixed at the start; one real reader. |
| dossier | `solo`: full reports and asks available to one real clerk; no register needed. |
| dossier | `team`: 3 real clerks. |
| inquiry | `probe`: one real clerk; scripted colleagues reply in sentences. |
| inquiry | `fields`: the same replies as structured fields. |
| inquiry | `flagged`: the same replies, and the world says in which fields each reply differs from the question. |
| inquiry | `solo`: the settling records are given; no questions. |

Dossier has no `guard` arm. Its `reader` arm is a different diagnostic, not a guard.
All 4 arms of each facet share the same 10 instances.

## Measures and decisions

Primary measure: `focal` in `probe`, separately for each facet.
In depot this measures correct groups that carry a real clerk's tickets.
In dossier this measures asks that depend on a real clerk.
In inquiry every case is the real clerk's own, so `focal` is `reward`.
The primary contrasts are `guard - probe` (depot only), `flagged - probe` (inquiry only) and `solo - probe` (all 3 families).
Report `reader - probe` (dossier only), `fields - probe` (inquiry only) and `team - probe` as secondary contrasts.
Dossier's solo removes the need for storage. It does not match the team's resources.
Report `reward` and diagnostic measures, including depot `clean` and `legs.<seat>`, dossier `register_sufficient`,
and inquiry `right.<kind>`, `off_replies_seen` and `filed_off_reply`.
Inquiry's `flagged` arm tells what differs; it also gives the arguments of the correction. It does not isolate noticing.
Inquiry's 6 facets of 1 instance index share figures and cases: every trial needs a fresh context.
Inquiry's single facets have chance baselines that pass on some instances (`card.json`); read single-facet results against them.

Use `python -m muster contrast JOBS ... --measure clean register_sufficient --seed 0`.
Calculate each instance's mean over valid attempts. Give each instance equal weight.
Pair by family, facet and instance within a fixed model and harness.
Use the 95% paired bootstrap (10,000 draws) and exact 2-sided sign test.
Exclude ties from the sign test. Do not report either statistic for fewer than 5 pairs.
A lost trial is not a 0. Report every lost and unplaced trial, including arm imbalance.
Do not replace losses in the cohort. Treat a new cohort after a repair as a separate run.

A synchronization gap (depot, dossier) or a checking gap (inquiry) needs all of the following, on a hypothesis facet:

- Control `focal` is at least 0.90; `probe` is at least 0.20 below the control on paired instances.
- The paired interval excludes 0 and the sign test has p at most 0.05.
- The solo control also reaches 0.90, and logs support the specified mechanism.

For dossier, use `solo - probe` for this decision. Use the reader contrast and register measures
 to distinguish writing and reading losses. A low reader score is not evidence of a writing loss.
For inquiry, the control is `flagged`. Use `fields - probe` to see whether the loss is in reading the sentence,
 and `right.<kind>` with `filed_off_reply` to see which kind of reply was filed unchecked.
A `solo`, `guard` or `flagged` score below 0.90 marks a broken family for this model and configuration.
Treat this as a task text or tool failure until investigated, not a team behaviour result.
The guard changes 3 checks and their instructions together. It does not isolate one check.
If all hypothesis probes reach 0.90 and no contrast reaches 0.20, the family is too easy for this model.
If the data meet none of these rules, report the result as unresolved.
The intervals and p-values are per facet, not simultaneous across facets.
The pilot screens for a gap; it does not establish a confirmed family-wide effect.

## Fixed resources and routes

| Stage | Model | Route |
|---|---|---|
| Pilot | strong model | to be decided by Peter |
| Replication and team | same model | to be decided by Peter |

Record the model, harness, task and Harbor revision, tool limits, token cap and wall-time cap.
Keep the same settings and fresh context across arms. Do not reuse a probe context in guard.
Depot matches the total write budget. This does not match aggregate agent time or tokens.
Dossier's report and answer caps follow the arms. Do not claim equal model cost across arms.
See `COST.md` for the oracle request counts and time caps. Actual model usage is unknown.

## Order and stop rules

1. Run a $0 Docker smoke with the scripted `oracle`, fresh containers and no model credentials.
   Check all arms, exact reward, liveness, replay and isolation. Do not run model trials yet.
2. After Peter approves a fixed paid cap, run 1 strong model, 10 instances, 1 attempt per condition.
   Run depot probe and guard on limbo, takeover, skew and storm. Run dossier probe and reader on
   cascade, dispute and mix. There is no dossier guard. Run inquiry probe and flagged on mixed.
   This is 160 trials total.
3. Finish the planned pilot unless a validity or spending stop applies. Do not add facets mid-wave.
   A candidate gap is a paired difference of at least 0.20 and a control score of at least 0.90.
   Widen only if a candidate gap shows. It is not a mechanism conclusion until solo is measured.
4. On Peter's separate approval, run solo, calibration facets and replication across all arms
   of all 3 families. Use a distinct cohort with 3 attempts per instance. Report the pilot apart.
   Start team (depot and dossier only) when the one-real-seat controls pass; team requires separate budget approval.
5. Stop when the planned cohort ends, a control falls below 0.90, an oracle fails, or logs are invalid.
   Pause for review on any missing reward, timeout, exception or unplaced trial. Report losses.
   Stop before exceeding the approved spend cap or when billing or cleanup state is unknown.
   Do not continue merely to obtain a significant result. Record every stop and its cause.

Only after approval, create the roster and the strict campaign plan with separate conditions as arms.
Use `python -m muster run muster/tasks/FAMILY/build/CONDITION --roster ROSTER.json --attempts 1 --dry-run`
 to build each approved Harbor config without starting a trial.
Validate the future plan with `python -m campaign check PLAN.toml` before authorization to run.
No executable campaign plan exists in this draft.

## Limits

A card proves only properties of scripted policies and explored schedules. It cannot prove model failure.
Depot can pass by talking, splitting items, or repair after the fact. Reward does not require one protocol.
Inspect `clean` and the event log to distinguish these routes. Team races are not deterministic.
A depot loss can come from arithmetic, ticket misreading, tool batching or exhausted budgets.
Inspect the solo control, request sequence and refused-for-budget measure before attributing a loss.
Single guards and a sham guard are later work.
Dossier tests a structured store and given times, not free-text notes or long histories.
The world checks that lines come from the writer's own reports or allowed dispute summaries. Dossier does not test honesty.
A dossier loss can come from a wrong reading of the rule or a mistake with a perfect register.
Tool batching is another possible cause. Inspect the solo, reader and event logs.
A dossier team loss can also come from the barrier: every clerk must wait until close, and a delayed put can replace a newer line.
In `dossier`, a reader that ignores 1 kind of special line and guesses which 2 of 4 look-alike questions have that status is fully right with chance 1/6 per instance (asserted by the generator).
In `dossier` v3.4, watched and off-watch reports share one draw law; the 7 round-5 writer policies without the watch list pass 0 of 10 built and at most 2 of 30 held-out instances in `scarce` and `mix`. These are empirical controls, not a bound for every writer.
Inquiry has 1 real agent. In `probe`, scripted colleagues state the full scope of each reply in 4 fixed sentence forms;
`fields` gives the same replies as fields, `flagged` adds which fields differ, and `solo` gives the settling records.
It tests the check and the correction, not the reading of loose prose. Its chain case is a designed adversary.
A clerk that already knows the chain rule can pass the chain without reading the corrected reply (`REVIEWS.md`).
In `part` and `range`, the reply form alone shows the fault (sentence length passes 5 of 10 in `part` and 8 of 10 in `range`); these facets test recognition of the form.
In `mixed`, a clerk that repairs part, range and crate replies by their form and guesses the month passes 6 of 10 instances.
An inquiry loss can also come from spending: `questions`, `fields_named_unneeded` and `refused_for_budget` show it.
Blind implementers (a model that writes a program from the seat text alone) test whether the text is sufficient.
They do not predict how an agent that works step by step will score.
No family measures general teamwork, model ranking, real-world utility or equal-dollar performance.
Attempts of one instance are not independent samples. Inference is limited to the tested facets and route.
