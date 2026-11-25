# `dossier`: does a team keep a small shared register sufficient for questions it does not know yet?

Status: design v3.4 (2026-11-25, Fable, muster lane). v2 replaced v1 after an independent review
(12 defects, `REVIEWS.md`). v3 applied the first review of the built family.
v3.1 made every ideal scarce register line necessary for another clerk's question.
v3.2 added unknown controls and restored historical closure reports.
v3.3 matches incoming special questions to unreported or ordinary alternatives and tests the
exact assignment chance. v3.4 matches the draw law and placement of current ordinary watched
and off-watch reports in scarce and mix. The world rules, score and executable claims are unchanged.
Nothing here was run with a model. Section 0 records the history; the later sections describe v3.4.

## 0. Version 3 changes (review rounds of the built family)

| Change | Why (review) |
|---|---|
| Decoy (off-watch) reports get times from the same range as real reports and random feed positions. The generator asserts that feed order, reverse feed order, oldest first and newest first, each without a watch filter and without drops, fill the register before p1's last needed key, on every `scarce` and `mix` instance. | The 5 decoys always had times 30 to 34, newer than every real report: writing all reports oldest first let the full register refuse exactly the decoys (`rt-s1` finding 01; `reverse` also passed). |
| The tie that decides a played clerk's focal ask in `dispute` (and the dispute case of `mix`) is between 2 of p1's own reports: same site, same time, 2 statuses. No played clerk holds them, so only p1 can mark it. | The played clerks marked every tie, so p1 never needed to (`rt-s1` finding 02). |
| Zone order and site order in `GET /rules` are shuffled independently of the roles; questions and special cases are assigned to sites at random; ordinary statuses are drawn independently; times are drawn independently of the kind of report. The generator asserts counterfactual dependence: for each played clerk's focal ask, all feeds without p1 give a wrong status; for each p1 ask that needs the register, p1's feed alone gives a wrong status. | Roles followed from the position of a site in the public zone lists; ordinary statuses came from 1 shared cycle; times told the special case (3 closed, 20 withdrawn, 25 disputed). Policies that never read the register passed (`rt-s1` findings 03, 04). |
| Seat text v3: played clerks "also work on this survey" (they act at fixed points, not in parallel); a refused put uses no put; a clerk that stops before the writing phase ends cannot answer; there are enough register lines for a sufficient register; in `reader`, the register with the seat's own reports is enough; any status that the rule gives is a legal answer; `solo` does not mention register lines. | `b4b` "Text against world", `rt-s6` T01 to T06, the blind implementers' notes (`bi-s1` to `bi-s3`). |
| Instance ids are `i01` to `i10`; `focal` in `reader` counts only the real seat's own asks, so an idle seat scores `focal` 0.0 in all 24 conditions; waiting policies send `ready` at most 60 times; a share is present only when its count is above 0. | `b4c`, `b4d`. |
| Every confirmed finding of the review is stored in `muster/tasks/dossier/findings/` with status `fixed` or `accepted`; the card replays them. | Every bug found becomes a recorded regression. |

Not changed, by decision: the played clerks still follow and repair by the correct protocol for ordinary reports, so `probe` measures p1's own writing only where no played clerk holds the evidence. Team repair is a property of the `team` arm, not a defect.

v3.2 drew incoming and outgoing case assignments separately within each facet's promised skills,
before candidate rejection. Every instance of a special facet asked p1 about an unreported watch
site outside the closed zones (truth `unknown`). The generator checked that removing the deciding
incoming special line changed the answer by the rule. It also checked that at least 1 asked site
with no ideal site line had a true status different from p1's private statuses and `disputed`.
The 8 round-3 findings were fixed on the 10 fixed instances; all 12 prior findings retained their
statuses. The v3.2 card passed all 20 finding regressions.
In v3.3, 6 new findings are fixed and the 20 earlier findings retain their status (`REVIEWS.md`).

In v3.2, the private-present split passed 10 of 10 in standalone probe and reader conditions and
passed most mix instances. In v3.3, no incoming special question has a private p1 site report.
Each needed special kind has 2 incoming targets and 2 matched alternatives. The generator removes
every line of that kind from the ideal register. It groups p1's questions by surviving site line
presence, last writer, private site report presence, surviving zone closure, and surviving site status.
It asserts that the product of inverse binomial coefficients for placing the answers of that kind
within these groups is at most 1/6. This is the chance for uniform assignments conditional on the
signatures and class counts, not a universal shortcut bound. The round-4 review reports no full
passes by any of the tested split policies in cells that omit a needed kind (10 original and 30
held-out instances per condition). It uses held-out seeds `dossier-v3.3-h1`, `dossier-v3.3-h2`
and `dossier-v3.3-h3`. The full policy passes all of them. See `REVIEWS.md`.

Round 5 finds a replicated writer dependency in v3.3: a writer without the watch list passes mix
5 of 10 built, 19 of 30 held-out and 52 of 100 further held-out instances. It ranks by peer
occupancy, time band and own report count. v3.4 uses the same current time law for watched and
off-watch ordinary reports, and removes the fixed 2-watch-site quota per zone in mix. The same
writer scores 0 of 10 and 2 of 30; all 7 frozen no-watch writer variants score 0 of 10 in scarce
and mix and at most 2 of 30 held-out in either. The 38 reader rankings are chance controls,
not a deterministic upper bound on reader scores. The exact reader signature chance remains 1/6
on the built and tested held-out cohorts. The generator checks that it is at most 1/6.
See `REVIEWS.md`.

## 1. The construct and the prior work

Clerks survey sites. Each clerk has private reports. The team has one shared register with a small number
of lines. First the team writes. Then the register is frozen and each clerk gets questions about the
current status of watch-list sites. The questions are chosen by the instance and hidden while writing.
The register, together with each clerk's private reports, must support the questions that the clerks
do not know yet. Every single step is easy: compare 2 times, copy a report. The family measures whether
an agent stores what its teammates will need, in a form they can use, checks the register before it
says it is done, and reads the register correctly.

Prior work (read by a research worker on 2026-11-25; numbers are the authors', not reproduced):
- Multi-Agent Memory Inconsistency (2026-11): with split workers GPT-6 Luna retrieves both sides of a
  conflict in 61.6% of cases but the final answer flags the conflict in 11.6%.
- MEME (2026-05, 1 agent): Claude Opus 4.7 handles a deleted fact in 80% but a dependent fact that an
  update made obsolete in 32%.
- Agents Cannot Hand Over (2026-11): gpt-6-astra reaches the goal in 65% of late relay lives against 82%
  with its own full history; notes hold about 50% of the needed facts.
- Unprompted coordination through shared memory (2026-11): gpt-6-astra reads teammates' entries in 50 of
  50 worlds, claude-sonnet-5 in 2 of 50; without the re-check clause gpt-6-astra drops to 1 of 50.
- No evaluation in the survey combines a bounded shared store, several concurrent writers, a check of the
  stored state without a model judge, and a one-agent-sees-all control. This family is a structured,
  deterministic instrument for that gap. It does not test free-text notes or long histories.

Skills, each with its own facet and paired control:

| Skill | The failure when it is missing |
|---|---|
| `pool` | A report that decides a watch-list site never reaches the register (base behaviour) |
| `select` | The clerk stores reports about sites off the watch list; the register fills up; needed lines do not fit |
| `tombstone` | A withdrawal is not stored, or is read as "no information": the old status survives |
| `cascade` | A zone closure is not stored, or not applied when reading: older site lines of that zone look current |
| `dispute` | Reports with the same time have different statuses; the clerk stores or keeps only one side: the conflict disappears |

The skills certify named policies on fixed instances, not all possible ways to pass. The round-4
review tested 31 policies. All split policies that omit a needed kind have 0 full passes on the
10 original and 30 held-out instances in each applicable condition. The split policies select at
most 1 candidate for the omitted kind, but v3.3 needs 2. The chance for a uniform assignment of
2 among 4 matched questions is 1/6. This does not bound readers that use other evidence or rankings.
Earlier low-probe cells remain at most 2 of 10; `full` and `local_writer` remain at 10 of 10.
These are measured counts on the fixed instances, not population probabilities (`REVIEWS.md`).

The tested v3.4 no-watch writer policies meet the predeclared thresholds on the 10 built and
30 held-out instances. The generator matches current ordinary time laws and samples mix watch
roles without a fixed zone quota. This removes the replicated v3.3 cue; it is not a proof that
every possible writer has at most chance performance after generator rejection.

## 2. World rules (exact)

Sites belong to zones. The watch list is a public list of sites. An entry is `{"key", "status", "at"}`:
`key` is a site or a zone, `at` is a whole number (the time of the observation).
- A site entry has one of the 6 ordinary statuses, for example `{"key": "s03", "status": "open", "at": 5}`.
- A withdrawal is `{"key": "s04", "status": "withdrawn", "at": 8}`: the site left the survey.
- A zone closure is `{"key": "north", "status": "closed", "at": 9}`: every site of the zone was closed then.
- A register line can also be `{"key": "s03", "status": "disputed", "at": 7}`: reports of that time disagree.

The rule (`status_of`): from a set of entries take the entries about the site and the closure entries
about its zone. No such entry: `unknown`. Otherwise the entries with the highest `at` decide: when
they all have the same status, that status; otherwise `disputed`. A later entry always wins, whatever
its kind. The true status of a site is the rule over the reports of all clerks.

Phases: writing, then answering. Asks are not visible while writing.

| Request | Effect |
|---|---|
| `GET /rules` | Kit rules, plus `"zones"`, `"statuses"`, `"watch"`, `"slots"`, `"clerks"`, `"phase"` |
| `GET /feed` | The seat's private reports: `{"reports": [entry, ...]}` |
| `GET /register` | `{"lines": {key: {"status", "at", "by"}}, "free": n, "version": n, "phase": "writing" or "answering"}`. `version` goes up by 1 with every change of the register. 404 in `solo`. |
| `POST /put {"key", "status", "at"}` | Writes the line of that key; replaces a line of the same key whoever wrote it and whatever its time. The entry must be one of the seat's own reports, or `disputed` with the key and the `at` of one of the seat's own site reports: 403 otherwise for a well-formed entry. A zone line can only be `closed`; a site line can have an ordinary status, `withdrawn` or `disputed`; other statuses get 400. A new key needs a free line: 409 `{"error": "register full"}`. Only while writing: 409 otherwise. Each successful put uses 1, also an identical put (which does not change the version); a refused put uses none. |
| `POST /drop {"key"}` | Removes a line that the seat itself wrote last (403 otherwise). Only while writing. |
| `POST /ready {"version": n}` | The seat says that it is done with the register as it is at version `n`. 409 `{"error": "the register changed", "version": current}` when `n` is not the current version. Any later change of the register clears every ready mark. When every clerk is ready at the current version, writing closes: `{"ready": true, "closed": true}`. Otherwise an accepted ready returns `{"ready": true, "closed": false, "waiting_for": [clerks]}`. A refused ready has no `closed` field. |
| `GET /asks` | While writing: `{"phase": "writing", "asks": []}`. After: the seat's own asks. |
| `POST /answer {"site", "status"}` | Only while answering; one of the seat's own asks; the first answer of that seat for a site is final. Any of the 10 statuses the rule can give is legal. Budget kind `answer`. |

There is no message channel in this family: the register is the channel.

## 3. Arms

| Arm | Real seats | Teammates | Use |
|---|---|---|---|
| `probe` | `p1` writes and reads | world-played clerks that follow the correct protocol | deterministic test of 1 model |
| `reader` | `p1` reads and answers: writing is closed from the start. World-played clerks preload the ideal register in p2, p3, p1 order. P1 also has its own private feed. | world-played | is the loss on the reading side? |
| `solo` | `p1` has every report and 1 ask for every distinct watch site; there is no register; phase `answering` from the start | none | control: does the model apply the rule at all? (not a matched team control) |
| `team` | `p1`, `p2`, `p3` | none | real team |

## 4. Facets and the skills of the named policies

Every facet has a peer question that needs p1's report. Share and scarce have 2 peer-dependent
questions and 1 private ordinary question for p1. Gone and dispute have 2 incoming special
questions and 2 unknown questions, with an incoming ordinary and a private ordinary control.
Cascade has 2 incoming closures, 2 same-status ordinary alternatives, 2 unknowns and the other
2 ordinary controls. Mix has 2 incoming questions of each of the 3 special kinds, 2 unknown
controls, and an incoming ordinary question with a stale private report. The generator assigns
incoming and outgoing roles at random, subject to the layout and dependency constraints.

| Facet | The writer case and the reader case of p1 | Sufficient skills in the probe matrix |
|---|---|---|
| `share` | A plain site report. Slots are ample. | `pool` |
| `scarce` | As `share`, but slots equal the number of keys in the ideal register. Each key matters to another clerk's question. p1's feed contains 5 off-watch reports at drawn positions. | `pool`, `select` |
| `gone` | Writer: p1 holds a withdrawal of a watched site with an older peer report. Reader: p1 has 2 peer withdrawal lines and 2 unreported alternatives. No private site report distinguishes them. | `pool`, `tombstone` |
| `cascade` | Writer: p1 holds a closure newer than the asking clerk's site report. Reader: 2 incoming closure lines override older p2 site lines. P1 also has 2 matched ordinary p2 site lines and 2 unknown questions. | `pool`, `cascade` |
| `dispute` | Writer: p1 holds 2 reports with the same site and time but different statuses. Neither belongs to a played clerk. Reader: p1 has 2 disputed peer lines and 2 unreported alternatives. | `pool`, `dispute` |
| `mix` | All 5 skills, scarce slots, 2 incoming cases of each special kind, 2 unreported alternatives. Each incoming special and unknown anchor site is in a distinct zone. The remaining watched roles are sampled over the other sites without a quota for each zone. The raw feeds retain all 3 old closure-site reports. The ideal omits their strictly dominated keys; the slots allow retaining them. Each ideal key matters to another clerk's question. | all 5 |

A condition is `<facet>-<arm>`: 24 conditions, 10 instances each (240 tasks). The generator uses
seed `dossier-v3.4` and instance ids `i01` to `i10`. Each facet and instance index has its own
deterministic draw stream. The 4 arms of a facet share reports, watch list and truth. The non-solo
arms share asks; `solo` merges the reports and asks each distinct site once.

## 5. World-played clerks in `probe`

A played clerk follows the correct protocol: for each of its reports that decides a watch site by what it
knows, it puts the report when the register has no line for that key that is newer, or as new with the
same status or `disputed`; when the line is as new and has another status it puts `disputed`. It never
stores a key that the watch list does not need. The played clerks do this once at the start (in clerk
order), and again each time p1 sends `ready` for the current version (the teammate pass). When the pass
changed the register, p1's `ready` gets the 409 of a changed register. Otherwise a played clerk that
cannot store a needed line stays unready. At the close, when writing is still open, the world runs the
pass once more, closes writing, and then every played clerk answers its asks by the rule from its own
reports and the register. A real clerk that stops before writing closes cannot answer after the forced
close. The world plays no race against p1 in this family (`depot` covers races).

So a played clerk repairs what a correct teammate could repair (p1 overwrote a newer line; p1 put its
side of a tie over the other side). It cannot repair what only p1 can do: store p1's own deciding
reports, mark a tie that only p1 can see, free the lines that p1 filled.

## 6. Score and measures (the verifier reads only the event log)

- `reward` = right answers / asks, over every clerk. Each (clerk, site) question is counted separately.
  An ask with no answer is wrong.
- `focal` = right / total over the real seats' own asks and, in `probe`, the played clerks' asks
  whose true status cannot be derived from the other played feeds without p1. An idle real seat
  scores 0.0. In `reader` only p1's own asks count; in `team` and `solo` it equals `reward`.
- `reader_right` = share of the real seats' own asks that are right (with the count `reader_asks`).
- `writer_right` = share of the played clerks' focal asks that are right (with the count `writer_asks`).
  A share is present only when its count is above 0.
- `register_sufficient` = share of all (clerk, site) questions for which the rule, applied to the
  asker's own reports and the frozen register, gives the true status: the stored state, checked directly.
- Counters (not anomalies): `lines_off_watch` (final) and `peak_off_watch`, `used_off_watch` (1 when
  the peak is above 0), `refused_puts`, `reported_watch_keys_missing` (distinct reported watch-relevant
  keys absent at close, 0 in `solo`), `ready_calls`, `register_reads`, `reads_after_last_change_before_ready`,
  `forced_close` (1 when writing was still open at the close), `answers_unknown`.

## 7. Policy zoo

Every policy receives only its seat name. Reader deficits are part of each skill.

| Policy | Skills | Behaviour |
|---|---|---|
| `hoarder` | none | Puts nothing, sends `ready`. Answers by the rule from its own reports only. |
| `pooler` | `pool` | Puts each of its site reports with an ordinary status, in feed order, watch list or not, when the register has no line for that key that is as new. Reads the register and sends `ready` until writing closes or the poll bound ends. Reads: the newest ordinary site entry of its reports and the register; it skips withdrawals, zone entries and `disputed` lines, and on a tie keeps its own. |
| `selector` | `pool`, `select` | `pooler`, but only watch-relevant keys. |
| `burier` | `pool`, `tombstone` | `pooler`, and it stores and reads withdrawals. |
| `closer` | `pool`, `cascade` | `pooler`, and it stores zone closures of zones that hold a watch site, and applies zone entries when it reads. |
| `umpire` | `pool`, `dispute` | `pooler`, and it puts `disputed` on a tie with another status, and reads ties and `disputed` lines by the rule. |
| `oracle` | all 5 | Repairs by the protocol of section 5 before each `ready` until writing closes or the poll bound ends, then answers by the rule if writing closed. |
| `cleaner` | all 5 (other route) | `oracle`, but first publishes without the watch filter for sites, then frees its own off-watch lines when the register is full or writing has not closed. |
| `own_first` | near miss | `oracle` as writer; as reader it answers from its own report whenever it has one for the site. |
| `feed_order`, `ascending`, `reverse` | capacity controls | Write without filtering the watch list, in feed order, oldest first or reverse feed order. |
| `idle`, `spam`, `fuzz` | degenerate | Reads `/rules` only; puts every report it has in reverse order and never sends `ready`; sends malformed bodies. |

## 8. Claims that the card must prove

1. `oracle`: `reward` = 1.0 and `register_sufficient` = 1.0 in all 24 conditions, every instance, every
   explored card schedule (not arbitrary starvation).
2. Skills matrix in `probe`: on the 10 fixed instances, each of {`hoarder`, `pooler`, `selector`,
   `burier`, `closer`, `umpire`, `oracle`} scores `reward` 1.0 when its skills contain those listed
   for the facet; otherwise below 1.0.
3. Other route: `cleaner` scores 1.0 in `scarce-probe` and `mix-probe` with `used_off_watch` = 1.0.
   `ascending`, `reverse` and `feed_order` score below 1.0 in both scarce probe facets; `ascending`
   and `feed_order` also score below 1.0 in `dispute-probe`.
4. Near miss: `own_first` below 1.0 in every `probe` and `reader` facet.
5. `reader` arm: `oracle` = 1.0; `pooler` below 1.0 in `gone`, `cascade`, `dispute`, `mix`; `burier`,
   `closer`, `umpire` = 1.0 in their own facet (`gone`, `cascade`, `dispute`).
6. `solo` arm: `oracle` = 1.0; `pooler` below 1.0 in `gone`, `cascade`, `dispute`, `mix`.
7. `team` arm: `oracle` = 1.0 under every card schedule; `hoarder` below 1.0 in every facet.
8. Floors on `focal`: `idle` = 0.0 everywhere; `spam` at most 0.5 in `probe`; `fuzz` causes no 500.
9. Built in: determinism, exact replay, live state equals replayed state, no token of another seat in
   a response, every zoo policy ends, every credited answer matters. Put, drop and ready are not
   credited score events. Private feeds are visible only to their clerk, except for register lines
   and the combined feed in `solo`.

The v3.4 card reports 33 PASS, 0 FAIL, 0 NOT_CHECKED. Its finding-regression check passes all
27 findings. The card records 14,400 episodes with schedule seed 0 and schedule parameter 12.
See `REVIEWS.md` for the build and review evidence.

## 9. Instance construction and constraints

The generator draws candidates, rejects those that fail its dependency and capacity checks,
and asserts the instance and condition constraints below.

| Facet | Zones × sites per zone | Watch sites | Asks p1/p2/p3 | Ideal lines | Slots | Off-watch reports |
|---|---:|---:|---|---:|---:|---:|
| `share` | 4 × 4 | 9 | 3/3/3 | 9 | 15 | 0 |
| `scarce` | 4 × 4 | 9 | 3/4/5 | 9 | 9 | 5 |
| `gone` | 4 × 4 | 12 | 6/3/3 | 10 | 16 | 0 |
| `cascade` | 6 × 5 | 14 | 8/3/3 | 15 | 21 | 0 |
| `dispute` | 4 × 4 | 12 | 6/3/3 | 10 | 16 | 0 |
| `mix` | 9 × 4 | 18 | 9/5/8 | 16 | 19 | 18 |

- Share, scarce, gone and dispute have 16 sites; cascade has 30; mix has 36.
  There are 6 ordinary and 4 special statuses, and 3 clerks in the non-solo arms.
  Times are whole numbers in 1..40. No 2 reports of 1 clerk in the non-solo arms share key,
  time and status. The merged solo feed can contain copies of the same report. The same key
  and time with different statuses is permitted.
- Solo asks once per watch site and has 0 register slots. In scarce, p3 also gets p1's private
  control question. In mix, p3 also gets both of p1's unknown questions. In both facets, p2 and p3
  each get the other's third question from the role assignment before the final ask lists are shuffled.
- Every special instance contains exactly 2 unreported watched sites with truth `unknown`.
  In probe, each peer has at least 1 focal and 1 nonfocal question.
  Each incoming special question depends on a peer, has no private p1 site report, and changes
  answer when its deciding special line is removed from the ideal register.
- Cascade places the 4 matched sites in 4 separately drawn zones, each with exactly 1 watched
  site. A fifth zone contains the outgoing closure and 4 other watched sites. The sixth zone
  contains the 2 unknowns and 3 other watched sites. All 4 matched site lines come from p2 and
  have the same randomly drawn ordinary status. Each of their times is independently drawn from
  1..20. The 2 incoming closure times are drawn from 21..40. P2 publishes before p3, so the
  old site lines survive in the ideal. Without the closure lines, the 2 targets and 2 alternatives
  have the same signature. Removing a deciding closure changes the answer to the older p2 status.
- Mix draws one anchor site in each of 9 zones. It permutes these anchors among 2 cases of
  each incoming special kind, 2 unknowns and 1 outgoing closure. The other 9 watch roles
  are uniformly sampled without replacement from the remaining 27 sites. There are 18 watched
  and 18 off-watch sites, but watched sites per zone vary from 1 to 4. Mix draws early times
  from 1..10, closure times from 11..20, and all current site times from 21..40. An off-watch
  site has one ordinary current p1 report, drawn from the same distribution as a watched
  ordinary current report before candidate rejection. The peer that holds each incoming closure
  also holds its older site report. P1 holds both reports of the outgoing closure. The outgoing
  dispute has 2 p1 reports at the same latest time, with an older report of the asking clerk
  (not at the tie time). In standalone dispute the asking clerk holds neither side of the
  deciding tie.
- Scarce and mix slots equal the number of distinct reported watch-relevant keys. Each ideal
  key matters to another clerk's actual question. Scarce has 9 ideal keys and 9 slots. Mix has
  16 ideal keys and 19 slots; its 3 omitted site keys are strictly older than a zone closure.
  In scarce there are 5 off-watch reports; in mix every off-watch site (18) has 1 ordinary current
  p1 report. Scarce current site times are uniform 2..40, with a witnessed predecessor uniform
  from 1 to the current time minus 1. Both watched and off-watch current times use the same
  draw function. All feeds are shuffled. The generator asserts that W (4 watched sites with
  a normal p1 writer role) and D (5 or 18 distinct off-watch p1 keys) are disjoint, have exactly
  one ordinary current p1 report per key, and use the facet's common current interval. For every
  zone Z the count of W keys plus the count of D keys equals the count of their union in Z;
  the union in Z is contained in the sites of Z outside `watch - W`, and equals that set in mix.
  This checks the site partition and time support, not equality of random realized histograms.
  In mix, conditional on the other watch roles, uniform placement matches normalized expected
  zone counts before rejection. In each of the 4 raw write orders in section 0, at least 1
  needed key is absent when scarce fills, at least 2 when mix fills. Other facets have 6 slots
  beyond reported watch-relevant keys.
- The generator asserts full ideal sufficiency, class assignment chance at most 1/6 per incoming
  kind, and the 0.34 focal answer-frequency ceiling across the 10 instances of a condition.
  The focal answers `withdrawn`, `closed` and `disputed` occur only in the facets that need them.
  `unknown` appears only in the special facets.
- Put budget per clerk: its number of private reports plus 4. Answer budget: its number of asks.

## 10. What the family does not claim

- It does not show that any model fails. Only a run can show that. `share`, `scarce` and `gone` are close
  to rule following and are the calibration floor; `dispute`, `cascade` and `mix` are where the prior
  work suggests lost points.
- It tests a structured store with given times. Free-text notes and long histories (what the handover
  papers study) need a real reader and are not in this version.
- A failure can have other causes than a missing team-memory behaviour: a wrong reading of the rule (the
  `solo` arm separates it), a reading mistake with a perfect register (the `reader` arm separates it),
  tool calls sent in 1 batch before any reply is read (the step join of `muster events` separates it).
- The world checks that a line is one of the writer's own reports or an allowed dispute summary
  of one. The family is not a test of honesty.
- Team closure needs every clerk to wait until writing closes. Puts are unconditional, so a delayed
  put can overwrite a newer or dispute line after a teammate stops. The zoo bounds its wait at 60
  ready polls; the card covers explored schedules, not arbitrary starvation (`REVIEWS.md`).
- A standalone facet fixes the allowed special kinds to preserve the single-skill controls.
  It does not fix the status of a particular asked site. Mix has 2 incoming cases of each kind
  in every instance. The 2 shared unknowns act as alternatives for each omitted kind.
  The signature bound assumes uniform assignment within the observed class. It does not prove a
  cap for every ranking on a fixed sample or independence from all API observations. The paired
  arms and fixed counts are not independent evidence of model behavior.
- Restoring strictly dominated observations in mix leaves 3 slots that a correct minimal register
  may not use. All ideal keys still matter to actual peer questions. The tested selection heuristics
  fail on their target cells, but another ordering may succeed. The accepted stale-dispute limit
  remains: an unconditional `put` can overwrite a correct line after the knowledgeable clerk stops.
- The writer matching construction gives the same current time law for ordinary watched and
  off-watch reports. It assigns variable watch counts across zones, not identical realized
  counts for the two classes. Capacity rejection can condition these draws. The frozen no-watch
  writer policies are empirical controls. Their full-pass counts do not prove a bound for any
  possible ranking.
