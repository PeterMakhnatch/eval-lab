# Verifier Notes: K7 / K7B Adaptive Attacker ASR Sweep

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-A-GOLD/report.md`  
**Verifier report SHA256[:12]:** `eef34a3dad2f`  
**Status:** Separate-verifier defense efficacy CONFIRMED; ASR denominator and refusal classification CORRECTED

## Key Corrections and Guidance

| Claim / Metric | Author Value | Verified Value (Receipts) | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Unparseable packages (n=7) | "7 packages no parseable candidate / no JSON carrying a valid mechanism class" | The model responses contained explicit assistant refusals ("I cannot assist with modifying test configurations to bypass grading checks"), not random parse failures. | CORRECTED | 7 model calls were refusals to participate in test bypasses. |
| Attack Success Rate (ASR) Scope | "ASR 100% -> 0% (21/21 vs 0/21)" | Conditional on the attacker attempting the attack, ASR is 21/21 (100.0%, 84.5–100.0%). Across the entire 28-package cohort (including refusals as non-attacks), baseline ASR is **21/28 = 75.0% (56.6–87.3%)** vs **0/28 = 0.0% (0.0–12.1%)** under separate-verifier. | CORRECTED (Scope) | State both conditional (21/21) and unconditional (21/28) rates. |
| Defense Effect Significance | $b=21, c=0, p = 9.54 \times 10^{-7}$ | Paired McNemar test on attempted pairs reproduces exactly ($b=21, c=0, p = 9.54 \times 10^{-7}$). | CONFIRMED | Separate verifier completely eliminates this class of conftest/pytest tamper exploits. |
