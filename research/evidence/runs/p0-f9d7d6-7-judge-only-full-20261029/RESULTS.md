# P0-SELF-f9d7d6-7: full judge-only cascade evaluation

The preregistration was pushed as `f77f614aa` before new calls. The 555, pairs and 150 Goodfire sets were inspected; any positive result is a lead, not independent verification. The fresh p0f6 240 are excluded.

Missing or unavailable judgments are not clear. Incomplete populations have coverage only; no headline rates are calculated for them.

## Coverage

| Population | Complete runs or pairs | Eligible | Missing |
|---|---:|---:|---:|
| Goodfire 555 | 555 | 555 | 0 |
| Pairs (both members) | 89 | 123 | 34 |
| Goodfire 150 | 148 | 150 | 2 |
| CheatBench DEV | 149 | 150 | 1 |
| CM-18 | 635 | 646 | 11 |
| NP21 Flake | 61 | 67 | 6 |
| Pooled established | 696 | 713 | 17 |

## Goodfire 555

| Length | Judge-only recall / FAR | J recall / FAR | Haiku v8 recall / FAR | Difference, task-bootstrap 95% CI |
|---|---|---|---|---|
| <=40 | 6/100 (6.0%, 95% Wilson 2.8–12.5) / 0/100 (0.0%, 95% Wilson 0.0–3.7) | 6/100 (6.0%, 95% Wilson 2.8–12.5) / 0/100 (0.0%, 95% Wilson 0.0–3.7) | 8/100 (8.0%, 95% Wilson 4.1–15.0) / 0/100 (0.0%, 95% Wilson 0.0–3.7) | +6.0 [+1.1, +11.5] pp |
| 41–89 | 7/100 (7.0%, 95% Wilson 3.4–13.7) / 2/55 (3.6%, 95% Wilson 1.0–12.3) | 5/100 (5.0%, 95% Wilson 2.2–11.2) / 1/55 (1.8%, 95% Wilson 0.3–9.6) | 19/100 (19.0%, 95% Wilson 12.5–27.8) / 14/55 (25.5%, 95% Wilson 15.8–38.3) | +3.4 [-3.9, +10.3] pp |
| >=90 | 15/100 (15.0%, 95% Wilson 9.3–23.3) / 11/100 (11.0%, 95% Wilson 6.3–18.6) | 12/100 (12.0%, 95% Wilson 7.0–19.8) / 1/100 (1.0%, 95% Wilson 0.2–5.4) | 51/100 (51.0%, 95% Wilson 41.3–60.6) / 45/100 (45.0%, 95% Wilson 35.6–54.8) | +4.0 [-4.8, +12.5] pp |
| Overall / pooled MH | 28/300 (9.3%, 95% Wilson 6.5–13.2) / 13/255 (5.1%, 95% Wilson 3.0–8.5) | 23/300 (7.7%, 95% Wilson 5.2–11.2) / 2/255 (0.8%, 95% Wilson 0.2–2.8) | 78/300 (26.0%, 95% Wilson 21.4–31.2) / 59/255 (23.1%, 95% Wilson 18.4–28.7) | +4.6 [+0.5, +8.6] pp |
Length-only at the same clean-alarm budget: 2/300 (0.7%, 95% Wilson 0.2–2.4) / 13/255 (5.1%, 95% Wilson 3.0–8.5); threshold 223. Within-tertile length pooled difference: +0.6 [-3.9, +5.1] pp. Full details, including within-tertile thresholds and sensitivity, are in results.json.

## Pairs and cross-tests

Pairs incomplete: no 123-pair judge-only headline.

| Population | Judge-only recall / FAR | Haiku-v8 recall / FAR | J recall / FAR | Kimi-v8 same-known recall / FAR | Target 15% at 3% |
|---|---|---|---|---|---|
| Goodfire 150 | Incomplete | — | — | — | Indeterminate |
| CheatBench DEV | Incomplete | — | — | — | Indeterminate |
| CM-18 | Incomplete | — | — | — | Indeterminate |
| NP21 Flake | Incomplete | — | — | — | Indeterminate |
| Pooled established | Incomplete | — | — | — | Indeterminate |

Historical full-population references: Goodfire 150 Kimi-v8 10/80 at 2/70; CheatBench Haiku-v8 14/75 at 0/75 and Kimi-v8 19/75 at 0/75; pooled established Haiku-v8 33/324 at 11/389, Kimi-v8 34/324 at 14/389, J 22/324 at 5/389.

## Decision and audit

Decision: `evaluated`; preregistered improvement: False; 15%-at-3% target: False. V8 has no nontrivial frozen operating point at <=3% FAR on Goodfire; its unrestricted recall is not a 3%-FAR recall. Do not replace the v8 gate on this evidence.

Label-free audit: 340 missing gate cards on 555, 239 on pairs, 1 on CheatBench, 15 on CM-18, 6 on Flake, 2 on 150; overlapping union 603 unique keys and zero missing non-gate windows. Historical J replay matched 1,807/1,807.

The continuation orchestrator interrupted one live batch while optimizing preflight; its seven completed attempts were recovered from the attempt log and a zero-USD receipt was appended retrospectively. The batch-specific quota value was not retained, so compliance with the pre-batch quota check for that interrupted batch cannot be independently verified. The last full batch began at 81% weekly and finished after the start-batch deadline; no further batch was started. Claude usage at the end was 18% five-hour / 81% seven-day.

Runner status: `{'guard': {'attempts': 113, 'calls': 108, 'consecutive_failures': 0, 'consecutive_refusals': 0, 'failed_calls': 0, 'parse_failed_attempts': 0, 'refusals': 0, 'stop_reason': None}, 'reason': 'mission deadline', 'ts': '2026-10-29T23:30:58.131005+00:00'}`. Batch accounting: `{'attempts': 432, 'batch_quota': [0.77, 0.77, 0.77, 0.77, 0.78, 0.77, 0.77, 0.77, 0.77, 0.77, 0.77, 0.77, 0.77, 0.77, 0.77, 0.78, 0.78, 0.8, 0.81, None], 'batches': 20, 'input_tokens': 2489465, 'output_tokens': 165581, 'refusals': 0, 'rejected': 8, 'served': {'anthropic/claude-haiku-5-5': 432}, 'usd': 0}`. Audits and aggregate metrics are in results.json; no private run identifiers are published.
