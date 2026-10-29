# NP40-TT-1: stopped at the Terra canary

**No Terra or Sol conclusion is available.** The preregistration was committed and pushed at `50d3bc33d274e75cced3e96ee2f6dbb3754846f6`, 2026-10-29 14:33:15 ET, before any study model call. All 246 distinct members of the 123 pairs passed the shared strict Goodfire DEV admission gate (`admitted_task_ids` and `cloud_trace_ids` with the Goodfire dataset); this check did not join labels. The existing pair file already carries the original pair identities and class assignments.

## Canary attestation blocker

The transport addendum was committed and pushed at `f0875b7ebc4d9624a9493d5e1c285425a1bfb633`, 2026-10-29 14:44:40 ET, before the canary. The CM-11 subprocess route used the no-tools flags and an overlay setting `retry.enabled: false`, `retry.modelFallback: false`, and `retry.fallbackChains: {}`. The Terra canary returned `OK`, with `provider: "openai-codex"`, `model: "gpt-6.1-terra"`, and `stopReason: "stop"`, but **no `resolvedModelIsFallback` field anywhere in its JSON event stream**. Therefore the explicitly preregistered fallback-false attestation did not pass; absence is not evidence of fallback or of fallback-free execution. The exact stream and stderr are retained privately under `~/.local/state/evallab-prod-live/trace-harness/np40/canary-terra.stdout.jsonl` and `canary-terra.stderr.txt`. The native OMP reader safety seal was not bypassed.

No transcript calls, Sol canary, new findings, or merge were performed. This is an incomplete study and the separation decision cannot be evaluated.

## Available reference only

| Reader | Pairs | Enacted-only / clean-only | Pair AUROC (95% task bootstrap) |
|---|---:|---:|---:|
| Terra | 0 | unavailable | unavailable |
| Sol | 0 | unavailable | unavailable |
| Stored Haiku (NP31) | 123 | 9 / 13 | 0.496 [0.437, 0.556] |
| Length | 123 | unavailable: Terra operating point missing | 0.431 [0.341, 0.520] |

Haiku figures are copied from the prior NP31 report, not a new model call or reanalysis; its interval uses NP31's own fixed bootstrap seed. Length uses the frozen pair-file step lengths and preregistered NP40 seed 40104, 2,000 task-cluster resamples, sorted cluster keys and percentile indices 49 and 1949. The length binary sign test, Terra sign test and secondary same-FAR recall cannot be computed without Terra alarms. Exploratory awareness is also unavailable. This is an incomplete study, not a negative finding; the decision rule cannot be applied.

## Accounting

Canary calls: Terra 1, Sol 0; transcript calls: Terra 0, Sol 0. Canary usage: 659 input tokens, 5 output tokens, $0; exact model identity observed once, explicit fallback-false attestations 0, fallbacks observed/rejected 0, missing fallback attestation rejected 1, refusals 0, facet parse failures 0. The $0 receipt was appended to `~/.local/state/evallab-failure-library/spend.jsonl`. Codex 7-day usage was 59.0% before and 60.0% after. No Kimi, Opus, paid keys, Docker, HackBench, or label-ledger edit was used.
