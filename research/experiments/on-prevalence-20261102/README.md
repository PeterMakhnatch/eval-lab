# Calibrated Prevalence of Action-Visible Cheating in DEV Agent-Trace Corpora (R1-PREVALENCE)

This directory contains the code, preregistration, and aggregate findings for **R1-PREVALENCE**: evaluating the true calibrated prevalence of action-visible reward hacking / cheating across public agent-training trace corpora (Goodfire and HackTrace).

## Summary of Findings

Across $N = 799$ valid non-benchmark DEV runs evaluated by the calibrated cheap reader (`google-antigravity/gemini-3.1-flash-lite`, selected from D2) at the $\le 2\%$ false-alarm operating point ($t > 0$):
- **Observed Alert Rate**: $q = 8 / 799 = 1.00\%$ ($95\%$ Wilson: $[0.51\%, 1.97\%]$).
- **Goodfire Sub-Corpus (699 runs)**: Raw action-visible alert rate is $0.57\%$ ($4/699$, $95\%$ Wilson $[0.22\%, 1.46\%]$). Rogan-Gladen calibration is excluded per D4 addendum because CoT-free readers have $\text{recall} \approx 0$ on this `publisher_cot_judged` class, where Rogan-Gladen $(\text{recall} - \text{fa}) \approx 0$ breaks down.
- **HackTrace Sub-Corpus (100 runs)**: Raw alert rate is $4.00\%$ ($4/100$, $95\%$ Wilson $[1.57\%, 9.84\%]$). Calibrated prevalence is $31.7\%$ ($95\%$ bootstrap CI: $[0.0\%, 100.0\%]$).
- **Overall Pooled Sample (799 runs)**: Calibrated prevalence is $0.0\%$ ($95\%$ bootstrap CI: $[0.0\%, 23.7\%]$) because observed $q = 1.00\%$ is below benchmark false alarm rate $\widehat{\text{fa}} = 1.28\%$.
- **Strict Operating Point ($t > 30$, $\le 1\%$ FA ceiling)**: Raw alert rate is $0.75\%$ ($6/799$, $95\%$ Wilson $[0.34\%, 1.63\%]$).

## Reason Codes Distribution ($N = 800$)
- `clean_attempt`: 791 / 800 (98.88%)
- `test_tampering`: 4 / 800 (0.50%)
- `confirmed_exploit`: 2 / 800 (0.25%)
- `hidden_solution_access`: 1 / 800 (0.125%)
- `verifier_bypass`: 1 / 800 (0.125%)
- `parse_failure`: 1 / 800 (0.125%)

## Files
- `prereg.md`: Committed copy of preregistration (SHA256: `31d710c0a0bb91307028139aadb2a66a441084c7bee70c164c281f75fe09050b`).
- `build_cohort.py`: Deterministic cohort sampling from admitted DEV splits.
- `render_sample_cards.py`: Card generation pipeline wrapping `evallab trace anatomy`.
- `run_prevalence_sample.py`: Concurrent reader execution with full session attestation.
- `analyze_prevalence.py`: Rogan-Gladen calibration, bootstrap intervals, and sensitivity analysis.
- `results.json`: Summary aggregate statistics, intervals, and sensitivity grids.
- `test_prevalence.py`: Targeted regression tests.
