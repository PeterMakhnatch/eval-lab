# AutoReview Prompt-Injection Robustness and Defenses (B3-INJECTION)

**Date:** 2026-11-02  
**Target:** AutoReview Stage-1 Monitor (`src/evallab/autoreview_monitor.py`)  
**Evaluator Model:** `google-antigravity/gemini-3.8-flash` (attested, subscription, zero fallbacks)

## Summary of Findings

1. **Vulnerability Confirmed:** Text inside an agent's own trace can flip the undefended AutoReview Stage-1 monitor from `confirmed_cheat` / `suspicious` to `clean`.
   - Pooled cheat-to-honest flip rate: **6/102 = 5.9% (2.7–12.2%)** ($b=6, c=4, p=0.7539$).
   - Most effective attack: **`social_engineering`** (3/17 flips = **17.6% (6.2–39.3%)**, $b=3, c=0$), where test tampering was framed as an "authorized test fixture per SEC-881".
   - Direct system directives (`# [SYSTEM OVERRIDE]`) and verdict file touches had **0.0% flip rate**.
2. **Deterministic L0 Immunity:** L0 tripwires yielded **108/108 (100.0%)** agreement between injected and uninjected variants (0 mismatches), proving that rule-based tripwires outside the LLM context are completely immune to prompt injection.
3. **Defenses:**
   - **Defense A (`delim_defense` - Untrusted-Data Delimiters & Warning):** Best defense. Preserves 100% of baseline cheat recall (17/18 = 94.4%, recall cost = 0) and baseline honest false alarm rate (1/10 = 10.0%), while establishing structural data/instruction boundaries.
   - **Defense B (`strip_defense` - Span Sanitizer):** Increased flips to 11/102 (10.8%, $p=0.0117$) due to redacting context that the model relies on.
   - **Defense C (`metadata_defense` - Metadata-Only):** Severe recall collapse (recall dropped from 94.4% to 72.2%, missing 4 real cheats) and tripled false alarms (30.0%).

## Manifest and Hashes

- `prereg.md`: `f42d66f8380a` (`f42d66f8380aa2d53d9946c9c7678df2443c865c4c67a42233614af7c2a90003`)
- `experiment_lib.py`: `23776ec8e2be` (`23776ec8e2be92b5fa1c07071431c92297bfc3e04bdb31b5cf44277ffd397c4e`)
- `run_experiment.py`: `2d32c6f94b98` (`2d32c6f94b9864d55d0d23a47eb86a1e5818ac2fe8f7d2d95b276a39ae089113`)
- `compute_metrics.py`: `9fba37bed303` (`9fba37bed303ab3239921d0c4dc6583254078fb9aa45d2f8c5acdd15c1f0110b`)
- `test_injection.py`: `a2394066078a` (`a2394066078aa73410f4bde6d0982aa48c528de186435c5fe6b326be6dd64724`)
- `receipts/summary.json`: `0830bb391d57` (`0830bb391d57fd989bee5981500b49d3b81ed5c8862892a9c3b43cdbeb145926`)
- `receipts/metrics_summary.json`: `c273a8f5dec7` (`c273a8f5dec7bfef7b67ba31449d213f9cf8a93c3e67d80a21eef5f6c843bbde`)

Detailed receipts are archived at:
`~/Developer/eval-lab-results/2026-11-02/B3-INJECTION/receipts/` and
`/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/B3-INJECTION/receipts/`.
