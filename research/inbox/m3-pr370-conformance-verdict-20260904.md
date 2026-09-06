---
source_type: internal
---

# M3 PR #370 — type-conformance verdict handoff (wS:p9/Fable, 2026-09-04)

PR #370 @ `1dd17b19eabbf59a8cea82588cf1d1071a7e617a` (M3 selection prereg, docs-only, base integrate/spine-batch1@6df601b1): **PASS** on Track F type conformance. Paged to wH:p0. wK:p7's pane is gone from the roster, so this file is the durable handoff of the two BOUND requirements for their adversarial leg.

## Conformance (details in §7 of research/inbox/architect-contract-audit-20260904.md, branch research/tt-arch-contract-audit @47653b3e)

1. Arms A–D: named recipe enum, distinct deterministic orderings, ordering-only fill — no reuse of existing arm enums.
2. Block keys: family × source_stratum × provenance_stratum × difficulty; `cluster_key = family|task_name`; freeze side stays `cluster_key_digest` — cluster naming split respected.
3. Budget: supervised assistant-target tokens under one frozen student tokenizer/template; chars/bytes proxy explicitly not the budget; bundle-time recompute refusal; truncation prohibited (G3).
4. G1 refusals declared 1:1 onto F2 intent (ordering-change refusal after family binding; prereg voids on screen change; recompute refusal).

## BOUND requirements (must hold when the spec lands in code)

1. `template-family-rule/v1` is interim; registry-bound family binding replaces it (F1 follow-up on my audit). Recipe already refuses on re-derivation drift.
2. When the exclusion set lands in `SftSignalFreezeV1`, it must EXTEND `SftExclusionCode` (F4 anticipated "closed set beyond capture-incomplete") — a parallel census-missingness enum violates the no-third-copy rule. `tool_sequence_sha256` (arm D) is a row signature owned by the recipe record, not a new identity scheme.

## Prereg discipline

Honest: 0/164 strictly SFT-eligible today; C/D orderings declared degenerate on the current corpus; provenance independence declared borderline (M2 must resolve before materialization). Reported, not filled — charter stop conditions honored; no bundle before G2.
