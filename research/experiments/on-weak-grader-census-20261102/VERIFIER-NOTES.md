# Verifier Notes: A2 Corpus-Scale Weak-Grader Census

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-A-GOLD/report.md`  
**Verifier report SHA256[:12]:** `eef34a3dad2f`  
**Status:** Census rates, Wilson intervals, and pilot replication CONFIRMED; task overlap count CLARIFIED

## Key Notes and Guidance

| Claim / Metric | Author Value | Verified Value (Receipts) | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Eligible / Audited Packages | 32 eligible, 28 audited, 0 control fails | CONFIRMED: 32 eligible, 28 audited, 4 excluded due to budget cap. | CONFIRMED | Validated numbers. |
| Weak Grader Rate | 18 / 28 = 64.3% (45.8–79.3%) | 18 / 28 = 64.3% (45.8–79.3% 95% Wilson CI). Total mutants: 22/74 survived = 29.7% (20.5–40.9%). | CONFIRMED | Validated numbers. |
| Pilot Replication Agreement | 11 / 11 tasks (100% agreement, b=0, c=0, p=1.0) | 12 total overlapping tasks exist between the 18-task pilot and the universe. 11 were audited before spend cap halted the run; all 11 agree identically ($b=0, c=0, p=1.0$). Task `003005` was audited in pilot but skipped in A2. | CLARIFIED | 11 of 11 audited overlapping tasks replicate pilot findings ($b=0, c=0, p=1.0$). |
| Row-Level Data Storage | Row-level `census.json` and `weak-tasks.json` | Moved to local storage (`~/Developer/eval-lab-results/2026-11-02/A2/`) per project third-party data policy. | KEPT LOCAL | Refer to `receipts-manifest.json` and aggregate JSON files (`summary.json`, `operators.json`, `replication.json`). |
