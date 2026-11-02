# Verifier Notes: B3 AutoReview Prompt-Injection Robustness

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-B-LOOP/report.md`  
**Verifier report SHA256[:12]:** `af6aba70ddae`  
**Status:** Vulnerability confirmed; defense comparative statistical claims CORRECTED

## Key Corrections and Guidance

| Claim / Metric | Original Claim | Verified Value | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Defense A ("best defense") | "Reduced SE flips 3 to 2" | Pooled flips were 6/102 vs 6/102; paired flip indicator was b=2, c=2 (p=1.0). Defense A showed no statistically significant reduction in flip rate over baseline. | CORRECTED | Defense A preserves cheat recall and false alarm rate, but does not measurably reduce injection flips. |
| Defense B "backfired" | "Increased flips ... p = 0.0117" | The $p=0.0117$ was the alert-level paired test on 108 cells, not a flip-level test. Flips were 11/102 vs 6/102 (b=8, c=3, McNemar $p=0.227$). | CORRECTED | Defense B had 11/102 flips vs 6/102; alert changes were significant, flip rate difference was not. |
| L0 Invariant 108/108 | "L0 caught all of them deterministically" | L0 scores identical on injected vs uninjected traces because 0 evaluated cases passed the benchmark grader and evaded L0. | QUALIFIED | L0 is deterministic and immune to text injection, but coverage depends on rule triggers firing. |
