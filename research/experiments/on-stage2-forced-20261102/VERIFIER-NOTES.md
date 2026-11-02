# Verifier Notes: D6B Forced Stage 2 Isolation

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-D6/report.md`  
**Verifier report SHA256[:12]:** `87846b43e87b`  
**Status:** Forced Stage-2 teacher superiority CONFIRMED; prompt equality claim and rerun-2 McNemar numbers CORRECTED

## Key Corrections and Track Coherence

| Topic | Author Claim | Verified Value | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Prompt Identity | "Exact same prompt and raw evidence" (comparing reruns with stored teacher) | Stored `ar_g38` Stage-2 prompts differ from rerun prompts (34 of 34 Terminal Wrench prompts differ). The prompt was identical across the rerun arms (teacher rerun vs cheap models), but not between the stored teacher and the reruns. | CORRECTED | Reruns isolate cheap models vs teacher under today's prompts; comparisons against historical stored teacher are confounded by prompt changes. |
| Noise Floor vs Optimistic Draw | "Stored teacher was an optimistic draw / sampling noise" | False. Repeat noise between teacher reruns is minimal (1 to 3 detections of 46; 46/48 verdict agreement, MAE 4.67). The gap between stored teacher (30/46) and rerun teacher (23-25/46) is explained by prompt version differences, not seed variance. | REFUTED | The difference is prompt-driven; teacher repeat stability is high. |
| Rerun-2 Paired McNemar | Table reported $b=0, c=7, p=0.0156$ | Recomputed from receipts: Rerun 2 McNemar vs stored teacher is **$b=8, c=1, p=0.0391$**. | CORRECTED | Use $b=8, c=1, p=0.0391$. |
| Cross-Track Coherence: Production Comparability | Comparing cheap models with production AutoReview | D7 found that stored Stage-1 scores alone (policy P4) yield AUROC 0.676 and recall 21/67, matching the cascade ($b=0, c=1$). Headlines comparing cheap models with the production teacher are **not comparable to production** until D9 (production Stage-1 path) reports. | **UNRESOLVED, D8/D9 PENDING** | Status is unresolved pending D8 prompt A/B and D9 production Stage-1 cheap re-runs. |
