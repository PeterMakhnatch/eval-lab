---
status: living
audience:
  - builder
  - runner
  - operator
---

# Night loops — convergent improvement missions

Five overnight missions structured as LOOPS, not builds. Context: ~55 missions
have merged in days; the standing risk is divergence — built ≠ proven ≠ used
(current symptoms: stale board, `research/cards/` empty, `docs/STATUS.md`
absent while `status_generator.py` exists). Each mission below cycles over
existing work: re-verify it, extend it one step, harden it with a test,
record. Every cycle ends with main mergeable.

How to dispatch: give this doc to the Integrator session; it refreshes the
board, assigns M-numbers, and confirms leases. Or paste one mission block per
herdr session directly — each block is self-contained given repo access.

## Integrator pre-flight (do before dispatching)

- `agents/missions/ACTIVE.md` is stale (written at `86380b0` / PR #49 era;
  main is `f836f6c` / PR #116). Refresh it first — a stale board is worse
  than no board (its own words).
- Leases below are drawn disjoint from each other AND from SG lanes
  (`authoring.py` internals, `library/meta/`, `authoring/templates/`,
  `calibrate.py`). Confirm against anything currently in flight.
- All five missions are free-lane only: local commands, oracle/nop controls
  through the queue at most, no billable dispatch, no GATE requests.
  Claude-token-dependent items defer as usual.

## The loop protocol (shared standing orders — every mission embeds this)

Run CYCLES, max 6 per night, each roughly ≤90 minutes:

1. **RECHECK** — re-run your own previous cycle's acceptance against current
   `origin/main` in your worktree. First cycle: re-run the claimed acceptance
   of the merged work your backlog builds on. A recheck failure becomes this
   cycle's work; nothing new starts until it's green again.
2. **EXTEND** — take the next backlog item. One item per cycle.
3. **PROVE** — the item's acceptance is a runnable command; paste its real
   output into the handoff.
4. **HARDEN** — add one test that would have caught the most likely
   regression of what you just built (property / golden / contract — match
   existing repo style).
5. **RECORD** — update the handoff 4-line header + append a cycle log line;
   open or refresh the mission PR (`M###: <slug> — cycle N`); one PR per
   mission-night, one commit per cycle; `scripts/premerge.sh` before every
   push.

Stop conditions (stop cleanly and record — don't limp): backlog empty; two
consecutive cycles with no net change; the same failure surviving two
different fix attempts; any lease conflict (stop + record, never resolve
unilaterally); main CI red for reasons not yours (note it, keep working
locally, don't rebase onto red).

Never, regardless of backlog: `evallab registry promote` (human-only);
`policy/` edits; billable runs; force-push; writes outside your lease;
deleting anything under `runs/` or `research/evidence/`.

---

## LOOP-AUDIT — prove what the handoffs claim

MISSION (M-number from Integrator). Worktree `.worktrees/m###-audit`, branch
`role/m###-audit`. Follow WORKFLOW.md + the loop protocol above.

**Lease:** `research/audits/**` (create it) — and read access to everything.
This mission writes ONLY its own ledger. No fixes, no edits elsewhere:
findings become ledger rows + `research/audits/board-notes.md` entries for
the Integrator. That keeps you conflict-free with every other loop.

**The loop:** one audit subject per cycle. For each: (a) read its handoff in
`agents/handoffs/<name>.md` and extract every checkable claim ("X works",
"acceptance: command Y"); (b) re-run those claims against today's
`origin/main` after fresh `uv sync`; (c) write a ledger row with verdict —
CONFIRMED / DRIFTED (was true, broke since) / UNPROVEN (claim isn't runnable
as stated) / FALSE — plus the exact commands and output evidence under
`research/audits/evidence/<subject>/`; (d) one-line risk note.

**Audit queue (ordered — invisible-surface modules first):** preflight,
storm, parquet-compaction, status/status_generator, spine purpose gate,
quota accounting vs. events.jsonl truth, attach surface, contextpack
determinism, canary suite paths vs. `library/tasks/`, behavior, provenance,
backups (restore path, not just dump).

**Ledger format:** `research/audits/ledger.md` — one table: date | subject |
handoff | verdict | evidence path | risk note. Append-only.

**Acceptance per cycle:** one new ledger row whose commands a stranger could
re-run. **Night acceptance:** ≥4 subjects audited, ledger + board-notes
committed, PR open.

---

## LOOP-SURFACE — the generated surfaces actually generate

MISSION (M-number from Integrator). Worktree `.worktrees/m###-surface`,
branch `role/m###-surface`. Follow WORKFLOW.md + the loop protocol.

**Lease:** `src/evallab/status_generator.py`, `src/evallab/digest.py`,
`docs/STATUS.md`, `tests/test_golden_rendering.py`, `tests/golden/**`;
`.github/workflows/**` only if one hook line is genuinely needed (prefer the
existing nightly path).

**Backlog (one item per cycle):**
1. Wire `status_generator` to a CLI entrypoint if absent; generate the first
   real `docs/STATUS.md` from live data and commit it. Golden test for the
   section skeleton.
2. Regeneration hook: STATUS refreshes on the existing nightly (not a new
   workflow if avoidable). Two consecutive generations on unchanged inputs
   must be byte-identical — determinism is the test.
3. Digest gains, if absent: a preflight section (per-provider quota
   remaining), a storm-alarm section, and a lessons section. For lessons,
   call a public function from `lessons.py` — additive import only; if the
   function doesn't exist yet, file a board-note for LOOP-LESSONS instead of
   editing their file.
4. Digest links the newest DISCOVERIES entries awaiting verdicts
   (read-only render).
5. Golden-file coverage for every section added tonight.

**Night acceptance:** `docs/STATUS.md` exists on the PR and answers "what
happened yesterday" with zero terminal use; golden tests green; determinism
proven in the handoff.

---

## LOOP-CARDS — turn finished studies into eval cards

MISSION (M-number from Integrator). Worktree `.worktrees/m###-cards`, branch
`role/m###-cards`. Follow WORKFLOW.md + the loop protocol.

**Lease:** `src/evallab/cards.py`, `tests/test_cards.py`,
`research/cards/**`.

**The gap:** `research/cards/` contains only README + TEMPLATE. Studies have
finished; zero cards exist. One card per cycle, from EXISTING data only — no
new runs.

**Card queue:** 1) the canary/drift suite (n, pass pattern, interval via
cohort.py); 2) judge calibration (the codex-below-0.90 finding: agreement
score, what it blocks); 3) the eight-run oracle-vs-codex cohort drafted in
DISCOVERIES `D-20260815-CHEY952N` (verdict framing: instrument finding, not
capability claim); 4) SG-1 meta-loop first outputs (what was produced, what
the battery said); 5) the behavior study (PR #100) as a descriptive card.

**Rules:** every number carries n + an interval, or the literal words
"insufficient n". Contamination and elicitation caveat lines are mandatory —
if the template lacks those fields, extend the template FIRST (cycle 1).
Cards must be regenerable: each embeds the exact queries/commands behind its
numbers. Where cards.py can't express something, extend it with tests.
If no small validator exists, add `evallab cards validate` (schema +
mandatory-caveats check) as part of cycle 1.

**Acceptance per cycle:** one card file + validator pass. **Night
acceptance:** ≥3 real cards merged, each independently regenerable.

---

## LOOP-FUZZ — property-test the state machines

MISSION (M-number from Integrator). Worktree `.worktrees/m###-fuzz`, branch
`role/m###-fuzz`. Follow WORKFLOW.md + the loop protocol.

**Lease:** new files matching `tests/test_*_properties.py`,
`tests/fixtures/**` additions. Source fixes ONLY for bugs the fuzz actually
finds: each in its own commit prefixed `fuzz-fix:`, ≤30 changed lines;
anything larger, and ANY fix touching `authoring.py` or `calibrate.py`
(SG lanes), becomes a board-note instead. Dependency `hypothesis` is
sanctioned by build-plan WS-F; if absent from pyproject, add it in its own
commit.

**Backlog (one state machine per cycle):**
1. Queue: if property tests exist from the queue-fuzz mission, RECHECK then
   extend; invariants — no spec lost, no double dispatch, credential
   deferral preserves approved state, vanished-file tolerance, quota never
   exceeded mid-tick.
2. Quota accounting: never negative, never exceeds ceiling, UTC-day
   rollover correctness, interleaved reserve/release sequences.
3. Authoring proposal state machine: only legal transitions
   (proposed → battery_passed → craft_reviewed → registered|rejected), no
   skips, ledger append-only. Tests only — src fixes via board-note (SG lane).
4. Parquet compaction: idempotence (recompact = byte-stable) and no row
   loss vs. source data.
5. Attach/catalog rebuild: drop derived, rebuild, same query results.

**Acceptance:** each new suite runs ≤60s in CI (mark slower profiles for
nightly); every fuzz-found bug gets a minimal regression test alongside its
fix; CI green.

---

## LOOP-LESSONS — aggregates with honest gates

MISSION (M-number from Integrator). Worktree `.worktrees/m###-lessons`,
branch `role/m###-lessons`. Follow WORKFLOW.md + the loop protocol.

**Lease:** `src/evallab/lessons.py`, `sql/lessons.sql`,
`research/lessons.md`, `tests/test_lessons*.py`.

**Backlog (one item per cycle):**
1. RECHECK the existing views against live data: rows return; where live n
   is too thin, views must render "insufficient n" — never crash, never
   emit empty-as-finding.
2. Statistical gates: every emitted row carries n + a cohort.py interval or
   the insufficient-n marker; refuse-to-rank propagates from cohort.
3. Add `v_outcome_by_verifier_type` and a `v_failure_by_facet` join to the
   craft parquet (read-only join; no craft.py edits).
4. Regeneration determinism: `research/lessons.md` regenerates
   byte-identically twice from a clean clone; generated-by header intact
   and its hand-edit detection tested.
5. Expose a public `lessons_digest_section()` for LOOP-SURFACE to call;
   file a board-note when it's ready (additive only).

**Night acceptance:** ≥3 views live on real data, lessons.md regeneration
proven deterministic, gates tested.

---

## Dispatch order and slots

All five leases are mutually disjoint — up to five parallel sessions, or any
subset sequentially; the loop protocol makes partial nights safe (every cycle
ends mergeable). If short on slots: AUDIT and FUZZ first (they need nothing
from anyone), then SURFACE, CARDS, LESSONS in any order.

## Morning read (Peter, ~10 min)

`scripts/fleet-status.sh`, then three files tell you if the night meant
something: `research/audits/ledger.md` (what's actually true),
`research/cards/` (what we now know), `docs/STATUS.md` (what happened).
The meaning test: you can learn something true about the lab from those
three without opening a terminal.

## Changelog

- 2026-08-18 — v1: five convergence loops (AUDIT, SURFACE, CARDS, FUZZ,
  LESSONS) + shared loop protocol, written at `f836f6c`.
