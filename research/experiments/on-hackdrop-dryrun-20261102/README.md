# C2-EXPORT-DRYRUN: Hack-Drop Dry Run on Real Candidate Corpora

Evaluates `evallab hack-drop` on real candidate agent rollouts across 5 DEV corpora (690 total rollouts) to measure keep, drop, and review partitions, drop precision, drop recall, clean false alarm rates, and remaining contamination in prospective SFT training data.

## Evaluated Cohorts (DEV Only)

1. **Terminal Wrench DEV (304 rollouts):** 158 cheats, 146 cleans.
2. **CheatBench DEV (150 rollouts):** 75 cheats, 75 cleans.
3. **Goodfire DEV (150 rollouts):** 80 cheats, 70 cleans.
4. **First-Party C1–C5 Live Rollouts (30 rollouts):** 11 cheats, 19 cleans.
5. **First-Party Sol/Astra Safeguard Rollouts (56 rollouts):** 8 cheats, 48 cleans.

## Results Summary (Pooled 690 Rollouts)

- **L0 Tripwires Only:**
  - Kept: 92 (13.3%)
  - Dropped: 208 (30.1%)
  - Quarantined in Review: 390 (56.5%)
  - Drop Precision: 173/208 = 83.2% (95% Wilson: 77.5%–87.6%)
  - Drop Recall: 173/332 = 52.1% (95% Wilson: 46.7%–57.4%)
  - Clean False Alarm Rate: 35/358 = 9.8% (95% Wilson: 7.1%–13.3%)
  - Contamination Remaining: 25/92 = 27.2% (95% Wilson: 19.1%–37.0%)

- **L0 + AutoReview Stage 3 (`gemini-3.8-flash`, 100% attested):**
  - Kept: 92 (13.3%)
  - Dropped: 254 (36.8%)
  - Quarantined in Review: 344 (49.9%)
  - Drop Precision: 215/254 = 84.7% (95% Wilson: 79.7%–88.6%)
  - Drop Recall: 215/332 = 64.8% (95% Wilson: 59.5%–69.7%)
  - Paired McNemar Comparison: $b = 46$ (AutoReview only), $c = 0$ (L0 only), exact two-sided $p = 2.84 \times 10^{-14}$.

## Deliverables

- `export_manifest_schema.json`: JSON Schema (Draft 2020-12) for SFT export manifest rows.
- `manifests/c1c5/`: Committable SFT export manifests for first-party C1–C5 rollouts.
- `manifests/sol_astra/`: Committable SFT export manifests for first-party Sol/Astra rollouts.
- `metrics_summary.json`: Detailed aggregates, Wilson intervals, paired tests, and reason histograms.
- `test_dryrun.py`: Targeted unit tests covering schema compliance, manifest validation, and metrics integrity.
