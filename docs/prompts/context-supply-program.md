---
status: living
audience:
  - builder
  - runner
  - operator
---

# Context-supply program — feeding the system what it cannot be trained on

Peter's framing, adopted as the program's premise: **"I gotta feed it the
right info since I can't post-train it."** Subscription agents arrive with
frozen weights; the only lever this lab has over their competence at eval
work is what appears in their context at dispatch time. That makes context
a supply chain with the same engineering standards as any other data path
in this repo: versioned inputs, deterministic transforms, measured outputs.
This program builds that chain. It extends `docs/prompts/context-loops.md`
and slots into `docs/prompts/build-program.md` as the detailed
specification of Phase 2 (ABSORB), plus one loop (VERIFIER) that straddles
Phases 1–3. The shared cycle protocol from `docs/prompts/night-loops.md`
governs all loops (RECHECK → EXTEND → PROVE → HARDEN → RECORD; max 6
cycles/night; the standing never-list applies; subscriptions only; GATE
for anything billable-class; registry promotion human-only).

The chain has four stages, one loop each:

    RAW SOURCES ──HARVEST──▶ inbox notes ──STANDARDS──▶ corpus files
    corpus files ──PACK──▶ compiled context ──(missions)──▶ agent work
    verification know-how ──VERIFIER──▶ corpus + experiments + SG-4 feed

---

## LOOP-HARVEST — raw sources into distillable inbox notes

**Problem this loop solves.** The lab's source material is scattered across
media the agents cannot reach: Peter's Google Drive (now archived into four
live files plus an archive folder), paper PDFs, X threads, GitHub repos,
and book-length PDFs. Agents building context packs can only consume what
lives in the repo as text. HARVEST is the intake valve: everything worth
absorbing gets landed in `research/inbox/` as a markdown note with
provenance, where STANDARDS can distill it. Nothing else in the program
touches external sources directly — this loop is the single doorway, which
keeps provenance honest and prevents five missions independently
re-fetching the same paper.

**Lease:** `research/inbox/**` (append and edit its own notes only; the
inbox is append-friendly by design), `tests/test_inbox_conformance.py`
(new, small: front-matter conformance check).

**What already sits in the inbox at program start.** Two deposits made by
the operator session on 2026-08-18/19: `drive-salvage-2026-08-18.md`
(links extracted from Drive before the archive sweep — scaffold-effect
paper 2607.22585, deepswe.datacurve.ai, reward-hacking thread, Lilian
Weng's harness post, Anthropic's demystifying-evals post, and more) and
`drive-evals-benchmarks.md` (Peter's own distillation of TB craft:
bad-task taxonomy, hidden-knowledge doctrine, verifier debugging
heuristics, the 9-step TB3 construction sequence, benchmark links). Treat
both as cycle-1 RECHECK material: they are already harvested; verify
front-matter/provenance conformance and move on.

**Intake queue (ordered; one or two items per cycle).**
1. Meta-Task appendix components (arXiv 2607.27929 HTML) — fetch targets
   are pinned precisely in `context-loops.md` EX-MT (F.1/F.2/F.3, B, D).
   HARVEST lands the raw text with figure/appendix references; STANDARDS
   adapts it. Do not summarize at intake — land verbatim-with-provenance;
   distillation is a separate, reviewable step.
2. llm-as-a-verifier repo (github.com/llm-as-a-verifier/llm-as-a-verifier)
   — intake the README, `criteria/TEMPLATE.md`, `criteria/terminal_bench.md`,
   and the pairwise/progress prompt structures. Note the dataset pointer:
   `data/terminal_bench_2.1_trajs/` (full TB 2.1 trajectory corpus —
   flagged for LOOP-INGEST/TRAJ as an external trajectory dataset; fetch ≠
   register discipline applies).
3. SWE-smith (github.com/SWE-bench/SWE-smith) — pipeline stages and
   validation gates, from code and docs.
4. METR task-standard + guidelines (github.com/METR/task-standard).
5. TOFFEE repo — inversion pipeline steps.
6. Scaffold-effect paper 2607.22585 — methods section (harness deltas
   measured; feeds the elicitation ladder rationale).
7. Drive stragglers, Peter-assisted: the four book PDFs in Drive > Books
   are NOT harvested wholesale (book-length, low density, copyright);
   instead, when a specific chapter becomes load-bearing, Peter exports
   that chapter's notes himself. The two mega-dumps (.Build, Notes -
   Content) stay archived — HARVEST takes curated sources, not chat logs.

**Note format (enforced by the conformance test).** Front-matter:
`source_url`, `source_type` (paper|repo|thread|drive|blog), `retrieved`,
`license_note` (verbatim-quotable? paraphrase-only?), `status`
(raw|distilled|superseded), `feeds` (list of standards-corpus targets).
Body: verbatim material clearly fenced and attributed; harvester
commentary kept to a short "why this matters here" paragraph. X/Twitter
threads: paraphrase claims with the link — do not reproduce long text.

**Acceptance (loop-done).** Intake queue items 1–6 landed conformant;
conformance test green in CI; every note's `feeds` field names at least
one STANDARDS target or explicitly `parked`; zero unfetched items remain
in the queue file; new-source suggestions discovered mid-program were
appended to the queue file rather than chased mid-cycle.

---

## LOOP-STANDARDS — inbox notes into the versioned craft corpus

**Problem this loop solves.** Raw sources are too long, too duplicative,
and too unstructured to inject into missions. The standards corpus
(`library/curated/standards/`) is the distilled, versioned, citable form:
one claim-dense file per source-topic, each entry carrying its provenance,
so a context pack can pull "instruction-writing rules" or "verifier
anti-patterns" as a unit, and an agent reading the pack can trace any rule
back to its origin. This is the difference between "the fleet read some
papers once" and "the lab owns its craft knowledge."

**Lease:** `library/curated/standards/**`, `tests/test_standards_conformance.py`;
`_proposed_templates/` staging as in `context-loops.md`; SG-owned
`authoring/templates/` only via board-note handshake.

**Corpus file format.** Front-matter: `derived_from` (inbox note paths),
`version`, `changelog` (one line per version with evidence citations),
`facets` (craft facet tags so packs can match tasks), `confidence`
(established|single-source|contested). Body: numbered, claim-dense rules
or templates; every nontrivial claim cites its inbox note. Hand-edits are
allowed here (this is curated, not generated), but version must bump and
changelog must say why.

**Cycle backlog (each EX is one to two cycles).**
1. **EX-MT** — as pinned in `context-loops.md`, with the task-instruction
   template (F.1/Figure 6) treated as the centerpiece per Peter's explicit
   direction. Adaptation mapping for `_proposed_templates/meta-instruction-v1.md`:
   their "Reference Study" section → our exemplar-and-experience-pack slot
   (LOOP-EXPERIENCE supplies content); "System Architecture" → our
   environment conventions (pinned deps, no live external services, docker
   patterns from the craft corpus); "Output Structure" → Harbor layout
   (task.toml / instruction.md / environment/ / solution/ / tests/);
   "Dockerfile Best Practices" → merge with our reproducibility rules;
   their 9-step "Self-Validation (REQUIRED)" checklist → mapped onto our
   battery vocabulary (their execute-solution/run-tests/consistency/
   leakage checks become: oracle must pass, nop reasoning, answer-leakage
   scan, cross-component consistency) PLUS our two additions they lack:
   fair-oracle framing and the adversarial "please hack" pass; their
   "Final Checklist" → qualification-ledger field list. The instruction-
   writing RULES from Peter's own Drive distillation (brevity, end-state
   clarity, no over-prescription, no clerical difficulty, agent-as-smart-
   human) become `instruction-rules.md` — this file is the one most
   missions will quote.
2. **EX-TB** — review-patterns from CONTRIBUTING + rubrics + 5–10 real PR
   threads; each pattern cites its PR. Merge Peter's taxonomy-of-bad-tasks
   phrasing (inbox: drive-evals-benchmarks) where it names the same
   pattern better.
3. **EX-METR** — task-standard digest: spec fields, QA checklist,
   human-baseline method.
4. **EX-SMITH** — pipeline stages + gate-comparison table (their gates vs
   our battery; adopt/skip verdict per row with one-line reasons).
5. **EX-TOFFEE** — inversion recipe; seed_class 4 sketch.
6. **EX-DRIVE** — distill the remaining unique content of the inbox Drive
   export into: `verifier-antipatterns.md` (hidden knowledge, backwards-
   built verifiers, over-specificity probe), `task-debugging.md` (the
   three-way split + container-shell techniques + failure triage), and
   merge the 9-step construction sequence into `authoring-workflow.md`.

**Iterate-and-improve mechanism (standing, from build-program):** template
and rules files are versioned; every battery/A-B outcome that used them
appends an evidence line to the qualification ledger; version N+1 cites
the rows motivating the change. STANDARDS never edits based on taste
alone once evidence exists.

**Acceptance (loop-done).** All six EX groups landed; conformance test
green; every corpus file's `derived_from` resolves; `instruction-rules.md`
and `meta-instruction-v1` staged with the board-note filed; at least one
corpus file already at version ≥2 driven by a cited evidence line (proof
the improve loop turns).

---

## LOOP-VERIFIER — absorb verification practice, adapt it to a no-logprob lab

**Problem this loop solves.** Verification is the scarce half of the whole
field, and the lab's verification knowledge is currently thinner than its
task-writing knowledge. The llm-as-a-verifier work (self-verification
lifts Terminal-Bench 2.1 best-of-5 from 78.7% to 88.0%±0.6 against an
oracle ceiling of 96.6%) is the strongest published recipe — but it
requires token-level logprobs (DeepSeek/Vertex/vLLM lanes), which
subscription CLIs do not expose. This loop absorbs the ideas at three
levels: corpus (what packs teach), adaptation (what we can run today
without logprobs), and experiment (what we measure when the pieces land).
It feeds SG-4 (verifier selection/calibration) via board-note rather than
writing in its lane.

**Lease:** `library/curated/standards/verification/**`,
`research/experiments/verifier/**`, `tests/test_verifier_corpus.py`;
TRAJ/SEAM/calibrate touchpoints via board-note only.

**Cycle backlog.**
1. **Corpus intake→distill:** from the HARVEST note, land
   `verification/score-token-method.md` (the method: expectation over the
   score-token logprob distribution on a 1–20 letter scale; why continuous
   beats discrete verdicts; PPT best-of-N at O(Nk); prefix-cache
   structuring; their reported numbers with the oracle ceilings quoted
   honestly) and `verification/criteria-templates.md` (their
   `criteria/TEMPLATE.md` + terminal_bench criteria structure + the
   pairwise prompt shape + the progress question "would the agent's
   CURRENT state already complete the task?").
2. **Adaptation design (the honest constraints section):** subscription
   CLIs return text, not logprobs. Three sanctioned approximations,
   written up with their known costs: (a) **k-sample discrete
   approximation** — ask the judge for the 1–20 score k times
   independently, average; measures noisier than true expectation; its
   agreement vs our answer-keyed calibration corpus must be measured
   before any real use (calibrate.py lane, board-note). (b) **self-
   verification instruction blocks** — generated tasks (and eventually
   missions) carry a final self-check section derived from Meta-Task F.1's
   self-validation ∪ the progress question; costs nothing, testable in the
   EXPERIENCE A/B as a third arm if capacity allows. (c) **progress-curve
   heuristics** — the ProgressTracker idea implemented deterministically
   in TRAJ features first (fraction of verifier-relevant artifacts present
   per step; loop-suspicion crossings), model-scored later behind SEAM.
   A fourth path is named and PARKED with its price: a metered logprob
   lane (DeepSeek API) is the only faithful implementation; it would be
   the lab's first real API spend, requires GATE + a Peter decision, and
   is NOT authorized by this document.
3. **External corpus intake:** their `data/terminal_bench_2.1_trajs/`
   fetched as an external dataset (fetch ≠ register; contamination note:
   these are a public model's rollouts on public tasks). Value: TRAJ gains
   a large labeled-outcome trajectory corpus to sharpen features and
   heuristic labels against; INGEST verify covers it like any source.
4. **Experiment spec (pre-registered, runs when SEAM cycle 2 exists):**
   best-of-N reranking on our own real trials — tasks where k≥3 attempts
   exist; judge-rerank via adaptation (a); compare selected-attempt pass
   rate vs pass@1 and vs oracle-best; report raw counts with the
   detectable-effect floor stated up front. Card:
   `research/cards/verifier-rerank-01.md` (file-disjoint from LOOP-CARDS'
   lease by the experience-* / verifier-* naming convention — extend the
   night-loops lease note accordingly in the board row).
5. **SG-4 feed:** one board-note summarizing what the verification corpus
   changes for verifier selection (when to prefer programmatic, hybrid,
   judge; what evidence each requires), citing corpus files.

**Acceptance (loop-done).** Both corpus files landed and cited; adaptation
doc names its three approximations with measurement plans and the parked
fourth with its price; TB2.1 trajectory corpus fetched and verified by
INGEST; rerank experiment pre-registered (spec + decision rule on file,
dispatch blocked only on SEAM); SG-4 note filed.

---

## LOOP-PACK — corpus into compiled, budgeted, measured context

**Problem this loop solves.** A corpus nobody injects is a library nobody
visits. Packs are the delivery mechanism: `evallab context build
<mission_type> [--task REF]` compiles exactly the context a mission needs
— living docs for its audience, standards entries matched to the target
task's facets, lessons rows, the mission brief — into one deterministic
file with a content hash. This loop finishes PACK from a compiler into a
measured system: budgeted, fresh, cited, and demonstrably worth its
tokens. "Provide enough relevant context" (Peter's goal) becomes an
engineering property with tests, not an aspiration.

**Lease:** `src/evallab/contextpack.py`, `tests/test_contextpack*`,
`docs/` front-matter fields it needs; digest/STATUS surface via board-note
to the SURFACE owner.

**Cycle backlog.**
1. **Standards section wiring:** packs gain a standards section selected
   by facet overlap with the target task (craft Parquet join); with no
   `--task`, audience-level defaults (authoring missions always get
   `instruction-rules.md` + `verifier-antipatterns.md`; analysis missions
   always get the taxonomy + reading-protocol entries).
2. **Budgets:** per-audience token budgets as config, enforced at build
   (authoring default ≤8k tokens for the standards section, ≤20k total;
   numbers are config, not code). Over-budget → deterministic priority
   drop (lowest facet-match score first) with a visible "dropped: N
   entries" line in the pack header — silent truncation is the failure
   mode this cycle exists to prevent.
3. **Citation duty + freshness:** every pack section names its corpus
   file@version; builds warn when a cited file's version is older than a
   newer inbox note that `feeds` it (staleness signal, computed from
   front-matter alone). Two consecutive builds on an unchanged repo are
   byte-identical (existing determinism promise — now golden-tested with
   the standards section included).
4. **Retrieval tests:** fixture task with known facets → the pack must
   include the three facet-matched entries and exclude a deliberately
   irrelevant one. This is the corpus's unit test as much as the
   compiler's.
5. **Effectiveness hook:** packs are an experimental variable. The
   EXPERIENCE A/B (arm B carries the pack) is the first measurement; its
   evidence lines land against the template/corpus versions used, closing
   the loop STANDARDS needs for versioning. Additionally, every mission
   brief the orchestrator issues from now on cites the pack hash it was
   built with — so any later "that mission went weird" investigation can
   reproduce the exact context.
6. **Skills variant (optional last cycle):** compile the two or three
   evergreen corpus files into repo-local agent skills
   (`.claude/skills/`, per WS-F) so interactive sessions get standing
   craft context without a build step; packs remain the mission-time
   mechanism.

**Acceptance (loop-done).** Standards-aware packs build deterministically
under budget with citations; retrieval + golden tests green; staleness
warning demonstrated in a test; at least one real mission brief on the
board carries a pack hash; EXPERIENCE evidence lines reference pack/corpus
versions.

---

## Continuous execution protocol (until done)

**Registration.** Orchestrator (OMP tab 1) registers HARVEST, STANDARDS,
VERIFIER, PACK as M-missions with the leases above. Dependencies:
STANDARDS consumes HARVEST notes (start after HARVEST cycle 1; run
concurrently thereafter — HARVEST stays ahead by at least one intake).
VERIFIER cycle 1 needs its HARVEST note only. PACK cycle 1 needs the first
two STANDARDS files. Nothing here blocks Phase-1 missions (INGEST, TRAJ,
SEAM) — they run in parallel under build-program priority: **if subagent
slots are scarce, Phase 1 wins.**

**Cadence.** Nightly cycles under the standard protocol. Every cycle ends
with the loop's mission PR updated, premerged, and the handoff header
current — a program interrupted at ANY cycle boundary loses nothing.
Morning: orchestrator integrates finished cycles, updates the board, and
refreshes STATUS ("context-supply: HARVEST 4/6 intake, STANDARDS EX-MT
landed@v1, PACK budget cycle in review").

**Resumption after interruption** (quota death, machine sleep, session
loss): re-read this doc, read your own handoff's Next line, RECHECK first
— never trust memory over the repo. The board row's state field is the
truth about whether a cycle merged.

**Blocked handling.** A loop blocked on a handshake (SG lane, SURFACE
digest hook, calibrate lane) files the board-note, marks the cycle
`blocked` on the board with what unblocks it, and takes its next
non-dependent backlog item. A loop blocked on quota defers to the next
night. Two consecutive blocked nights on the same item → escalate to
Peter's morning read via STATUS open-decisions.

**Scope discipline.** New sources discovered mid-program append to the
HARVEST queue file with one line of why — they do not get fetched
mid-cycle. New loop ideas go to the board's backlog section, not into
this doc; this doc changes only by Peter-level direction (changelog
below). The program is DONE when all four loop-done acceptances hold;
the orchestrator then writes a completion note in STATUS naming the
corpus version set, and the standing residue is: HARVEST reopens on
demand when the queue file gains entries; PACK budgets and staleness
warnings run forever as part of normal builds.

**Reporting.** Weekly rollup line in the digest: corpus files and
versions, packs built with hashes, evidence lines appended, experiments
pre-registered/run. The program KPI joins build-program's: battery
pass-rate per template version — this program's success shows up there or
it didn't happen.

## Dispatch prompt (paste to the orchestrator verbatim)

> Read docs/prompts/context-supply-program.md and register its four loops
> (HARVEST, STANDARDS, VERIFIER, PACK) on the board with the leases and
> dependencies as written; dispatch subagents under the continuous
> execution protocol section, Phase-1 missions keep slot priority.
> HARVEST cycle 1 starts tonight: RECHECK the two existing inbox notes,
> then intake queue item 1 (Meta-Task appendices). Report per the
> protocol's reporting section; escalate only per its blocked-handling
> rules.

## Changelog

- 2026-08-19 — v1: four-loop supply chain (HARVEST → STANDARDS → PACK,
  plus VERIFIER), written after harvesting Peter's Drive evals notes into
  research/inbox/ and mapping the llm-as-a-verifier repo (components,
  TB2.1 numbers, logprob constraint, trajectory dataset).
