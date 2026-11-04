# Model attestation addendum

## What happened

The main experiment's 20 gateway synthesis calls attest `openai-codex/gpt-6.1-terra`, with `tools: []` and `tool_choice: none`.
The certification follow-up's 3 gateway calls attest the same.

A provider fallback at **2026-11-04T08:37:34.466Z** changed the lead and the `Synth2` worker away from Codex.
The sequence recorded in the lead session is Muse, then Gemini Argon, then **DeepSeek Flash** at `08:37:35.492Z`, all with `resolvedModelIsFallback: true`.
The lead ran **107** assistant messages on DeepSeek Flash; `Synth2` ran **149**.

## Served models

| Session | Requested route | Served models | Assistant messages | Recorded cost |
|---|---|---|---:|---:|
| lead | Codex Sol | Sol 101, Muse 1, DeepSeek 107 | 209 | Sol $2.190195 (subscription), Muse $0, DeepSeek $0.350377 |
| `Synth2` | Codex Sol | DeepSeek 149 | 149 | DeepSeek $0.295139 |
| `Public2391` | Codex Sol | Sol 50 | 50 | Sol $0.637866 (subscription) |

DeepSeek total: **256 messages, $0.645516 recorded**.
The Sol costs are subscription usage and are excluded from paid totals.

## Effect on the experiment

The task-solving calls remained terra: 20/20 in the main certification and 3/3 in the follow-up.
No task-solving generation, grading, or scoring used DeepSeek.
The fallback affected orchestration and tool use only.
Every result remains bound to its receipts, and the parent re-verified the public check, 86 main cell receipts, and 20 gateway attestations.

## Corrected accounting

- Modal runtime estimate: **$0.356187**.
- DeepSeek recorded cost: **$0.645516**.
- Total paid estimate: **$1.001703**.
- Lane cap: **$2.00**.
- A `deepseek` row is appended to `~/.local/state/evallab-ov1104/spend.jsonl` with both session hashes.

DeepSeek cost comes from OMP per-message usage records, not a provider invoice.
Provider billing can lag.
The earlier repository RESULT omitted this fallback and understated spend; this addendum supersedes those numbers.

## Files

- `audit/addendum-attestation.json`: session paths, hashes, per-model counts, fallback timestamps and totals. No session text.
- Lead session SHA-256: `7a5268443a8fff74d361628cab25b334d431d30f0203493c2824db2a60f1f7d5`.
- `Synth2` session SHA-256: `3c046967e6bdc26c521cf97d2ad931608bc6e65034128b9b1dc5a5926f3496bf`.
- `Public2391` session SHA-256: present in `addendum-attestation.json`.
