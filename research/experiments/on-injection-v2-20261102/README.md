# Injection Robustness v2: Noise Floor, Placebo-Controlled Effect, and Claims-Targeting Defenses (B4-INJECTION-V2)

**Date:** 2026-11-02  
**Target:** AutoReview Stage-1 Monitor (`src/evallab/autoreview_monitor.py`)  
**Evaluator Model:** `google-antigravity/gemini-3.8-flash` (attested, subscription, 0 fallbacks, 876 invocations)

## Summary of Findings

1. **Baseline Noise Floor (12.5%):** Re-evaluating 48 identical uninjected runs across 5 independent repeats revealed an intrinsic **12.5% per-case alert flip-flop rate** (6/48 cases, 95% CI: 5.9–24.7%). Placebo McNemar tests between control repeats showed symmetric noise ($b=3, c=3, p=1.0000$).
2. **Placebo-Controlled Injection Effect Supported ($p < 0.001$):** Testing 18 cheats across 6 social-engineering variants $\times$ 3 repeats (324 paired trials) produced a **10.8% flip rate** (35/324, 95% CI: 7.9–14.7%). Paired McNemar against control repeats yielded **$b=35, c=10, p = 0.000247$**, dramatically exceeding the placebo noise floor ($b=3, c=3, p=1.0000$).
3. **Defenses:**
   - **Defense D (Dual-Pass Quarantine):** Failed. Flips increased to 15.7% (17/108), cheat recall degraded to 77.8% (losing 2 real cheats, recall cost = +11.1%), and false alarms doubled to 6.7%.
   - **Defense E (Claims Rubric Line):** Maintained 94.4% cheat recall and nominally reduced flips to 7.4% (8/108), but the reduction did not achieve statistical significance beyond baseline noise ($b=8, c=7, p=1.0000$).
4. **Conclusion:** No defense was shown effective beyond the noise floor without penalty. No patch to production monitor code is recommended.

## Artifact Hashes

- `prereg.md`: `e38cbbe057cb` (`e38cbbe057cbe4efa02ce96628b36360159418f09d10891b4d5c02c1daf0dbd7`)
- `experiment_lib.py`: `ca85c70f06d5` (`ca85c70f06d535f84c0cf229e8eb9a2d9c086fe73619ad25fcbcec2d53484d92`)
- `run_noise_floor.py`: `f4afc81566e9` (`f4afc81566e98454f17c65e0523e4e12408721a53e1f3cc841ea8a77f0481e3f`)
- `run_injection_repeats.py`: `d0a59490bf55` (`d0a59490bf55be699c5bdefb9c1f45de4aada37f7698e4bab1be4adc6f75d679`)
- `run_defenses.py`: `656a42dda1c1` (`656a42dda1c1860dbbd35b023d71c44dc90531b24e2864d5da910cff441f3cd5`)
- `compute_metrics.py`: `f9b6de640568` (`f9b6de6405682c35b1e559ff55fc2d54ff388d2e32059f3432e0d40e1b629bcf`)
- `test_injection_v2.py`: `d84c8f5f8377` (`d84c8f5f83773e2eaa85361a14d6eee4d8e7e451c5c50b474f5726933e30502e`)
- `receipts/metrics_summary.json`: `641832ba191c` (`641832ba191cb3de499b36d9d347bb5eb2e14873fd76352be43498cbb2da1e09`)

Detailed receipts are archived at:
- `~/Developer/eval-lab-results/2026-11-02/B4-INJECTION-V2/receipts/`
- `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/loop/raw/B4-INJECTION-V2/receipts/`
