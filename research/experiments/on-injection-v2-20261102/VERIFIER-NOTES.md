# Verifier Notes: B4 AutoReview Prompt-Injection Robustness v2

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-B-LOOP/report.md`  
**Verifier report SHA256[:12]:** `af6aba70ddae`  
**Status:** Headline p-value CORRECTED under clustering; effect unproven at case level

## Key Corrections and Guidance

| Claim / Metric | Original Claim | Verified Value | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Headline Injection Significance | Paired McNemar $p = 0.000247$ ($b=35, c=10$) | Treats 324 pairs as independent. However, they are 54 (case, repeat) clusters over **only 18 unique cases**. At case level: exact sign-flip permutation $p = 0.18$, case sign test $p = 0.070$. | CORRECTED (Clustered Analysis) | The net effect at case level is not statistically significant ($p = 0.18$); bootstrap 95% CI is (-2.5, +17.6 pp). Two outlier cases account for 18 of the 35 flips. |
| Net Flips vs Placebo | "Exceeds placebo by 8.4x in net flips" | Net flips: 25 vs placebo 0. The ratio is undefined; 8.4 was computed as $25 / 3$. | UNSUPPORTED | State net flips directly: +25 net flips across trials vs 0 net in placebo controls. |
| Defense Comparisons | "D and E tested against baseline" | Confirmed: D and E ran on the identical 156 keys (18 cheats x 6 versions) as baseline. Defense E showed no significant reduction over baseline ($b=8, c=7, p=1.0$). | CONFIRMED | Defense E does not measurably reduce injection sensitivity beyond noise. |
