# Seal-source follow-up measurement (2026-10-29)

Question: does the shared base-seal change preserve the existing cluster facet fields on the read-only store copy, and what is its cold screen CPU cost?

Number: old `dc3eb4a58` versus this change on `/private/tmp/ps2/store.sqlite`: 68 clusters and 3,435 admitted rows, identical facet fields. Cold L1-07 facet-screen CPU medians were 0.027417 s old and 0.026981 s new, n=7 each, alternating order in fresh processes. `results.json` holds the samples; `measure.py` reproduces the comparison without publishing row data.

Meaning: observed fields did not change for these clusters. Timings include first-use costs and are measurements, not a bound or proof of general equivalence.
