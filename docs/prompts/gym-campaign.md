---
status: living
audience:
  - builder
  - runner
  - operator
---

# Gym campaign — runs tonight, data tomorrow, the gym starts breathing

Peter's direction (2026-08-19 eve): the lab is a GYM — a growing collection
of environments plus the machinery to run them, capture everything, analyze
trajectories, and evolve the collection. Tonight's missions make the gym
BREATHE: real runs dispatched, analysis moving, experiments unblocked, more
data acquired, visibility built. Long-running missions — expect multiple
nights; every cycle boundary is merge-safe per the night-loops protocol.

Standing constraints unchanged: subscriptions only; quota metering (#64);
GATE for billable-class; preflight sizes every batch; registry promotion
human-only; fetch ≠ register for external data.

Priority note: build-program Phase-1 (INGEST, TRAJ, SEAM) is REGISTERED
INTENT but not yet built — those three missions keep top build priority
because GYM-UI and analysis depend on them. Context-supply (M025–M028)
continues per its own protocol. This doc adds the run/data/visibility
lanes that make the gym generate interesting data while the builds land.

## GYM-RUN — the first campaign: fill the lake, ship the experiments

**Lease:** `queue/**` submissions (through the CLI, never hand-written
files), `library/frozen/gym-v0/**` (new manifest), one schema addition
(below), `research/cards/campaign-*.md`.

**Cycles:**
1. **Freeze gym-v0.** Write `library/frozen/gym-v0/manifest.json`: every
   currently-registered task (registry list at runtime), each with task
   digest and battery evidence pointers. All campaign results cite
   `gym-v0`. Frozen means frozen: the manifest never changes; gym-v1 is a
   new file. This is what makes next month's numbers comparable to
   tomorrow's.
2. **Unblock EXP-S03 (the one-field fix the PROGRAM ledger already
   specifies):** add `extra_instruction_path` to ExperimentSpec
   (schemas.py) and forward `--extra-instruction-path` in build_command.
   Small, tested, its own PR. This is the elicitation lever — the
   scaffold-effect experiment class needs it.
3. **Dispatch the campaign, sized by preflight:** every gym-v0 task ×
   codex × k=3 (canary policy cap), purpose=baseline, PLUS one oracle
   control per task family (instrument re-validation, free). If preflight
   says the codex daily quota can't hold it, run families in quota-sized
   waves across nights — the campaign is done when every gym-v0 task has
   k=3 scored codex attempts on record.
4. **Submit the unblocked pre-registered experiments:** EXP-S03 treatment
   arm (preamble A/B, paired with the 2026-08-15 control per the ledger —
   never fabricate a second control); EXP-S06 (query-optimize family
   validity) if policy admits it. EXP-S02/S04/S05 stay blocked on Peter
   decisions (listed at the bottom) — do not work around ceilings or
   provision credentials.
5. **Campaign card:** `research/cards/campaign-gym-v0.md` — what ran, per-
   task raw counts, interval via cohort, instrument notes, and the honest
   caveat block (contamination status per task, elicitation level =
   default harness, k=3).

## GYM-DATA — more data than trajectories

Peter: "want MORE data, not just trajectories, idk how." Here is how — two
kinds: acquire external corpora, and start capturing data types the lab
emits but doesn't keep.

**Lease:** `research/external/**` (new), `src/evallab/fetch.py` (extend),
`sql/external_views.sql`, funnel/STATUS line via board-note to SURFACE.

**Cycles:**
1. **Harbor Hub trial corpus:** harbor-index publishes its 82 tasks and
   all 1,476 leaderboard trials (frontier agent-model pairs, ATIF
   included) — already noted in `docs/research/external-datasets.md`.
   Fetch via the pinned-acquisition path (fetch.py), verify digests, land
   under `research/external/harbor-index/`. This multiplies the lab's
   trajectory holdings ~15× for free and gives TRAJ/feature work a
   frontier-diverse corpus. Contamination note mandatory: public rollouts
   on public tasks — behavior-study material, never capability claims.
2. **llm-as-a-verifier TB2.1 trajectories** (`data/terminal_bench_2.1_trajs/`
   in that repo): same discipline. Both corpora enter INGEST verify's
   scope as external sources (join spine: external flag, no reward
   recompute).
3. **New capture: analysis narratives.** Design decision, small module:
   every model-assisted analysis (when SEAM lands) and every researcher
   pass stores its full reasoning transcript as a first-class artifact
   keyed to the trial it analyzed (`analyses` already exists — add the
   transcript pointer + render path). The gym's most under-collected data
   is what the ANALYST thought — Peter explicitly wants to read it.
4. **New capture: environment truth deltas.** Audit what Harbor already
   records per trial (verifier stdout, artifact digests, exit codes,
   container logs) vs what ingest keeps; close the keep-gap (cheap: they
   are files already on disk — catalog them). If `harbor analyze` or ATIF
   extensions expose more (see GYM-HARBOR), wire it here.
5. **Funnel + holdings line in STATUS** (board-note to SURFACE): trials
   held by source (lab / harbor-index / tb2.1-trajs), labels held, analyses
   held, proposals funnel counts. "Interesting data" needs a visible
   inventory to steer by.

## GYM-HARBOR — use the Harbor we already have

**Lease:** `docs/research/harbor-capability-audit.md` (new),
`src/evallab/fetch.py`/`runner.py` touchpoints only with board-row note.

**Cycles:** (1) Systematic audit of Harbor 0.21 surface vs. our usage:
`harbor analyze` (what does it compute that our analysis doesn't?),
`harbor check` (task validation — does it belong in the battery?), `hub`
dataset pulls (task datasets beyond TB3 — enumerate what's pullable),
`plugins` (what exists?), trajectory export formats, `start-env -a -i`
(already doctrine for debugging — confirm documented in skills), sync.
Output: capability-audit doc with adopt/skip/park verdict per item and a
one-line reason — same format as the SWE-smith gate table. (2–3) Wire the
top adopt verdicts, each its own small PR (e.g., `harbor check` into the
battery if it adds checks ours lack; `analyze` output into analysis
sidecars if it computes novel features). (4) Cookbook pass: the Harbor
cookbook's recipes vs our patterns — anything better than ours, note in
standards corpus via inbox.

## GYM-UI — read every trajectory, and read the analyst's mind

Peter: "want visibility — read all trajectories nicely in the UI, and see
what my agents are thinking when analyzing particular trajectories."

**Lease:** `src/evallab/explorer.py` (extend — it is the existing
streamlit surface), `tests/test_explorer*`; TRAJ module consumed read-only
(board-note dependency: outline function from Phase-1 TRAJ; until it
lands, render raw ATIF steps with a simple built-in condenser and swap
later).

**Cycles:**
1. **Trajectory browser:** list all trials (lab + external corpora),
   filter by source/family/agent/outcome/label; click → outline view
   (phase markers, tool calls, errors, per-step tokens); toggle to full
   step detail. Target: Peter's daily 3-trajectory reading happens
   entirely in this view.
2. **Truth panel:** alongside the trajectory — verifier output, reward
   dims, artifact digests, exit codes (the world's point of view next to
   the agent's; the flight-recorder-plus-weather pairing from
   research-questions.md).
3. **Analyst panel:** for any trial with analyses — the analysis
   conclusion AND the full analyst reasoning transcript (GYM-DATA cycle 3
   capture), rendered side-by-side with the trajectory it analyzed.
   Researcher-pass narratives included. This is the "what were my agents
   thinking" view.
4. **Label-in-place:** Peter's taxonomy labels writable from the browser
   (calls the same path as `traj label`); reading queue honored (the
   day's 3 surfaced first).
5. **Phoenix deep-links** where a trace was shipped; no second
   observability stack — explorer is for reading, Phoenix stays for span
   timing.

## Tomorrow-morning definition of success (Peter's 10-minute read)

- STATUS shows: campaign wave 1 scored (N tasks × k=3 + controls), zero
  gaps from ingest on new trials, EXP-S03 treatment submitted or its PR
  merged, external corpus fetch started or landed.
- At least one NEW card: campaign-gym-v0 (even partial: "wave 1 of 2").
- Explorer shows the new trials; by night 2–3, the analyst panel shows
  its first reasoning transcript.
- Open decisions section lists ONLY the three Peter decisions below.

## Peter decisions (the campaign does not need these; the ledger does)

1. **EXP-S02:** raise per-job ceiling / canary max_attempts to allow k=5
   on transaction-reconciliation (measured est. <$0.15 actual compute at
   k=5 — the $3 ceiling is the binding constraint, not cost) — or accept
   k=3 and close the question.
2. **EXP-S05:** register a curated-nominee slice (the 5 cards) as tasks,
   or reject the study.
3. **EXP-S04 / the eternal one:** provision the Claude OAuth keychain
   item (`scripts/claude-token-setup.sh`) — unblocks the claude-code
   lane, Anthropic judges, inter-judge agreement, and half the roster
   questions. Still the single cheapest unblock in the lab.

## Dispatch prompt (paste to the orchestrator verbatim)

> Read docs/prompts/gym-campaign.md. Register GYM-RUN, GYM-DATA,
> GYM-HARBOR, GYM-UI on the board with the leases as written; confirm
> Phase-1 (INGEST, TRAJ, SEAM) missions are registered and keep top build
> priority; context-supply continues per its protocol. Tonight: GYM-RUN
> cycles 1–3 (freeze gym-v0, the EXP-S03 field fix, dispatch campaign
> wave 1 sized by preflight) and GYM-DATA cycle 1 (harbor-index corpus
> fetch). Long-running: continue nightly until each mission's cycles are
> done, reporting per the context-supply protocol; escalate only the
> three Peter decisions listed in the doc.

## Changelog

- 2026-08-19 — v1: written with queue empty, HARVEST c1 merged, Phase-1
  unbuilt, five pre-registered experiments blocked in the PROGRAM ledger.
