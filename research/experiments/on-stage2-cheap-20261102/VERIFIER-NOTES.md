# Verifier Notes: D6 Cheap Models at Stage 2

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-D6/report.md`  
**Verifier report SHA256[:12]:** `87846b43e87b`  
**Status:** Cheap models inferior to teacher at Stage 2 CONFIRMED; prompt divergence, trigger blindness isolation, and production comparability CORRECTED

## Key Corrections and Track Coherence

| Topic | Author Claim | Verified Value | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Prompt Divergence | Comparing cheap cascade against stored teacher readings | Stored `ar_g38` teacher readings used **different Stage-2 prompts** than today's code (34 of 34 Terminal Wrench stage-2 prompts differ); cheap arms reused D2's stage-1 output. | CORRECTED | The stored teacher's performance edge is confounded by prompt version differences, not clean teacher-vs-cheap model capability alone. |
| Noise Floor vs Optimistic Draw | "The stored teacher was an optimistic draw / sampling noise" | False. Repeat noise between teacher reruns is minimal (only 1 to 3 detections of 46; 46/48 verdict agreement, MAE 4.67). The gap between stored teacher (30/46) and rerun teacher (23-25/46) comes from prompt version differences, not random seed variation. | REFUTED | The difference is prompt-version driven; repeat noise is low ($<6\%$). |
| Stage-1 Trigger Blindness vs Stage-2 Failure | "Teacher strictly required at Stage 2" | The cheap cascade's missed detections partition into Stage-1 Trigger Blindness (7–9 cases never reached Stage 2) and Stage-2 Evidence Blindness (6–8 cases reached Stage 2 but exonerated). | CORRECTED / QUALIFIED | Detection loss is driven equally by Stage 1 failing to trigger Stage 2 and Stage 2 false exonerations. |
| D6B Forced Stage-2 Results | Forced Stage 2 on 46 positives | Cheap models detect 10–14 of 46 positives vs 23–25 for the teacher. Rerun-2 McNemar is $b=8, c=1, p=0.0391$. | CONFIRMED | When forced to evaluate Stage 2, the teacher still detects significantly more cheats than cheap models. |
| Cross-Track Coherence: Production Comparability | Comparing cheap models with production AutoReview | FIX-D23 claimed "Stage 2 carries the signal". D7 found that stored Stage-1 scores alone (policy P4) yield AUROC 0.676 and recall 21/67, matching the cascade ($b=0, c=1$). Headlines comparing cheap models with the production teacher are **not comparable to production** until D9 (production Stage-1 path) reports. | **UNRESOLVED, D8/D9 PENDING** | Status is unresolved pending D8 prompt A/B and D9 production Stage-1 cheap re-runs. |
