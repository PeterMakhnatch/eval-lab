# Reviews of the team-state families

Every review below is by a separate worker (Codex `gpt-6.2-sol`, 1 worker per surface, each with its own session; all workers share the same task context file) or by Fable, before any model trial of the families. Raw worker reports, prompts and session records are kept in `~/Developer/muster-runs/night-2026-11-25/work/`; the incident record is `~/Developer/muster-runs/night-2026-11-25/INCIDENTS.md`. A finding counts as confirmed only when `muster finding verify` printed `CONFIRMED` for it, or, for a text defect, when the worker gave the exact sentence and the request sequence. Every confirmed executable finding is stored in `muster/tasks/<family>/findings/` and replayed by the family's card.

## Design reviews

| Design | Reviewer | Defects | Result |
|---|---|---:|---|
| `depot` v2 | `s1-skeptic-depot` | 12 | v3 |
| `dossier` v1 | `s2-skeptic-dossier` | 12 | v2 |
| `inquiry` v1 | `s3-skeptic-inquiry` | 13 | v2 |
| `inquiry` v2 | `s4-skeptic-inquiry2` | 13 | v2.1 |
| `inquiry` v2.1 | Fable (rule against seat text) | 1 rule clarification | v2.2 |
| `inquiry` v2.2 | build worker `b8d` | 1 route around the chain trap | v2.3 |

## Built families, round 1

### `depot` (built v3, fixed in v4)

| Surface | Worker | Confirmed | What it found |
|---|---|---:|---|
| Routes without the skills | `rt-d1-shortcuts` | 1 | A draw decided from the starting book passed `skew` and `storm` 10 of 10: the slack of the tight pair was always 12. 5 sound alternative routes (narrow readset, selective log reads, repair). |
| Careful clerks | `rt-d2-careful` | 1 | `seat.p1` is 0.0 for a correct p1 with idle teammates (a group measure). 211,360 episodes of 9 careful policies and the oracle: no other failure. |
| Scoring script | `rt-d3-verifier` | 0 executable, 3 measure issues | `limbo` exposure counted faults that cannot fire in `probe`; the idle floor is not 0 in `takeover` and `storm`; empty shares are omitted. |
| Malformed requests | `rt-d4-world`, then `rt-d4b-world` | 0 | `rt-d4-world` was stopped by a provider safety filter (incident 1 in `INCIDENTS.md`). The plainly worded `rt-d4b-world` made 135 explicit checks in 9 sampled episodes: no world defect; 2 unqualified properties hold only with the documented exceptions for staged and delayed effects. |
| Regularities and hidden data | `rt-d5-leaks` | 6 | Fault positions, takeover state, ticket-to-item maps were the same in all 10 instances; pair slack told the tight pair. |
| Seat text | `rt-d6-text` | 6 text defects | Refusal shapes, the moment a late write is charged, "while you work" in `calm`, personal versus logged settlement, final versus clean. |
| Staging | `rt-d7-dodge` | 0 | The staging is keyed by ticket, the design said by item. |
| Packaging | `rt-d8-pack` | 0 | 288 of 288 local HTTP parity cases pass. A static check of the seat build context found no private instance file; image isolation in Docker is not tested. |
| Text sufficiency | blind implementers `bi-d1` to `bi-d3` | 0 | 3 of 3 policies written from the `storm-probe` text alone reach reward and clean 1.0 in all 24 conditions on the round schedule. This tests the text, not a model run. |

`rt-d6-text`, `rt-d7-dodge` and `rt-d8-pack` first declined their task and were sent the same prompt again by the launcher (incident 2 in `INCIDENTS.md`). Their results are used only where they are executable or quoted.

### `dossier` (built v2, fixed in v3)

| Surface | Worker | Confirmed | What it found |
|---|---|---:|---|
| Routes without the skills | `rt-s1-shortcuts` | 4 | Decoys always newest (oldest-first writing passed `scarce` and `mix`); played clerks marked every tie (`dispute` passed without p1's dispute skill); roles followed from public positions and times (policies that never read the register passed `share` and `mix`). |
| Seat text | `rt-s6-text` | 6 text defects | Played clerks' timing, forced close, put accounting, statuses for puts and answers, reader sufficiency. |
| Text sufficiency | blind implementers `bi-s1` to `bi-s3` | 0 | On v3, `bi-s1` and `bi-s3` reach 1.0 in all 24 conditions. `bi-s2` reaches 1.0 in all probe, reader and team conditions and 0.0 in the 6 solo conditions: it reads `GET /register` 4,000 times in solo, where no register exists. The policy was written from the probe text; the solo text does not name a register. |

The other round-1 `dossier` reviews were stopped (incident 2) and are not counted.

### `inquiry`

No round-1 review of the built family; see round 2.

## Built families, round 2

Round 2 used a new brief: the reviewer owns 1 surface, states what it did not cover, and may decline any part. A decline is recorded and not sent again. All 17 round-2 reviewers and 9 blind implementers ran on `openai-codex/gpt-6.2-sol` (provider and model checked in every session record).

Declines: `rv-q3-world` declined its whole task (malformed requests to `inquiry`); Fable covered that surface (see the `inquiry` table). `rv-d5-info` and `rv-s5-info` declined the parts that would test seat authentication (token discovery, impersonation, access bypass). Seat authentication is therefore not covered by round 2; it is the frozen kit code that the older families share.

### `depot` v4

| Surface | Worker | Confirmed | What it found |
|---|---|---:|---|
| Routes without the skills | `rv-d1-skills` | 11 | 28 policies on 6 probe facets (1,680 episodes). No other deterministic shortcut was found in the tested policies, but guessing works: each skew and storm instance has 1 tight and 1 roomy pair, so a fixed guess of the tight pair is clean on 4 to 6 of 10 instances; each limbo instance has 1 dropped write among 3 faults, so retrying a fixed 504 is clean on 3 to 6 of 10. Sound narrower routes: `peer_only`, `selective_log`, `targeted`, `version_ack` (deduplicates by version counts), `refusal_reply`. |
| Malformed requests and accounting | `rv-d4-requests` | 0 | 354 episodes, 237,420 requests, 1,506 replies with status 504 (1,422 in the 316 main episodes, 84 in 18 snapshot episodes): no status 500; the request and event accounting rebuilt independently matches in all 334 audited episodes, including late landings at 0 budget and lapses at close. |
| Regularities and hidden data | `rv-d5-info` | 0 | 400 accounting checks consistent; no full ticket map repeats; draw classifiers near chance. The shared inherited ticket and the 504 outcome are readable from documented state, which is the intended skill. |
| Seat text | `rv-d6-text` | 0 executable, 5 text precision issues | Refusal precedence, a generic 504 sentence in conditions without faults, the moment late work lands (the text says "any moment", which is conservative). |
| Text sufficiency | blind implementers `bi-d4` to `bi-d6` (v4 text) | 0 | `bi-d4` and `bi-d5` reach reward and clean 1.0 in all 24 conditions; `bi-d6` reaches 1.0 in the 18 probe, solo and team conditions and in 3 of the 6 guard conditions, and stops on a guard refusal in the other 3 (it was written from the probe text). The round-1 policies `bi-d1` to `bi-d3` reach 1.0 in all 24 conditions on v4. |

### `dossier` v3

| Surface | Worker | Confirmed | What it found |
|---|---|---:|---|
| Careful clerks | `rv-s2-careful` | 0 | 9 independent policies in all 24 conditions, with worst-schedule search in `team` (1,080 selected results from 52,920 explored schedules); 8 careful variants score 1.0 in every cell. The family source changed to v3.1 during the run, so the reviewer reran all 24 conditions for all 9 policies and the team worst search for 3 policies on a fixed v3.1 snapshot (source digests recorded): the careful variants score 1.0. The 9th policy, which tests a stricter zone rule, fails by its own errors. |
| Routes without the skills | `rv-s1-skills` | 5 | Policies that never read the watch list pass `mix-probe` on 3 to 5 of 10 instances: the slot count includes p1's own-report control site, which only p1 is asked about, so losing that key costs nothing. `scarce` already needs selection (0/10). The 4 round-1 shortcuts no longer work. |
| Scoring script and status rule | `rv-s3-verifier` | 0 | 190 values recomputed independently, 27,870 rule sequences and all 2,160 stored truths agree. 1 misleading measure name: `needed_keys_missing` counts every reported watch key. |
| Malformed requests | `rv-s4-requests` | 0 | 580 episodes, 71,688 requests, 57,222 refusals: no 500; no refused request has an effect of its own (in 21 cases a played clerk's separate, logged action changed the register at the same time); every accepted put is the seat's own report or an allowed summary. |
| Information visibility | `rv-s5-info` | 0 | In 200 response-check episodes, every response to a valid seat request holds only the documented fields, and no response holds another seat's data or token. Seat authentication was not tested (declined part). |
| Seat text | `rv-s6-text` | 0 executable, 6 text precision issues | Identical puts use quota; a refused ready has no `closed` field; the rules status list names more statuses than a report can have. Fable fixed these 3 sentences. |
| Packaging | `rv-s8-pack` | 0 | 54 of 54 sequential parity cases pass; 71 of 72 concurrent cases pass; the 1 failure is the known schedule-dependent `pooler` control. |
| Team schedules and mixed teams | `rv-s7-team` | 3, accepted | 2,940 all-oracle team episodes on 49 schedules; 1,020 end below 1.0. None of the 960 bursty episodes with runs of at most 32 requests per clerk fails. With runs of 64 or more, serial blocks or a long-delayed clerk, the scripted clerks' bounded wait (60 ready polls) can end before the barrier, and a delayed unconditional put can replace a dispute line after a teammate stopped. Puts are unconditional by design and the team arm needs every clerk to wait until close. In the 2,880 mixed-team episodes where the third clerk sends valid ready requests, the 2 oracle clerks lose exactly the answers that the final register and their own reports cannot support; in the other 1,440 (idle, spam, fuzz and a clerk that never sends ready) writing never closes and no oracle answer counts. |
| Text sufficiency | blind implementers `bi-s4` to `bi-s6` (v3 text) | 0 | All 3 reach reward 1.0 in the 18 probe, reader and team conditions. `bi-s5` and `bi-s6` score 0.0 in the 6 solo conditions, as `bi-s2` did: they were written from the probe text and wait for a register that solo does not have. |

### `inquiry` v2.3

| Surface | Worker | Confirmed | What it found |
|---|---|---:|---|
| Routes without the skills | `rv-q1-skills` | 5 | 84 policies, 6,720 episodes. Policies that never compare a reply with the question pass: a calendar prior passes `month-probe` 8/10 (the off month is always the month before); "correct the 5 smallest figures" passes `part-probe` 10/10; "correct the smaller case of each site" 10/10; a plan fixed before the chain correction passes `mixed` 10/10 (the off field after the trap is always `parts`); a ratio policy passes `mixed` 7/10. |
| Careful clerks | `rv-q2-careful` | 0 | 7 independent careful policies reach 1.0 in all 240 tasks each (1,680 episodes), with 12 or 13 questions in `mixed`. |
| Malformed requests | `rv-q3-world` declined; covered by Fable | 0 | Fable ran a scripted request audit on 50 episodes in 5 conditions: 0 status 500; refused requests do not change the budget; repeats return the same reply id; the first filing stays final; `flagged` refusals carry `check`; live state equals replay. |
| First-reply information | `rv-q4-info` | 1 | The same figure-size leak in `part`; crate replies are the smallest figure in 7 of 10 `mixed` instances and the only figures that are not multiples of 40; fault positions in `mixed` are not balanced. |
| Seat text and packaging | `rv-q5-text-pack` | 0 executable, 4 text or usability issues | Correction fields must be 1 to 4 different names; a refused filing does not count. Fable fixed both sentences. Parity passes. |
| Text sufficiency | blind implementers `bi-q1` to `bi-q3` | 0 | On v2.3, all 3 policies reach 1.0 in the 10 probe and flagged conditions of the 5 single facets. They were written from the probe text and do not read the `fields` or `solo` reply forms (0.0 there). The `mixed` rows of this run are not valid: the v2.4 fix started during the run. |

## Fixes after round 2

Each round-2 fix worker reran the reviewers' own policy logic unchanged against the fixed family (the dossier v3 fix, after round 1, changed only how 2 round-1 policies stop on a failed assumption). Counts are exact passes out of 10 probe instances.

### `inquiry` v2.4

Changes: off months drawn from the non-case months with no fixed direction or gap; case months drawn freely; all first figures of `mixed` drawn from 1 interval with no multiple-of-40 grid; part-fault ranks and crate and chain ranks balanced across the 10 instances; the off field after the chain trap drawn per instance from `parts`, `value`, `unit`; `mixed` fault positions balanced (each position off 5 times). The 6 findings are regressions with status `fixed`. Card: 36 PASS, 0 FAIL, 0 NOT_CHECKED.

| Policy (finding) | Facet | v2.3 | v2.4 |
|---|---|---:|---:|
| Calendar prior (`rv-q1-02`) | month | 8 | 0 |
| Correct the 5 smallest (`rv-q1-03`, `rv-q4-01`) | part | 10 | 1 |
| Correct the smaller case of each site (`rv-q1-04`) | part | 10 | 2 |
| Fixed follow-up after the chain correction (`rv-q1-01`) | mixed | 10 | 3 |
| Ratio policy (`rv-q1-05`) | mixed | 7 | 4 |
| Crate as the smallest figure | mixed | 8 | 1 |
| Crate as the figure not divisible by 40 | mixed | 7 | 0 |

The 7 careful policies reach 1.0 in all 240 tasks (1,680 episodes). The 3 blind policies keep 1.0 in all 10 single-facet probe and flagged conditions; `bi-q1` scores 0.95 in `mixed-probe` and `mixed-flagged` because it names 2 unneeded fields and runs out of questions on 4 instances.

Accepted limits, stated in `family.toml` and the pre-registration:

- Sentence length tells a partial or range reply (a shorter or longer sentence passes 7 of 10 in `part` and `range`). Every question asks for all parts and an exact figure, so recognizing the reply form is the intended shape check, not a leak of the hidden fault.
- In `mixed`, a policy that repairs part, range and crate replies by their shape and guesses which reply has the wrong month passes 4 or 5 of 10. Full-blind fixed-field policies pass 0 of 10.
- 2 plans fixed before the second reply pass `mixed` 10 of 10 if the clerk already knows the trap rule: name `month`, `parts` and `value` together and always convert from crates, or correct the original reply a second time. A clerk without that knowledge must read the corrected reply.

### `dossier` v3.1

Changes: in `scarce` and `mix`, every key of the ideal register is needed by an actual question of a clerk that did not write it, and the slot count equals the ideal register (the generator asserts that removing any 1 line makes a question of another clerk wrong); p3 also gets p1's own-report control question, and p2 and p3 each get a question about the other's control; the score counts questions as (clerk, site) pairs; the measure `needed_keys_missing` is renamed `reported_watch_keys_missing` (same value). The 5 `rv-s1` findings are regressions with status `fixed`; the 3 `rv-s7-team` findings are regressions with status `accepted`. Card: 33 PASS, 0 FAIL, 0 NOT_CHECKED; 12 findings replayed.

| Policy class (findings) | scarce v3 | scarce v3.1 | mix v3 | mix v3.1 |
|---|---:|---:|---:|---:|
| 11 `no_select_*` policies, best of them (`rv-s1-01` to `05`) | 0 | 0 | 5 | 1 |
| `full` (all skills) | 10 | 10 | 10 | 10 |

The 7 single-skill omission policies still fail every facet that needs the omitted skill. The 2 remaining `mix` passes are on instance i01, where the write order fills the needed keys before any decoy. 2 reader policies that never read the register pass 0 to 3 of 10 per facet by guesses (`private_zone` 3 of 10 in `dispute`). All 6 blind policies keep 1.0 in all 18 probe, reader and team conditions.

### `depot` v4.1

Changes: `skew` has 4 draw pairs and `storm` 3, each pair tight or roomy by an independent draw after the visible values (all-tight is allowed); `limbo` and `storm` have 5 faults on p1 whose modes are a random order of dropped, dropped, lost reply, lost reply, late. Extra ordinary tickets keep the `decliner` ceiling. Aggregate write budgets: `skew` 44 to 66, `limbo` 36 to 42, `storm` 58 to 76 (equal across the 4 arms of a facet). The 11 `rv-d1` findings are regressions with status `fixed`. Card: 51 PASS, 0 FAIL, 0 NOT_CHECKED; 19 findings replayed (18 fixed, 1 accepted).

Exact chance of a fixed guess per instance, enumerated by the selfcheck: the whole class mask 1/16 in `skew` and 1/8 in `storm` (was 1/2); a fixed retry set 1/10 (was 1/3), or 1/6 if the late position is known.

| Policy (finding) | Facet | v4 clean | v4.1 clean |
|---|---|---:|---:|
| `first_cas` (`rv-d1-01`) | skew | 4 | 1 |
| `second_cas` (`rv-d1-02`) | skew | 6 | 2 |
| `slack_cas` (`rv-d1-03`) | skew | 6 | 0 |
| `combined_cas` (`rv-d1-04`) | skew | 6 | 0 |
| `margin_guess` (`rv-d1-05`, `06`) | skew, storm | 5, 4 | 0, 1 |
| `high_take_guess` (`rv-d1-07`) | storm | 5 | 0 |
| `first_unknown_retry` (`rv-d1-08`, `09`) | limbo, storm | 6, 3 | 0, 0 |
| `last_unknown_retry` (`rv-d1-10`, `11`) | limbo, storm | 3, 4 | 0, 0 |

The 5 sound narrower routes (`peer_only`, `selective_log`, `targeted`, `version_ack`, `refusal_reply`) stay clean 10 of 10 in all 6 probe facets. All 6 blind policies reach reward and clean 1.0 in all 18 probe, solo and team conditions; `bi-d6` still stops on a guard refusal in 3 guard conditions. The oracle needs at most 88 requests in `storm-probe` and 115 in `storm-solo` (was 57 and 79); see `COST.md` for the time cap.

## Built families, round 3 (fixed versions)

Fresh workers (Codex `gpt-6.2-sol`) reviewed the fixed versions and were told not to repeat the round-2 policies.

### `depot` v4.1

| Surface | Worker | Confirmed | What it found |
|---|---|---:|---|
| Routes without the skills | `rv3-d-skills` | declined | The worker declined to write policies that reach a clean score without the skills and offered a measurement instead (`INCIDENTS.md`). Not sent again. |
| Information in the instances (measurement only) | `rv3-d-indep` | 0 executable, 3 accepted observations | The fixed-guess chance numbers are right. 3 small dependences: the late fault is always on an item with no teammate ticket and the delta draw is rejected by stock layout, so the late role has a slightly different delta distribution; in `storm` 1 unfaulted own adjustment shares its item with an inherited ticket; batch validation limits the joint class and mode distribution. The worker found no visible value that tells a pair class or a fault mode. |
| Careful clerks | `rv3-d-careful` | 0 | 5 independent careful policies reach reward, clean and `legs.p1` 1.0 in all 24 conditions, 10 instances, also with `--worst 24` in `team` (1,500 episodes); independent write accounting holds in 100 of 100 checked episodes. The most thorough policy needs up to 206 requests in `storm-solo` and 68 in `limbo-probe`; the smallest write margin is 6. |

INFERRED: The late-fault observations give no route around a skill: a clerk learns a fault's mode only by reading the log after the 504 (`dedupe`), and a retry of a late write is safe only with `expect` (`cond`). The 206-request maximum of a careful clerk makes the 900 s time cap a risk (`COST.md`).

### `dossier` v3.1

| Surface | Worker | Confirmed | What it found |
|---|---|---:|---|
| Routes without the skills | `rv3-s-skills` | 8 | 1,220 episodes. A reader that drops the incoming withdrawal, closure or dispute lines and, for an asked site with no site line, predicts the dropped status from p1's own private markers passes `gone-probe`, `cascade-probe` and `dispute-probe` 10 of 10 and `mix-probe` 3 or 10 of 10. Cause: the incoming special kind matched p1's own special kind in each single-status facet, and "no site line" meant "special" in every instance. A zone-density selection without the watch list and a reader that never reads the register pass `mix-probe` 3 of 10 each. Sound narrower routes: `local_writer` (writes without a first register read and trusts the played clerks' repair) and a one-pass `full`. |

### `inquiry` v2.4

| Surface | Worker | Confirmed | What it found |
|---|---|---:|---|
| Routes without the scope check | `rv3-q-skills` | 4 in-sample | 125 single-facet and 24 `mixed` selectors that never compare scope (2,980 episodes): 29 selector and facet cells pass more than 2 of 10. 4 representative findings: figures close to the other month of the same site (`month` 4/10), figure remainder by the crate factor (`part` 4/10), many distinct digits and a small gap between the 2 months (`range` 5/10 each). The worker said these are in-sample counts after a search and asked for a held-out test. |
| Held-out replication | `q4-holdout` | 1 mechanism | The rule was fixed before the run: a selector shows a generator dependency if, on 50 new instances per facet (5 new seeds; 1 seed rejected by the generator's own balance check), it beats the best fixed-position control by at least 0.15 and by 2 standard errors. Only the crate-factor remainder replicates: `month` 23/50 against 11/50, `part` 25/50 against 10/50. Cause: crate and chain totals must be whole crates, so a right figure is more often a multiple of the factor than a wrong one. The other 3 findings do not replicate (10/50, 10/50, 8/50 against controls of 11 to 13). The oracle scores 1.0 in all 1,200 held-out episodes. |

INFERRED: In-sample counts on 10 instances, after a search over many selectors, overstate the information in a generator. A held-out cohort from new seeds, with a decision rule set before the run, separates a real dependency from a chance pattern. This test belongs in the review process for every family.

## Fixes after round 3

### `dossier` v3.2

Changes: each single-status facet asks p1 about an unreported watch site whose answer is `unknown`; incoming and outgoing special cases are assigned independently within the facet's skills; closure cases keep the older site report, so the newer zone line must win; `mix` has 24 sites, 12 off-watch reports and 3/5/7 questions, with 7 withdrawals, 7 closures and 6 disputes as incoming cases over the 10 instances. The generator asserts that removing the deciding special line changes the answer, and that an asked site without a site line can have the answer `unknown`. The 8 `rv3-s` findings are regressions with status `fixed` (20 findings in total). Card: 33 PASS, 0 FAIL, 0 NOT_CHECKED.

| Policy (finding) | Facet | v3.1 | v3.2 |
|---|---|---:|---:|
| Reader without withdrawal lines (`rv3-s-01`, `04`) | gone, mix | 10, 3 | 0, 0 |
| Reader without closure lines (`rv3-s-02`, `05`) | cascade, mix | 10, 10 | 0, 0 |
| Reader without dispute lines (`rv3-s-03`, `06`) | dispute, mix | 10, 3 | 0, 0 |
| Zone-density selection (`rv3-s-07`) | mix | 3 | 0 |
| No register read (`rv3-s-08`) | mix | 3 | 0 |

`full` and `local_writer` pass 10 of 10 in all 12 probe and reader conditions; the 11 round-2 policies without selection pass 0 of 10 in `scarce` and `mix`; the 8 careful policies score 1.0 in all 24 conditions; the 6 blind policies score 1.0 in all 18 conditions other than solo (3 of them still wait for a register in solo). The `rv-s7` stale-dispute regression moved to another schedule (`bursty:524:128`) with the same mechanism. The oracle needs at most 25 requests in `mix-probe` (was 21).

Chance-baseline review of v3.2 (`rv4-s-split`, 9,920 episodes on the 10 built and 30 held-out instances per condition): a reader that removes every line of the special kind that its facet needs and then splits its look-alike questions by a fixed rule passes about half of the instances that need the kind (site number, question order, zone order, neighbour times: 2 to 8 of 10, 11 to 20 of 30), because each instance has 1 special and 1 `unknown` question. The rule "the site that p1 reported itself is the special one" passes `gone`, `cascade` and `dispute` 10 of 10 and 30 of 30, because every incoming special site had a private report and the `unknown` site never has one. The finding tool confirmed 112 cells.

### `dossier` v3.3

Changes: for each incoming special kind that an instance needs, p1 is asked about 2 sites of that kind and 2 look-alike alternatives (2 unreported sites with the answer `unknown`; in `cascade`, 2 sites with an ordinary line of the same draw law as the 2 stale closed site lines); p1 has no private report on an incoming special site; `mix` has 9 zones of 4 sites and 2 incoming sites of each special kind. The generator asserts, per instance and per needed kind, that a reader that removes that kind and assigns it at random inside groups of questions with the same visible signature (site line present, its writer, p1 private report, surviving zone closure, line status) is fully right with chance at most 1/6; all 240 checks on the built and 3 held-out cohorts give exactly 1/6. 6 representative `rv4-s` findings are regressions with status `fixed` (26 findings: 23 fixed, 3 accepted). Card: 33 PASS, 0 FAIL, 0 NOT_CHECKED.

All 31 `rv4-s` policies pass 0 of 10 and 0 of 30 held-out in every condition that needs their omitted kind (they pick 1 target, and v3.3 needs 2); a policy that picks 2 of the 4 look-alikes at random has the generator's chance 1/6. No round-2 or round-3 cell that was at most 2 of 10 rises; the 8 careful policies and every blind cell that was 1.0 stay 1.0. The oracle needs at most 31 requests in `mix-probe` and 22 in `cascade-probe`; every budget fits the careful policies. Sequential local HTTP parity of the 6 team conditions exceeds its 120 s limit per condition; concurrent parity passes 24 of 24.

### `inquiry` v2.5

Changes: every displayed figure is a whole number of crates (amounts, monthly totals, part shares and range offsets on the factor grid; chain totals on the factor-squared grid). The generator asserts remainder 0 for every first figure and bounds the gap in the share of figures divisible by the factor squared between faulty and right replies at 15 points per facet. 2 new regressions (`month` and `part` remainder) with status `fixed` (8 findings). Card: 36 PASS, 0 FAIL, 0 NOT_CHECKED.

The same held-out test with the same rule on 5 new v2.5 seeds (none rejected): no round-3 selector in `month`, `part`, `range` or `mixed` shows a dependency. The remainder selectors fall to their controls (`month` 10/50 against 10/50, `part` 12/50 against 12/50). The 7 careful policies score 1.0 in 1,680 episodes; no blind cell that was 1.0 drops. Remaining signals on the 10 built instances: sentence length passes `part` 5 of 10 and `range` 8 of 10 (the reply form shows the scope, as intended); a policy that repairs part, range and crate by form and guesses the month passes `mixed` 6 of 10 (was 4).

## Round 5 on the landed versions (held-out instances)

Fresh workers reviewed `main` at `78c455390` on the built instances and on new seeds (a scratch copy of the generator; the lab builds instances from the generator). Each fixed a decision rule before the run.

| Family | Worker | Result |
|---|---|---|
| `depot` v4.1 | `rv5-d-holdout` | 0 findings. On 3 new seeds (21 of 24 seeds fail the generator's own batch checks), the oracle, the 5 careful policies and the 5 sound narrower routes score 1.0 in every held-out episode; no stored guessing policy exceeds its exact chance by 2 standard errors (12,720 episodes). |
| `dossier` v3.3 | `rv5-s-skills` | 1 open finding. A writer that never reads the watch list and ranks reports by peer occupancy, the time band of current reports and its own report counts passes `mix-probe` 5 of 10 built, 19 of 30 held-out and 52 of 100 further held-out instances. Cause: `mix` has exactly 2 watched sites per zone, and current watched reports are drawn from times 21 to 40 while off-watch reports are drawn from 1 to 40. 8 reader cells passed more than 2 of 10 and 6 of 30, but on 100 further instances they score 12 to 23 of 100, near the chance of 1/6 (16.7). |
| `inquiry` v2.5 | `rv5-q-skills` | 0 findings. 218 new selector cells (grid position, divisibility by other numbers, digit sums, range density, chain arithmetic) on 50 held-out instances per facet: none meets the rule. |

INFERRED: `mix-probe` measures watch-list selection only above a baseline of about 1 in 2 for a writer that uses this layout. Fix before the pilot: draw off-watch report times from the same law as watched reports, and vary the number of watched sites per zone.
