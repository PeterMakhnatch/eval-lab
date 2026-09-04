---
type: validation-report
mission: M6-TRAINING-S0-VALIDATION
date: 2026-09-04
status: blocked-at-g3
base: 6df601b1042e254507bde3ae743283a1c45be1bd
scope: plan-and-format-only
---

# S0 rendering validation and S1 render staging

## Verdict

**BLOCK: S0 does not pass G3.** The committed fixture proves deterministic prompt/completion projection, exact tool-call linkage, byte-bound fixture identities, forbidden-field absence, semantic preservation at the structured-message layer, and pure non-executing TRL plan rendering. It does **not** prove assistant-only token masks or exact Qwen3-0.6B template/tokenizer rendering.

The blocking fact is structural: `TrainerRenderingContractV1.assistant_only_loss` and `TRLPlanPayloadV1.assistant_only_loss` are both `Literal[False]` (`src/evallab/trainer_bundle.py:127-136` and `:333-350`). Supplying `assistant_only_loss=True` is rejected by Pydantic, and the rendered plan carries `assistant_only_loss=false`. This cannot satisfy the charter's G3 requirement for assistant masks only (`research/inbox/trajectory-training-execution-charter-20260904.md:108,135,174`).

No trainer, GPU, network, billable model call, task registration, or queue submission was used.

## Dependency pins and stop state

| Dependency | Head read | State consumed by this report |
|---|---|---|
| M1 architecture audit | `f3d6ee427a9aea76fe3c077ebcc76da3c12b372c` | Complete with F1 provenance, F2 recipe/budget carrier, and F3 ownership-domain gaps still missing from the spine |
| M2 source census | `b81edd5e515d887fca64600ee03b5054bdfc7930` | G0 closed: all 24 local run sources unadmitted, redaction unassessed, splits unassigned, public dataset license unresolved |
| M3 selection preregistration | `47653b3eb141e4ca3a73a3a15d2351f0f6aeca84` | Conditional preregistration only; G2 closed, zero strictly eligible rows, C/D degenerate, tokenizer budget recompute and Wave-1 declarations pending |

These are materialization stops, not values to infer or fill.

## Committed fixture bundle

`research/tt-fixtures/s0-qwen3-0.6b/` contains:

- `conversation.json`: one canonical tool call, exact tool response, and terminal assistant answer.
- `expected-render.json`: two supervised records; every target is exactly the single `response` assistant message.
- `tokenizer-fixture.json` and `chat-template-fixture.json`: byte-identified offline contracts which explicitly say upstream Qwen3-0.6B tokenizer/template bytes are unavailable and tokenization is unsupported.
- `trainer-bundle/`: immutable train/validation/test JSONL, exclusions, manifest, fixture checkpoint, and `trainer-bundle.json`.
- `fixture-spec.json`: the fixture identity and forbidden-field contract.
- `validate_s0.py`: a smoke validator; it imports no trainer runtime and executes no training operation.

The Qwen model name is a fixture target identity, not a claim that upstream Qwen weights or the exact upstream template were vendored. The 48-byte checkpoint is explicitly fixture-only and contains no weights.

## S0 acceptance matrix

| Requirement | Result | Evidence |
|---|---|---|
| Deterministic fixture bundle | **PASS** | Committed bytes validate twice to the same plan digest `sha256:ee2255b581d0affb863d45f8be6a7a6630a06aac371b6993e629cd51e5ecc9eb` |
| Qwen3-0.6B identity | **PASS, fixture scope only** | Model/checkpoint identity is byte-bound; `enable_thinking=false` validates |
| Exact Qwen3-0.6B tokenizer/template | **BLOCK** | Exact upstream bytes are not locally available; network access was prohibited. The committed tokenizer/template files are honest offline contract fixtures, not upstream artifacts |
| Assistant-only supervised target boundary | **PASS at record layer** | `prompt_response_sft` projects each assistant message into `response`; committed train payloads equal the expected projection (`training_export.py:1285-1334`) |
| Assistant-only token masks / backend binding | **BLOCK** | Contract rejects `assistant_only_loss=true`; rendered TRL payload is false. No exact tokenizer means token-level mask positions cannot be computed or checked |
| Tool-call round-trip | **PASS** | Call id `call_read_alpha`, function `read_record`, canonical arguments, ordered response id/name, tool output, and terminal answer survive the committed JSONL exactly; production linkage rules are exercised (`training_export.py:970-1001`) |
| No labels/logprobs/reward/mask inputs | **PASS** | Recursive validation finds none of the forbidden fields in the expected records or committed train payloads; bundle validation independently refuses forbidden training keys (`trainer_bundle.py:47-69,490-500,536-539`) |
| No semantic truncation | **PASS at structured-message layer; token proof BLOCKED** | Exact call arguments, tool output, and terminal answer round-trip with no omission marker. Production rejects `terminal_span_status != complete` as `truncated_terminal_span` (`training_export.py:1156-1159`), and the TRL plan sets `truncation="error"`. Exact token-budget overflow behavior cannot be proved without the exact tokenizer/template |
| Pure TRL plan compatibility | **PASS for rendering only** | Two renders from one committed bundle are equal, the stable cross-process plan digest matches expected-result parity, and `trl` is not added to `sys.modules`; renderer contract explicitly says it never imports or invokes TRL (`trainer_bundle.py:991-1027`) |
| Checkpoint and effective config digests | **PASS** | Checkpoint `sha256:95d795b5139640ada341dcee4cafc42063f8d2053b155c86671b1246df36f2dc`; effective config `sha256:97edc3c754718ea8e461cad571e85db3c1b284708ff6b84b2975db7faa92abad` |

The record-layer result must not be relabeled as a mask pass. Prompt/completion separation makes assistant-only supervision possible for a compatible backend, but the current rendered plan does not bind that behavior.

## Four-arm S1 render staging

`research/tt-fixtures/s1-four-arm-render-staging.json` stages all four named arms without inventing bundle or plan digests:

| Arm | Recipe disposition | Bundle / plan state |
|---|---|---|
| A — stratified random | Eligible only after all gates pass | Not materialized / not rendered |
| B — concise process ordering | Eligible only after all gates pass | Not materialized / not rendered |
| C — process quality | Unavailable: degenerate support under M3 | Not materialized / not rendered |
| D — quality plus structure diversity | Unavailable: degenerate support under M3 | Not materialized / not rendered |

Rendering any S1 bundle requires all of the following, none of which may be substituted:

1. M1 F1/F2/F3 fields have typed carriers on the integration spine.
2. M2 admits source bytes through G0, resolves licenses/redaction, and assigns ownership-safe splits.
3. M3 becomes a live G2 preregistration and has nondegenerate support for every arm intended for materialization.
4. Equal supervised assistant token budgets are recomputed under one frozen student tokenizer/template.
5. The exact Qwen3 4B-class model revision, tokenizer, template, hardware class, and external backend identity are frozen.
6. G3 explicitly binds and verifies assistant-only supervision.

M3 currently narrows the available corpus to an A-vs-B estimation-only pilot and explicitly refuses C/D materialization. Therefore four successful S1 bundle manifests and rendered plans would be fabricated evidence at this point; the staging artifact records the refusal instead.

## Reproduction

From the isolated worktree with its local environment:

```text
.venv/bin/python research/tt-fixtures/validate_s0.py
```

Observed result:

```text
overall_status: blocked_at_g3_assistant_mask_binding
records: 2
plan_digest: sha256:ee2255b581d0affb863d45f8be6a7a6630a06aac371b6993e629cd51e5ecc9eb
checkpoint_digest: sha256:95d795b5139640ada341dcee4cafc42063f8d2053b155c86671b1246df36f2dc
effective_config_digest: sha256:97edc3c754718ea8e461cad571e85db3c1b284708ff6b84b2975db7faa92abad
assistant_only_loss_in_plan: false
trl_imported_by_renderer: false
truncation: error
```

This is a validation/refusal result only. External TRL wrapper operation remains Wave 3 and requires separate explicit approval.
