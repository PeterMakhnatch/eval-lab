# TQ-BLIND: re-adjudicate the TQ-UNVERIFIED units whose prompts named the benchmark

Driver for the blinding check of TQ-UNVERIFIED. Preregistration, results and interpretation:
`research/evidence/runs/tq-blind-20261029/`.

`redact.py` is the pure identity redaction and its proof (`redact`, `verify`); `run.py` stages: `prepare` (select the
units whose rendered prompt contains a token, redact, prove only tokens changed), `adjudicate --who sol|opus --arm
redacted|control` (quota-gated, attested, resumable, built on the TQ-UNVERIFIED call machinery), `score`
(preregistered flips, per-gate both-real counts, calibration, decision rule). `export.py` writes the committed
`results.json` (opaque ids only) from the private scored state, adding the Fisher comparison of flip rates.

Raw prompts, responses and sessions stay under `~/.local/state/evallab-fleet/2026-10-29/TQ-BLIND/`. Nothing here
changes a gate, prompt, cohort or stored score. Tests: `tests/test_tq_blind.py`.
