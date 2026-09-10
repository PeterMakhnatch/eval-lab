# System Architect — review MemGym source-only C0 ingestion

After the current projection settlement re-review, review this exact MemGym source branch.

## Exact candidate

- Worktree: `/private/tmp/eval-lab-memgym-source-ingestion`
- Branch/head: `data/memgym-source-ingestion @ 5c966a960fbdf47810a49b02d50be6640a11cdb3`
- Base: canonical `9768ad60a6d5a0cb90e4ff5dd1fbe116b050cc63`
- Upstream verification: `/tmp/librarian-memgym-local-source-verification.md`
- Exact upstream: `WujiangXu/MemGym @ 50b404e6ae4e1fcd453d3e07963eb3e6312cbded`, tree `68c081f0271cfd7951e490afd59457b029ba0535`
- Candidate: 11 paths, +2115/-0; adapter/export/card/reply, exact three fixtures + attribution/LICENSE/NOTICE, tests.

Read-only. Do not edit/rebase/integrate/register/promote/activate, run MemGym/install/model/control, or spawn subagents.

## Required review

### Source/package integrity

- Vendored LICENSE/NOTICE and three fixtures match exact upstream bytes/digests/pin/tree/path; attribution is machine-readable and accurate.
- Card states Apache-2.0 code/fixture authority, paper’s contradictory MIT corpus statement, and that HF corpus license/content remain unverified.
- No network/runtime dependencies, copied corpus, invented fixture, or unstated third-party code are adopted.

### Ordering and identity

- `step_index` comes only from strict integer `steps[].msg_index`, with global uniqueness and canonical order; `steps[].step` is present but never used as total order because it restarts per side.
- `session_id` is exact validated `side`; trial and operation identities are domain-separated composites of exact native IDs, never path/list-position identities and never misrepresented as tool-call IDs.
- Missing/duplicate/bool `msg_index`, invalid sides, conflicting training/result identity, and representation reorder fail closed or canonicalize without changing identity.

### Semantic honesty

- Source steps are mapped to `session_boundary` only if the established enum semantics genuinely mean message/context boundary. No arbitrary row becomes memory write/read/use/compaction.
- Direct token fields preserve exact types/units; message counts do not become byte/token sizes; null/absent prompt tokens are unavailable.
- Reward/outcome remain direct descriptive source facts. Null evaluation fields and reward 0 never establish verifier validity, success, certification, or measurement readiness.
- Generic memory-continuity output does not claim positive write/read/use linkage for this fixture.

### Compaction hard boundary

- The released fixture’s zero-compaction branch is source-verified.
- No `ContextOperationPayloadV1`, ordered forgotten indices, or payload digest is fabricated.
- A compaction/count-without-indices negative is typed unavailable/refused and does not emit a positive digest-bound compaction fact.
- `forgotten_message_indices=()` is never used to represent a positive forgotten count.
- No synthetic positive compaction fixture is presented as upstream validation.

### Scope/regression

- Adapter reuses integrated facts/producer; no second fact schema or shared semantic schema change.
- Card is explicit C0/source-only and HOLD for compaction, verifier, read/use, certification, registration, measurement.
- Existing memory-continuity/semantic behavior remains green; no hidden feature-registry activation.

## Verification/output

Independently run the adapter + memory-continuity + semantic matrix, exact source/tamper/order/compaction-negative probes, touched `ty`, Ruff/format, compile, diff check, source digest comparison, clean status.

Write `/tmp/system-architect-memgym-source-ingestion-review-5c966a96.md` beginning `APPROVE` or `BLOCK`, with exact evidence/any blockers. Page `wH:p9`; confirm no edits/activation/model/subagent.
