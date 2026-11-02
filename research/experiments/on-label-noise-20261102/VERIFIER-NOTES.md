# Verifier Notes: D4 Label Noise Study

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-D-BENCH/report.md`  
**Verifier report SHA256[:12]:** `da431ecf0064`  
**Status:** Consensus coverage and disagreement counts CONFIRMED; CoT-class noise framing QUALIFIED

## Key Corrections and Guidance

| Topic | Author Claim | Verified Finding | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Disagreement vs. Noise Interpretation | "Reader disagreement, not proven label error" | CONFIRMED: On container-verified labels (ground truth), readers agreed 15/16 with labels (1/16 disagreement). On publisher labels, disagreement was 2/16 (12.5%). | CONFIRMED | Validated as reader disagreement measurements. |
| CoT-Class Label Noise Scope | 9/19 disagreement on CoT-judged class | Neither reader ever alerted on any of the 20 CoT-judged cases; the readers' inability to detect cheats on CoT-free/bounded cards cannot be separated from underlying label error. The true CoT-class noise rate is **NOT IDENTIFIED / NOT MEASURED**. | QUALIFIED | Do not quote 47.4% as proven label noise; it represents reader blindness plus unmeasured noise. |
| Data Policy Storage | `sample-case-ids.txt` with 60 case IDs was committed to repo | Removed from repo tree (`receipts-manifest.json` added); local copy preserved in `~/Developer/eval-lab-results/2026-11-02/D4/`. | KEPT LOCAL | Refer to `receipts-manifest.json` and aggregate metrics in `results.json`. |
