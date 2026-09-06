---
source_type: internal
---

# Trajectory-Training Program — Results for Peter (2026-09-04)

Original receipt from wH:p0 (Integration Lead) for wH:p9 (Program Lead) and Peter.
Corrected 2026-09-06: this is an offline-design receipt, not a claim that every
mission is staffed, merged to main, or scientifically available. M2 remains
unstaffed in the recorded register; PR #370 is open against the integration spine.
The program's no-paid/no-GPU/no-registration boundary remains in force.

## Delivered artifacts and recorded integration scope

| Mission | Owner | Deliverable | Head / PR | Status |
|---|---|---|---|---|
| M1 contract audit | wK:p6 (Arch.) | architect-contract-audit-20260904.md — F1–F6 proven-missing gaps + second-convention risk table (program type authority) | spine @99d471d1 | CLOSED |
| M3 selection prereg | wK:p5 (Analyst) | selection-recipe-prereg-20260904.md — four-arm recipe and materialization stop | PR #370 @f3d6ee42 | SPEC DELIVERED; PR OPEN |
| M5 pilot design | wH:pE (Synth) | conflicting-source-pilot-design-20260904.md — funcdag_cross_source_conflict twin, quarantine-first, replay receipts | PR #372 @6d8da521, spine @3d40d6c5 | CLOSED |
| M4 held-out freeze | wK:p8 (Eval Runner) | held-out-freeze-design + trajectory_training_eval.py + tests — local controls frozen, digests witness-tested | PR #371 @48b686f9, spine @189242dc | CLOSED |
| M6 S0 validation | wH:p1 (Training Eng.) | s0-validation-report + deterministic fixture bundle + validate_s0.py | @dc02b1b4, spine @95f0569e | CLOSED |
| M7 methodology | wT:p1 (Codex) | signed rubric + G2 review (5/8 converted, 3 external deps) | research/tt-method-review @2707a47d | CLOSED |
| M8 adversarial seat | wT:p4 (Zai) | two CLEAN exact-head campaigns + digest witnesses executed | — | ACTIVE |

Verified integration reference as of this correction: **95f0569e**
(`origin/integrate/spine-batch1`), distinct from `origin/main` at **20e1f08d**.
The table records branch-scoped receipts, not new verification of their scientific
claims. PR #370 is not integrated, and missing census/gate inputs remain missing.

## Blockers

1. **Charter materialization STOP (formally filed, M3 §18):** 0/164 strict-eligible;
   provenance independence undemonstrated (3 nominal strata, one lab/harness/teacher);
   no license statement; arms C/D degenerate (0 process-quality rows).
2. **M2 source/authority census UNSTAFFED** — the program's sole critical path. Offer
   pending with wT:p3 (Muse); alternates wT:p2. Prototype handed over
   (sha256:646c61be…, adopt-or-supersede).
3. **G3 open items:** assistant_only_loss binding (plan hardcodes false) + exact
   Qwen3-0.6B tokenizer/template token proof — blocked on offline upstream bytes.
4. **G2 CONDITIONAL / NOT AN UNSTOP:** Wave-1 lead declarations (delta_min +0.10 /
   delta_protect −0.05 proposed) and M7 conditions 02/04/08 remain prerequisites.

## Unresolved risks

- craft×facts digest **zero-overlap** (551 craft / 108 facts / 0 shared): lessons views
  render zero until craft-coverage ingestion is decided.
- M7 conditions 02/04/08 re-open if the corpus changes after unstop.
- Provenance concentration: 152/164 trajectories one model — no cross-teacher claim
  possible without new sources.

## Single next integration action

**M2 census acceptance/staffing is the recorded dependency, not a peer assignment.**
Peter controls the backlog decision. A pinned machine-readable source manifest is
required before G0, the §18 unstop checklist, and Wave-2 materialization, alongside
the calibration-tier and craft-coverage decisions recorded for Peter.
