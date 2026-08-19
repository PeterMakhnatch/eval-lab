---
status: living
audience:
  - builder
  - runner
  - operator
---

# Context loops — teach agents the standards, wire the brain, buy experience

For the OMP orchestrator (Integrator): three loop missions answering Peter's
standing questions — "how do I teach agents the eval standards," "how do
agents get onboarded onto relevant context," and "how do agents run similar
envs to gain experience before building new ones" (the execution-grounded
synthesis pattern). Register with M-numbers; run under the shared cycle
protocol in `docs/prompts/night-loops.md` (RECHECK → EXTEND → PROVE →
HARDEN → RECORD, max 6 cycles/night, same never-list).

Context from tonight's scout pass (main OMP session, 2026-08-18): every
internal model-call seam is a refusing stub — `analyst.py` ModelAnalyzer
raises ModelProviderRefusedError even with `--model`; `analysis_worker`
defaults to `_no_adapter`; `authoring` novel-designer is deterministic; no
provider SDK installed. Also: the antigravity/Gemini subagent lane hit
`resource_exhausted` tonight — schedule scout-heavy work after quota reset
or on the codex lane.

Lease compatibility: drawn disjoint from night-loops missions (AUDIT,
SURFACE, CARDS, FUZZ, LESSONS) and SG lanes. One shared directory:
`research/cards/` — LOOP-EXPERIENCE may write only files matching
`research/cards/experience-*.md`; everything else in that directory belongs
to LOOP-CARDS.

---

## LOOP-STANDARDS — the standards become repo data agents consume

**Goal:** external eval-craft standards stop living in Peter's head/browser
and become versioned corpus files that context packs compile into authoring
missions. Teaching agents = curating what the pack compiler feeds them.

**Lease:** `library/curated/standards/**` (new), `tests/test_contextpack*`
additions, `src/evallab/contextpack.py` (audience wiring only).
`authoring/templates/` is an SG lane: stage adapted templates in
`library/curated/standards/_proposed_templates/` + board-note, never write
the SG path directly.

**Cycle backlog:**

1. **EX-MT — Meta-Task appendix extraction** (arXiv 2607.27929, HTML
   version). Pull these specific components, verbatim, one file each, with
   provenance headers (source URL, appendix ref, retrieval date):
   - `meta-task/F1-instruction-template.md` — Appendix F.1 / Figure 6: the
     fixed synthesis instruction ("Reference Study", "System Architecture",
     "Output Structure", "Dockerfile Best Practices", the 9-step
     "Self-Validation (REQUIRED)" checklist, "Final Checklist").
   - `meta-task/F2-spec-design-prompt.md` — F.2 / Figure 7: the multi-phase
     prompt that *designs* new (category, scenario) specs (output format
     "=== CATEGORY === / === SCENARIO ===").
   - `meta-task/F3-trajectory-judge.md` — F.3 / Figure 8: the KEEP/DISCARD
     trajectory judge (GOOD vs BAD: "Shortcutting", "Fabrication",
     "Unproductive behavior").
   - `meta-task/B-dimensions.md` — Appendix B: all 39 categories (with the
     example's "Task Pattern Ideas / Skills & Tools / Verification
     Approaches" structure), 10 scenario styles, 4 difficulty levels with
     anti-pattern lists.
   - `meta-task/D-review-rubric.md` — Appendix D: the 19-criterion
     proposal/implementation review pipeline (described; prompt not
     published — note that honestly).
   Then one ADAPTED file: `_proposed_templates/meta-instruction-v1.md` —
   F.1 rewritten for OUR task format and OUR battery (their self-validation
   checklist maps onto oracle/nop/fair-oracle/adversarial; keep their
   answer-leakage checks, add our contamination fields). Board-note to SG.
2. **EX-TB — Terminal-Bench as textbook.** Read-only `gh` against the TB
   repo: CONTRIBUTING, task quality rubric, and 5–10 task PR review threads
   (mix merged + rejected). Distill into
   `terminal-bench/review-patterns.md`: recurring reviewer objections as
   named patterns, each citing its PR. This is the apprenticeship corpus.
3. **EX-METR — task standard.** From github.com/METR/task-standard +
   guidelines: the spec checklist (what a task must pin down, QA steps,
   human-baseline method) → `metr/task-standard-digest.md`.
4. **EX-TOFFEE — inversion recipe from code.** The TOFFEE repo (not the
   paper): the answer-first inversion pipeline steps → `toffee/inversion-
   recipe.md` + a sketch for our seed_class 4 (scenario inversion over
   real data assets).
5. **PACK — wiring.** `evallab context build authoring` gains a standards
   section: pack includes the corpus entries whose facets match the target
   task (reuse craft facet matching). Determinism test; token budget
   asserted; retrieval test: pack for a sample task cites ≥3 standards
   entries.

**Acceptance (night):** ≥3 corpus dirs landed with provenance; pack build
consumes them deterministically; board-note filed for template adoption.

---

## LOOP-SEAM — one subscription-lane adapter for the lab's own model calls

**Goal:** close the scout finding. The lab's internal analysis/design calls
get ONE adapter: shell-out to a subscription CLI in headless mode (codex
first; claude the day the keychain token lands). No API SDKs, no keys —
the zero-key law stands. Runs metered as provider runs (quota #64);
anything billable-class still passes GATE (#65).

**Lease:** `src/evallab/modeladapter.py` (new), `tests/test_modeladapter.py`,
plus injection-point wiring in `analyst.py` and `analysis_worker.py`
(constructor/registry params only — stubs stay the default). `authoring.py`
is SG lane: expose the designer injection via board-note handshake.

**Cycle backlog:**
1. `modeladapter.py`: subprocess adapter (headless CLI, timeout, captured
   transcript under `runs/adapter/`, quota event emitted per call, refusal
   if credential absent — mirror queue deferral semantics). Golden test
   with a fake CLI binary.
2. Wire `analyst.py`: `--adapter cli-codex` selects it; acceptance = one
   REAL analysis of one existing real trial writes its sidecar, with quota
   event logged and $0.00 spend.
3. Wire `analysis_worker` adapter registry; batch of 3 existing trials
   analyzed unattended through the queue.
4. Calibration mini-suite: 3 known-answer prompts through the adapter;
   record latency + determinism notes into `research/calibration/adapter/`.
5. Designer injection prep: define the callable signature authoring needs,
   file the SG board-note with a working example.

**Acceptance (night):** stub default untouched (all existing tests green);
one real trial analysis exists end-to-end through the codex lane with its
quota event; calibration notes committed.

---

## LOOP-EXPERIENCE — run similar envs, compile the experience, measure it

**Goal:** the execution-grounded pattern from Meta-Task, at home scale:
before agents author a new env, they get a compiled pack of *executed*
sibling envs — instructions, verifier shapes, one passing and one failing
trajectory each, failure labels. Then measure whether it helps, because
context provision is an experimental variable here, not a vibe.

Works TODAY without LOOP-SEAM: generation runs as a Harbor task through the
executor (SG-1, PR #107), so the generating agent is a subscription CLI
that simply finds the pack in its environment.

**Lease:** `research/experience/**` (new), `src/evallab/experience.py`
(new, small: pack compiler), `tests/test_experience.py`,
`research/cards/experience-*.md` only.

**Cycle backlog:**
1. **FAMILY:** query craft Parquet for one facet-coherent family (same
   verifier_type + overlapping languages), 3–5 registered tasks. Record
   the query + choice in `research/experience/family-01.md`.
2. **RUNS:** assemble execution data for the family: existing trials first
   (the catalog holds ~33 real agent trials — reuse before spending),
   fill gaps with codex k=2 through the queue (purpose=craft,
   quota-capped), plus free oracle re-checks for instrument validity.
3. **PACK:** `experience.py` compiles experience-pack-01: per task —
   instruction, env summary, verifier sketch, ONE successful + ONE failed
   trajectory condensed via the atif module, failure labels, facets.
   Deterministic (same inputs ⇒ same hash), token-budgeted.
4. **A/B:** two SG-1 generation runs through the executor, n≥3 proposals
   per arm. Arm A: skeleton+exemplar (SG-1 default). Arm B: same + the
   experience pack in the environment. Full battery on every proposal.
   Report battery pass-rate and review score per arm with raw counts —
   n is tiny; no significance theater, just honest counts in the ledger.
5. **CARD:** `research/cards/experience-ab-01.md` — question, config,
   counts, caveats, verdict proposal. Whether packs enter the default
   authoring flow stays a human (Peter) decision.

**Never:** register generated tasks (human-only, always); exceed provider
run quotas; author outside the family experiment.

**Acceptance (night):** family chosen with query on record; pack-01 builds
deterministically; ≥1 arm of the A/B dispatched; card drafted even if
partial ("insufficient n so far" is a valid interim verdict).

---

## Order and quota notes

STANDARDS and SEAM are independent — start both. EXPERIENCE can start
FAMILY/RUNS immediately; its A/B (cycle 4) benefits from STANDARDS cycle 1
landing first (the adapted template) but does not block on it. All three
respect: subscriptions only, quota metering, GATE for anything
billable-class, registry promotion human-only. Tonight's Gemini lane is
exhausted — put mission execution on codex/OMP-native lanes until reset.

## Changelog

- 2026-08-19 — v1: written after reading the OMP orchestrator session
  (M020–M024 era, four scouts, stub-seam finding) and the Meta-Task
  appendix (F.1/F.2/F.3, B, D specifics).
