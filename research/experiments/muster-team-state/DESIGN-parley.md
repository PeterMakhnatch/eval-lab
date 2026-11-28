# `parley`: coordination without a referee

Status: v0.1, prototype. No independent review round yet. This record describes scripted controls, not model results.

## Mechanics

3 clerks share a stock book of 12 items. Each clerk has its own queue of adjust and move tickets. The book stays fixed during planning. Clerks store batches of absolute quantities. A batch can name any item, up to that clerk's limit. A new batch replaces the previous one.

A message clears all ready marks. Every accepted batch store clears its clerk's mark. An added ticket clears the mark of its clerk. A ready request must name the current message count and the current size of the clerk's queue. Once all clerks are ready, the world applies the batches in seat order. The last batch that names an item decides its final quantity. If the barrier does not close, the book stays unchanged.

In `late` and `storm`, the dispatcher adds 1 ticket on the named clerk's first otherwise-current ready request. This request gets 409 with the new counts. The event log records the append separately.

| Facet | Mechanics |
|---|---|
| `calm` | Disjoint queues, limit 12 per clerk. |
| `shared` | Shared items, limit 12 per clerk. |
| `tight` | Shared items, limits sum to 10. |
| `late` | Shared items, 1 late ticket, limit 12 per clerk. |
| `storm` | Shared items, 1 late ticket, limits sum to 10. |

`team` has 3 real clerks and private queues. `open` has the same world but anyone can read all queues and limits. `solo` combines all queues in 1 clerk with limit 12. The 3 arms share the same book and ticket multiset. The solo arm shuffles the combined queue and maps the late clerk to p1.

The score is the fraction of the 12 final quantities that equal the starting quantities plus the effects of tickets present at close. The verifier replays the event log and does not read the live world. `focal`, `right.shared`, `right.own`, `unwritten`, `disagreed`, `stale`, per-seat counts and `ended` give diagnostic evidence. An empty diagnostic denominator yields 0.0.

## Generator constraints

- Seed `parley-v0.1`, 10 draws per facet. All 150 packaged tasks use matched facet draws.
- Draw 10 touched items from 12 without replacement. Starting quantities are independent integers in [42, 95].
- In `calm`, each touched item has 1 stakeholder. Every clerk has at least 1 item.
- In the other facets, 5 to 7 items have 2 or more stakeholders. At least 1 item has all 3. Each clerk has an exclusive item. The 10 shared-item sets differ. Their union is all 12 items and their intersection is empty.
- The generator draws a sign per item and a magnitude in [1, 9] per stakeholder. A clerk with both signs can combine 2 legs into a move. It shuffles the remaining adjust tickets. Every draw has at least 1 move.
- The generator asserts nonzero final net deltas and that the starting quantity covers the sum of all negative ticket legs. It enumerates proper nonempty subsets of stakeholder totals and asserts that each differs from the truth.
- The tight limits are a permutation of (2, 3, 5) or (2, 4, 4). At least 2 clerks touch more items than their limit. At least 1 exclusive item belongs to an over-limit clerk. Each seat gets the unique smallest limit in at least 2 draws.
- A late ticket is an adjust of magnitude 1 to 9 on an already-shared item of the late clerk. It has the item's original sign. The generator repeats the arithmetic and subset assertions after the append. Each seat is late in at least 2 draws of each late facet.
- A draw that fails an assertion is discarded. Search is bounded at 1,000 draws per instance. The fixed seed needed 0 rejected draws in the 50 base instances.

## Checks and limits

The card checks oracle reward and phase completion, own-queue behavior, sharer losses, stale blind-ready behavior, capacity losses of hog, and solo controls. It also checks deterministic replay, live-state equality, request errors, reward range and refusal-credit behavior. The card uses 12 seeded schedules in addition to round for multi-seat cells. It cannot express every claim of the design directly: `selfcheck.py` checks round-only sharer frequency, conditional mixed-roster quitter failure, oracle worst search and the effect of final batch removal. It also checks invalid batch values and both request budgets under seeded cases.

A common deterministic partition of the sorted set of touched items fits because the sum of limits is sufficient. When all clerks share the latest queue, each can compute the same truth and its own part of that partition. A late clerk must send an update. This clears any previous ready marks. An accepted ready mark alone is not proof that work has ended. The oracle polls until `done` or its mark is lost. This protocol needs fair scheduling and enough requests; no finite budget protects against arbitrary starvation.

`open` makes initial queue sharing unnecessary, but late updates still need coordination: the dispatcher clears only the late clerk's ready mark. `open` does not isolate allocation from every form of synchronization. The starter quantities do not identify shared items, but the same-sign construction and the restricted limit compositions are intentional regularities and limit generalization.

The seat text states the barrier and message invalidation rules. It does not state a coordination protocol. Some response field names and the exact timing of the late ticket are not shown in the text. The API replies give the fields, and a protocol need not know the exact timing. A blind implementer review is still needed. The host controls and concurrent HTTP parity do not establish container isolation. No real-model result follows from the card.
