# Verifier Notes: R3 Repair vs. Gold Grader Hardening Audit

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-R3-REPAIR/report.md`  
**Verifier report SHA256[:12]:** `a00046d16163`  
**Status:** Hardening direction CONFIRMED; degraded task finding REFUTED; Gold 2x2 NOT MEASURED

## Key Corrections and Guidance

| Claim / Metric | Author Value | Verified Value (Receipts) | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Task-Level Weakness Reduction | 17/24 (70.8%) $\to$ 8/24 (33.3%), $b=10, c=1, p=0.0117$ | Recomputes exactly from raw cells ($b=10, c=1, p=0.0117$). Under clean reruns without original teardown flakiness, $b=10, c=0, p=0.00195$. | CONFIRMED | Repaired tasks significantly reduce weakness. |
| Mutant-Level Survival Reduction | 21/64 (32.8%) $\to$ 8/64 (12.5%), $b=14, c=1, p=0.000977$ | Over valid pairs (n=63, excluding 1 unapplied mutant on original), 21/63 (33.3%) $\to$ 8/63 (12.7%), $b=14, c=1, p=0.000977$. | CONFIRMED | Validated mutant kill improvement. |
| Degraded Task `000322` ($c=1$) | "Repair made grader weaker on `boundary_flip` `web_fileresponse.py:176`" | In three fresh independent reruns, the original grader returned reward 1.0 (accepting the mutant). The original 0.0 recorded in A2 was caused by an incidental pytest teardown error, not test detection. | REFUTED (Flaky Original Cell) | The repair did not degrade detection; clean pair counts are $b=10, c=0$ ($p=0.00195$). |
| Gold 2x2 Overlap Table | "0/27 repair-qualified are Gold certified" | R3 relied on A4's Gold v1 manifest, which suffered from a harness defect that rejected 32/32 packages. | NOT MEASURED | Discard the 0/27 Gold comparison. Re-evaluate against Gold v1.1 (A4B). |
| Repaired Reference Control | "24/24 repaired reference controls pass" | R3 executed no reference control cells. Independent verifier executed them: 24/24 pass (100%). | CONFIRMED (by Verifier) | 24/24 repaired packages pass their reference implementation. |
| Row-Level Data Storage | Row-level `repair_vs_gold.json` and `summary.json` | Moved to local storage (`~/Developer/eval-lab-results/2026-11-02/R3-REPAIR-VS-GOLD/`) per third-party data policy. | KEPT LOCAL | Refer to `receipts-manifest.json` and aggregate JSON files (`paired_comparison.json`, `manifest_overlap.json`). |
