# Verifier Notes: D7 Trigger-Policy Study (Stage 2 Everywhere)

**Cross-track review context:** Coordinator overnight review; cross-check with VF-D6 (`87846b43e87b`) and D6B  
**Status:** Trigger policy findings CONFIRMED; cross-track coherence context ADDED

## Key Findings and Track Coherence

| Policy / Finding | Result | Status | Context & Cross-Track Implication |
|---|---|---|---|
| P1: Stage 2 Everywhere | AUROC 0.6401, recall 18/67 (vs P0: 0.6661, 20/67, $p = 0.6250$). False alarms inflate on clean traces, forcing operating threshold up. | CONFIRMED | Indiscriminate Stage-2 execution on 100% of cases degrades detection. |
| P4: Stored Stage 1 Alone | Stored Stage-1 scores alone (Stage 2 never executed) achieve AUROC **0.676** and recall **21/67**, matching the stored cascade ($b=0, c=1$). | CONFIRMED | Stage 2 adds approximately zero net signal in stored production data. |
| Prompt Version Confound | Fresh reruns in D7/D6B used updated Stage-2 prompts, whereas stored P0 used historical prompts (34/34 Terminal Wrench prompts differ). | QUALIFIED | Comparisons with stored production teacher remain subject to prompt drift. Status is **unresolved, D8/D9 pending**. |
