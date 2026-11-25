# `depot`: do agents synchronize on shared records when the world does not force them?

Status: design v4.1 (2026-11-25, Fable, muster lane). v3 applied an independent review of v2
(`s1-skeptic-depot/review.md`, 12 defects). v4 applied the first review round of the built family
(section 0). v4.1 adds independent draw classes and more unknown outcomes after round 2 (`REVIEWS.md`).
Nothing here was run with a model. Section 0 records the v4 history; the later sections describe v4.1.

## 0. Version 4 changes (review round 1 of the built family)

| Change | Why (review) |
|---|---|
| A 504 uses 1 write at the moment of the 504, in every fault mode. A late write that lands later costs nothing more, also when the seat has none left; a lapsed one gives nothing back. `budget_left` then depends only on the seat's own requests and their statuses. | Before, a lost reply was charged at once, a dropped write never, a late write when it landed: `budget_left` told the seat whether its write happened (`rt-d1` policy `budget_check`; `rt-d6` D6-T02). |
| Draw values of `skew` and `storm`: the real clerk's take and the pair slack come from 1 distribution for both classes; then the class (tight or roomy) is drawn; then the other clerk's take is drawn from the class's range. The generator asserts that the guess "tight when slack < 2 × own take" agrees with the class for 7 to 13 of the 20 pairs of a condition. | Before, the tight pair always had slack 12 and the roomy pair 25, with takes of 8 to 11: the starting book told which pair was tight (`rt-d1` finding 01, `rt-d5` findings 03, 05, 06). |
| The order of p1's queue, which tickets get which fault, which inherited tickets the leaver completed or half did, and which inherited ticket is shared, are drawn per instance. Balance asserts: no queue position carries the same fault mode in more than 4 of 10 instances; the shared ticket takes 3 or more positions; the completed set takes 3 or more values. | Before, these were the same in all 10 instances (`rt-d5` findings 01, 02, 04 and its table "Regularities"). A policy that knows the layout of 1 instance knew all. |
| A staging rule is keyed to a ticket, not to the first write to an item. Every keyed ticket must be written, so the conflict always happens. | Section 5 said "first applicable write to item X"; the code keys by ticket (`rt-d7`). The code is sound; the text of section 5 was wrong. |
| `n.<hazard>` and `right.<hazard>` count only the groups where the hazard is active in that arm. | `limbo-probe` counted fault groups of played clerks, which cannot fault in `probe` (`rt-d3` D3-V2). |
| New diagnostic `legs.<seat>`: the share of the legs of the seat's own tickets that are right on their own (exactly 1 write with right arithmetic; a draw written once without breaking the pair rule, or declined when writing it would have broken it). `seat.<seat>` stays a measure of whole groups. | An `oracle` p1 with idle teammates had `seat.p1` 0.0 in `clash-team`, because its groups also hold the idle teammates' tickets (`rt-d2` finding 01). |
| Idle floor: `focal` of an idle seat is 0.0 except in `takeover` and `storm`, where groups that the leaver or another clerk settled are already right. | Claim 8 of v3 was false for those 2 facets (`b2c`, `rt-d3` D3-V3). |
| Seat text v3: a ticket is settled when the log has its writes (by any clerk); writes must be whole numbers of 0 or more on an item of a ticket in the seat's queue; a second write for the same ticket and item is not refused (outside `guard`); the stale refusal has `"error": "stale"`; a decline is not checked and uses no write; the accounting sentence above; 1 request at a time; a late write can land at any moment, also after the seat stops; other clerks "can" write while you work; an item is in at most 1 pair and only draws change pair items. The generator asserts the last 2 facts. | `rt-d6` D6-T01 to D6-T05, the blind implementers' notes (`bi-d1` to `bi-d3`), `rt-d1`. |
| Every confirmed finding of the review is stored in `muster/tasks/depot/findings/` with status `fixed` or `accepted`; the card replays them (`builtin.findings_regress`). | Every bug found becomes a recorded regression. |

Not changed, by decision: a narrower but sound readset (checking only the item of a pair that can change), checking the log only after an unknown outcome or only for inherited tickets, and repair after the fact are accepted routes (`rt-d1` "Other routes", `rt-d7`). The family claims that the named skills are sufficient, not that they are the only way. `clean` separates repair from prevention.

## 1. The construct and the prior work

A team of clerks settles tickets against one shared stock book. Every single step is easy: read a number,
add a delta, write a number. The book offers what a safe clerk needs (a write that names the versions it
expects, and a log of every write) but forces nothing. The family measures whether an agent synchronizes
without being told to.

Closest prior work (read by a research worker on 2026-11-25; numbers are the authors', not reproduced):
- ConcurrencyBench (2026-11): shared records with optional compare-and-swap and leases. With clear
  wording the strongest models pass single-record conflicts (claude-fable-5: 0 failures in 128 with locks).
  Cross-record rules stay hard (Claude Sonnet 5 commits both sides in 14 of 16 write-skew episodes).
  So `clash` below is a calibration floor, not the headline.
- LIMBO (2026-09): after an unknown outcome, 6 frontier models duplicate an effect in 0.5% of lost replies
  but in 56% of late commits and 74% of redeliveries. `limbo` and `takeover` below stage those 2 cases.
  The rates do not transfer: this world has whole-value writes and 1 documented log.
- HSC-Bench, Handoff Debt, UndoBench: successors repeat finished work; half-done multi-step work is
  rarely completed correctly.

What `depot` adds: 1 real agent among world-played teammates with a just-in-time adversary (each trap is
deterministic and cheap), unknown outcomes and takeover in the same world as conflicts, a solo arm and a
world-enforced arm for every facet, and a machine-checked proof matrix (policies x conditions).

Skills, each with its own facet and paired control:

| Skill | The anomaly when it is missing | Standard name |
|---|---|---|
| `cond` | A teammate's change disappears under my whole-value write | lost update |
| `readset` | My write is fine alone, a teammate's is fine alone, together they break a rule over 2 items | write skew |
| `dedupe` | I apply a ticket twice, or never, because I do not know what already happened | not exactly-once |

The skills name the behaviour of the zoo policies. They are sufficient, not necessary. The round-2
policies `peer_only`, `selective_log`, `targeted`, `version_ack` and `refusal_reply` stay clean on all
10 instances of every probe facet. A clerk that writes blind, audits the log and repairs also reaches
a right book in `clash-probe` (`repair`). The reward accepts that route; `clean` does not.

## 2. World rules (exact)

Items have a quantity and a version (starts at 1; +1 with every write to that item). Some items form a
pair with a `floor`. Clerks: `p1`, `p2`, `p3`.

| Request | Effect |
|---|---|
| `GET /rules` | Kit rules, plus `"pairs": [{"items": [a, b], "floor": n}]`, `"guard": bool`, `"clerks": [...]`, `"left": [...]` |
| `GET /book` | `{"items": {name: {"qty": int, "version": int}}}`: the whole book |
| `GET /tickets` | The seat's queue: `{"tickets": [...]}`; a ticket taken over from a clerk who left has `"from": "<clerk>"` |
| `GET /log` | `{"writes": [{"n", "seat", "ticket", "item", "qty", "version"}], "declined": [{"seat", "ticket"}]}` |
| `POST /write {"ticket", "item", "qty", "expect"?}` | Whole-value write of `qty` (integer, 0 or more) to `item`, for a ticket in the seat's queue and an item that ticket touches. `expect` is an optional map of 1 to 8 items of the book to positive integer versions. An otherwise valid write with a version mismatch gets 409 `{"error": "stale", "current": {item: {"qty", "version"}}}`. A successful write or a 504 uses 1 write; a late landing uses no additional write. Other refusals use none. |
| `POST /decline {"ticket"}` | Declines a `draw` ticket of the seat's queue. 400 for another kind. It does not check pair feasibility and uses no write request. |
| `POST /msg`, `GET /msgs` | Kit channel; only in the `team` arm |

Tickets:
- `{"id": "p1-1", "kind": "adjust", "item": "bolt", "delta": -4}`: 1 write to `bolt`.
- `{"id": "p1-4", "kind": "move", "item": "bolt", "to": "clamp", "n": 5}`: 2 writes, 1 per item.
- `{"id": "p1-5", "kind": "draw", "item": "cable", "take": 9}`: 1 write to `cable`, or a decline. The 2
  quantities of the pair, added together, must not be below the floor after the draw. A draw that would
  break the rule must be declined; a draw that does not break it must be written.

Outside `guard`, the world does not check arithmetic, refuse a second write for a ticket and item, or
check the pair rule. `guard` still does not check arithmetic or decline feasibility.

Unknown outcomes: a write can end with status 504 and the body
`{"error": "no reply from the world; the request may have happened, may happen later, or may never happen"}`.
The body is the same in the 3 modes, fixed by the instance per ticket:
- `lost_reply`: the write happened.
- `dropped`: it did not and never will.
- `late`: it has not happened yet. It happens just before the same seat's next write to that item that
  would otherwise be applied, or at the close. At that moment the world checks its `expect` again; if a
  named item has another version by then, the late write never happens.
A fault applies only to a write that would otherwise be applied (valid body, own ticket, `expect` holds,
budget left). A refused write gets its real refusal and leaves the fault armed. A fault fires once.

Takeover: a clerk can have left before play starts. Its 5 tickets are in the queues of the remaining
clerks, marked `from`. 2 are already written, 1 move is half written, and 2 are open. One open ticket
is also in the other remaining clerk's queue. Every write that the clerk who left made is correct.

`guard` arm: the world also enforces. For an in-budget write, the check order is body validation,
duplicate (409 `{"error": "ticket already written for this item"}`), required `expect` on the written
item (428), version mismatch (409 stale), then the pair rule for a draw (409 `{"error": "pair rule",
"current": {...}}`). A late write is checked again when it lands, without another budget check.

## 3. Arms

| Arm | Real seats | Teammates | Channel | Use |
|---|---|---|---|---|
| `probe` | `p1` | world-played, staged just in time | no | deterministic test of 1 model |
| `guard` | `p1` | as `probe`; the world enforces the 3 guards | no | causal control |
| `solo` | `p1` | no live teammate: `p1` holds every ticket, including the inherited tickets already written. Faults stay. A clerk who left stays gone, with its writes in the log; its 3 pre-start writes count against the aggregate budget. | no | same underlying work, no live concurrency |
| `team` | every clerk who has not left | none | yes | real concurrency |

Every trial of a run plan starts in a fresh context. The guard arm states its 3 checks in the seat text,
so a model that saw it must not be the same context that runs the probe arm.

## 4. Facets and the skills of the named policies

| Facet | Staged situation | Sufficient skills in the probe matrix |
|---|---|---|
| `calm` | Clerks touch disjoint items | nothing |
| `clash` | Every item of `p1`'s adjust tickets is also written by a teammate; 2 items by both teammates | `cond` |
| `skew` | `clash`, 4 pairs and extra separate adjust and move work. Each pair has 1 draw for p1 and 1 for a teammate. Each draw fits alone. The pair is tight if both do not fit together, roomy otherwise. | `cond`, `readset` |
| `limbo` | 1 ordinary triple-clerk conflict, plus 5 faulted adjust tickets per clerk: 2 `lost_reply`, 2 `dropped`, 1 `late`. The `late` item's only ticket is its own. | `cond`, `dedupe` |
| `takeover` | Ordinary conflicts, plus `p2` has left: 2 of its tickets are written, 1 move is half written, 2 are open (1 also given to `p3`). | `cond`, `dedupe` |
| `storm` | Ordinary conflicts, 3 pairs, takeover and unknown outcomes. p1 has 5 faults like `limbo`; p3 has 1 fault of each mode. Extra separate adjust and move work. | all 3 |

Each pair's class is drawn independently, after the visible take and slack. All-tight and all-roomy
layouts are possible. A fixed full class guess has chance 1/16 per instance of `skew`, 1/8 of `storm`.
A fixed retry set for p1's 5 faults has chance at most 1/10 per instance, or 1/6 if the late position
is known. These are fixed-guess bounds, not bounds for routes that use evidence of effects.

A condition is `<facet>-<arm>`: 24 conditions, 10 instances each (`i01`..`i10`). An instance has the same
items, tickets, faults and pre-start writes in the 4 arms of a facet.

## 5. Staging in `probe` and `guard` (the just-in-time teammate)

World-played teammates are units of correct work ("settle ticket T, or 1 leg of it, correctly now"),
logged as that clerk. A unit runs at most once. Every unit belongs to exactly 1 rule, or to none (then it
runs at the close). At most 1 `before` rule fires per request. A `before` rule and a fault fire only for
a request that would otherwise be applied (`would_apply`): an invalid or already stale request does not
use up the teammate.

- Conflict: `before` an applicable write by p1 for the keyed ticket, a teammate's unit on its item
  runs. The change lands between p1's last read and its write. A re-read does not help; `expect` on
  the item does. Where 2 such rules match a ticket, the second fires on its next applicable request.
- Skew: `before` an applicable write or decline for p1's draw ticket, the teammate's draw on the other
  item runs. For a write with `expect` on both items, p1 sees 409 stale and the new quantities if
  the teammate wrote and no earlier guard check refuses the write.
- `lost_reply` and `dropped`: the item has exactly 1 teammate unit, and it runs right `after` the 504. So
  neither "the version changed" nor "the quantity is not what I sent" tells p1 what happened to its write.
- `late`: the item has no teammate unit, so the pending write stays valid until it lands.
- Takeover: the units of the clerk who left run at the start. The unit of the other clerk for the shared
  ticket runs `before` p1's first applicable write for that ticket; that item has no other unit.
- At the close: pending late writes land if still valid, without another charge. Then every unit
  that did not run runs, in instance order. All of this is logged before the close event.

`team` arm: no conflict staging; real seats race. Faults stay (keyed by ticket). The clerk who left is
still world-played at the start; the shared ticket is in the queues of both real seats.
`solo` arm: no unit except the pre-start writes of a clerk who left; faults stay.

## 6. Score and measures (the verifier reads only the event log)

The verifier replays effects: every successful `write` and `decline` event in the log, by a real seat or
world-played. A lost reply has a successful write event and a stage note; a late write that lands has
its own successful event. A 504 event with no successful write is not an effect.

Groups: each item that is in no pair, and each pair.
- An item is right when its final quantity equals its start quantity plus every adjust delta on it,
  minus every move out of it, plus every move into it, AND every adjust ticket and every move leg on
  it has at least 1 successful write in the log (by any clerk).
- A pair is right when (a) the pair total is not below the floor at the end, (b) every draw on it has a
  write or a decline and not both, (c) each final quantity equals the start quantity minus the takes of
  the draws that have a write, (d) no declined draw could still be applied at the end.

Scores:
- `reward` = right groups / groups: the whole book, the team's result.
- `focal` = right groups / groups, over the groups that carry a ticket of a real seat's queue. In `probe`
  and `guard` this counts whole groups of p1's tickets, not just its own work. The idle floor is 0.0
  except in `takeover` and `storm`, where pre-start or played work can make groups right.
- `seat.<name>` for each real seat: the same share over the groups that carry a ticket of that seat.
- `legs.<name>`: share of that seat's distinct ticket legs that have exactly 1 arithmetically correct
  write (also respecting the pair rule for a draw), or only declines when the draw could not fit.
- `exact` = 1.0 when `reward` is 1.0.
- `clean` = 1.0 when `exact` and none of these happened: (1) a successful write whose quantity is not
  the quantity just before it changed by exactly its ticket leg; (2) a second successful write for the
  same ticket and item; (3) a pair total below its floor after any write; (4) a draw with a write and a
  decline; (5) a decline of a draw that could be applied at that moment.
- Per hazard: `right.clash`, `right.skew`, `right.limbo`, `right.takeover` (share of the groups exposed
  to that active hazard that are right) with the counts `n.clash`, `n.skew`, `n.limbo`, `n.takeover`.
  A share is present only when the count is above 0. `clash` excludes `limbo` groups.

Exposure and response counters: `conflicts` (409 stale), `unknown_outcomes` (504 or fault note),
`blind_writes` (applied writes without `expect` on the written item), `log_reads`, `log_reads_after_504`
(first log read after a 504 before the next write attempt), `book_reads`, `write_attempts`,
`refused_for_budget`, `messages`, `real_writes`, `unsettled` (tickets with no write and no decline).
Anomaly counters: `stale_writes` (wrong arithmetic), `double_applied`, `rule_breaks_end`,
`rule_breaks_ever`, `needless_declines` (feasible at end), `declined_and_written`, `feasible_declines`
(feasible at the moment of the decline).

## 7. Policy zoo

Every policy receives only its seat name and talks through requests. The careful policies obey explicit
refusals (428: adds `expect`; 409 stale: updates or reads the book; 409 pair rule: declines;
409 already written: goes on). `reread` and `follower` use the same reactive code.

Order rule of `cas_dedupe` and `oracle`: first take the version (from `GET /book` or from the
`current` of a 409), THEN read the log, THEN write with `expect` on that version.
After a refusal retry with a new version and log check. Never combine an older log with a newer version.

| Policy | Skills | Behaviour |
|---|---|---|
| `blind` | none | Reads the book once; computes from its own running copy; writes without `expect`; ignores refusals; on 504 sends the same write again once. |
| `reread` | none (near miss) | Reads the book again right before a write; starts without `expect`; on 504 reads the book and writes again. It reacts to a guard's 428. |
| `follower` | none | `reread`, reactive. |
| `repair` | none (other route) | Writes without `expect`; reads the log after each attempt. After a successful write, if a teammate's write on that item came between its read and its write, it writes again with the teammate's change added; bounded. |
| `cas` | `cond` | `expect` on the written item; on 409 stale recomputes from `current`. On 504: reads the book, writes again. |
| `cas_version` | `cond` (near miss) | `cas`; on 504: if the item's version changed, it takes its write as done. |
| `cas_value` | `cond` (near miss) | `cas`; on 504: if the quantity equals what it sent, it takes its write as done. |
| `cas_check_once` | `cond` (near miss) | `cas`; reads the log only right after a 504. |
| `cas_dedupe_first` | `cond` (near miss) | `cas`; reads the log before the first attempt of each ticket and after a 504, not after a 409. |
| `cas_dedupe_ticket` | `cond` (near miss) | Reads the log before every attempt, but skips a whole ticket when its id is in the log. |
| `cas_readset` | `cond`, `readset` | `cas`; for a draw sends `expect` for both items of the pair and decides on fresh numbers. |
| `cas_dedupe` | `cond`, `dedupe` | `cas`; before every write attempt reads the log (order rule) and skips a leg whose (ticket, item) is there, or a ticket already declined. |
| `oracle` | all 3 | `cas_dedupe` and `cas_readset` together. |
| `slack_guess` | chance baseline | Uses the oracle for non-draw legs; guesses the tight draw from the smallest starting slack. No pass or fail claim applies. |
| `idle`, `decliner`, `spam`, `fuzz` | degenerate | Reads `/rules` only; declines every draw and writes nothing; repeatedly writes 0 to the first ticket's item until refused or the bound ends; sends malformed bodies. |

## 8. Claims that the card must prove

1. `oracle`: `reward` = 1.0 and `clean` = 1.0 in all 24 conditions, every instance, every explored schedule.
2. Skills matrix in `probe`: on the 10 fixed instances, each of {`blind`, `cas`, `cas_readset`,
   `cas_dedupe`, `oracle`} scores `reward` 1.0 when its skills contain those listed for the facet;
   otherwise below 1.0. This does not assert necessity on other layouts or for other policies.
3. Near misses in `probe`: `reread` and `follower` below 1.0 in every facet except `calm`; `cas_version`,
   `cas_value`, `cas_check_once`, `cas_dedupe_first` below 1.0 in `limbo` and `storm`; `cas_check_once`,
   `cas_dedupe_first`, `cas_dedupe_ticket` below 1.0 in `takeover` and `storm`.
4. Other route: `repair` scores `reward` 1.0 and `clean` below 1.0 in `clash-probe`.
5. `guard`: `follower`, `cas`, `cas_readset`, `cas_dedupe`, `oracle` = 1.0 in all 6 facets.
6. `solo`: `oracle` and `cas_dedupe` = 1.0 in all 6 facets. `blind` = 1.0 in `calm`, `clash`, `skew`,
   `limbo`; below 1.0 in `takeover` and `storm`. `cas` below 1.0 in `limbo`, `takeover`, `storm`.
7. `team`: `oracle` = 1.0 under every explored schedule; for each failing cell of the matrix a schedule
   exists with a reward below 1.0 on every instance. The card reports the mean over random schedules.
8. Floors on `focal`: `idle` = 0.0 except in `takeover` and `storm`, where the card computes the floor
   from already correct groups; `decliner` and `spam` at most 0.25; `fuzz` causes no 500. The oracle
   causes no 429.
9. Equal budget: the write budget of all clerks together is the same in the 4 arms of a facet.
10. Built in: determinism, exact replay, live state equals replayed state, no token of another seat in
    any response, every zoo policy ends, event log continuous, every credited event matters.

## 9. Instance constraints (the generator asserts each one)

- Single items (including move items), by facet `calm`, `clash`, `skew`, `limbo`, `takeover`, `storm`:
  8, 8, 15, 10, 11, 18. `skew` has 4 pairs (8 more items), `storm` 3 pairs (6 more items).
  Only draw tickets touch pair items. Start quantities 40..90.
- Original adjust ticket counts for p1/p2/p3: 5/5/5 in `calm` and `clash`, 6/5/5 in `skew`,
  6/6/6 in `limbo`, 4/4/4 in `takeover`, 7/4/6 in `storm`. Deltas are distinct across all
  adjusts, non-zero, in -9..9. Move counts: 3/0/0 in `skew`, 1/1/1 in `takeover`, 3/1/0 in `storm`;
  none elsewhere. Each move has 2 exclusive items and moves 3..9 units.
- The net change of every single item is not 0. No ordering of the intended single-item changes
  can make a quantity negative.
- No accident: for every single item, no sum over a strict subset of its changes, and no sum with
  at most 1 change counted twice and others counted at most once, equals the full sum.
- Tight pair: each draw fits alone and both together do not. Roomy pair: both fit together.
  p1's take is 3..20; slack is that take plus 3..19. Both classes have a feasible peer take for
  every supported visible value. Across the fixed batch, 30%..70% of pairs are tight; the classifier
  `slack < 2 × own take` agrees with the class on 35%..65% of pairs.
- Faulted tickets are adjust tickets. The `late` ticket's item has no other ticket. The `lost_reply`
  and `dropped` tickets' items each have exactly 1 teammate ticket; the shared takeover ticket's
  item has no other ticket. Only the real seats have fault rules. The p1 ID-to-item maps are distinct
  across all 10 instances. A queue position has at most 4 `late` or 6 of either other mode across
  the batch; a fault ordinal has at most 7 of a mode. There are at least 3 shared-ticket positions
  and at least 3 completed sets in each takeover condition.
- Every unit is named by at most 1 rule. Every rule names at least 1 unit or is a fault.
- Write budget per clerk with a queue: 2 for each leg in its queue, plus 2. The departed clerk has
  2 per pre-start unit plus 2. The aggregate write budgets in facet order above are 36, 36, 66, 42,
  50, 76. In `solo`, p1 has the aggregate less the 3 pre-start writes if a clerk left.

## 10. What the family does not claim

- It does not show that any model fails. Only a run can show that. The prior work suggests that strong
  models pass `clash`; `limbo`, `takeover`, `skew` and `storm` are where points can be lost.
- A team can also pass by talk, by splitting hot items, or by repair after the fact. The reward accepts
  every route to a right book; `clean` and the measures tell the routes apart.
- The `team` arm is not deterministic: real seats race. The `probe` arm is.
- A failure in `probe` can have other causes than a missing synchronization behaviour: arithmetic or
  ticket misreading (the `solo` arm of `calm`, `clash`, `skew` separates it), tool calls sent in 1 batch
  before any reply is read (the step join of `muster events` separates it), an exhausted budget
  (`refused_for_budget`). The guard arm changes 3 things at once and states them in the text; single
  guards and a sham guard are later work.
