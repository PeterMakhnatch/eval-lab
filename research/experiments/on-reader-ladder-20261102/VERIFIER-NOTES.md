# Verifier Notes: D2 Cheap-Reader Ladder

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-D-BENCH/report.md`  
**Verifier report SHA256[:12]:** `da431ecf0064`  
**Status:** Arithmetic and rungs CONFIRMED; headline context, attestation denominator, and production comparability QUALIFIED

## Key Corrections and Track Coherence

| Item | Author Claim | Verified Value | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Attestation Denominator | 300/300 requested == served models across all 8 rungs (2,400 calls) | 2,389 of 2,400 calls had a recorded served model; 11 calls lacked a served model (2 for `gemini-2.5-flash-lite`, 9 for `mistral-nemo` due to returncode 1 or timeout). 0 fallbacks, 0 `gemini-4-argon`. | CORRECTED | 2,389/2,400 calls attested with served model; 0 fallbacks. |
| Baseline Capability Context | "Cheaper readers can match production AutoReview detection performance..." | Reference `gemini-3.8-flash` operates **near chance** on Stage-1 cards (AUROC 0.511 [0.443, 0.580], recall 6/71 = 8.5%). Four Gemini rungs have tune threshold 0. Matching reference performance means matching a baseline that lacks discriminative Stage-1 signal. | QUALIFIED | Readers match a reference operating near chance; this is not proof of high detection capability. |
| Cross-Track Coherence: Production Comparability | Comparing fresh cheap reader re-reads with production AutoReview | D2 evaluated a Stage-1 card-only re-read (AUROC 0.493). Later D7 analysis found stored Stage-1 scores alone (policy P4) yield AUROC 0.676 and recall 21/67. VF-D6 found stored production readings used different prompt versions than today's code (34/34 Terminal Wrench prompts differ). Net: D2/D3/D6 comparisons between cheap models and the stored production teacher are **not comparable to production** until D9 (production Stage-1 path) reports. | **UNRESOLVED, D8/D9 PENDING** | Status is unresolved pending D8 prompt A/B and D9 production Stage-1 cheap re-runs. |
