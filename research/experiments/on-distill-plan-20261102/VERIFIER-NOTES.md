# Verifier Notes: D3 Student Distillation Plan

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-D-BENCH/report.md`  
**Verifier report SHA256[:12]:** `da431ecf0064`  
**Status:** DO-NOT-DISTILL verdict UPHELD; decision rules, teacher scope, and cross-track coherence CORRECTED

## Key Corrections and Track Coherence

| Topic | Author Statement | Verified Value | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Teacher Scope | "Teacher lacks signal on 5 of 6 families" | Describes **D2's Stage-1 card-only re-read** (pooled AUROC 0.493), NOT production AutoReview (pooled AUROC 0.666; Terminal Wrench 0.740 vs 0.500; DeepSWE 0.708 vs 0.476). | CORRECTED | The "no signal to distill" statement is true only of Stage-1 cards, not production AutoReview. |
| Decision Gate Evaluation | "Fails all three gates" (Gates 1 & 3 hardcoded `False` in code) | Fails Gate 1 ($X=0\%$) and Gate 2 ($N_{\text{pos}}$ deficit in container-verified families). Gate 3 depends on cost basis: fails at $0 subscription, passes at $6.97/1k list price. | CORRECTED | "Fails Gates 1 and 2; Gate 3 depends on cost basis." The DO-NOT-DISTILL verdict still stands. |
| Gate 2 Data Sufficiency | "Zero families meet 62 positives" | Trusted publisher families `cheatbench` (75) and `terminal_wrench` (158) meet the 62-positive threshold. `c1c5` (6) and `sol_astra` (8) fail, and `deep_swe` (50) is below threshold and CoT-judged. | CORRECTED | Two families meet the sample size threshold; container-verified families fail. |
| Pending Queue Scope | "69 pending verdicts will add container-verified labels" | 69 queue lines represent 60 unique run IDs (52 benchmark runs) to resolve whether publisher CoT-judged labels hold; they do not add container-verified rollouts. | CORRECTED | Pending verdicts evaluate publisher CoT reliability, not container ground truth. |
| Cross-Track Coherence: Production Comparability | Comparing D3 distillation economics against production | D2's Stage-1 re-read gave AUROC 0.493; stored production readings gave 0.666. D7 found stored Stage-1 scores alone (P4) give AUROC 0.676 and recall 21/67. VF-D6 found stored production readings used different prompt versions than today's code (34/34 Terminal Wrench stage-2 prompts differ). Net: Headlines comparing cheap models with the production teacher are **not comparable to production** until D9 (production Stage-1 path) reports. | **UNRESOLVED, D8/D9 PENDING** | Status is unresolved pending D8 prompt A/B and D9 production Stage-1 cheap re-runs. |
