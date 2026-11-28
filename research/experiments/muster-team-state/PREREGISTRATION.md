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

## Amendment 1 (2026-11-28): subscription pilot on a local seat host

Written before any model trial of these families. Peter approved on 2026-11-28 (about 11:30 AM ET): continue, with a budget of $20.
OBSERVED: the paid route cannot start. The OpenRouter key is at its own limit of $100 (remaining $0); the account has $6.91.
This amendment adds a pilot that uses subscription models and no API money. Steps 1 and 2 of "Order and stop rules" stay as written and stay pending.

### Harness

`muster host` serves the packaged world of a task over local HTTP in 1 process and grades with the packaged verifier. No Docker and no Harbor.
The seat agent is `omp -p` (v19.2.1) with the `bash` tool only: no rules, skills, context files, memory, extensions or model fallback.
The system prompt is 1 fixed paragraph. The user message is the packaged seat instruction, byte for byte. `WORLD`, `SEAT` and `SEAT_TOKEN` are in the agent's environment.
The agent's working directory is a fresh empty directory outside the output directory. Every trial is a new session.
The run directory records the muster revision, the wrapper, the overlay and the system prompt with their sha256 before the first scored trial.
Differences from the Harbor pilot: the agent runs on the host as the user, not in a container, and it can reach the network. The agent harness is `omp`, not `terminus-2`.

### Models and cohort

Primary: `anthropic/claude-fable-5-2` and `openai-codex/gpt-6.2-sol`, both at thinking level `high`.
Each primary model runs the 160 trials of step 2: 16 conditions, 10 instances, 1 attempt.
Secondary, as quota and time allow, in this order: `xai-oauth/grok-4.7`, `zai/glm-5.3`, `meta/muse-spark-1.3`, `anthropic/claude-haiku-5-5`, each at `high`,
on the same 16 conditions and instances i01 to i05 (80 trials). A secondary model is a screen; it gets no decision by the gap rule.
Wall-time limit: 1,800 s per trial. The packaged value is 900 s; the depot oracle needs up to 88 requests in `storm-probe`, and a time-out must not hide the protocol result.

### Calibration (not scored)

Before its scored trials, each model runs instance i01 of `calm-probe`, `storm-solo` (depot), `share-probe` (dossier) and `plain-probe` (inquiry).
These 4 conditions are not in the pilot cohort. They test the harness, the text and the time limit.
A model that scores below 0.90 on a calibration probe is investigated before its scored trials start; a change to the harness after that restarts calibration.

### Validity

Each transcript is scanned for commands that read outside the agent's working directory, list processes or open files, or name the repository, the task files or the output directory.
A flagged trial is reviewed by hand. A trial whose agent saw task, instance, verifier or other-trial data is invalid: it is reported and excluded.
A trial that ends by the time limit, by a harness error or with a model other than the planned one is lost. It is reported apart, with its graded reward where one exists, and is not a 0.

### Decisions

The measures, contrasts, statistics and gap rule of "Measures and decisions" apply to each primary model separately, with no change.
The solo arm is not in the first wave. If a primary model shows a candidate gap on a facet (step 3), that model runs the solo arm of that facet on the same 10 instances. No other widening.
A candidate gap is not a mechanism conclusion until solo is measured.
Transfer of a result to the Harbor harness is not claimed.

### Stop rules

Stop a provider's trials when its weekly quota reaches 80% used (`policy/standing-approvals.yaml`), or when a model is replaced by another one.
Finish the planned cohort of a model once it starts, unless a validity stop applies. Do not add facets mid-wave.

## Amendment 2 (2026-11-28, 12:13 PM ET): changes after calibration, before any scored trial

OBSERVED in calibration 1 (28 trials, not scored; a throwaway driver on the same world loader):

- `claude-fable-5-2`, `gpt-6.2-sol`, `grok-4.7` and `claude-haiku-5-5` score 1.0 on the 4 calibration conditions. `gpt-6-luna` scores 1.0 on 3 conditions and 0.90 on `storm-solo`.
- `zai/glm-5.3` (no balance) and `meta/muse-spark-1.3` (the subscription admits only its own client) cannot run. Each agent stopped at its first model request, and the verifier still gave the score of an idle seat (0.05 to 0.63).
- 3 models wrote scratch files under `/tmp`, which all trials share.

Changes:

1. Secondary models, in this order: `xai-oauth/grok-4.7`, `anthropic/claude-haiku-5-5`, `openai-codex/gpt-6-luna`. `glm-5.3` and `muse-spark-1.3` are removed.
2. The system prompt has 1 more sentence: "Create files only in the current working directory." `TMPDIR` is a private directory in the agent's working directory.
3. A trial is lost when its agent exits with a code other than 0, records a model or provider error, or has no assistant message.
4. A trial is invalid when its transcript names a path under `/tmp`, `/private/tmp` or `/var/tmp` that another trial of the same wave also names in an overlapping time window.
5. The time limit is enforced by `muster host` only (TERM, then KILL after 10 s).

OBSERVED in calibration 2 (20 trials, not scored; the final wrapper through `muster host`, before its code was committed):
the same 4 models score 1.0 on the 4 conditions; no trial names a shared scratch path; `gpt-6-luna` scores 1.0 on 3 conditions and 0.57 on `storm-solo`.
Investigation of `gpt-6-luna` on `storm-solo`: in both runs the agent worked through the task (12 and 18 commands, 55 and 49 world requests) and lost units by its own work.
Run 1: 26 writes without `expect`, 1 ticket applied twice, 1 pair below its floor. Run 2: 9 tickets of its queue left unsettled, with a final message that claims completion.
The harness and the text are not the cause. Its scored trials start.

OBSERVED in the Docker smoke: Harbor refuses every packaged task of the 3 families, because the task name has no `org/` part.
`muster host` does not read the task name. `host.json` records the sha256 of the world files and of each instruction, so a result can be tied to a repackaged version with the same bytes.
The Harbor pilot (steps 1 and 2) needs the repackaged tasks.

## Amendment 3 (2026-11-28, 12:55 PM ET): follow-up arms after wave 1, before any follow-up trial

OBSERVED in wave 1 (muster revision `c903bd964`): `claude-fable-5-2` and `gpt-6.2-sol` each score 1.0 in all 160 trials (16 conditions, 10 instances).
No trial is lost or invalid. In the 40 depot `probe` trials of each model, `clean` is 1.0 and no write is sent without `expect`.
By the rule above, all 3 families are too easy for both models in the arms with 1 real seat. No candidate gap exists, so no `solo` arm runs.
The secondary wave (3 models, instances i01 to i05) runs as planned.

The follow-up asks 2 questions that the saturated arms cannot answer. Both are exploratory: they were not in the draft.

### Follow-up 1: `terse` (depot)

Question: do the agents use the safe protocol when the text does not list what can go wrong?
The `terse` arm is the `probe` task with 1 change to the seat text: the section "What can happen" is removed, and the pointer to it in the write budget line.
The world, the instance, the staged teammates and the verifier are the bytes of `probe`. The run directory holds the derivation script and a manifest with the sha256 of each derived text.
The text keeps the API reference: `expect`, the versions, `GET /log`, and the write budget line that names status 504. The reply to a 504 states that the write may have happened.
Conditions: `limbo`, `takeover`, `skew`, `storm`; 10 instances; both primary models; 1 attempt. The secondary models run instances i01 to i05 after their wave.
Contrast: `probe - terse`, paired by instance, with the wave 1 `probe` trials. Measure: `focal`; diagnostics: `clean`, `blind_writes`, `log_reads_after_504`, `double_applied`, `stale_writes`.
A prompt dependence needs: `probe` at least 0.90, `terse` at least 0.20 lower on paired instances, a paired interval that excludes 0, and a sign test with p at most 0.05.

### Follow-up 2: `team` with real agents (depot, dossier)

Question: do 2 or 3 real agents of the same model keep the shared state right when they work at the same time?
Conditions: depot `limbo-team`, `takeover-team`, `skew-team`, `storm-team` and dossier `cascade-team`, `dispute-team`, `mix-team`; instances i01 to i05; both primary models; 1 attempt.
Every real seat of a trial gets the same model and wrapper. All seats start together. The time limit is 1,800 s for the trial.
A trial is lost when 1 of its seats is lost by the rules of Amendment 2. Report `reward`, and `team - probe` on the same 5 instances, with no decision by the gap rule.
Team races depend on real timing. A full score does not show that a race was tested; report `conflicts` and `stale_writes` next to it.

## Amendment 4 (2026-11-28, 1:25 PM ET): agent start failure, and when a lost trial runs again

OBSERVED in follow-up 2: all 5 `mix-team` trials of `claude-fable-5-2` are lost. Each of the 15 agents stopped at start with `SQLITE_BUSY` on the agent program's own state database.
No session file exists and no model request was made. The 15 agents started at the same moment while 3 other waves and 3 workers ran on the machine.

Changes:

1. The wrapper starts the agent again (at most 6 starts, 2 to 7 s apart) when the agent exits with a code other than 0, no session file exists, and its error output names the database lock.
   A seat directory of a trial with the new wrapper has a file `start.1.stderr`. The wrapper file changed in place at 1:25 PM ET: trials of the secondary wave that start later use it.
2. A trial that is lost before any model request (no session file in any seat) runs again once, in the same cohort. Its first directory moves to `_lost/` and stays as evidence.
   This replaces "Do not replace losses in the cohort" for this case only: such a loss carries no information about the result.
3. The 5 lost trials run again, 2 trials at once.

## Amendment 5 (2026-11-28, 1:30 PM ET): more attempts on `storm` for the `terse` observation

OBSERVED in follow-up 1 (attempt 1): `claude-fable-5-2` scores 1.0 in all 40 `terse` trials. `gpt-6.2-sol` scores 1.0 in `limbo`, `takeover` and `skew` (30 trials),
and 0.98 in `storm-terse`: 8 of 10 instances are full, 2 have focal 0.9 with 2 writes applied twice each. Its `storm-probe` was 10 of 10 in wave 1.
In both losses the agent got status 409 `stale` (a teammate had written the same ticket, or its own write after a 504 had happened), computed a new quantity from the new book, and wrote again without a look in the log.
By the rule of Amendment 3 this is no prompt dependence (difference 0.02).

Change: `gpt-6.2-sol` runs attempts 2 and 3 of `storm-probe` and `storm-terse` (10 instances, 40 trials). `claude-fable-5-2` runs attempts 2 and 3 of `storm-terse` (20 trials).
Report for each arm the number of trials with focal 1.0 of 30, the instance means, and the contrast of Amendment 3 on the instance means. Report Fisher's exact test on full trials as a description, not as a decision.
Attempts of one instance are not independent samples.
