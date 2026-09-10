---
source_type: internal
---

# Trajectory-Training Program — Mission Registration Receipt (wH:p0)

Date: 2026-09-04. Charter: PR #369 (be777229), INTEGRATED to spine as 6df601b1 (five-file
training contract suite green at merge, diff-check clean, ancestry verified). All branches
off spine 6df601b1. Boundary preserved program-wide: no paid calls, no GPU, no task
registration, no queue, no network; Wave-3 external weight updates require separate
explicit approval.

## Mission board

| ID | Mission | Pane | State | Exclusive writer paths |
|---|---|---|---|---|
| M1 | ARCH-CONTRACT-AUDIT | wK:p6 | GRANTED, in flight | research/inbox/architect-contract-audit-20260904.md (branch research/tt-arch-contract-audit) |
| M2 | DATA-SOURCE-CENSUS | OFFERED → wT:p3 (Muse), alternates wT:p2 | awaiting acceptance | research/inbox/source-authority-census-20260904.md + research/census/source-manifest-20260904/ (branch research/tt-source-census) |
| M3 | ANALYST-SELECTION-SPEC | wK:p5 | ASSIGNED | research/inbox/selection-recipe-prereg-20260904.md (branch research/tt-selection-spec) |
| M4 | EVAL-HOLDOUT-FREEZE | wK:p8 | GRANTED, adjusted | research/inbox/held-out-freeze-design-20260904.md now; code module src/evallab/trajectory_training_eval.py + tests CONTINGENT on M1 field map (branch feat/trajectory-training-eval-freeze) |
| M5 | SYNTH-PILOT-DESIGN | wH:pE | ASSIGNED | research/inbox/conflicting-source-pilot-design-20260904.md (branch research/tt-pilot-design) |
| M6 | TRAINING-S0-VALIDATION | wH:p1 (Training Engineer) | ASSIGNED | research/inbox/s0-validation-report-20260904.md + research/tt-fixtures/ (branch research/tt-s0-validation) |
| M7 | METHOD-PREREG-REVIEW | wT:p1 (Codex) | ACCEPTED | research/inbox/methodology-prereg-review-20260904.md (branch research/tt-method-review) |

## Staffing notes

- Overnight roster turnover: wS:p9 and wK:p4 are off the roster. Data Engineer (M2) and
  Methodologist (M7) re-offered to newly arrived panes. M7 accepted (wT:p1). M2 offer
  pending with wT:p3 (Muse, ledger-parse shape; alternates wT:p2 Fable).
- Pane wS:pA (ZAI :3) holds the prior wS:p2 persona and is mid-task on a lessons/
  trajectory fix; not reassigned.

## Peter-decision dependencies (parked items, unchanged)

1. Calibration-tier approval — gates tier-LABELING in M2; census runs descriptively.
2. Craft-coverage ingestion — affects M2 denominators and non-zero view rendering.
3. Census base head — resolved for this program: missions pin to spine 6df601b1.

## Integration policy for the program

Exact-head dual review (p7 + p6) on every branch before merge; generated docs only after
integration; no overlapping writer leases (table above is authoritative). Program lead:
wH:p9. Integration Lead: wH:p0.

## Update 2026-09-04 (roster turnover + M3 delivery)

- M1 CLOSED: audit integrated to spine at e3856849 (merge 78f1d92f + whitespace tidy,
  ancestry verified). F1-F6 gap list is the program's type authority. Attribution fixed
  upstream at research/tt-arch-contract-audit @99d471d1 (author: wK:p6 via wS:p9
  delegation); spine copy substance unchanged, fast-forward on next pass.
- M3 DELIVERED: PR #370 @1dd17b19 (research/tt-selection-spec, sole leased path, base
  6df601b1). Key declarations: SFT-eligible 0/164 on current corpus; arms C/D degenerate
  (0 process-quality rows); provenance independence BORDERLINE — G0/G2 refuse until M2
  resolves source comparability. Underpowered pilot declared, not hidden.
- M3 review: methodologist leg -> wT:p1 (rubric-conformance, activated); type-conformance
  -> wK:p6; adversarial leg -> wS:p5 (wK:p7 off roster). Standing adversarial seat
  M8-ADVERSARIAL-REVIEW offered to wT:p4.
- M2 pending wT:p3 acceptance; M4/M5/M6 confirmed in progress per pane titles.
- Stray branch analyst/provenance-census: not present on origin — nothing to clean.
- Asks routed: M2 (rehydrate 75, reopen 128, license statement, adopt-or-supersede
  census prototype /private/tmp/eval-lab-analyst-census sha256:646c61be...), Training
  tokenizer pin (M6), Peter: Wave-1 declarations + parked calibration/craft decisions.

## Update 2026-09-04 (M3 v2 + formal materialization stop)

- M5 CLOSED: design integrated at spine 3d40d6c5 (dual exact-head PASS: wT:p4 deep-clean
  adversarial, wK:p6 type-conformance). Implementation conditions (a)-(c) recorded;
  candidate generation awaits certified parent receipts.
- M8 adversarial seat ACCEPTED by wT:p4 (zai/glm-5.3); M4 PASS with one binding condition
  (digest calculator committed + regeneration test before any F2 freeze counts); M6
  confirmed mid-work by wH:p1 (fixtures + four-arm staging materialized; G3 blockers
  correctly detected: assistant_only_loss hardcoded false, Qwen3-0.6B token-mask proof
  unavailable offline).
- M3 review legs PASS at cab28f59 (wS:p5 adversarial delta PASS; wK:p6 type PASS, F2 1:1).
- M3 v2 @f3d6ee42 (+189 lines, PR #370): claims G2-01..07 resolved analyst-side
  (estimand Delta_{r,f}, precision grid, ALL-protected -0.05 margin, Holm/max-T, C/D
  refused materialization as unavailable:degenerate, seven falsification probes pinned,
  denominator-preserving stopping); section 18 = FORMAL CHARTER STOP-CONDITION REPORT:
  program STOPPED for materialization (provenance independence not demonstrated;
  164/164 redacted-unrehydrated; no license statement; C/D degeneracy is lead scoping).
- G2 DISPOSITION: wT:p1 re-running the rubric against the eight conditions at f3d6ee42.
  Materialization unblock requires the section-18 checklist: M2 census head, rehydration
  + reopens, independence OR lead re-scope, license statement, tokenizer pin, held-out
  freeze + Wave-1 declarations. Peter's calibration-tier and craft-coverage decisions
  feed M2 directly.

## Update 2026-09-04 (M7 signed re-verdict + M4 in review)

- M7 RE-VERDICT @f3d6ee42 (research/tt-method-review @2707a47d, signature
  sha256:3242f40b...): G2 BLOCK MAINTAINED, narrowed to 02/04/08 — all three are
  external dependencies (M2 component graph bytes; lead SftSignalFreeze sign-off on
  margins/protected set; unstop checklist incl. independence-or-rescope, tokenizer
  pin, held-out freeze). Conditions 01/03/05/06/07 PASS. Spec work is complete;
  analyst seat stood down pending M2/lead.
- M4 implementation @48b686f9 (PR #371): wT:p4 adversarial CLEAN, binding
  digest-calculator condition DISCHARGED (witness test regenerates all three digests
  by execution, 5/5 pass, ruff clean; minor note: pin the three projection digests in
  the suite). wK:p6 F3/F4 conformance leg pending — sole remaining gate.
- M2 remains the program's critical path: wT:p3 offer still pending acceptance.

## Update 2026-09-04 (M3 closed; M4 in dual review)

- M3 CLOSED (analyst side): wK:p5 acknowledged 5/8 converted, remaining 02/04/08
  external. Sections 12-17 ready to go live at a new immutable head post-unstop.
  F6 stays accepted/parked post-outcomes per M1. wK:p5's claim slot returned to the
  parked calibration-tier deficit census.


## Update 2026-09-04 (M6 verdict; consolidated handoff for wH:p9)

- M6 M8 verdict CLEAN @dc02b1b4: author's G3 BLOCK confirmed correct and complete;
  PASSes reproduced byte-identical (three pinned digests); live probes found no missed
  refusal surfaces; two non-blocking fixture negative-assert notes queued.
- M4 dual review: wT:p4 CLEAN/condition discharged; wK:p6 F3/F4 leg verdict pending.
- Integration-ready: M4 (48b686f9), M6 (dc02b1b4) — each merges cleanly to spine
  3d40d6c5 once final legs land.
- PROGRAM CRITICAL PATH = M2 census (wT:p3 offer pending) + Peter decisions
  (calibration-tier, craft-coverage) + Wave-1 lead declarations. G2 closed per charter
  stop-conditions until then.

- M6 delivered @dc02b1b4 (origin/research/tt-s0-validation, base 6df601b1): honest G3
  BLOCK self-declared (assistant_only_loss hardcoded false; exact Qwen3-0.6B token proof
  unavailable offline); S1 A/B staged, C/D unavailable per M3 ruling. M8 adversarial
  grant issued to wT:p4.
- M2 offer to wT:p3 remains unaccepted — critical path unchanged.

## Update 2026-09-04 (M4 + M6 INTEGRATED — Wave-0/1 complete)

- Spine head: 95f0569e. M4 merged as 189242dc (dual PASS: wT:p4 CLEAN + condition
  discharged; wK:p6 6/6 convention, execution gap dual-covered). M6 merged as 95f0569e
  (M8 CLEAN; author's honest G3 BLOCK is the spine's recorded S0 state).
- Carry-forwards: pin M4 projection digests in test suite; fixture negative asserts
  (orphan-linkage, incomplete-span); G3 open items = assistant_only_loss binding +
  Qwen3-0.6B token proof (blocked on offline tokenizer/template bytes); F2 freeze
  counts + refusal codes owed when F3/F4 producer fields land.
- PROGRAM STATE: Wave-0/1 complete. Sole critical path = M2 census (wT:p3 offer
  pending) + Peter decisions (calibration-tier, craft-coverage) + Wave-1 lead
  declarations -> G2. All missions otherwise closed or integrated.

## Cross-references

- M7 signed G2 verdict (MAINTAIN BLOCK, 5/8 converted): research/tt-method-review
  @2707a47d — research/inbox/methodology-prereg-review-20260904.md (wT:p1).
- Program results for Peter: research/inbox/trajectory-training-results-20260904.md
  (wH:p0).
