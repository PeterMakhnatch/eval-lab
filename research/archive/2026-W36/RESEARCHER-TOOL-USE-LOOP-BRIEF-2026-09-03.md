---
source_type: internal
---

# Tool-use capability loop: researcher brief

**Author:** researcher track · **Date:** 2026-09-03 · **Status:** commissioned by Peter, Class-B descriptive unless noted
**Read time:** ~10 min. Detail threads live in session history; this file is the whole plan.

## 1. Verdict up front

Your loop is viable and worth running: **Harbor tool-use evals on an open model → filter good traces by verifier-native reasons → SFT → held-out evals → repeat; synthetics only from C1+ certified features; RL only after verifier + cost proven.** Niche: **compositional tool-use (FuncDAG value propagation) + exposure-conditioned recovery (per-class PRR)**. Memory is a control arm, not the treatment. The corpus already contains a pilot-scale version of every input the loop needs — the work ahead is gates and sequencing, not discovery.

## 2. What the corpus actually holds (164/164 trials resolved)

| Family | Pass / Fail | Signal |
|---|---|---|
| action-memory (wave-2 + overnight + egress) | 38 / 27 | duplicates 0-in-all-passes vs mean 11.1 in fails; dose 4k~72% → 64k~15%; semantic < neutral every dose |
| e0b addressing (indexed/opaque/range) | 42 / 24 | **scheme gap 50pp:** indexed 25% vs opaque/range 75%; mechanism reopened at mutation stage |
| FuncDAG composition (new family) | 8 / 2 | depth-5 3/3 (clean narrow chains, expected); name-sim 2/3 — the failure is wrong-graph traversal, the key specimen |
| recovery | 14 / 1 | all-pass problem except one blind-retry specimen (`auto_clear=true`, reward 0.0) that justifies the certified-vs-observational distinction |

Failure mechanisms, each with a specimen: re-read thrashing + typos (memory), first-pass wrong binding (e0b), wrong-graph traversal (composition), blind retry (recovery), malformed output (hygiene). SFT filter that works today: `reward==1.0 AND duplicates==0 AND complete-retrieval reason` — verifier-native, family-specific (duplicates don't transfer to single-pass tasks).

## 3. Staged plan

- **Stage 0 (now):** land approved spine; promotion emits `benchmark_contract.json` + provenance (converts corpus to Class A); fix ATIF envelope detector (route through existing `is_application_error`; pin harness per REQ-8); materialize + register the 17 cells; run 17-trial screening canary below.
- **Stage 1:** ~36-seed main-effect contrasts (repo MDE 0.30); memory RFT pilot off existing passes; interaction designs need ~4× and wait for a main effect.
- **Stage 2:** synthesis only from C1+ features; first target = conflict cell (B2/cross-source, double-witnessed gap) + addressing-permutation transform.
- **Stage 3:** RL (GRPO-class) only after verifier + cost proven. Not this quarter's problem.

## 4. The 17-trial canary (submittable; filing table in session)

9 FuncDAG (`baseline` / `name_similarity_high` / `schema_drift_twin` × seeds 42,101,2024; twin-paired drift-vs-baseline only) + 8 recovery (timeout/schema/silent/signature × fault+clean twin, persistence 1; per-class PRR, never pooled) + zero-billable wrong-repair mutants as acceptance. Model: ZAI flash via proxy lane (terra fallback). Label: screening, calibration-only. Kill rules: 8/8 recovery pass → escalate difficulty; name-sim 3/3 → pivot to conflict synthesis.

## 5. Non-negotiable gates (adopted from analyst/tutor/catchup)

Twin-paired contrasts only · capture-loss Fisher gate pre-claim · pass^1 primary + pass^3 reliability · leak assertion per image build (`oracle/`,`solution/`,`verifier/` absent) · G7 provenance travels · no cross-benchmark claims without a published crosswalk · `syn-funcdag-easy` banned from training (2 leak vectors) · feature must vary across arms or be refuted (killed 2 of my own metrics here) · <6 discordant units / zero-variance cells → descriptive only.

## 6. Literature standing (all body-quoted)

FuncBenchGen: hidden-DAG construction (CIN/DIN), depth + connected-distractor effects, restatement mitigation. ToolBench-X: recoverability guarantee (≥1 viable path per injected fault). AgentCheck: 12-type taxonomy, silent-dominance, timeout-easy/stale-hard asymmetry. τ-bench: DB-state verdicts, `pass^k` unbiased form. SPADE: designer memory ≠ executing-agent memory — hypothesis, not result. One recorded divergence: our distractors are lexical, theirs type-compatible — analogue, never replica.

## 7. Decisions for Peter

1. Approve parked shakedown r2 (recommended: r1 root-caused to pre-fix tree, HEAD verified fixed).
2. Bless the 17-cell follow-up + its registration prerequisite.
3. Sequence the memory RFT pilot (ready now, Class-B) vs holding for Class A.
4. Name the Stage-2 synthesis target (recommended: conflict cell).
