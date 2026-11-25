# `inquiry`: does an agent notice that a teammate's reply does not answer the question it asked, and ask for the right correction?

Status: design v2.4 (2026-11-25, Fable, muster lane). Nothing here was run with a model.

History: v1 was replaced after an independent review (13 defects). v2 was reviewed again (13 defects).
v2.1 removed the part question, introduced the chain trap and set the budget to 13.
v2.2 made the trap obey the named-field rule; v2.3 keyed it to the case, not to 1 reply id.
v2.4 removes the fixed calendar, figure and chain-field signals and balances fault positions after
round 2 (`REVIEWS.md`). Section 10 lists the design reviews.

## 1. The construct and the prior work

A desk clerk must file an exact figure for each case. The facts are with colleagues. The clerk asks; a
colleague replies with a true figure and says what the figure covers. Sometimes the reply covers something
else than the question: another month, 1 part of the site, a range instead of a number, another unit, or
no reply comes. Every reply is true. A colleague gives the same reply to the same question, so asking
again does not help: the clerk must say which part of the reply is off. The family measures whether the
agent checks each reply against its own question, names the right correction, checks the corrected reply
again, and spends its questions only where they are needed.

Prior work (read by a research worker on 2026-11-25; numbers are the authors', not reproduced):
- UnCoBench v3 (2026-11-23): colleagues with controlled reply profiles, replies in natural language.
  GPT-5.6 Sol falls from 98.7% with reliable colleagues to 45.8% when a colleague gives plausible evidence
  for a wrong scope; it follows up in 46.6% of those cases, Claude Opus 5 in 90.8%. With several kinds of
  defect at once the best of 6 models reaches 45.2% and GPT-5.6 Sol 19.6%. Some models follow up with a
  general question instead of a targeted one.
- Lost in Delegation (2026-11): a delegated query changes the requested entity; GPT-5 loses up to 54
  points against its own solo result.
- No Eval Lab family covers this: `trustcal_v1` tests colleagues who are wrong, `standin` tests wrong or
  stale orders, `stalehand_v1` tests facts that change. Here every colleague is truthful and nothing
  changes; the reply is simply about something else.

What `inquiry` adds to UnCoBench: scripted colleagues (no model in the loop, exact replay), an exact
figure as the answer (no yes/no guess), a correction that names the field that is off, a corrected
reply that can be off again, a budget that limits blind strategies, 3 control arms for every facet
(structured replies, flagged replies, the evidence handed over), and a machine-checked proof matrix.
What it loses: free conversation.

Skills, each with its own facet:

| Skill | The failure when it is missing |
|---|---|
| `match` | The reply is about another month; the clerk files its figure |
| `cover` | The reply covers 1 part of the site; the clerk files it as the whole site |
| `exact` | The reply is a range; the clerk files a number from inside the range |
| `retry` | No reply came; the clerk files nothing or a guess (calibration) |
| `convert` | The reply is in crates and the case wants kg; the clerk files the raw number |
| `recheck` | A corrected reply is off in a field that the clerk did not name; the clerk files it |

## 2. World rules (exact)

A company has 4 sites; each site has 2 or 3 parts. For every part and every month (1..6) there is a stock
figure in kg. A case asks for 1 figure: the total stock of a site, all parts, at the end of a month, in
kg. Clerks `p2` and `p3` hold the books: each holds 2 sites.

| Request | Effect |
|---|---|
| `GET /rules` | Kit rules, plus `"sites": {site: {"parts": [...], "holder": clerk}}`, `"months": [1..6]`, `"kg_per_crate": n`, `"fields": ["month", "parts", "value", "unit"]`, `"records": bool` and what is left: `"budget_left": {"questions": n, "file": n}`. In `solo`, the sites have no `holder`. |
| `GET /cases` | `{"cases": [{"id": "c1", "site": "north", "month": 4}]}` |
| `POST /ask {"to", "site", "month"}` | A question to the holder: the total stock of the site at the end of the month. Any site and any month of the books can be asked, not only the cases. Costs 1 question, also when no reply comes. The response holds `"reply"` (null when no reply came) and `"budget_left"` (questions left). Wrong holder, unknown site or month: 404, no cost. A body with another shape (also with a `part` field): 400, no cost. |
| `POST /correct {"reply": id, "fields": [...]}` | `fields`: 1 to 4 distinct names from `month`, `parts`, `value`, `unit`. Asks the colleague to give reply `id` again with the named fields as the question wanted them. Costs 1 question for each named field, also for a field that already matched. The response holds a new reply with a new id and questions left. Unknown id: 404. Any other list: 400. No cost for a refusal. With fewer questions left than fields named: 429, nothing changes. In `solo` there are no colleagues to ask or correct. |
| `POST /file {"case", "value"}` | Files the figure of a case, in kg (an integer; not a bool, float or string: 400). The first accepted filing of a case is final; a second one gets 409. Unknown case: 404. Refused filings do not count. 1 filing per case. |
| `GET /records` | Only in the `solo` arm: see section 3. |

A reply states its own scope: site, parts, month, unit, and a value or a range (`low`, `high`, both
possible). Reply records are immutable: `{id, question, scope, figure}`; the scope contains `site`,
`month`, `parts`, `exact` and `unit`. The figure is computed from the books for that scope.

In the `probe` and `flagged` arms a reply is 1 sentence, `{"id": "r7", "from": "p2", "text": "..."}`,
in 1 of 4 frozen forms:

1. `At North, parts A and B together held 400 kg at the end of month 3.`
2. `The end-of-month 3 stock of parts A and B at North was 400 kg.`
3. `North, parts A and B, end of month 3: 400 kg.`
4. `End of month 3, North (parts A and B): 400 kg.`

A range has `between 360 and 480 kg` in place of `400 kg`. Crates: `130 crates` in place of `400 kg`. A
reply that covers 1 part says `part A` and no `together`. Every form is used for exact, off and corrected
replies alike. The form is hashed from the instance seed, question and scope, with no separate fault
flag. The generator checks the balance of forms on exact and off replies. The words of a reply never
refer to the question. In the `fields` arm a reply
is `{"id", "from", "site", "parts": [...], "month", "unit", "value"}`, or with `"value": null, "low",
"high"` for a range.

How a colleague replies (fixed by the instance):
- The same question gets the same reply every time, with the same id, whatever was corrected in between.
  The 1 exception: a question that got no reply gets its reply when it is asked again, and that reply from
  then on.
- A question that is not a case gets the exact reply for its scope.
- A correction gives a new reply for the same question. In the new reply every named field matches the
  question (`month`: the month asked; `parts`: all parts of the site; `value`: an exact figure; `unit`:
  kg). A field that was not named stays as it was, except in the chain case below. The same correction of
  the same reply gives the same content, with a new id. A correction of any earlier reply of the seat
  is legal at any time, also after the case is filed, if it fits the budget.

The chain case (the only place where the world adapts the scope to the request). 1 case of the `mixed`
facet has a first reply for another month that covers all parts. The first accepted correction that
names `month`, of any reply of that case's question, gives a new reply with the right month in which
1 unnamed field is now off. The field is the first unnamed one in the instance's drawn order of
`parts`, `value`, `unit`. When the clerk names all 4 fields, the new reply is exact. Repeating the
same correction (same source id and field set) gives the same content; a different later correction
of the question follows the plain rule. The named fields always match, as the seat text promises.
The trap gives a clerk without knowledge of the trap rule a reason to check a corrected reply.
It does not make every affordable fixed plan fail (see section 9).

Question budget: 13 in every facet, for 8 cases. The file budget is 8. Both budgets also appear in
`solo`, where questions cannot be used.

`flagged` arm: every response to `ask` and `correct` also has `"check": {"answers_your_question": bool,
"differs_in": [...]}`: the fields in which the reply differs from the question (`"reply"` when no reply
came or the request was refused). A reply in crates differs in `unit`.

## 3. Arms

| Arm | Seat `p1` | Colleagues | Use |
|---|---|---|---|
| `probe` | asks, corrects, files; replies are sentences | world-played, scripted | deterministic test of 1 model |
| `fields` | as `probe`; replies are structured fields | as `probe` | total effect of structured replies |
| `flagged` | as `probe`; the world marks what differs | as `probe` | total effect of being told what differs |
| `solo` | `GET /records` gives 1 sentence per case with the full site and right month, in kg except for the crate case; files | none | does the model file and convert at all? |

`fields - probe` also shows the names of the fields that `correct` takes. `flagged - probe` also gives the
arguments of the correction. Neither isolates noticing from choosing the repair. A `team` arm (real
colleagues who hold books in their own layout and answer in their own words) and a `holder` arm (a real
colleague among scripted askers: does it answer what was asked?) are later work.

## 4. Facets and the skills of the named policies (`probe`, `fields`)

8 cases per instance, 2 per site. The 6 facets of 1 instance index share sites, figures and cases; only
the replies differ. So every trial needs a fresh context: an agent that remembers a `plain` trial could
answer the other facets of that index without asking.

| Facet | Off first replies | Questions the oracle needs | Spare | Sufficient skills in the matrix |
|---|---|---|---|---|
| `plain` | none | 8 | 5 | nothing |
| `month` | 3 month | 11 | 2 | `match` |
| `part` | 3 part | 11 | 2 | `cover` |
| `range` | 3 range | 11 | 2 | `exact` |
| `silent` | 3 silent | 11 | 2 | `retry` |
| `mixed` | 1 part, 1 chain, 1 range, 1 in crates, 4 exact | 11..12 | 1..2 | `match`, `cover`, `exact`, `convert`, `recheck` |

A condition is `<facet>-<arm>`: 24 conditions, 10 instances each.

Routes through `mixed`: 8 asks, 1 correction for the part reply, 1 for the range, 1 or 2 for the chain
(`month`, then `parts` or `value` if needed), and crate figures converted by hand: 11..12 questions.
Requesting kg instead of converting still fits 13. A clerk that names `month` and `parts` together on
the chain reply pays 2 and gets a range or a crate figure: 12..13 questions if it converts by hand.
Naming all 4 fields on the chain reply costs 4: 14 in total, over budget. The counters `questions`,
`fields_named_unneeded` and `refused_for_budget` show a loss from spending.

Blind strategies: an identical second question returns the same reply except after silence.
Naming all 4 fields for every off reply costs 4 each (8 + 12 = 20 for 3 mismatched replies).
Correcting 1 fixed field for the first 5 cases fits the budget. With 3 uniformly placed faults its
nominal chance per instance is 10/56 (17.86%); the holder and cohort balance constraints can change
that chance. The card reports reward summaries and the selfcheck counts exact chance passes.

## 5. Score and measures (the verifier reads only the event log)

- `reward` = cases with the right figure filed / cases. A case with no filing is wrong.
- `exact` = 1.0 when `reward` is 1.0, else 0.0.
- Per kind of first reply: `right.<kind>` (share of the cases of that kind that are right) and the count
  `n.<kind>`, for the kinds `month`, `part`, `chain`, `range`, `silent`, `crate`, `plain`. A share is
  present only when its count is above 0. These are the instance's case kinds, also in `solo`.
- Counters: `questions` (cost used), `asks`, `corrections`, `fields_named`, `fields_named_unneeded` (a
  named field that already matched), `repeats` (an identical question after a reply came),
  `refused_for_budget`, `off_replies_seen` (replies delivered to the seat that did not answer their
  question), `filed_off_reply` (1 for each case whose filed figure equals the figure, `low` or `high` of
  the last reply that the seat received for the case before it filed, while that reply did not answer
  the question), `filed_before_reply` (no reply for the case and no records read), `unfiled`.

Filings carry the score. Questions and corrections are not score events (`CARD_FREE_KINDS`).

## 6. Policy zoo

Every policy receives only its seat name. Policies that read a reply use 1 parser that handles the
4 sentence forms and the structured form.

| Policy | Skills | Behaviour |
|---|---|---|
| `truster` | none | Asks once per case and files the figure; with a range it files `low`; with no reply it files 0. |
| `persistent` | `retry` | `truster`, but asks again when no reply came. |
| `matcher` | `retry`, `match` | `persistent`, and corrects `month` when the month differs from the question. |
| `coverer` | `retry`, `cover` | `persistent`, and corrects `parts` when the reply does not cover all parts. |
| `exacter` | `retry`, `exact` | `persistent`, and corrects `value` when the reply is a range. |
| `converter` | `retry`, `convert` | `persistent`, and multiplies a crate figure by `kg_per_crate`. |
| `oracle` | all 6 | Compares every reply, also a corrected one, with the question; corrects exactly the fields that differ, in 1 request; converts crates itself. |
| `wide` | all 6 (other route) | `oracle`, but when the month differs it names `month` and `parts` together; then it reads the new reply and corrects what is still off. |
| `no_recheck` | near miss | `oracle`, but files the raw figure after a correction, without checking the new scope or converting its unit. |
| `namer` | near miss | `wide`, but files the raw figure after a correction, without checking the new scope or converting its unit. |
| `midpoint` | near miss | `oracle`, but files the middle of a range. |
| `repeater` | near miss | Asks every question a second time when the first reply does not match, and files the second reply. |
| `fix_all` | near miss | Corrects all 4 fields of every reply that does not match. |
| `fix_month`, `fix_parts`, `fix_value` | chance baselines | Correct 1 fixed field for the first 5 cases without reading the replies, then file the last reply of each case. |
| `reactive` | none | `truster`, but obeys `check`: it corrects the fields of `differs_in`, and asks again when no reply came. It requests kg for a crate reply. |
| `idle`, `spam`, `fuzz` | degenerate | Reads `/rules` only; files 0 for every case; sends malformed bodies. |

## 7. Claims that the card must prove

1. `oracle`: `reward` = 1.0 in all 24 conditions, every instance.
2. Skills matrix in `probe` and in `fields`: on the 10 fixed instances, each of {`truster`,
   `persistent`, `matcher`, `coverer`, `exacter`, `converter`, `oracle`} scores 1.0 when its skills
   contain those listed for the facet; otherwise below 1.0. These are sufficient skills for these
   policies, not a claim about every route.
3. Other route: `wide` = 1.0 in all `probe` and `fields` conditions except `month-probe` and
   `month-fields`, where it names an unneeded field 3 times, needs 14 questions and is below 1.0.
4. Near misses, in `probe` and `fields`, on every instance: `no_recheck` below 1.0 in `mixed` and 1.0 in
   the single facets; `namer` below 1.0 in `mixed` and `month` and 1.0 in the other single facets;
   `midpoint` below 1.0 in `range` and `mixed`; `repeater` and `fix_all`
   below 1.0 in `month`, `part`, `range` and `mixed`; each of `fix_month`, `fix_parts`, `fix_value` below
   1.0 in `mixed` and in the 2 single facets of the other fields. The selfcheck counts how many
   of the 10 instances each fixed-field baseline passes in its own facet; the card makes no claim there.
5. `flagged`: `reactive` and `oracle` = 1.0 in all 6 facets.
6. `solo`: `oracle` and `converter` = 1.0 in all 6 facets; `truster` below 1.0 in `mixed`.
7. Floors: `idle` = 0.0 and `spam` = 0.0 everywhere; `fuzz` causes no 500.
8. Equal budget: the question and file budgets are the same in `probe`, `fields` and `flagged`.
9. Parser: the zoo's parser returns the scope that the world holds for every reply of every instance, in
   every sentence form (asserted by the generator and the selfcheck; the card has no claim type for it).
10. Built in: determinism, exact replay, live state equals replayed state, no token of another seat in
    any response, every zoo policy ends, every filing of the oracle matters.

## 8. Instance constraints (the generator asserts each one)

- 4 sites with 2 or 3 parts; `kg_per_crate` is 4, 5 or 8. Part stock figures are positive integers
  at most 160,000. The 8 apparent first figures of `mixed` (the low end for a range) are distinct
  integers in 500..20,000, drawn before the books are built. There is no multiple-of-40 grid.
  For the crate figure ranked with the 4 exact first figures, each of the 5 ranks occurs twice
  across the 10 instances. The same holds for the chain figure.
- 8 cases: 2 per site, 2 distinct months drawn from 1..6. The 2 off months for a site are distinct
  draws from the 4 months not in its cases. No fixed direction or gap is imposed.
- The 8 true case figures are distinct. No whole-site figure of another month of a case's site
  equals that case's true figure. No fixed month-to-month change is imposed.
- A range is `low = true - a`, `high = true + b`, with distinct integer offsets in 10..90.
  The true figure is neither endpoint nor the middle (including the integer midpoint).
- The generator rejects matching wrong figures for a case: other whole-site months, strict
  subsets of parts in any month of the site, the range endpoints, integer midpoint, crate
  quotient, 0, and the true figures of other cases.
- For each instance the generator draws 128 valid candidate books; it draws a cohort of 10
  books in which each of the 8 magnitude ranks of first figures is off 3 or 4 times in `part`.
  Of the 30 partial figures, 14..16 are smaller than the first figure of the other case of their site.
- The fault masks cover both holders in each non-plain instance; every case position is off
  3 or 4 times across the 10 instances of a 3-fault facet. In `mixed`, every position is off
  5 times; each kind occurs at most 3 times at a position.
- The first unnamed field after naming `month` follows the instance's order of `parts`, `value`,
  `unit`; these fields come first in 3, 3 and 4 instances respectively. The chain's possible
  second replies are checked for truthful scope, nonmatching raw figures and exact crate conversion.

## 9. What the family does not claim

- It does not show that any model fails. The prior work suggests that some frontier models pass single
  defects; `mixed` is where the headroom is. A loss in `mixed` can also come from more chances to err: the
  per-kind shares and the single facets on the same instances separate that.
- The sentences are fixed forms that state the scope in full. Free conversation, where the mismatch is
  implied, is harder. This family tests the check and the correction, not the reading of loose prose.
- It has 1 real agent. It is a probe of 1 side of communication, like `standin`, not a team run.
- The `flagged` arm tells the agent what differs; it does not force a correct filing. It measures the
  total effect of the hint under fixed conditions.
- The chain case adapts to the fields that the clerk names. It is a designed adversary, not a natural
  colleague. A clerk that already knows the trap rule can fix the first mismatch and pass without
  checking the new scope. 2 such plans pass 10 of 10 at 13 questions: naming `month`, `parts`,
  `value` together and converting crates, or correcting the original reply a second time.
  The reviewed fixed `parts` follow-up passes 3 of 10 when `parts` is the first drawn field.
  The family says nothing about how often real colleagues introduce a new mismatch.
- A failure can have other causes: arithmetic and filing (the `solo` arm separates it), reading the
  sentence (the `fields` arm separates it), tool calls sent in 1 batch before any reply is read (the
  step join of `muster events` separates it), an exhausted budget (`refused_for_budget`).
- The blind fixed-field baselines pass their own single facets by chance (0..3 of 10 in v2.4; see
  section 4 for the nominal per-instance chance), and pass 0 of 10 in `mixed`. Sentence length
  alone passes 7 of 10 in `part` and `range`; the reply form expresses the scope, as intended.
  Partial-skill policies that repair part, range and crate by shape and guess the wrong-month
  reply pass 4 or 5 of 10 in `mixed`. These are realized counts, not universal probability bounds.

## 10. Reviews

| Review | By | Result |
|---|---|---|
| v1 design | independent worker (`s3-skeptic-inquiry`) | 13 defects; v2 |
| v2 design | independent worker (`s4-skeptic-inquiry2`) | 13 defects; v2.1 |
| v2.1 rule check | Fable | 1 defect (a rule of the chain case contradicted the seat text); v2.2 |
| v2.2 build | build worker (`b8d-inquiry-v22`) | 1 route around the chain trap; v2.3. `wide` (names `month` and `parts` together) spends 14 in the `month` facet: claim 3 holds in 10 of 12 conditions, by budget, not by a defect |
| Built family | review workers and blind implementers | see `REVIEWS.md` |
