# Trajectory-Training Program — Results for Peter (2026-09-04)

From: wH:p0 (Integration Lead). For: wH:p9 (Program Lead) -> Peter. All missions
staffed, run, closed or integrated. Zero paid calls, GPU, registration, network.

## Done — outputs, heads, PRs

| Mission | Owner | Deliverable | Head / PR | Status |
|---|---|---|---|---|
| M1 contract audit | wK:p6 (Arch.) | architect-contract-audit-20260904.md — F1–F6 proven-missing gaps + second-convention risk table (program type authority) | spine @99d471d1 | CLOSED |
| M3 selection prereg | wK:p5 (Analyst) | selection-recipe-prereg-20260904.md — four-arm recipe, G1 gates, sections 12–17 live-ready | PR #370 @f3d6ee42 | CLOSED (spec complete) |
| M5 pilot design | wH:pE (Synth) | conflicting-source-pilot-design-20260904.md — funcdag_cross_source_conflict twin, quarantine-first, replay receipts | PR #372 @6d8da521, spine @3d40d6c5 | CLOSED |
| M4 held-out freeze | wK:p8 (Eval Runner) | held-out-freeze-design + trajectory_training_eval.py + tests — local controls frozen, digests witness-tested | PR #371 @48b686f9, spine @189242dc | CLOSED |
| M6 S0 validation | wH:p1 (Training Eng.) | s0-validation-report + deterministic fixture bundle + validate_s0.py | @dc02b1b4, spine @95f0569e | CLOSED |
| M7 methodology | wT:p1 (Codex) | signed rubric + G2 review (5/8 converted, 3 external deps) | research/tt-method-review @2707a47d | CLOSED |
| M8 adversarial seat | wT:p4 (Zai) | two CLEAN exact-head campaigns + digest witnesses executed | — | ACTIVE |

Current spine: **95f0569e**. SFT signal gate + charter + all program artifacts integrated;
every merge dual exact-head reviewed; ancestry verified post-push.

## Blockers

1. **Charter materialization STOP (formally filed, M3 §18):** 0/164 strict-eligible;
   provenance independence undemonstrated (3 nominal strata, one lab/harness/teacher);
   no license statement; arms C/D degenerate (0 process-quality rows).
2. **M2 source/authority census UNSTAFFED** — the program's sole critical path. Offer
   pending with wT:p3 (Muse); alternates wT:p2. Prototype handed over
   (sha256:646c61be…, adopt-or-supersede).
3. **G3 open items:** assistant_only_loss binding (plan hardcodes false) + exact
   Qwen3-0.6B tokenizer/template token proof — blocked on offline upstream bytes.
4. **G2 closed** pending Wave-1 lead declarations (delta_min +0.10 / delta_protect
   −0.05 proposed) + M7 conditions 02/04/08.

## Unresolved risks

- craft×facts digest **zero-overlap** (551 craft / 108 facts / 0 shared): lessons views
  render zero until craft-coverage ingestion is decided.
- M7 conditions 02/04/08 re-open if the corpus changes after unstop.
- Provenance concentration: 152/164 trajectories one model — no cross-teacher claim
  possible without new sources.

## Single next integration action

**Staff/accept M2 and pin it to spine 95f0569e.** The census's machine-readable source
manifest is the gate for everything downstream: G0, the §18 unstop checklist, Wave-2
materialization, and the calibration-tier + craft-coverage decisions Peter holds.
