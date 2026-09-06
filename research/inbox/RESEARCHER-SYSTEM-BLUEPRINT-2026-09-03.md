---
source_type: internal
---

# Recursive Tool-Use Self-Improvement: System Blueprint v1

**Author:** researcher track · **Date:** 2026-09-03 · **Status:** opinionated proposal, supersedes the Harbor-tasks rendering in `RESEARCHER-TOOL-USE-LOOP-BRIEF-2026-09-03.md`
**Thesis:** train RL agents *inside* self-generated tool worlds; Harbor measures, never trains. Lab owns the domain layer + measurement; all engines borrowed.

## 1. System top-down (six parts, one loop)

```
            ┌──────────────┐  writes Python worlds   ┌──────────────┐
            │   DESIGNER   │ ──(tools, goal, checker)─▶│    WORLDS    │
            │    (model)   │                           │  reset/step/ │
            └──────────────┘                           │    reward    │
                   ▲                                   └──────────────┘
                   │ regret signal                          │ validate:
                   │ (hint-solve minus                       │ runs, winnable,
                   │  bare-solve)                            │ frontier band
                   │                                   └──────────────┘
                   │                                        │ pool
            ┌──────────────┐                           ┌──────────────┘
            │  CURRICULUM  │◀── frontier band ─────────┤
            │   (policy:   │                           ▼
            │  what next)  │                    ┌──────────────┐  RL inside worlds  ┌──────────────┐
            └──────────────┘                    │    SOLVER    │ ──traces+rewards──▶│    TRAIN     │
                                                │    (model)   │                    │ TRL→verl     │
                                                └──────────────┘                    └──────────────┘
                                                      │                                    │
                                                      │ fixed suites                       │ new checkpoint
                                                      ▼                                    ▼
                                                ┌──────────────┐                    (solver improves,
                                                │  HARBOR EVAL │                     designer must
                                                │  held-out,   │                     go harder)
                                                │  frozen      │
                                                └──────────────┘
```

Data contracts between parts: worlds are Python modules (`reset/step`, tool surface, hidden goal, programmatic verifier → scalar reward + component facts); traces are full step streams in ATIF; SFT records are verifier-filtered prompt/response pairs; eval is frozen Harbor suites the trainer never sees.

## 2. Why each part exists (one line each)

1. **Designer** — fixed task lists go stale the moment the solver masters them; something must mint frontier-difficulty worlds forever.
2. **Validation gate** — unfiltered generated worlds are mostly broken/trivial/impossible; only winnable frontier-band worlds enter the pool.
3. **Solver-in-worlds (RL, not SFT)** — multi-turn trial-and-error with environment reward is the only signal that teaches *recovery and diagnosis*; imitation teaches only what success looks like.
4. **SFT stage** — scaffolding: cheap smoke test that the data carries signal + cold start so RL doesn't begin from random flailing. Not the destination.
5. **Harbor eval, held-out and frozen** — without an uncontaminated measuring stick, "improvement" is meaningless. Train/eval separation is load-bearing, enforced by cluster-key splits and frozen suites.
6. **Curriculum policy** — hint-regret (solve-with-help minus solve-alone) decides what the designer writes next; this is what makes the loop *recursive* rather than merely repeated.

## 3. What the lab builds (thin) vs borrows (everything else)

**Build (lab-owned, domain + measurement only):**
- B1. Tool-use world grammar: FuncDAG DAG spec + recovery fault taxonomy + MCP tool surface as the designer's constrained vocabulary. *Why:* free-form Python worlds aren't measurable; constrained worlds yield per-class rates. This is THE research contribution.
- B2. Harbor rollout adapter for solver episodes (or borrow SPADE core loop if it ports cleanly — spike first, 1 day max).
- B3. ATIF normalization + trace filter (verifier success + duplicate/blind-retry/order hygiene). Mostly exists; fix the MCP-envelope detector.
- B4. Two training adapters: `atif_to_sft_record`, `verifier_reward_fn` (per librarian: ADP `process_trajectory` covers conversion; TRL templates rendering).
- B5. Frozen eval suites + gates (exist: FuncDAG/recovery families; add conflict cell + addressing-permutation in Stage 2).
- B6. Scheduler: the alternating driver (design → validate → pool → RL → eval → regret → design). ~200 lines; the only new control plane.

**Borrow (pinned, never reimplemented):** SPADE core/orchestration + validators (`spade-rl/spade`, MIT) · TRL SFTTrainer→GRPOTrainer, verl at scale (Apache-2.0) · ADP converter (MIT) · Harbor runner (Apache-2.0) · AWM envs as seed material · FuncBenchGen as convergent-validity anchor (BSD-3).

**Explicitly NOT built:** native env engine, MCTS scaffold, KV-cache research, second runner, custom trainer math, trajectory UI. The dossier's Phase 3/4 "implement native" items are rejected.

## 4. Staging (each stage has a kill rule)

- **S0 — Prove signal (weeks, CPU-only + API spend):** SFT smoke on existing 38/27 action passes + depth-5/name-sim traces; fixed-suite delta. *Kill: no held-out lift → traces lack signal, fix data before any RL.*
- **S1 — Borrowed loop, one family (needs Linux GPU):** FuncDAG worlds via SPADE-shaped generator → solver RL (GRPO, verifier rewards) → Harbor delta. *Kill: RL ≤ SFT-only baseline → environment reward too sparse; densify with component rewards.*
- **S2 — Second family + conflict synthesis:** recovery faults + cross-source conflict cell + addressing permutation. *Kill per class, never pooled.*
- **S3 — Full recursion:** designer trained by regret, curriculum closes the loop. *Kill: world difficulty flatlines while solver improves → designer capacity is the bottleneck, upgrade designer.*

## 5. Standing rules (imported from analyst/tutor/catchup, non-negotiable)

Twin-paired contrasts only · capture-loss Fisher gate before any arm claim · per-class PRRs, no pooled headlines · leak assertion per image build · G7 provenance travels · calibration-only labels on Darwin runs · syn-funcdag-easy banned from training (2 leak vectors) · features must vary across arms or be refuted · spine/promotion merge precedes Class-A claims.

## 6. Paper backbone (why each is load-bearing, not decoration)

SPADE (self-play envs + hint-regret) = loop logic · Absolute Zero (abduction/deduction/induction from execution) = cold-start alternative if designer stalls · SWE-Gym/R2E (RFT→20% SWE-Lite) = trace-training precedent · FuncBenchGen (hidden DAG, CIN effects, restatement mitigation) = compositional eval + filter rationale · ToolBench-X/AgentCheck (hazards, silent-dominance, timeout-easy/stale-hard) = recovery taxonomy · τ-bench (DB-state verdicts, pass^k) = grading + consistency precedent · SWE-smith (break-code-make-task) = construction pattern · AWM (1K MCP envs) = seed material · STaR/ReST = self-improvement lineage.

## 7. Open decisions (Peter)

D1. Approve parked shakedown r2 (recommended: root-caused, HEAD fixed). D2. Bless this blueprint's core bet — RL-in-envs with Harbor-as-evaluator — or redirect. D3. GPU venue for S1 (Linux host with Docker + GPUs; Darwin is analysis-only by construction). D4. Starting open model (smallest that passes baseline cells; TBD by smoke, not by debate).
