# TQ-UNVERIFIED: are Task QA gate alarms on unchanged tasks real defects?

Driver for the adjudication of every production-gate alarm (Sonnet 5.5 from OQ-10, Codex terra from R29-OQ-1) on
tasks without an injected defect. Preregistration, results and interpretation:
`research/evidence/runs/tq-unverified-alarms-20261029/`.

`run.py` stages: `build` (frozen gate cells + sealed labels, private), `match` (terra matcher for calibration),
`select` (alarm and calibration units), `adjudicate --who sol|opus` (quota-gated, attested, resumable), `score`.
`core.py` is the pure logic (eligible findings, dedupe, evidence check, labels, Wilson, kappa);
`prompts.py` is the frozen adjudicator and matcher prompts (SHA-256 pinned in the preregistration).

Raw prompts, responses, sessions and the label-bearing tables stay under
`~/.local/state/evallab-fleet/2026-10-29/TQ-UNVERIFIED/`. Nothing here changes a gate, prompt, cohort or stored
score. The local-only AutomationBench and PAIChecker corpora are read by hosted subscription models for evaluation
only. Tests: `tests/test_tq_unverified_alarms.py`.
