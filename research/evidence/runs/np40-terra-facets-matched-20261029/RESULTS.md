# NP40-TT-1: Terra facets on the 123 matched Goodfire pairs. Stopped by the call cap, not scored

**No reader-family conclusion is available.** The study ran with the preregistered, attested transport, but the 600-call cap cannot cover the design, and the run stopped when its 1-hour worker timeout expired. This is an incomplete study, not a negative finding. The earlier version of this file (merged in #1573) said the study stopped at the canary; that was true of attempt 2 and is superseded by this record.

## Sequence
1. Preregistration `50d3bc33d` (2026-10-29 14:33:15 ET) and transport addendum `f0875b7eb` (14:44:40 ET), both before any call.
2. Attempt 2: the Terra canary returned exactly `openai-codex/gpt-6.1-terra` but the stream carried no explicit `resolvedModelIsFallback: false`; the addendum's stricter rule rejected it. No study transcript was sent.
3. Lead ruling, committed as `612a379f7` before the first study call: a call is valid iff the served provider/model equals the requested route, `resolvedModelIsFallback` is not truthy, the fallback-off overlay was active, and no tool was used. This is the standard #1529/#1534 used for Claude readers. It is a disclosed amendment made after one rejected canary and with no label-joined number in existence. The Terra and Sol canaries passed under it (`canary-*.stdout.jsonl` in the private store).
4. Attempt 3 ran the study (reader in `tools/reader.py`: the frozen Haiku facet prompt and schema, one blinded run per call, long runs read by the existing map/reduce chunking) until the worker's 1-hour limit.

## Why it cannot finish inside the cap
The design assumed about one call per run. The facet reader splits long runs (map stage) and then forms the facets (form stage): 73 runs used 289 calls, about 4 per run (1 to 7). Projection: 246 Terra runs about 1,000 calls, plus the Sol 40-run subset about 160, against a cap of 600. Even if the remaining 311 calls were spent, about 150 runs would be done in random order and few pairs would be complete. At the stop only **15 of the 123 pairs had both members read** (57 pairs had one), far too few for the preregistered sign test.

## Accounting (attempt 3, Terra; no Sol study call)
| Item | Value |
|---|---|
| Calls dispatched / completed | 289 / 288 (1 in flight at the timeout) |
| Served model | 288/288 exactly `openai-codex/gpt-6.1-terra`; 0 fallbacks, 0 tool use, 0 refusals |
| Stages | 215 map, 73 form (1 retry) |
| Tokens | 8,065,416 input, 96,518 output |
| Facet records written | 72 (of 246 runs), 1 parse failure (a 22-word goal; the limit is 20) |
| Canaries | Terra and Sol, 1 each, both exact model |
| Codex 7-day | 60% before, 64% after (shared with other fleet workers) |
| Cost | $0 (subscription) |

No labels were joined, so no alarm rate, discordant-pair count or AUROC is reported for Terra. The reference rows from NP31 stand: stored Haiku facets 9 enacted-only vs 13 clean-only, pair AUROC 0.496 [0.437, 0.556]; length 0.431 [0.341, 0.520].

## To finish
A cap of about 1,300 calls (Terra about 1,000 plus Sol about 160 plus retries) is needed. The 289 calls here moved the shared Codex 7-day line from 60% to 64%; at that rate about 1,000 more calls would take it to roughly 75-80% (an extrapolation; other workers also draw on it), under the 85% stop. Resume with `tools/reader.py` and the 72 facet records already written; the seeded order and decision rule in the preregistration are unchanged.
