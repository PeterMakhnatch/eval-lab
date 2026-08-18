# Eval lab discovery journal

Append-only draft findings. Entries become validated only after human review or
calibrated analysis. Every entry cites evidence and either names the prior entry
it extends or justifies a new thread.

## D-20260815-KTXJSHGZ — draft

- Claim: Across this small control-only cohort, event-summary and transaction-reconciliation showed the expected oracle-pass/nop-fail pattern without exceptions, supporting basic verifier discrimination for those tested cases; the evidence does not establish grading quality, acceptance of diverse valid solutions, or ordinary-agent capability.
- Builds on: new thread — The supplied discovery journal contains no existing D-* entries to extend. This starts a genuinely new ordinary-agent-capability thread because the source cohort contains only oracle and nop controls.
- Evidence:
  - [queue/researchers/passes/2026-08-15/01KZZCK33HJM4R8HW3V0Y25DXE/evidence.json](../queue/researchers/passes/2026-08-15/01KZZCK33HJM4R8HW3V0Y25DXE/evidence.json) — Reports the transaction-reconciliation oracle successes, its paired nop failure, the repeated event-summary oracle/nop outcomes, and the unpaired html-js-filter nop outcome, all without exceptions.
  - [research/evidence/runs/event-summary-nop-evidence/result.json](../research/evidence/runs/event-summary-nop-evidence/result.json) — Reports an event-summary nop control with reward 0.0 and no exception.
  - [research/evidence/runs/event-summary-oracle-evidence/result.json](../research/evidence/runs/event-summary-oracle-evidence/result.json) — Reports an event-summary oracle control with reward 1.0 and no exception.
- Proposed spec: [queue/proposed/codex-01KZZCN7X9PA643W1QCKQNNNY5.json](../queue/proposed/codex-01KZZCN7X9PA643W1QCKQNNNY5.json)

## D-20260815-CHEY952N — draft

- Claim: In this eight-run cohort, three oracle controls succeeded, while all five Codex canaries failed or produced no scored result. This pattern supports basic task/verifier viability for the oracle configurations and indicates a recurring problem in the sampled Codex execution path, but it does not establish that Codex lacks task capability or identify whether configuration, launcher, environment, tooling, or agent behavior caused the failures.
- Builds on: D-20260815-KTXJSHGZ
- Evidence:
  - [queue/researchers/passes/2026-08-15/01M023NTT3R9TYRJBYTDDT1YC1/evidence.json](../queue/researchers/passes/2026-08-15/01M023NTT3R9TYRJBYTDDT1YC1/evidence.json) — Reports the transaction-reconciliation oracle control with reward 1.0, no exception, and zero cost.
  - [runs/canary-event-summary-codex-20260814/result.json](../runs/canary-event-summary-codex-20260814/result.json) — Reports a Codex event-summary run with null reward, ValueError, no recorded model, and zero cost.
  - [runs/canary-terminal-bench-html-js-filter-codex-20260814/result.json](../runs/canary-terminal-bench-html-js-filter-codex-20260814/result.json) — Reports the initial Codex terminal-bench/html-js-filter run with null reward, ValueError, no recorded model, and zero cost.
  - [runs/canary-terminal-bench-html-js-filter-codex-20260814-r2/result.json](../runs/canary-terminal-bench-html-js-filter-codex-20260814-r2/result.json) — Reports the gpt-5.6-terra terminal-bench/html-js-filter rerun with reward 0.0 and NonZeroAgentExitCodeError.
  - [runs/canary-transaction-reconciliation-codex-20260814/result.json](../runs/canary-transaction-reconciliation-codex-20260814/result.json) — Reports the initial Codex transaction-reconciliation run with null reward, ValueError, no recorded model, and zero cost.
  - [runs/canary-transaction-reconciliation-codex-20260814-r2/result.json](../runs/canary-transaction-reconciliation-codex-20260814-r2/result.json) — Reports the gpt-5.6-terra transaction-reconciliation rerun with reward 0.0 and NonZeroAgentExitCodeError.
  - [runs/checkpoint-oracle-20260814/result.json](../runs/checkpoint-oracle-20260814/result.json) — Reports an event-summary oracle checkpoint with reward 1.0 and no exception.
  - [runs/control-reset-oracle-20260814/result.json](../runs/control-reset-oracle-20260814/result.json) — Reports an event-summary oracle control after reset with reward 1.0 and no exception.
- Proposed spec: [queue/proposed/codex-01M023RP03KGSHB4WZ29WE9DGR.json](../queue/proposed/codex-01M023RP03KGSHB4WZ29WE9DGR.json)

## D-20260816-7CQRVDQ6 — draft

- Claim: In this small, heterogeneous cohort, gpt-5.6-terra Codex passed the configured verifier in 2 of 3 canary trials and failed it in 1 of 3; this suggests task-dependent performance in these specific runs, but does not establish a general capability rate or explain the outcomes.
- Builds on: D-20260815-CHEY952N
- Evidence:
  - [runs/canary-event-summary-codex-20260815/result.json](../runs/canary-event-summary-codex-20260815/result.json) — Catalog-reported reward 1.0 and no exception for the Codex event-summary canary.
  - [runs/canary-terminal-bench-html-js-filter-codex-20260815/result.json](../runs/canary-terminal-bench-html-js-filter-codex-20260815/result.json) — Catalog-reported reward 0.0 and no exception for the Codex HTML/JavaScript-filter canary.
  - [runs/canary-transaction-reconciliation-codex-20260815/result.json](../runs/canary-transaction-reconciliation-codex-20260815/result.json) — Catalog-reported reward 1.0 and no exception for the Codex transaction-reconciliation canary.
  - [queue/researchers/passes/2026-08-16/01M05E0284TEKNMXJAT81RQZVA/evidence.json](../queue/researchers/passes/2026-08-16/01M05E0284TEKNMXJAT81RQZVA/evidence.json) — Catalog-reported reward 1.0, no exception, and zero cost for each of the five oracle event-summary smoke controls.
- Proposed spec: [queue/proposed/codex-01M05E2RT4ZMS2YYWTET9TTQS5.json](../queue/proposed/codex-01M05E2RT4ZMS2YYWTET9TTQS5.json)
