# Mission board

The sole live board. Only the integrator edits this file. States:
`ready` -> `active` -> `review` -> `merged`, with `blocked` possible anywhere.
Template: `TEMPLATE.md`. Finished missions move to `agents/archive/` with a
date prefix.

Workers own implementation inside their path lease and stop at review. The
integrator owns cross-mission conflict resolution, semantic review, rebase,
fresh exact-head CI, merge, board transition, and worktree sunset. A review
bot may advise later; it is never merge authority.

## Build result: M020–M024 all merged (40 PRs total)

Five parallel build missions, five PRs, all green on exact-head CI and merged. I
re-verified the load-bearing claim of each myself by mutation rather than accepting
its handoff — break the guarantee, watch the named test fail, restore, watch it pass.

| PR | Mission | Landed | My own mutation check |
|---|---|---|---|
| #126 | M020 QUEUE-PARALLEL | `running/<spec>.lease` with atomic `O_EXCL` claim, 30s runner heartbeat, `tick --parallel N` | `O_EXCL` → check-then-create: 2 racers both claim one spec, property test fails |
| #122 | M021 CLI-REGISTRY | `cli.py` linear dispatch → 52 `set_defaults(func=…)` handlers | renaming `tick` → `tickk` fails 3 surface tests incl. "unexpected commands outside golden" |
| #124 | M022 MEMORY-ANALYSES | `analyses` LanceDB table beside tasks/trials/steps, joinable to trial and job ids | disabling the identity guard turns "1 rows" into "7 rows", guard test fails |
| #123 | M023 CRAFT-BATCH | batched classify with the idempotence contract finally executable | one-character off-by-one in the batch slice fails 5 tests |
| #125 | M024 TIDY-SQUASH | content-based merged detection, three-state `merged`/`unmerged`/`unproven` | missing branch ref reading `merged` instead of `unproven` fails 3 tests |

### The three findings worth reading first

**M021 nearly shipped a 106-line `if False:` block, and it was there to make a
detector lie.** The registry conversion removes the `args.command == "x"` chain that
`repomap.py` pattern-matches to attribute each command to its implementing module, so
the agent kept the whole dead chain under `if False:` labelled "static AST attribution
mapping for repomap". `repomap check` passed either way. That is worse than a broken
test: the map's reachability signal is the tool that caught `parquet_compaction.py`,
`lessons.py`, `storm.py` and `status_generator.py` being built-and-dead, and dead code
retained to satisfy it makes every future answer untrustworthy. Fixed properly —
`repomap.py` now reads the registry (`_registry_owners`), and the block is gone. A real
bug surfaced while doing it: `_called_names` counted type annotations, so handlers typed
`harbor: HarborBackend` were attributed to `fetch`; 20 commands moved. Both behaviours
now have mutation-verified tests.

**A golden that pins CPython's argparse formatter is not a behaviour test.** M021's
safety net snapshotted rendered `--help` text. It passed on 3.12 and failed on 3.14 in
CI, because argparse changed its rendering. Replaced with a structural golden —
per-command flags, metavars, defaults, choices, required-ness, help strings — generated
from `origin/main`'s own pre-conversion parser, so it is evidence rather than a
self-portrait. Passes on both interpreters and compares strictly more than help text did.

**M022 was reporting a partial skip as silence.** Its builder printed the skipped-row
count only when *every* row was skipped; its own test seeded 1 valid and 6 invalid rows
and asserted only `analyses: 1 rows`, locking the silence in. Same shape as the
`status_generator.py` defect fixed the night before. Sent back: skips are now always
surfaced with examples, a swallowed catalog-read failure is surfaced instead of
presenting as "rows missing identity", and the test now fails if either goes quiet.

### M024 proved itself on live data within the hour

While the missions ran, five worktrees existed and one (`m023-craft`) got squash-merged
mid-flight — the exact production scenario. Old code: `Stale worktrees (0 items, 0 B)`.
New code, same moment: `1 items, 459.4 MB … branch merged into origin/main (content)`,
with the three genuinely in-flight worktrees correctly left alone. 2.4 GB of merged
worktrees has since been swept.

It also revealed its own limit, recorded in `research/audits/board-notes.md`:
`merge-tree` compares against `main` **as it is now**, so once main moves past a branch
in any shared file — including the generated `docs/INDEX.md` every mission regenerates —
a genuinely merged branch reads as `unmerged`. That is a false negative: it refuses to
delete rather than deleting live work, which is the correct direction to fail. The fix is
to add recorded PR merge state as a third signal, not to loosen the content predicate.

### Process note: every agent died, none of the work did

All five authoring agents were killed mid-flight — three by transport errors at 15–17
minutes, two by provider rate limits at 26–31 minutes. Their partial work was
checkpointed from their worktrees, and the missions were finished from those checkpoints
rather than restarted. The durable lesson is the one the loop protocol already encodes:
commit and push every few minutes, because an unpushed branch is the only thing a dead
agent actually loses.

## Night result: all five loops finished and merged into main (35 PRs total)

29 cycles across five missions, every cycle committed and pushed. All 5 PRs verified in exact-head GitHub Actions CI and merged into main after Actions limits were lifted by making the repository public.

| PR | Mission | Cycles | Status | What was verified |
|---|---|---|---|
| #117 | M015 AUDIT | 7 | **MERGED** | Ledger is 17 CONFIRMED / 2 DRIFTED / 1 UNPROVEN with a full append-only correction chain |
| #119 | M016 SURFACE | 6 | **MERGED** | Zero-trial day renders "nothing ran" with no trial ids; reverting the guard fails 2 tests |
| #121 | M017 CARDS | 5 | **MERGED** | Five cards valid; validator rejects a card with either mandatory caveat stripped |
| #118 | M018 FUZZ | 6 | **MERGED** | Reintroducing PR #102's local-time bug fails the quota property suite |
| #120 | M019 LESSONS | 5 | **MERGED** | 18 powered findings vs 14 gated `insufficient n`; regeneration byte-identical |

### The two findings worth reading first

**`docs/STATUS.md` shipped confidently wrong, and I caught it before it became a
habit.** Its "RECENT (Yesterday: 2026-08-17)" section listed five trials. The
catalog holds **zero** trials for that date; the runs it named actually executed
on 2026-08-13 and 2026-08-14. Cause: when the catalog legitimately returned
nothing for the reporting day, `status_generator.py:175` fell back to an
unfiltered all-time filesystem scan and printed it under yesterday's heading. An
empty day is a real finding — "nothing ran" is true and useful — and substituting
a different dataset for it is worse than rendering nothing, because it is
confidently wrong in the one file meant to be read without a terminal. Now gated
on catalog inaccessibility, labelled `trials_source`, and guarded by a test I
verified bites.

**`TrialConsumption.day` bucketed timezone-aware timestamps by local date** —
PR #102's defect surviving in a second location, found by M018's property tests
rather than by reading. Fixed in 7 lines. I confirmed the suite catches the
regression by reintroducing it.

### Where my own steering went wrong, recorded because it shaped the ledger

I told M015 that a clean sweep of CONFIRMED verdicts across invisible-surface
modules was suspicious. It responded by producing two DRIFTED rows claiming
`storm.py` and `status_generator.py` were "not imported or called anywhere in
`src/`" — refuted by one grep (`digest.py:29`, `status_generator.py:22`,
`automation.py:34`). I had pushed toward a conclusion instead of toward a method;
the corrected instruction was to establish unreachability by finding the absence
of a caller. The ledger keeps all of it — original, wrong correction, and
corrected correction — which is the right behaviour for an append-only record and
more useful than a clean sheet. The salvaged finding is sharper than either
verdict: `status_generator` was **wired but never run**, since nothing loads the
nightly schedule.

## Now

`origin/main` is `0ad6446` (merged PR #147, 2026-08-23). GitHub records
**143 merged PRs and zero open PRs**. PR #147 repaired the portable registry
and lessons truth boundary; M049 retains only the broader named workbench
certification cases it did not claim. The type-check ratchet is **28
diagnostics** in `scripts/premerge.sh` and CI.

### Five build missions, all merged (M020–M024)

Peter green-lit the top five unbuilt items from the v2 architecture audit and
asked for them in parallel. These are BUILDS, not loops: each takes one audit
row, lands it with a test proven to bite by mutation, and stops at review.

| Mission | Item | Lease | Worktree |
|---|---|---|---|
| M020 | QUEUE-PARALLEL — E01 leases, per-provider semaphores, orphan reconcile, quota deferral | `queue.py`, `runner.py`, `tests/test_queue*.py`, + the one `--parallel` flag on `tick` | `.worktrees/m020-queue` |
| M021 | CLI-REGISTRY — `cli.py` linear dispatch chain → `set_defaults(func=…)` registry, behaviour-preserving | `cli.py`, `tests/test_cli_audit.py`, `tests/test_cli_registry.py` | `.worktrees/m021-cli` |
| M022 | MEMORY-ANALYSES — index analyst conclusions into LanceDB beside tasks/trials/steps | `lance.py`, `tests/test_lance.py` | `.worktrees/m022-memory` |
| M023 | CRAFT-BATCH — E07 classify batching with its documented idempotence contract finally tested | `craft.py`, `tests/test_craft.py` | `.worktrees/m023-craft` |
| M024 | TIDY-SQUASH — content-based merged detection, three-state classification | `tidy.py`, `tests/test_tidy.py` | `.worktrees/m024-tidy` |

Leases are disjoint with one deliberate exception: `cli.py` belongs to M021, and
M020 may add only the `--parallel` argument on the existing `tick` subparser.
M021 rebases every cycle and re-applies its conversion over whatever main holds;
the integrator merges M020 before M021. Every other mission needing a CLI surface
exposes a public function plus its own `python -m evallab.<module>` entry and
files a board-note. `research/audits/board-notes.md` is the only shared writable
file, append-only. `docs/INDEX.md` and `docs/repo-map.md` are generated and their
conflicts are resolved by regeneration at merge, never by hand.

Each mission must prove its test bites by mutation — reintroduce the defect, show
the test failing, restore, show it passing — because the recurring finding all
week has been tests that pass while reality disagrees. `scripts/premerge.sh`
gates every push; no mission merges its own PR.

### Phase-1 DATA TRUTH result (M029–M031)

The three Phase-1 missions are merged, not unstarted.

| Mission | Outcome | Merge evidence | State |
|---|---|---|---|
| M029 | INGEST completeness verification and projection reconciliation | PR #138, merge `55cb9ee` | merged |
| M030 | deterministic trajectory outline, queue, facts, and SQL views | PR #142, merge `bd819b1` | merged |
| M031 | pinned subscription-CLI model adapter with fail-closed injection | PR #134, merge `64e1e96` | merged |

Their completed leases are recorded in the completed ledger below. Follow-on
gaps use new mission IDs rather than reopening these spent branches.

### Gym campaign registered (M032–M035)

Spec: `docs/prompts/gym-campaign.md` v1. Peter's framing: the lab is a GYM — a
growing collection of environments plus the machinery to run them, capture
everything, analyze trajectories, and evolve the collection. Long-running
missions; every cycle boundary is merge-safe.

| Mission | Loop | Lease (as written) | State |
|---|---|---|---|
| M032 | GYM-RUN — the first campaign: fill the lake, ship the experiments | `queue/**` submissions (through the CLI, never hand-written files), `library/frozen/gym-v0/**` (new manifest), one schema addition (`extra_instruction_path`), `research/cards/campaign-*.md` | merged — cycles 1–2 (#129, #128); M046 froze non-empty `gym-v1` in #139; no comparative result claimed |
| M033 | GYM-DATA — more data than trajectories | `research/external/**` (new), `src/evallab/fetch.py` (extend), `sql/external_views.sql`; funnel/STATUS line via board-note to SURFACE | merged — cycle 1 (#130); corpus `pending`, acquisition path documented |
| M034 | GYM-HARBOR — use the Harbor we already have | `docs/research/harbor-capability-audit.md` (new), `src/evallab/fetch.py`/`runner.py` touchpoints only with a board-row note | candidate — unassigned; no live handoff |
| M035 | GYM-UI — trajectory truth and analyst explorer | `src/evallab/explorer.py`, `dashboard/explorer.py`, `tests/test_m035_ui.py` | merged (#145, `fcefae5`) |

**GYM-RUN cycle 3 status:** the registry blocker is closed. Four human-approved
registered records now define `gym-v1`; `gym-v0` remains the immutable empty
generation. The campaign card and candidate control inputs cite
`library/frozen/gym-v1/manifest.json`.

The Gemini Antigravity lane (`antigravity-cli`) is proven by M037/M041, and the
staged Low/Medium screen is running. Queue inputs remain unapproved; no billable
work has been dispatched and no comparative result is claimed.

### Context-supply program registered (M025–M028)

Spec: `docs/prompts/context-supply-program.md` v1 (Peter-level direction; this
board registers, it does not edit that doc). Premise in his words: *"I gotta feed
it the right info since I can't post-train it."* Subscription agents arrive with
frozen weights, so context is the only lever, and the program treats it as a
supply chain with the same standards as any other data path here — versioned
inputs, deterministic transforms, measured outputs. Four stages, one loop each:

```text
RAW SOURCES ──HARVEST──▶ inbox notes ──STANDARDS──▶ corpus files
corpus files ──PACK──▶ compiled context ──(missions)──▶ agent work
verification know-how ──VERIFIER──▶ corpus + experiments + SG-4 feed
```

| Mission | Loop | Lease (as written in the spec) | State |
|---|---|---|---|
| M025 | HARVEST — raw sources into distillable inbox notes | `research/inbox/**` (append and edit its own notes only), `tests/test_inbox_conformance.py` | merged — cycle 1 landed in #127; cycle 2 is an unassigned candidate with no live handoff |
| M026 | STANDARDS — inbox notes into the versioned craft corpus | `library/curated/standards/**`, `tests/test_standards_conformance.py`, `_proposed_templates/` staging; SG-owned `authoring/templates/` only via board-note handshake | candidate — unassigned; prior quota attempt produced no worktree or work product |
| M027 | VERIFIER — absorb verification practice for a no-logprob lab | `library/curated/standards/verification/**`, `research/experiments/verifier/**`, `tests/test_verifier_corpus.py`; TRAJ/SEAM/calibrate touchpoints via board-note only | candidate — unassigned; depends on HARVEST queue item 2 |
| M028 | PACK — corpus into compiled, budgeted, measured context | `src/evallab/contextpack.py`, `tests/test_contextpack*`, `docs/` front-matter fields it needs; digest/STATUS surface via board-note to the SURFACE owner | candidate — unassigned; depends on the first two STANDARDS files |

**Historical quota attempt:** M025 cycle 2 and M026 cycle 1 were dispatched
after a clean probe, but both processes were killed by provider quota before
creating a worktree or producing work. They are not assigned blocked missions.
Any retry requires a fresh owner, live handoff, and board lease; until then the
follow-ups remain unassigned candidates.

**Escalation counter: night 1 of 2.** The rule is *"two consecutive blocked nights on
the same item → escalate to Peter's morning read via STATUS open-decisions."* This is
night one, so it is deliberately NOT escalated. If the next night's cycles die on quota
again for the same two items, that becomes a STATUS open-decision — and the decision on
the table would be provider capacity for unattended loops, not the loops themselves.

Dependencies, verbatim from the protocol: STANDARDS consumes HARVEST notes (start
after HARVEST cycle 1; run concurrently thereafter — HARVEST stays ahead by at
least one intake). VERIFIER cycle 1 needs its HARVEST note only. PACK cycle 1
needs the first two STANDARDS files. Leases are disjoint by construction; the one
naming convention to respect is that VERIFIER's eval card is
`research/cards/verifier-*.md`, file-disjoint from LOOP-CARDS by the
`experience-*` / `verifier-*` split.

Phase-1 priority is satisfied: M029, M030, and M031 are merged as PRs #138,
#142, and #134. Context-supply dependencies remain governed by their own rows;
none may claim those Phase-1 modules are absent.

Cadence: nightly cycles under the shared `night-loops.md` protocol (RECHECK →
EXTEND → PROVE → HARDEN → RECORD, max 6 cycles, one commit and one push per
cycle, every cycle ends mergeable) so an interruption at any cycle boundary loses
nothing. Program is DONE when all four loop-done acceptances hold.

### Five loop missions dispatched tonight (M015–M019)

These are LOOPS, not builds. The standing risk after ~55 missions in days is
divergence — built ≠ proven ≠ used — so each mission re-verifies existing work,
extends it one step, hardens it with a test, and records. Max 6 cycles each,
one commit and one push per cycle, every cycle ends mergeable. Spec:
`docs/prompts/night-loops.md`.

| Mission | Loop | Lease | Provider |
|---|---|---|---|
| M015 | AUDIT — re-run merged handoffs' claims, verdict them | `research/audits/**` only | cursor |
| M016 | SURFACE — first real `docs/STATUS.md`, digest sections | `status_generator.py`, `digest.py`, goldens | antigravity |
| M017 | CARDS — eval cards from existing data | `cards.py`, `research/cards/**` | cursor |
| M018 | FUZZ — hypothesis properties per state machine | `tests/test_*_properties.py` | antigravity |
| M019 | LESSONS — aggregates with statistical gates | `lessons.py`, `sql/lessons.sql` | cursor |

Leases are mutually disjoint and clear of SG lanes (`authoring.py` internals,
`library/meta/`, `authoring/templates/`, `calibrate.py`). Cross-loop
coordination is append-only through `research/audits/board-notes.md`; no loop
edits another's files. Split 3/2 across two providers because four concurrent
Cursor streams hit `resource_exhausted` earlier tonight, and dispatched
staggered by 20s for the same reason. All five run as supervised processes with
`restart: on-failure`, which is safe precisely because the protocol rechecks
before extending and pushes every cycle — a restarted agent resumes rather than
repeats.

The one handshake: M019 exposes `lessons_digest_section()` for M016 to import.
M016 uses it only if it is already on `origin/main`, otherwise files a
board-note and does other work. Neither blocks on the other.

### What the earlier wave established (33 PRs — context for why these loops)

These five loops exist because the previous wave kept finding one class of
defect: code that was built, tested, and unreachable.

- **`NightlyCycle` was a hardcoded sequence**, which is why `parquet_compaction.py`
  (751 lines) and `lessons.py` (910 lines) were fully tested and completely dead.
  PR #106 replaced it with a declared step registry; both are now reached.
- **`storm.py` (517) and `status_generator.py` (484)** were likewise unreachable
  until PR #103 wired them into the digest and nightly path. `status_generator`
  has still never produced a file — that is M016's cycle 1.
- **The dashboard and the CLI disagreed about the same number.** Daily spend
  buckets were four hours off because the dashboard used local time while
  `quota.py` normalises to UTC (PR #102).
- **The verdict feature took three review rounds**, each defect the same shape:
  tests passed while reality disagreed. Real discovery ids
  (`D-20260815-KTXJSHGZ`) were rejected by a blanket ULID rule; writes persisted
  while reads queried a view that was never created; three tests passed only on a
  machine with Postgres.
- **The suite was writing to the live catalog.** `verdicts` held 583 rows when
  found and 768 by the time PR #116 isolated it — it grew during the verification
  runs themselves. On an append-only decision table that pollution is permanent
  by design. Those 768 rows are deliberately left in place; clearing an
  append-only audit trail is Peter's call, not a side effect of a test fix.

Planning consequence, now better evidenced: budget review rounds as part of the
mission. A single green CI run on a first head has not once been sufficient. And
a module's tests passing says nothing about whether anything calls it — which is
the entire premise of M015.

### Completed mission ledger: M029–M046 and platform buildout

The merge commit is the source of truth. Acceptance below is limited to the
observable outcome recorded by the merged handoff and diff; it does not promote
follow-on work into a completed claim.

| ID | Outcome | Lane / owner | Completed lease | Deps | Source evidence | Observable acceptance | PR | Status | Next executable step |
|---|---|---|---|---|---|---|---|---|---|
| M029 | Account every catalog/disk trial as projected or by named reason | Platform | `ingest_verify.py`, `atif.py`, `sql/ingest_views.sql`, ingest tests | existing catalog/Parquet | archived M029 handoff; `55cb9ee` | completeness report exposes named gaps/reasons and reconciliation views exist | #138 | merged | use M047+ follow-ons; never reuse branch |
| M030 | Deterministic trajectory outline, review queue, features, and views | Research + Platform | `traj.py`, `atif.py`, `attach.py`, `sql/traj_views.sql`, trajectory tests | M029 data surface | archived M030 handoff; `bd819b1` | real ATIF paths render deterministic outlines and the review queue emits candidates without persisting labels | #142 | merged | consume through new mission leases only |
| M031 | Pinned local subscription-CLI model adapter with fail-closed injection | Platform | `modeladapter.py`, analyst injection, adapter tests | existing analyst/worker seams | archived M031 handoff; `64e1e96` | explicit pinned transports capture raw output/provenance; no adapter remains a refusal | #134 | merged | use fresh branches for adapter follow-ons |
| M035 | Show trajectory truth separately from analyst inference | Platform | explorer modules and `tests/test_m035_ui.py` | M030 | archived M035 handoff; `fcefae5` | explorer exposes trajectory, truth, and analyst surfaces with unavailable/redacted states | #145 | merged | no branch reuse |
| M036 | Add authenticated Cursor subscription lane and pinned profiles | Platform | `profiles.py`, `credentials.py`, profile tests | M031 | archived M036 handoff; `3fe5916` | `cursor-cli` has a CLI-session probe and explicit default profile | #132 | merged | no branch reuse |
| M037 | Add authenticated Antigravity subscription lane and pinned profiles | Platform | profiles, credentials, Antigravity lane docs/tests | M031 | archived M037 handoff; `a814d72` | `antigravity-cli` is registered separately from API-key transport | #135 | merged | policy/spend remains Peter-owned |
| M038 | Promote/register tasks only from digest-bound control evidence | Tasks | registry implementation, CLI surface, registry tests | existing control runs | archived M038 handoff; `5564fd2` | promotion refuses missing/contradictory controls and registration remains explicit | #133 | merged | M049 repairs durable evidence binding in #147 |
| M039 | Replace live-corpus snapshot equality with corpus invariants | Research | evidence-query tests | growing corpus | archived M039 handoff; `bf1c931` | corpus growth no longer fails a frozen count while accounting/disappearance invariants remain | #131 | merged | no branch reuse |
| M040 | Account for Cursor and Antigravity as distinct quota lanes | Platform | `quota.py`, quota/preflight tests | M036, M037 | archived M040 handoff; `12c9afb` | both lanes render explicit observed or unknown headroom instead of inheriting another provider | #136 | merged | policy ceilings remain Peter-owned |
| M041 | Translate local Antigravity model IDs at the Harbor boundary | Platform | `runner.py`, runner tests | M037 | archived M041 handoff; `c8e1803` | local and Harbor model namespaces remain distinct and mapped at command construction | #137 | merged | no branch reuse |
| M042 | Capture Antigravity structured output as sanitized ATIF | Platform | Antigravity capture modules, runner wiring/tests | M041 | archived M042 handoff; `c4eac8e` | structured events convert to ATIF and fallback is explicitly final-response-only | #141 | merged | live paid smoke remains separately authorized |
| M043 | Replace production authoring stub with injected model-backed proposal seam | Tasks + Platform | `authoring.py`, schema/tests, authoring docs | M031 | archived M043 handoff; `b568243` | strict `spec/1` validation precedes quarantined proposal creation; nothing self-registers | #143 | merged | live paid smoke remains separately authorized |
| M044 | Repair queue progress, restart reconciliation, and provider attribution | Platform | queue, runner, quota, CLI and focused tests | M040–M042 | archived M044 handoff; `58880a9` | operator progress names the spec/log; expired launches fail; quota readings stay per-agent | #144 | merged | no branch reuse |
| M045 | Stage ladder screening and separating-task follow-up | Research + Platform | `screen.py`, `ladder.py`, `power.py`, CLI/tests | registered tasks | archived M045 handoff; `1064a31` | stage 1 classifies explicit cohorts and stage 2 proposes follow-up only for separating tasks | #140 | merged | M047 adds executed-factor provenance and an empirical curve |
| M046 | Preserve empty gym-v0 and freeze non-empty gym-v1 | Tasks + Research | frozen gym-v1 manifest/card/specs and freeze tests | registered task records | archived M046 handoff; `527efb6` | gym-v1 contains four frozen records while gym-v0 bytes remain historical evidence | #139 | merged | registry repairs proceed in M049/#147 |
| PLATFORM-146 | Land the platform buildout represented by the exact PR diff | Platform | paths changed by PR #146, including `containers/state-journal/`, event mart/evidence/state-journal/import modules, cohort data, and focused tests | merged M029–M046 surfaces | PR #146; merge `2178311` | the named files are present on `origin/main`; no stronger runtime claim is made here | #146 | merged | execute gaps M047, M048, M051 independently |

#### Completed repair record

| ID | Outcome | Lane / owner | Completed lease | Deps | Source evidence | Observable acceptance | PR | Status | Next executable step |
|---|---|---|---|---|---|---|---|---|---|
| REPAIR-147 | Repair portable registry evidence binding and lessons truth boundaries | Tasks + Research | exact PR #147 paths in registry/schema/registry records and lessons SQL/generator/report/tests/CI | PLATFORM-146; existing registry and lessons surfaces | PR #147; merge `0ad6446` | durable evidence is identity-bound in the committed registry and lessons eligibility/lineage/freshness repairs are on `origin/main`; broader M049 workbench fixtures remain unclaimed | #147 | merged | continue only M049's named workbench fixture acceptance on a fresh lease |

### Owned acceptance contracts (M047–M053)

These are executable owner contracts, not roadmap prose. Leases are exclusive
while their status is `ready` or `active`.

| ID | Handoff | Exact outcome | Lane / owner | Exclusive path lease | Dependencies | Source evidence | Observable acceptance | Status | Next executable step |
|---|---|---|---|---|---|---|---|---|---|
| M047 (A) | `agents/handoffs/m047-factor-curve.md` | Execute declared factor points with identity/provenance, then produce an empirical paired curve | Research / Research lane owner | additive factor-provenance fields in `src/evallab/schemas.py`, `src/evallab/ladder.py`, `src/evallab/queue.py`, `src/evallab/facts.py`, `src/evallab/cohort.py`, their SQL/Parquet projection schemas and focused tests; curve files remain leased but untouched until this slice lands | factor execution/provenance first; curve second; consumes M030, M045, PLATFORM-146 | factor-provenance hard gates pass on `role/m047-factor-provenance`; M045 handoff supplies staged cohorts | declared factors bind real execution levers or fail closed; canonical point/factor/binding and evidence-based task-block coordinates survive spec→run→facts→cohort; legacy catalogs remain nullable; preamble controls and treatment content are comparable without name parsing | review | review and merge factor provenance, then implement the empirical paired curve from projected coordinates |
| M048 (B) | `agents/handoffs/m048-state-events.md` | Make `StateEventFact` ingestible, compactable, temporally queryable, and explicitly non-causal | Platform / Platform lane owner | shared state-journal producer module/image hook; additive `harbor_state_journal.py`, `state_events.py`, `schemas.py`, `facts.py`, `atif.py`, `attach.py`, `parquet_compaction.py`, golden inventory, focused tests and producer fixture | independent follow-on to PLATFORM-146 | repaired implementation from `fbc21d9`; 60 focused and 1,768 collected full tests, full Ruff/gates, pinned ty baseline | producer-regenerated baseline→write→revert→rewrite remains three typed-operation sequence/predecessor facts while final diff retains only baseline→final; available streams require valid unambiguous state-diff and distinguish known state, known absence, and unknown baseline; direct invalid evidence fails closed and fact extraction emits a deterministic invalid sentinel without erasing siblings; query semantics are temporal and non-causal | review-wanted | independent diff review |
| M049 (C) | `agents/handoffs/m049-workbench-certification.md` | Bind portable workbench certification to exact task bytes and prove fair-alt, nop-repeat, and please-hack evidence cases | Tasks / Tasks lane owner | workbench, registry/schema/CLI binding, focused tests/fixtures, durable registration packets, and M049 board/handoff | merged PR #147 supplies durable registry evidence; F waits for this certificate binding | M007/#49 workbench; M038/#133 registry; PR #147 merge `0ad6446`; implementation diff on this branch, with validation intentionally unclaimed | certificate packet binds exact task/version/path/package/candidate identities; axes remain separate; fair-alt, nop-repeat, invalid, and please-hack replays are represented; tamper/replay/circular and absent replay cases are refusal contracts; legacy absence is explicit | review-wanted | independent diff review and validation; record only observed results |
| M050 (D) | `agents/archive/2026-08-23-handoffs/m050-lessons-repair.md` | Repair lessons eligibility, optional annotation boundary, lineage, and deterministic freshness | Research | completed PR #147 lease: `src/evallab/lessons.py`, `sql/lessons.sql`, `research/lessons.md`, `tests/test_lessons.py`, additive CI freshness gate | PLATFORM-146 | PR #147 body/diff and merge `0ad6446` | exception trials are excluded and counted, unannotated eligible trials remain in cohorts, excluded rows cannot receive powered intervals, and regeneration from resolved lineage is byte-identical | merged | none; spent lease archived |
| M051 (E) | `agents/handoffs/m051-exgentic-adapter.md` | Bind file-only Exgentic trajectory JSONL and Recovery-Bench result JSON to one strict `UpstreamSource`/`AdapterManifest` contract | Tasks / Tasks lane owner | `src/evallab/upstream_adapter.py`, `library/adapters/exgentic/**`, `library/adapters/recovery-bench/**`, `tests/test_upstream_adapter.py`, `tests/fixtures/upstream_adapters/**` | independent follow-on to PLATFORM-146 | immutable upstream revisions and licenses are recorded; fixtures are constructed with no copied upstream bytes | offline imports preserve exact raw bytes/digest/revision; Exgentic emits validator-conformant ATIF plus evidence while Recovery-Bench is explicitly external-evidence-only with `trajectory: null`; strict claims and schema/revision/license/drift/path incompatibilities refuse | review-wanted | review manifest strictness, unavailable markers, and external-evidence-only Recovery boundary |
| M052 (F) | `agents/handoffs/m052-capability-contract.md` | Enforce typed P/R/U/C/Y capability contracts at harness, policy, and heldout boundaries without a scalar score | Research / Research lane owner, Platform review | `src/evallab/capability_contract.py` (new), `tests/test_capability_contract.py` (new), `research/experiments/capability-contracts/**`, additive integration in `screen.py` and policy admission tests | blocked on M047 + M049 | M045/#140 supplies staged cohorts; M049 supplies byte-bound certification; neither defines typed P/R/U/C/Y enforcement | each dimension is a typed value with provenance and explicit unavailable state; harness and policy reject undeclared/mismatched contracts before execution; heldout identities cannot enter authoring/tuning inputs; reports preserve the vector and never emit or rank by a scalar aggregate | blocked | after M047 and M049 merge, freeze one heldout fixture and implement pre-execution rejection |
| M053 (G) | `agents/handoffs/seqgen.md` | SEQGEN v0: deterministic sequence-first synthetic task generator, gate-clean Zone 03 batch, and the evaluation-factory audit note | Platform (src/, tests/) + Tasks (library/synthetic/) / orchestrator-builder session | `src/evallab/seqgen.py` (new), `tests/test_seqgen.py` (new), `library/synthetic/**` (new), `agents/handoffs/seqgen.md`, `docs/research/evaluation-factory-2026-08.md` (new) | none (reads `task_workbench.py` and `schemas.ProvenanceMetadata` unchanged) | brief `/private/tmp/eval-lab-orchestrator-brief.md`; `docs/research/synthetic-tasks.md` blueprint; upstream pins in the research note | 10 focused tests green incl. real `inspect_candidate` static pass; committed batch reproduces from `--seed 7 --count 4 --pool 40`; oracle 1.0 ×2 tasks and nop 0.0 on the inherit scratch batch; finding F-SEQGEN-1 recorded | review | PR #152 review; then decide F-SEQGEN-1 before the certification battery |

#### Source evidence and dependency order

- PR #147 merged M049's portable registry prerequisite; M049's broader
  certificate binding implementation is review-wanted without a validation claim.
- Factor execution and provenance precede M047's curve. M047 and M049 precede
  M052. M048 state events and M051's file-only adapters are independent follow-on
  PRs.

## Ready

- M047 is in review; M048 and M050 are merged.
- M049 and M051 are review-wanted.
- M052 remains blocked until M047 and M049 merge.
- M025 cycle 1 is merged; M025 cycle 2 and M026–M028 are unassigned
  candidates with no live handoff. M029–M031 are merged, not absent.

## Next

Ranked by what actually blocks the lab, from the v2 architecture audit:

- **Per-provider semaphores and orphan reconcile (E01, unassigned — the half M020 did
  NOT build).** M020 landed the lease layer it all depends on: `running/<spec>.lease`
  with atomic `O_EXCL` claim, a 30s runner heartbeat, and `tick --parallel N`. Two of
  the four §3.1 deltas are still open, and the board must not read E01 as complete:
  (a) there is **no per-provider semaphore** — `--parallel` is a single global cap, so
  provider limits are still managed by hand, which was the original complaint; (b)
  **orphan reconcile is not built** — stale leases are reclaimed opportunistically on
  the next acquire, but nothing checks for a live container by Compose label and nothing
  transitions an interrupted spec to `failed(execution_interrupted)` with its partial dir
  preserved. This is now the largest unbuilt item and it is well-scoped.
- **`tidy` should use recorded PR merge state as a third signal (unassigned, found by
  using M024's own tool).** `merge-tree` compares a branch against `main` as it is now,
  so once main moves past a branch in any shared file — including the generated
  `docs/INDEX.md` every mission regenerates — a genuinely merged branch reads `unmerged`
  and is never swept. Measured right after tonight's merges: only the last-merged
  worktree was flagged, four earlier merged ones sat as "active" holding 1.8 GB while
  `gh pr list --state merged --head role/<branch>` said all five were merged. Fail
  direction is safe (refuses to delete), so this is a reclaim gap, not a risk. Do not fix
  it by loosening the content predicate.
- **`repomap`'s command-to-module column needs real import-graph attribution
  (unassigned, found in M021).** It is a name-frequency heuristic and is wrong on `main`
  in places — `verdict` was attributed to `__version__`, which is not a module. The
  registry conversion kept all 84 command edges and shifted 11 attributions; three
  tie-break rules were measured (helper recursion 25 shifts, first-reference 20, body
  frequency 11 — the last was kept). An exact answer means attributing from the import
  graph rather than counting names.
- **Type ratchet is 28, not a follow-up.** `scripts/premerge.sh` and
  `.github/workflows/typecheck.yml` both enforce 28 diagnostics. Move it only
  when the exact-head count supplies new evidence.
- **Profiles CLI cutover and credential unification (E01, unassigned).** Two
  credential paths still coexist (`credentials.py` and `quota.py`), with a
  single `AgentProfile` specified but not cut over. Held back from tonight's batch
  because it overlaps M020's provider-capacity source; M020 has now landed and did
  **not** introduce a profile-backed capacity map, so this is unblocked and should
  probably land with the per-provider semaphore above rather than separately.
- **Operator board (E-board, unassigned).** Still no single read-only surface
  for what ran, what is running, what is queued, what is certifiable. M016's
  `docs/STATUS.md` is the cheap file-based answer to part of this; whether the
  full board is still wanted afterwards is a Peter question, not a build one.

### Mission candidates — recorded here so they cannot vanish with an archived handoff

Not active work. Each is unassigned, has no lease, and needs an M number and a
brief before dispatch.

- **Mission candidate — verifier-build observation is a text scan, not an
  observation (Tasks + Platform, unassigned).** M007's build-time check reads
  task-authored files and matches install idioms as *text*; roughly 20 plain
  idioms were added in its fourth repair round. That is a blocklist, and a
  blocklist over an arbitrary Dockerfile is defeated by any idiom nobody
  enumerated. The honest replacement is to build the verifier image in a
  container and observe what it actually does at build time. Scope note for
  whoever takes it: this changes M007 from "reads the task" to "runs the task's
  build", so it needs a Docker-daemon dependency the current check does not
  have.
- **Mission candidate — install Harbor in CI so M007's live drift comparison
  stops skipping (Platform, unassigned).** M007's Harbor drift check was split
  in its fourth round so that a static pin runs when no Harbor is installed.
  CI has no Harbor, so CI exercises only the static pin and the live comparison
  never runs there — the half that would actually catch Harbor drift is the half
  that is skipped. Harbor 0.21.0 is present on the workstation at
  `~/.local/bin/harbor`, so the gap is CI provisioning, not capability.
- **Mission candidate — the `ingest` perf metric measures the wrong thing
  (Platform, unassigned).** PERF-REBASELINE reports that the `ingest` metric
  times `initialize()` inside the measured region — a full `sql/schema.sql` DDL
  replay plus a second fresh connection — so the number is not ingest logic and
  carries most of the variance. Re-baselining the budget (#53, `e080dd0`)
  treated the symptom; this is the cause. Two constraints for whoever takes it:
  moving `initialize()` outside the timed region requires a **second**
  re-baseline, because the number drops sharply and the existing ceiling becomes
  far too loose (which would start tripping the below-50%-of-budget
  re-baseline notice); and it makes the pre-fix and post-fix `ingest` series
  **non-comparable**, which must be said wherever the series is read.
- **Mission candidate — calibration ground truth is mostly unscored (Research,
  unassigned).** The archived `observatory.md` produced 25 draft
  completed-trial records, but 23 of its 25 trajectory labels point at
  `harbor-practice/` source paths that do not exist in this repository, so those
  labels were never scored. Its recorded 8/8 field agreement therefore covers
  only **two** in-repo trials. Any claim resting on that agreement figure is
  resting on a sample of two. Archiving the handoff recorded where the gap came
  from; it did not close it. This also sets the floor for M010, whose gate is
  defined against measured agreement of 0.90.

## Needs Peter

Six open items: four raised by the recent build waves, then the two carried
documentation and sequencing questions. Everything else on this board is a lane
decision.

- **Vector memory: wire it, and with which embedder?** M022 added the `analyses` table,
  so the study loop's last structural gap is closed — but two facts bound what it can do.
  (a) There are **zero analyses in the repo**, because no analyst has run with a real
  model; the table was proven against four real trial and job identities paired with
  hand-written conclusions, which is honest evidence of the mechanism and nothing more.
  (b) `lance.py` has **no callers** in `src/`, `scripts/`, `dashboard/` or `cli.py` — it
  is reachable only as `python -m evallab.lance`, so nothing indexes automatically. Also
  worth knowing before anyone quotes "semantic search": the default embedder is
  deterministic lexical hashing and says so in its own docstring, so "find analyses
  similar to this one" is word overlap today. Three separable decisions: wire it into the
  nightly, adopt a real embedding model (spend), or leave it as an operator-invoked tool.

- **Green-light real generation and analysis runs? CORRECTED — this was never purely a
  spend decision, and the previous wording on this board was wrong.** Trial *execution*
  with a real agent already works: the catalog holds **33 `codex` trials** beside 57
  `oracle` and 2 `nop` controls, across `local-lab/event-summary` (67),
  `petermakhnatch/transaction-reconciliation` (13) and `terminal-bench/html-js-filter`
  (12). What has never run is the *analysis* and *generation* halves, and the reason is
  that **nobody wrote the provider call** — every model seam is a refusing stub:
  - `analyst.py:150` — `ModelAnalyzer.analyze()` raises `ModelProviderRefusedError`
    **even when `--model` is supplied**. `--model` only changes which class is
    constructed (`analyst.py:404`); the call itself is unimplemented.
  - `analysis_worker.py:657` — the `AnalyzerCallable` adapter seam defaults to
    `_no_adapter`, which raises `no analysis adapter is wired`.
  - `authoring.py:642` — `design_novel_spec(designer=…)` defaults to
    `default_novel_designer`, a deterministic stub spec designer.

  No provider SDK is even installed: `openai`, `litellm`, `dspy` and
  `sentence-transformers` are all absent (only the `openinference-instrumentation-*`
  wrappers are declared, which is why `tracing.py` contributes 2 of the 27 `ty`
  diagnostics). So the paper-derived >85% synthesis figure and any real trajectory study
  are blocked on **roughly one small mission** — pick a client, implement three
  adapters against seams that already exist and are already injected in tests — and only
  then on a spend decision. Splitting the two is the point: authorising spend today buys
  nothing.
- **Turn the nightly schedule on?** The pipeline exists and the step registry
  landed (PR #106), but `launchctl list` shows nothing loaded, so nothing runs
  unattended. Building the pipeline and enabling it were deliberately kept as
  separate decisions. Only free `oracle`/`nop` work can dispatch without a gate
  request, so switching it on does not itself spend.
- **Clear the 768 residue rows in `verdicts`?** The suite was writing to the live
  catalog until PR #116; the table held 583 rows when found and 768 by the time
  the fix landed, all of them test and verification residue rather than real
  decisions. Clearing is therefore safe, but the table is append-only precisely
  so that judgements cannot be rewritten, and truncating it must be an explicit
  human act rather than a side effect of a cleanup. Say the word and it goes to
  empty, so the next verdict is row one.

- **Keep or archive the four hand-authored HTML documents under `docs/`?**
  `docs/agent-workflow.html`, `docs/eval-rd-roadmap.html`,
  `docs/repository-state.html`, `docs/system-cartography.html`, and their
  shared `docs/repository-overview.css`. No committed code generates them
  (`src/`, `scripts/`, `dashboard/`, `Makefile`, and `.github/` contain no
  reference to any of them), so they cannot be rebuilt and they will drift
  silently against the Markdown they duplicate. Reference status, measured:
  the first three plus the CSS are referenced *only* by each other;
  `system-cartography.html` is additionally named by
  `docs/checkpoints/2026-08-15-system-cartography.md` and
  `docs/prompts/system-cartographer-2026-08-15.md`, so it has an authored
  owner the other three lack. This is a documentation-surface question, not a
  lane decision: either they are a deliberate human-readable surface worth
  maintaining by hand, or they are spent one-off renders that belong in
  `agents/archive/`. Carried unchanged from the previous board update; COORD-GC
  and BOARD-REFRESH both deliberately left the files in place.

- **~~Sequence a `cli.py` command-registry conversion before M012?~~ ANSWERED —
  Peter said go; dispatched as M021.** Kept here because the measured case is the
  record of why, and because the drift figures are worth re-reading if the same
  question recurs for another shared file. It was a spend-and-sequencing call —
  it buys no new capability and delays a feature mission — so it was Peter's, not
  the integrator's. The measured case,
  **re-measured at `25c8228`** (earlier figures were taken at `86380b0` and
  `e5d3257` and have since been overtaken; the direction of drift is the
  argument, and it never once reversed):
  - `src/evallab/cli.py` is **2,192 lines**, up from 1,412 at `86380b0` and 2,117
    at `e5d3257` — it grew **780 lines, 55%,** without the structure changing.
    Argparse wiring scaled with it: **160 `add_argument`** (was 111, then 154) and
    **61 `add_parser`** (was 46, then 60).
  - There are still **zero `set_defaults(func=...)`**, so dispatch remains a
    linear string-comparison chain that every new command must edit in the same
    place. That is the sequencing point: the cost of conversion rises with every
    command added, and 14 new commands landed while the question sat open.
  - It is the **highest-churn file in `src/`**: **39 commits** by
    `git log --follow` (was 24, then 37), against 14 for the next file
    (`researchers.py`) and 13 for `schemas.py`.
  - It is already designated a shared file — additive-only, smallest possible
    diff — by `docs/prompts/overnight-missions.md:48`. Note the correction:
    that designation lives in a dispatch brief, **not** in `policy/`, which
    contains no reference to `cli.py` at all. If the shared-file rule is meant
    to be binding it currently has no binding home.
  - It has **already stalled a mission**. The archived `pipeline` handoff
    (`agents/archive/2026-08-15-handoffs/pipeline.md:2`) records a required
    rebase aborted after `cli.py` conflicted with five new `origin/main`
    commits, with the worker correctly refusing to resolve another role's
    conflict.
  - M007 just shipped **6,577 insertions across 47 files** — a 3,238-line
    `src/evallab/task_workbench.py` and a 1,580-line test — with its own
    `python -m evallab.task_workbench` entry point, and touched `cli.py`
    **zero times**. `src/evallab/` now has four modules with their own
    `__main__` entry (`calibrate.py`, `cli.py`, `smoke.py`,
    `task_workbench.py`) while `[project.scripts]` exposes only `evallab`. By
    contrast M006 did route through the CLI, adding 57 lines to it.
  - Why it bears on M012 specifically: M012's whole premise is *one* operator
    surface. Building a unified cockpit on top of a dispatch layer that new
    features are routing around means the cockpit inherits the fragmentation.
    Converting first costs a mission slot; converting after M012 means
    converting a file M012 has just grown.

  Peter retains policy/spend, publication, research-direction, and
  task-registration authority; current implementation and merge decisions
  belong to the integrator.

---

## Missions

This board is authoritative; a prompt set records what was dispatched on its
date. Two generations are live and neither supersedes the other wholesale:

- `docs/prompts/functionalization-missions-2026-08-15.md` specifies **M005,
  M006, M007** — all three now merged.
- `docs/prompts/next-functionalization-missions-2026-08-15.md` (later, merged
  as PR #48) holds the **M009 flight** and the **M010–M014** forward plan, and
  is the operative brief for everything ahead.

`docs/prompts/README.md` indexes both. COORD-GC and PERF-REBASELINE were
dispatched directly by the integrator without an M number and without a
committed brief; their scope was their board row. BOARD-REFRESH is the same
kind of Integration bookkeeping mission.

| ID | Outcome | Lane | Agent/model | Worktree / branch | Exclusive paths (lease) | Deps | Acceptance | PR | State | Merge owner |
|---|---|---|---|---|---|---|---|---|---|---|
| M006 | Every eligible completed trial gets one provenance-frozen analysis lifecycle, with zero calls outside profile + policy admission | Research, Platform review | Claude Code, claude-opus-5[1m] | worktree removed; branch `role/m006-analysis-worker` still present locally and on `origin` (squash-merged, spent — do not reuse) | `src/evallab/analysis_worker.py`, `tests/test_analysis_worker.py`, `tests/fixtures/analysis_worker/`, `docs/analysis-worker.md`, archived handoff; minimal additive schema/database/automation/queue/CLI wiring | M002, M003 | met after four review-and-repair rounds; merged at head `3b15e25` as `4d23d7d`. Calibration gate CLOSED, default adapter `_no_adapter` — saved-response path only | #47 | merged | integrator |
| M007 | Candidate task can be inspected, control-tested, mutation-tested, and packaged for review without self-registration or publication | Tasks | OpenAI Codex / GPT-5 | worktree removed; branch `role/m007-task-workbench` still present locally and on `origin` (squash-merged, spent — do not reuse) | `src/evallab/task_workbench.py`, `tests/test_task_workbench.py`, `tests/fixtures/task_workbench/`, `library/synthetic/`, `research/registration/candidates/`, `docs/task-workbench.md`, archived handoff | M005 | met after four review-and-repair rounds plus one packet withdrawal; merged at head `4d47054` as `86380b0`. `admission_granted` false, nothing self-registers, `library/registry/` still empty | #49 | merged | integrator |
| COORD-GC | The coordination layer describes the repository that exists: spent handoffs archived, board factually correct, structure map true, stale CLI claims retired | Integration | Claude Opus 4.5, Oh My Pi | worktree removed; branch `role/coord-gc` still present locally and on `origin` (squash-merged, spent — do not reuse) | `agents/handoffs/`, `agents/archive/`, `agents/missions/ACTIVE.md`, `agents/STRUCTURE.md`, `docs/prompts/README.md`, `docs/checkpoints/2026-08-14.md` | none | met; merged at head `a41266e` as `2173268`. Archived 34 handoffs 1:1; found and fixed an already-merged root-freeze violation (`dashboard/` absent from `STRUCTURE.md`) | #54 | merged | integrator |
| PERF-REBASELINE | The `ingest` CI perf budget is calibrated to measured CI reality instead of a laptop capture, so the gate fails on regressions rather than on runner noise | Platform | **not recorded** — the archived handoff names no agent or model anywhere, so this mission's executing identity is unrecoverable from the repository | worktree removed; branch `role/perf-rebaseline` still present locally and on `origin` (squash-merged, spent — do not reuse) | `scripts/profile/budgets.json`, `docs/engineering.md` (one appended dated subsection), archived handoff | none | met; merged at head `a12ea3c` as `e080dd0`. Budget set to 115.0 ms from 14 CI artifact samples. Left the measurement-region cause open — now a mission candidate above | #53 | merged | integrator |
| SYSTEM-CARTOGRAPHER | The evaluation R&D platform is mapped as it exists, with corrected component cards and closed status labels | Integration | Grok 4.6 (xAI), Grok Build TUI | worktree removed; branch `role/system-cartographer` still present locally and on `origin` (squash-merged, spent — do not reuse) | `docs/system-cartography.html`, `docs/checkpoints/2026-08-15-system-cartography.md`, archived handoff | none | met; merged at head `a408881` as `1471f41`. 19 cards and 29 CLI groups corrected | #52 | merged | integrator |
| M009 | The merged lab is proven as one local, restartable Harbor-to-analysis product, with exact recorded evidence and every failure turned into a narrowly scoped follow-up | Integration (integrator-run acceptance exercise) | integrator session; recorded in its archived handoff | worktree removed; branch `role/m009-flight` is squash-merged and spent | flight record plus `agents/archive/2026-08-23-handoffs/m009-flight.md`; no feature lease | M006, M007 | met; free control run, indexed saved-response analysis, explorer inspection, and recovery evidence merged as `ad67126` | #56 | merged | integrator |
| BOARD-REFRESH | `agents/handoffs/` holds only live missions, and the board states current truth with recorded follow-ups that cannot vanish with an archived file | Integration | Claude Opus 4.5, Oh My Pi | worktree removed; branch `role/board-refresh` is squash-merged and spent | `agents/missions/ACTIVE.md`, `agents/handoffs/`, `agents/archive/`, `agents/archive/2026-08-23-handoffs/board-refresh.md` | none | met; archival and board refresh merged as `510713b` | #55 | merged | integrator |
