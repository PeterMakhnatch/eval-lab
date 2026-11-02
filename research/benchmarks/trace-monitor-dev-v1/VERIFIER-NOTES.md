# Verifier Notes: D1 TraceMonitor-Bench DEV v1

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-D-BENCH/report.md`  
**Verifier report SHA256[:12]:** `da431ecf0064`  
**Status:** Benchmark counts, splits, baselines, and AUROC calculations CONFIRMED; prompt divergence noted

## Key Notes and Cross-Track Coherence

| Topic | Verified Finding | Status | What Reader Should Use Instead |
|---|---|---|---|
| Dataset Composition | 780 total cases (split 776/776 across 6 families) confirmed from local files. Zero third-party cards or per-case labels committed to git. | CONFIRMED | Use composition and aggregate baselines in `results.json` and `baselines.json`. |
| Baselines & AUROCs | Baseline reader scores (AUROC, length-matched intervals, McNemar tests) reproduce exactly from stored reading files. | CONFIRMED | Validated numbers. |
| Production Stage 1 vs Stage 2 | Stored `ar_g38` baseline in D1 includes Stage-2 retrieval where triggered (AUROC 0.666). D2 evaluated a Stage-1 card-only re-read (AUROC 0.493). Later D7 analysis found stored Stage-1 scores alone (policy P4) yield AUROC 0.676 and recall 21/67. | CLARIFIED | Comparisons between stored production readings and fresh re-reads are subject to prompt version differences (see D6/D7/D8/D9 notes). Status is **unresolved, D8/D9 pending**. |
