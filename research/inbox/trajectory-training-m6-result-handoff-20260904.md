---
source_type: internal
---

# M6 trajectory-training result handoff

- Branch head: `dc02b1b42cc3183c35fcd2a143a1e40454e12148`
- Integrated: `95f0569e`
- PASS: deterministic fixture and prompt/completion projection; `enable_thinking=false`; exact tool-call/result round-trip; no labels/logprobs/reward/mask fields; structured-message semantic preservation; pure nonexecuting TRL render; stable plan digest `sha256:ee2255b581d0affb863d45f8be6a7a6630a06aac371b6993e629cd51e5ecc9eb`; checkpoint and effective-config digests bound; 36 focused tests pass.
- BLOCK G3: `TrainerRenderingContractV1` and `TRLPlanPayloadV1` hard-code `assistant_only_loss=false`; assistant-only token masks are not proved. Exact Qwen3-0.6B tokenizer/template bytes are unavailable offline, so exact template, mask, and token-truncation proof cannot run.
- S1: A/B staged but gated by G0, G2, and model freeze. C/D unavailable under M3 because process-quality support is degenerate.
- Next executable action: add explicit assistant-only supervision to the TRL contract/renderer, provide immutable Qwen3-0.6B tokenizer and template bytes, then rerun `research/tt-fixtures/validate_s0.py` for G3. S1 additionally waits for M2 admission and a live M3 freeze.
