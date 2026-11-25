# `dossier`: does a team keep a small shared register sufficient for questions it does not know yet?

Status: design v3.1 (2026-11-25, Fable, muster lane). v2 replaced v1 after an independent review
(`s2-skeptic-dossier/review.md`, 12 defects). v3 applied the first review round of the built family
(section 0). v3.1 makes every line of the ideal scarce register matter to another clerk's question
(`REVIEWS.md`). Nothing here was run with a model. Section 0 records the v3 history; the later sections
describe v3.1.

## 0. Version 3 changes (review round 1 of the built family)

| Change | Why (review) |
|---|---|
| Decoy (off-watch) reports get times from the same range as real reports and random feed positions. The generator asserts that feed order, reverse feed order, oldest first and newest first, each without a watch filter and without drops, fill the register before p1's last needed key, on every `scarce` and `mix` instance. | The 5 decoys always had times 30 to 34, newer than every real report: writing all reports oldest first let the full register refuse exactly the decoys (`rt-s1` finding 01; `reverse` also passed). |
| The tie that decides a played clerk's focal ask in `dispute` (and the dispute case of `mix`) is between 2 of p1's own reports: same site, same time, 2 statuses. No played clerk holds them, so only p1 can mark it. | The played clerks marked every tie, so p1 never needed to (`rt-s1` finding 02). |
| Zone order and site order in `GET /rules` are shuffled independently of the roles; questions and special cases are assigned to sites at random; ordinary statuses are drawn independently; times are drawn independently of the kind of report. The generator asserts counterfactual dependence: for each played clerk's focal ask, all feeds without p1 give a wrong status; for each p1 ask that needs the register, p1's feed alone gives a wrong status. | Roles followed from the position of a site in the public zone lists; ordinary statuses came from 1 shared cycle; times told the special case (3 closed, 20 withdrawn, 25 disputed). Policies that never read the register passed (`rt-s1` findings 03, 04). |
| Seat text v3: played clerks "also work on this survey" (they act at fixed points, not in parallel); a refused put uses no put; a clerk that stops before the writing phase ends cannot answer; there are enough register lines for a sufficient register; in `reader`, the register with the seat's own reports is enough; any status that the rule gives is a legal answer; `solo` does not mention register lines. | `b4b` "Text against world", `rt-s6` T01 to T06, the blind implementers' notes (`bi-s1` to `bi-s3`). |
| Instance ids are `i01` to `i10`; `focal` in `reader` counts only the real seat's own asks, so an idle seat scores `focal` 0.0 in all 24 conditions; waiting policies send `ready` at most 60 times; a share is present only when its count is above 0. | `b4c`, `b4d`. |
| Every confirmed finding of the review is stored in `muster/tasks/dossier/findings/` with status `fixed` or `accepted`; the card replays them. | Every bug found becomes a recorded regression. |

Not changed, by decision: the played clerks still follow and repair by the correct protocol for ordinary reports, so `probe` measures p1's own writing only where no played clerk holds the evidence. Team repair is a property of the `team` arm, not a defect.

## 1. The construct and the prior work

Clerks survey sites. Each clerk has private reports. The team has one shared register with a small number
of lines. First the team writes. Then the register is frozen and each clerk gets questions about the
current status of watch-list sites: any site of the public watch list can go to any clerk. So the register
must be sufficient for the whole watch list, not for questions that somebody already knows. Every single
step is easy: compare 2 times, copy a report. The family measures whether an agent stores what its
teammates will need, in a form they can use, checks the register before it says it is done, and reads
the register correctly.

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

The skills are sufficient for the named policies in the probe matrix, not the only ways to pass.
In the round-2 rerun without selection, the best policies pass 0 of 10 `scarce` instances and 1 of 10
`mix` instances by fortunate order. Policies that never read the register pass 0 to 3 of 10 per facet
by guesses. These are observed counts, not exact probabilities for arbitrary instances.

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
| `POST /put {"key", "status", "at"}` | Writes the line of that key; replaces a line of the same key whoever wrote it and whatever its time. The entry must be one of the seat's own reports, or `disputed` with the key and the `at` of one of the seat's own site reports: 403 otherwise. A new key needs a free line: 409 `{"error": "register full"}`. Only while writing: 409 otherwise. Each successful put uses 1, also an identical put (which does not change the version); a refused put uses none. |
| `POST /drop {"key"}` | Removes a line that the seat itself wrote last (403 otherwise). Only while writing. |
| `POST /ready {"version": n}` | The seat says that it is done with the register as it is at version `n`. 409 `{"error": "the register changed", "version": current}` when `n` is not the current version. Any later change of the register clears every ready mark. When every clerk is ready at the current version, writing closes: `{"ready": true, "closed": true}`. Otherwise an accepted ready returns `{"ready": true, "closed": false, "waiting_for": [clerks]}`. A refused ready has no `closed` field. |
| `GET /asks` | While writing: `{"phase": "writing", "asks": []}`. After: the seat's own asks. |
| `POST /answer {"site", "status"}` | Only while answering; one of the seat's own asks; the first answer of that seat for a site is final. Any of the 10 statuses the rule can give is legal. Budget kind `answer`. |

There is no message channel in this family: the register is the channel.

## 3. Arms

| Arm | Real seats | Teammates | Use |
|---|---|---|---|
| `probe` | `p1` writes and reads | world-played clerks that follow the correct protocol | deterministic test of 1 model |
| `reader` | `p1` only reads: writing is closed from the start and the register holds what correct clerks would have stored, with p1's own reports included | world-played | is the loss on the reading side? |
| `solo` | `p1` has every report and 1 ask for every distinct watch site; there is no register; phase `answering` from the start | none | control: does the model apply the rule at all? (not a matched team control) |
| `team` | `p1`, `p2`, `p3` | none | real team |

## 4. Facets and the skills of the named policies

In every facet a played clerk is asked about a site that only p1's reports decide (writer case), and
p1 is asked about 2 sites that need teammates' lines and 1 site that its own report decides.

| Facet | The writer case and the reader case of p1 | Sufficient skills in the probe matrix |
|---|---|---|
| `share` | A plain site report. Slots are ample. | `pool` |
| `scarce` | As `share`, but slots equal the number of keys in the ideal register. Each key matters to another clerk's question. p1's feed contains 5 off-watch reports at drawn positions. | `pool`, `select` |
| `gone` | Writer: p1 holds a withdrawal of a watch site on which the asker has an older report. Reader: a site of p1's asks has a withdrawal line that is newer than p1's own report. | `pool`, `tombstone` |
| `cascade` | Writer: p1 holds a zone closure newer than the site's report of the asking clerk. Reader: a site of p1's asks has a zone closure line that is newer than p1's own site report. | `pool`, `cascade` |
| `dispute` | Writer: p1 holds 2 reports of a watch site with the same `at` and different statuses; neither belongs to a played clerk. Reader: p1 is asked about a site whose line is `disputed`. | `pool`, `dispute` |
| `mix` | All 5 skills, scarce slots, and 2 of the 3 special reader cases. Closure cases omit the older site report so a redundant key does not reserve a slot. | all 5 |

A condition is `<facet>-<arm>`: 24 conditions, 10 instances each. An instance has the same underlying
reports, watch list and truth in the 4 arms of a facet. The non-solo arms share asks; `solo` merges
the reports and asks each distinct site once.

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
| `oracle` | all 5 | The correct protocol of section 5, then `ready` until writing closes, then the rule. |
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

## 9. Instance constraints (the generator asserts each one)

- 16 sites in 4 zones of 4; 6 ordinary statuses and 4 special statuses; 9 watch sites; 3 clerks.
  `scarce` and `mix` have 3/4/5 asks for p1/p2/p3 (12 questions, 9 distinct sites). p3 also gets p1's
  own-report control question, and p2 and p3 each get the other's control. Other facets have 3 asks
  per clerk, 9 questions. `solo` has 9 asks in every facet. Times are whole numbers 1..40. No 2 reports
  of 1 clerk share the same key, time and status; the same key and time can have 2 different statuses.
- The true status of every watch site is never `unknown`.
- Every played clerk has at least 1 ask in `focal` (its truth needs a report of p1) and at least
  1 ask that it can answer right without p1. p1 has 2 asks that need the register and 1 that its
  own report decides.
- The facet's writer and reader cases exist as section 4 states; in `dispute` the focal tied site is
  asked of the clerk who holds neither of the 2 reports.
- `scarce` and `mix`: slots = the number of distinct reported watch-relevant keys = the number of
  ideal register lines. Removing any 1 ideal line makes a question of another clerk wrong. p1
  holds exactly 5 reports about distinct sites off the watch list, at drawn positions and times.
  The 4 raw writing orders in section 0 are checked for the capacity trap, not a fixed feed prefix.
  Other facets: slots = the number of reported watch-relevant keys plus 6.
- The generator builds the register that correct sequential clerks produce and asserts that it
  fits the slots and is sufficient for every actual question.
- No fixed answer scores above 0.34 on the focal asks over the 10 instances of a condition; `withdrawn`,
  `closed` and `disputed` each are the true answer of a focal ask only in the facets that need them.
- Put budget per clerk: its number of reports plus 4. Answer budget: its number of asks.

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
