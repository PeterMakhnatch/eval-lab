# Engineer Agent Data — close last two memory-continuity blockers

## Exact state

- Worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/agent-data-locomo-ingestion`
- Branch/head: `data/locomo-atif-memory-ingest @ b051081cb62f2d98917e9fc351b6f537cb6523b3`
- Rereview: `/tmp/system-architect-memory-continuity-rereview-b051081c.md`
- A1 exact payload, A4 exact names, tied-invalid reversal, A3 step order, domains, and C0 wording pass.

Do not rebase/integrate/activate, call a model/control, or broaden scope.

## A2 universal use assignment

Current code iterates only digests present in `linked_reads_by_digest`, so `write(A), read(A), use(B)` silently reports observed with zero links. Repair the assignment/refusal logic to inspect every admitted `memory_use`.

- Every use must have exactly one eligible unused preceding read with the same exact digest.
- Zero eligible reads, including a digest absent from all reads, is unmatched and makes the trial’s link/latency metrics typed unavailable.
- More than one eligible unused read is ambiguous and makes the same metrics unavailable.
- A read is consumed after one use; reuse/refire with no remaining eligible read is unavailable.
- Never emit a positive numeric subset when any use assignment in the trial is unmatched or ambiguous.
- Preserve valid alternating one-to-one assignments.

Add exact public regression: write/read(A), use(B) => unavailable with null link/latency metrics. Retain existing one-read/two-use, ambiguous-read, and alternating-positive tests.

## A5 complete emitted fact identity

Current fact-set serialization hand-selects seven fields and omits emitted fields including `configured_size`, `realized_size`, `prompt_tokens`, `before_token_count`, and `after_token_count`.

- Canonicalize the complete serialized value of every emitted `ContextOperationFact`, including inherited fact fields and explicit nulls according to the project’s existing canonical model serialization convention.
- Sort those complete canonical fact serializations only for representation stability, then apply the existing distinct fact-set domain digest.
- Do not use the representation sort for temporal inference.
- Avoid a second hand-maintained field list; schema evolution must not silently fall outside fact-set identity.

Add exact public regression: otherwise identical emitted facts with `prompt_tokens=10` versus `20` must have distinct fact-set digests. Retain tied-invalid reversal equality and valid reversal stability tests.

## Verification/handoff

Run focused producer + semantic facts + feature-governance, touched `ty`, Ruff check/format, compile, diff check, clean status. Commit and push cleanly. Page `wH:p9` with exact head/commit/stat, test count, the complete-serialization primitive used, and no-activation/no-model confirmation. Return exact head to Architect; do not claim approval.
