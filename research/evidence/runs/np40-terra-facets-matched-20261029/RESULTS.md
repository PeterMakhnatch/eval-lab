# NP40-TT-1: blocked before reading

**No Terra or Sol conclusion is available.** The preregistration was committed and pushed at `50d3bc33d274e75cced3e96ee2f6dbb3754846f6`, 2026-10-29 14:33:15 ET, before any study model call. All 246 distinct members of the 123 pairs passed the shared strict Goodfire DEV admission gate (`admitted_task_ids` and `cloud_trace_ids` with the Goodfire dataset); this check did not join labels. The existing pair file already carries the original pair identities and class assignments.

## Blocking transport mismatch

The available `evallab.readers.omp_model` live transport is deliberately sealed: `require_native_omp_transport` rejects every live call because a native reader executed a mutating tool despite no-tools/no-skills/no-extensions flags. Bypassing that safety seal is not a valid way to read untrusted trajectories. The Atlas `GatewayTransport` checks the gateway response's `model`, but its `Completion` and receipts do not contain an OMP session record or `resolvedModelIsFallback`; the available gateway CLI help does not establish a fallback-disabled session-record path. Thus neither available route establishes the assigned per-call session attestation and fallback-off canary. No canary or transcript call was attempted, no new findings were created or merged, and no spend entry is due for a nonexistent call. A safe, attested, fallback-disabled transport is a prerequisite; an empty fallback flag or a gateway-reported model alone cannot prove it.

## Available reference only

| Reader | Pairs | Enacted-only / clean-only | Pair AUROC (95% task bootstrap) |
|---|---:|---:|---:|
| Terra | 0 | unavailable | unavailable |
| Sol | 0 | unavailable | unavailable |
| Stored Haiku (NP31) | 123 | 9 / 13 | 0.496 [0.437, 0.556] |
| Length | 123 | unavailable: Terra operating point missing | 0.431 [0.341, 0.520] |

Haiku figures are copied from the prior NP31 report, not a new model call or reanalysis; its interval uses NP31's own fixed bootstrap seed. Length uses the frozen pair-file step lengths and preregistered NP40 seed 40104, 2,000 task-cluster resamples, sorted cluster keys and percentile indices 49 and 1949. The length binary sign test, Terra sign test and secondary same-FAR recall cannot be computed without Terra alarms. Exploratory awareness is also unavailable. This is an incomplete study, not a negative finding; the decision rule cannot be applied.

## Accounting

Study calls: Terra 0, Sol 0, canary 0; input and output tokens 0; refusals 0; parse failures 0; fallbacks rejected 0. Served-model attestation and fallback-off proof were not performed. `omp usage` reported Codex 7-day 59.0% before and 59.0% after inspection. No Kimi, Opus, paid keys, Docker, HackBench, or ledger edit was used.
