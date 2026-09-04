---
type: preregistration-spec
mission: M3-ANALYST-SELECTION-SPEC
author: wK:p7 (Fable 5.1, Analyst)
date: 2026-09-04
status: spec-plus-dry-run-inputs
charter: research/inbox/trajectory-training-execution-charter-20260904.md
base: integrate/spine-batch1@e3856849 (rebased; spine delta 6df601b1..e3856849 is the M1 audit doc only)
depends: M1 field map, M2 source census (freeze blocked until both land)
aligned: architect-contract-audit-20260904.md §3b + F2 (SelectionRecipeV1); F6 post-outcome duty accepted
---

# Selection recipe preregistration — provenance-aware census spec and four-arm recipe

## 1. Primary question (frozen)

Holding checkpoint family, tokenizer/template, renderer, optimizer, seed policy, total
supervised assistant-target budget, source strata, and held-out evaluation fixed, which
selection policy (arms A-D below) produces the strongest predeclared family-specific
held-out effect without a protected-family regression?

## 2. Arms (frozen semantics)

All four arms draw from the **same block pool**. Outcome and authority are the only hard
gates; arms differ exclusively in the deterministic **ordering** used to fill the block
budget (charter says "prefer", never "exclude" - a hard quality filter would conflate
selection policy with corpus shrinkage and break the source-comparability control).

| Arm | Ordering within block | Tests |
|---|---|---|
| A | bundle/task order; Fisher-Yates with `seed = sha256(block_key + freeze_digest)` at materialization | honest baseline (arm enum: NEW named `Literal["A","B","C","D"]` per M1 F2 — never a reuse of `base/variant` or `baseline/candidate`) |
| B | shortest `assistant_turns` first, ties by source path; preferred region `assistant_turns < 5` (transparency metric only) | short-trajectory hypothesis |
| C | process-quality pass first (grounded tool use, post-mutation inspection when mutating, non-empty terminal output, no blind identical retry), then shortest-turns | process-quality selection |
| D | arm C's order with one row per tool-sequence signature (`tool_sequence_sha256`) in the leading positions | nonredundant process coverage |

Screen definitions are the typed, deterministic ones in the census prototype
(`MUTATION_TOOLS`, `INSPECTION_TOOLS`, `process_quality_pass` - digests in §7). Changing
a screen after outcomes are observed voids the prereg.

## 3. Blocks (frozen)

`block = family x source_stratum(bundle) x provenance_stratum(model@harness) x difficulty`,
with `cluster_key = family|task_name` (design cell) and difficulty in
`turns_1_4 / turns_5_9 / turns_10_plus`.

**Family rule (interim, explicit):** `template-family-rule/v1` - `action-memory-*` ->
`action-memory`; `*funcdag*` -> `funcdag`; `mcp-rec-*` -> `mcp-recovery`; else full stem.
Historical sidecars carry no registry family binding (`family_binding_absent` is a
recorded hold reason). When M1 binds families from the task registry, blocks are
re-derived; **if any block's arm ordering changes, the recipe refuses** (G1).

**Hard pool gates (per row):** `verifier_result.rewards.reward == 1.0`; authority either
`current_contract` or a reopened historical record whose disposition is
`descriptive-complete` (`refused` and `descriptive-incomplete` never admit); zero
missingness types outstanding. Input rehydration (R1 system/user text via PROMOTION
parent digests) must succeed before any row is admitted.

**Splits** follow blocks (source x family x cluster), never rows or filenames; three
ownership domains (training/discovery, curation-development, sealed test) are assigned
at cluster granularity by the Program Lead + Methodologist in Wave 1.

## 4. Budget and `SelectionRecipeV1` carrier (frozen rule; typed per M1 F2)

Typed carrier is M1's `SelectionRecipeV1` record (referenced by
`TrainingDatasetManifestV1`; not a new manifest family). Mapping of this spec onto its
fields:

| `SelectionRecipeV1` field | This spec binds |
|---|---|
| `arm: Literal["A","B","C","D"]` | §2 orderings; one recipe record per arm per bundle materialization |
| `selection_policy_id` | `provenance-census/v1#arms` — the §2 orderings plus the sub-stratum rule below |
| `block_keys` | `provenance_stratum\|family` (M1's provenance × family). The census's bundle × difficulty cells are sub-strata of a block key used for stratification at materialization; they never split a block into separate recipes |
| `supervised_assistant_token_budget: int` | per block_key: `0.9 ×` pool total, recomputed under the pinned tokenizer before materialization; refuse if any arm ordering flips |
| `token_budget_tokenizer_digest` | Training Engineer's frozen student tokenizer/template digest; the dry-run char budgets in §6 are draft values until this digest exists |

Equalized quantity = **supervised assistant target tokens** under the pinned
tokenizer/template (charter S1). Census proxy = assistant target characters/bytes
(explicitly draft until the pin; producer completion tokens are reported but are NOT
the budget — producer tokenizers, may include hidden reasoning).

**G1 comparability refusals attach to F2** (M1 §4): `arm_changes_source_or_teacher` —
an arm's ordering may never alter which sources, teachers, families, templates, or
action spaces a row's provenance carries; `budget_mismatch` — arms within a block_key
must carry equal `supervised_assistant_token_budget` or the bundle refuses.

`budget(block) = 0.9 × pool target total` (all arms share one pool, so the
min-over-arms attainable size equals the pool total). Each arm fills the budget in its
own order; **semantic truncation of any tool call, tool result, or terminal assistant
target is prohibited** (G3 refuses; tool-call/result truncation refusal rides message
binding per #367). At bundle time the Training Engineer recomputes budgets under the
frozen tokenizer; the recipe refuses if any block's arm-feasibility or ordering changes.

## 5. Preregistration fields to `SftSignalFreezeV1` (Wave 1 freeze checklist)

Declared NOW by this spec: family rule, block definition, pool gates, arm orderings,
budget rule, analysis rule (below), typed exclusions (census `missingness` taxonomy +
`refused`/`descriptive-incomplete` authority + exception rows).

Declared in Wave 1 by Program Lead + Methodologist (NOT defaulted here, per charter):
minimum effect, minimum eligible pairs per family, protected families (recommendation:
**all** families protected - any cross-family regression vetoes), interval method
(per-family percentile cluster bootstrap, deterministic seed — consistent with
`SftSignalFreezeV1` I4's no-pooled-headline rule), and stopping rule (all four arms
materialize for a block or the block is excluded from the primary analysis).
Carrier note (M1 F4): `stopping_rule`, `preregistered_exclusions`
(extending `SftExclusionCode` — never a fourth taxonomy), and `hardware_class` land on
`SftSignalFreezeV1` via the Researcher-Evals lane; this spec's exclusion taxonomy feeds
that closed set.

**No pooled summary is primary.** Per-family decisions only; cross-family numbers may
appear as descriptive denominators with explicit "not a headline" labels.

## 6. Census dry-run on this base (read-only; no model calls)

Command: `uv run python -m evallab.provenance_census --repo-root . --out <dir> --head 6df601b1`.
Byte-identical across reruns (verified). Artifacts digests (prototype run):

- `census.jsonl` `sha256:9fa76ce394a3fde1de5524c86630a488047f5b6aeb2dffd990225081ff737758`
- `report.md` `sha256:ddb11cc545d80e1ddb7e610f40f4bae9ab11bf5c3782e932334143c033274235`
- `prereg-inputs.json` `sha256:a20fcf01f96a6985f280c90f8abdf664cb0f3485a31a99894a3793cdf62d3035`

### Denominators (descriptive, not a headline)

| Quantity | Value |
|---|---|
| Promoted trajectories | 164 (18 bundles, 3 provenance strata) |
| Families (rule v1) | action-memory 130 / mcp-recovery 15 / funcdag 10 / event-summary 3 / transaction-reconciliation 3 / html-js-filter 3 |
| Authority | current_contract 3 / historical_sidecar 161 / none 0 |
| Verified success (reward=1.0) | 100/164 |
| G0-strict SFT-eligible | **0/164** |
| Rehydration-pending (would admit iff CAS rehydration + descriptive-complete reopen) | 75/164 (action-memory 72, event-summary 3) |
| Authority hold classes | descriptive-complete 128 / refused 31 / descriptive-incomplete 2 |
| Exception rows | 2 |

### Feasibility of the four-arm study

- Strict pool: **0 feasible blocks** - no row is SFT-admissible today. Track A's own
  admission gate agrees; this is consistent, not contradictory.
- Pending pool: **13 feasible blocks** (29 formed), all in action-memory + event-summary;
  total action-memory block budgets **51,438 assistant target chars** (~a four-arm study
  on ~72 rows). This is an underpowered pilot by construction; the prereg must predeclare
  it as such, not as a decisive recipe comparison.
- Arm C quality screen: **0 rows pass** among pending rows - blind identical retries are
  endemic (the exact loop behavior Track B mines). The C/D orderings are therefore
  **degenerate** on the current corpus and the study cannot detect a process-quality
  effect until a corpus with quality-passing rows exists (environment loop or rehydration
  of richer producers). Declared now so it cannot be spun later.
- Arm B preferred region (<5 turns) empty in 3 strata (informational; B orders within
  block, never drops a stratum).
- Provenance strata: 3 model@harness strata exist; **independence NOT established**
  (same lab, same harness family, one teacher per family). Charter stop condition
  "cannot form at least two independent provenance strata" is **borderline and
  declared**: M2 must resolve source-comparability before materialization.

### Missingness -> concrete asks

| To | Ask | Unblocks |
|---|---|---|
| M2 (Data) | Rehydrate R1 system/user text for the 75 pending rows via PROMOTION parent digests; reopen 128 descriptive-complete authorities; refuse the 33 refused/incomplete | every admitted row |
| M2 (Data) | License/redistribution statement for the local corpus + any pinned public set | G0 |
| M1 (Architect) | Registry-bound family/template binding to replace `template-family-rule/v1` | block stability |
| Training | Student tokenizer/template pin | budget unit (chars -> tokens recompute) |
| Program Lead | Protected families + minimum effect + minimum pairs (Wave 1) | `SftSignalFreezeV1` |

## 7. Prototype provenance (not part of this lease's paths)

The census prototype lives uncommitted at
`/private/tmp/eval-lab-analyst-census/src/evallab/provenance_census.py`
(sha256 recorded at handoff; deterministic, ruff-clean, read-only). M2 owns census
machinery per the mission split; adopt verbatim or supersede - do not fork silently.

## 8. Freeze gate

This spec becomes the preregistration only when: (a) M1 field map lands and family
binding replaces rule v1; (b) M2 census reproduces the denominators above from an
independent implementation (or diffs are reconciled on the record); (c) Wave 1 declares
the remaining `SftSignalFreezeV1` fields. Until then this document is the spec + dry-run
inputs, and no bundle may materialize (G2 refuses).

## 9. Post-outcome obligation (M1 F6, Analyst-owned)

After outcomes exist, this lane owns **F6**: add `discovery_evidence_epoch` (checkpoint
identity digest) to `CapabilityDeficitArtifact` so every deficit label is tied to the
checkpoint it was mined against and is refused as stale after a model update — the
charter's closed-loop rerun rule as a typed carrier. Implementation follows on
`capability_deficits.py` as a separate exact-head change once the SFT signal gate
produces the first checkpoint transition; nothing in this PR touches that type.

