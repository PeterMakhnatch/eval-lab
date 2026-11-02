# R8-V2-REAL-SMOKE: Real omp Worker Smoke of Hardened Squad Runner (`ds_squad_v2.py`)

## Overview
This experiment verifies Phase 0 crash resilience and durability of `ds_squad_v2.py` versus baseline `ds_squad.py` using **real `omp -p` headless workers** on `google-antigravity/gemini-3.8-flash`.

Prior work in R7-DS-HARDEN and F11-FAIR-KILLTESTS evaluated `ds_squad_v2.py` with synthetic fake agent processes. This experiment provides empirical ground truth that surviving real `omp` processes are adopted seamlessly without duplicate executions or process overlap.

## Key Findings

1. **Trial A: Runner Kill Resilience (10 Paired Trials)**
   - `ds_squad_v2.py`: **10/10 adoption (100.0%, 95% Wilson CI: 72.2%–100.0%)**, 0 duplicate executions, 0.0s live process overlap.
   - `ds_squad.py` (v1 baseline): **0/10 adoption (0.0%, 95% Wilson CI: 0.0%–27.8%)**, **10/10 duplicate executions**, launching concurrent duplicate `omp` processes with up to **11.7s of live concurrent overlap** (mean 8.73s).
   - Paired McNemar exact test: $b=10, c=0, p=0.001953$ (statistically significant).

2. **Trial B: Worker Kill & Continuation Retry (3 Trials)**
   - Retried with `# CONTINUATION (attempt 1)`: **3/3 passed (100.0%)**, final attempt count 1, clean `DS-DONE`.

3. **Trial C: Late Gate File Creation**
   - Held in `waiting-gate` without starting workers; upon touching gate file, launched within runner's 10-second gate poll sleep (9.91s) and completed `DS-DONE`.

4. **Trial D: Quota Refusal Parking & Resumption**
   - Auto-resume: Detected HTTP 429 quota exhaustion, parsed reset timestamp, burned **0 retries** while parked, and auto-resumed at cooldown expiration.
   - Signal resume: Intercepted 1-hour quota ceiling, parked, and immediately unparked upon touching `RUN/RESUME`.

5. **Trial E: Process Hygiene**
   - 0 stray `omp` workers, 0 stray runners, 0 stray background processes.

6. **End-to-End Toy Plan Smoke**
   - 4/4 items completed cleanly: 2 trivial write & report Gemini 3.8 Flash workers, 1 late-gated item, 1 90s hashing worker survived mid-flight runner kill and adopted.
   - Model attestation: 100% served `google-antigravity/gemini-3.8-flash`, 0 off-route calls, 0 fallback events.

## Contents
- `run_trial_a.py`: Matched 10-trial runner kill resilience test harness.
- `run_trials_b_c_d_e.py`: Worker kill, late gate, quota parking, and process hygiene harnesses.
- `run_toy_plan_smoke.py`: 4-item end-to-end toy plan smoke script.
- `calc_stats.py`: Statistical computation script (Wilson intervals, exact McNemar).
- `aggregate_results.json`: Summary metrics from all trials.
- `manifest.json`: SHA-256 hashes of scripts and receipts.
