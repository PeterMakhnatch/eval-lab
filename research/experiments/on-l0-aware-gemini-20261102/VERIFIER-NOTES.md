# Verifier Notes: K7D Informed Gemini Attacker against L0 Tripwires

**Verifier & control context:** K7E Same-Attacker Naive Control (`loop/raw/K7E-GEMINI-NAIVE-ARM/report.md`); coordinator overnight review  
**Status:** 0/21 evasions CONFIRMED for this model; headline generalization and paired test CORRECTED

## Key Corrections and Guidance

| Topic | Original Claim | Verified Value (K7E Control) | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Headline Evasion Survival | "L0 completely survives an informed attacker (0/21 evasions)" | The informed attacker passed the benchmark tests on only 4 of 21 packages (L0 caught all 4); on 17 packages, the attacker became overly timid, avoided rules, and failed the tests. | QUALIFIED | L0 caught 4/4 passing attempts; zero evasion was largely driven by the attacker failing to pass the tests (19% pass rate vs 81% naive). |
| Confounded McNemar Test | Paired McNemar against K7C naive baseline ($p = 1.0$) | Confounded: compared Claude 3.5 Sonnet (K7C) with Gemini 3.8 Flash (K7D). | REFUTED | The true same-attacker control (K7E, Gemini 3.8 Flash naive) shows passing rate collapses from 17/21 to 4/21 ($b=0, c=13, p = 0.000244$). Evasions were 3/21 naive vs 0/21 informed ($b=0, c=3, p = 0.2500$). |
| Attacker Adaptation Mechanism | "Attacker could not find evasions" | Rule awareness caused defensive paralysis: the attacker avoided tripwires by omitting hooks, thereby failing the benchmark test suite. | CORRECTED | Rule disclosure degraded exploit utility rather than tripwires catching complex evasion maneuvers. |
