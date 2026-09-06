# System Architect — memory continuity ingestion final review

## Exact target

Read-only review:

- Worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/agent-data-locomo-ingestion`
- Branch: `data/locomo-atif-memory-ingest`
- Original blocked checkpoint: `6171a6a275103ab03a716055f0286e9693227d94`
- Integrated canonical base: `1ecfc4a587be1301fa1e5a3ddf4e4bf8a942c3ee`
- Exact candidate head: `e5c44bcde22047a5d2ac8c2c9c919626053fcc9d`
- Repair brief: `research/inbox/agent-data-repair-memory-continuity-on-step-index.md`

Do not edit, rebase, integrate, push, activate a dataset, or run a model.

## Required review A1–A5

### A1 — content identity

- A declared content digest is mandatory for positive first-class memory operations.
- It must be canonical lowercase `sha256:` syntax and exactly equal `context_operation_content_digest(ContextOperationPayloadV1)` from the shared integrated semantic contract.
- Missing/malformed/digest-only/unparseable/raw-undomained/type-mismatched/mismatched values yield typed `missing_content_identity` and no positive link.
- No fallback to content, observation, context, raw SHA, or local parallel digest function remains.

### A2 — operation identity

- Complete nonempty first-class `tool_call_id` is mandatory.
- Missing/partial/duplicate IDs produce typed `missing_operation_identity`, emit no positive facts, and generate no synthetic IDs/counters.
- Read→use linking is one-to-one over exact identities/content, never name/text similarity.

### A3 — ordering

- Integrated `ContextOperationFact.step_index` is the sole temporal coordinate.
- Missing/duplicate/same-step ambiguity yields typed `missing_step_order` and null link/latency metrics.
- No lexical operation name, tool ID, sorted map/set, or output-container order becomes event order.
- Fact-set canonical serialization must not be interpreted as temporal inference.

### A4 — exact operation names

- Only exact canonical operations are admitted after at most one exact sanctioned prefix removal.
- Repeated/chained prefixes, arbitrary namespaces, suffix/substring matches, case folding, fuzzy aliases, and near matches refuse.

### A5 — distinct digest domains

- Upstream `source_digest` remains source identity.
- `fact_set_digest` is a separate `evallab.memory-continuity-fact-set.v1\x00` domain over canonical emitted facts and is not an alias of source/content/raw/container digest.
- Stable fact set is representation-order stable; changed emitted facts change it.

## Broader contract

- Rebase preserved the canonical c95/1ec context-ordering and exact-type payload contract.
- Feature registry wording remains source feature availability only; no certification, registration, promotion, benchmark validity, LoCoMo activation, or MemGym activation claim.
- Changed scope is limited to memory producer/exports/feature registry/tests and the existing handoff note.
- Public adversarial tests cover each boundary, including raw undomained SHA, digest-only input, bool-vs-int, duplicate/missing IDs, same-step ambiguity, prefix tricks, and source-vs-fact-set domains.

## Writer evidence

- 59 passed (25 memory continuity + 34 semantic facts/registry contract)
- `ty`: 0 diagnostics
- Ruff format/check: clean
- clean exact head
- no model, LoCoMo activation, or integration.

## Required output

Write `/tmp/system-architect-memory-continuity-review-e5c44bcd.md` with first-line `APPROVE` or `BLOCK`, evidence for A1–A5, exact commands/results, blockers, and no-edit/no-model/no-activation confirmation. Page `wH:p9` with exact verdict/report/head.
