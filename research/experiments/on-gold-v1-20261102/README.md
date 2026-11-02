# MiMo Gold v1 Batch Certification (A4)

This experiment evaluates and certifies the 32 executable DEV packages in the MiMo Gold universe using the hardened test synthesis feedback loop (`run_rounds` with fresh mutants), producing an immutable catalog: **"MiMo Gold v1"**.

## Artifacts

- `prereg.md`: Frozen pre-registration document (sha256[:12]: `a42b91c16082`).
- `run_gold_batch.py`: Resumable batch driver with bounded concurrency, explicit 300s cell timeout, and ReserveSettle spend gating.
- `test_gold_batch.py`: Offline unit test suite verifying invariant preservation under crash/kill and resume.
- `gold-v1-manifest.json`: Complete manifest with exactly one row per package containing certification status, taxonomy reason, and non-equivalence metrics.
- `gold-v1-summary.md`: Summary report with 95% Wilson intervals and operational overrun bounds.
- `attestation.json`: Model attestation receipt (100% `google-antigravity/gemini-3.8-flash`, 0 fallbacks, 0 argon).
- `cells.csv`: Manifest of executed container cells with outcome hashes and runtime costs.

## Results Summary

- **Total Eligible Universe:** 32 strictly-DEV packages
- **Gold Structural:** 0 / 32 = 0.0% (95% Wilson CI: 0.0–10.7%)
- **Gold Held-Out:** 0 / 32 = 0.0% (95% Wilson CI: 0.0–10.7%)
- **Demonstrated Weak Graders:** 0 / 18 = 0.0% (95% Wilson CI: 0.0–17.6%)
- **Failure Taxonomy:** 32 / 32 rejected by hard false-reject safety guard (`ref_failed_no_repair`)
- **Total Settled Cost:** $0.0737 USD (against $3.00 cap) across 56 cells
