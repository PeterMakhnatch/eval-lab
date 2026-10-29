# OQ-8: data-access A/B for Task QA

Same auditor (`openai-codex/gpt-6.1-terra`), same judge (`openai-codex/gpt-6.1-sol`), same OQ-6 scoring.
Only the context source differs:

- **Arm A**: the frozen OQ-6 "B" prompt with embedded retrieved repair diffs, no tools.
- **Arm B**: the same prompt without the embedded retrieval block; the auditor may call a read-only
  `evallab qa-data` proxy (`qa_proxy.py`, at most 8 calls, own-target answer data masked).

Population: 44 keyed OQ-6 DEV tasks + 20 seeded unkeyed DEV tasks, then the 12 prepared OQ-6 cross cases once, unchanged.
"Confirmed" means a match to an admitted DEV answer key, not an executed grader differential. Unkeyed tasks are not negatives.

Files: `ab_driver.py` (freeze, dispatch, attestation, scoring), `qa_proxy.py` (masked, capped tool),
`metrics.py` (per-1M-token and per-hour rates with bootstrap CIs, paired B-A).

Run (needs the OQ-6 science directory and the cloud trace store; no network, subscription models only):
`ab_driver.py --smoke`, then `ab_driver.py --run --concurrency 4`, then
`metrics.py --cells <root>/cells --output <dir>`. A fallback model, a non-proxy tool call, or a quota of 85% or more
stops or invalidates a cell; consumed cells are never re-run.

Result (Oct 29, 2026): both arms matched the same 7 DEV keys on 6 tasks; arm B used about 38% more tokens.
Arm B called the tool about once per task. See the bridge `opus-vs-swarm-qa.md`.
