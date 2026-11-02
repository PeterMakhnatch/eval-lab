# Verifier Notes: A3 Gold-Certifier Feedback Rounds

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-A-GOLD/report.md`  
**Verifier report SHA256[:12]:** `eef34a3dad2f`  
**Status:** Rounds comparisons, kill rates, and paired McNemar tests CONFIRMED; prompt rule update CLARIFIED

## Key Notes and Guidance

| Claim / Metric | Author Value | Verified Value (Receipts) | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Primary Arm 2 vs Arm 1 (Base-Accepted) | 2/7 killed (28.6%, 8.2–64.1%), $b=0, c=0, p=1.0$ | Exact arithmetic confirmed from cell receipts ($b=0, c=0, p=1.0$). | CONFIRMED | Validated numbers. |
| Arm 2 vs Arm 1 (All Eval Mutants) | 13/18 killed (72.2%, 49.1–87.5%), $b=0, c=0, p=1.0$ | Exact arithmetic confirmed from cell receipts ($b=0, c=0, p=1.0$). | CONFIRMED | Validated numbers. |
| Repair Prompt Consistency | "Same rules, no rule changes across rounds" | The repair prompt was updated after round 1 to include test failure diffs for probe mutants. | CLARIFIED | Probe diffs are provided to round $r+1$ per feedback design. |
| Row-Level Data Storage | `results/selection.json` | Removed from git tree (`receipts-manifest.json` added); preserved locally under `~/Developer/eval-lab-results/2026-11-02/A3/selection.json`. | KEPT LOCAL | Refer to `receipts-manifest.json` and aggregate results in `results/summary.json`. |
