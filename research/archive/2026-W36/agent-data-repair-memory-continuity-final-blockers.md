# Engineer Agent Data — repair final memory-continuity blockers

## Exact state

- Worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/agent-data-locomo-ingestion`
- Branch: `data/locomo-atif-memory-ingest`
- Current clean head: `e5c44bcde22047a5d2ac8c2c9c919626053fcc9d`
- Review report: `/tmp/system-architect-memory-continuity-review-e5c44bcd.md`
- Architect verdict: BLOCK on four exact adversarial gaps; A3/shared step-index spine otherwise passes.

Do not rebase yet, integrate, activate LoCoMo/MemGym, register/promote/run measurement, call a model/control, or broaden scope.

## Required repair

### A1 — exact payload authority only

- `_extract_payload_v1` may admit only the exact authorized typed `arguments["payload"]` field.
- Delete fallback to `arguments["content"]`.
- Delete synthesis from top-level `summary` / `forgotten_message_indices` / other flattened argument fields.
- A declared digest without a valid exact typed payload remains `missing_content_identity`; observation/context/raw content is never authority.
- Add public tests for a valid shared-domain digest paired with a valid typed payload under `content`, and for flattened top-level payload fields. Both must refuse positive content binding.

### A2 — one-to-one read/use identity

- A read may contribute to at most one positive read→use link.
- Do not independently reuse “latest preceding read” for multiple uses.
- Refuse link/latency metrics for the affected trial whenever the exact digest/step relation has no unique one-to-one assignment: one read/two uses, multiple simultaneously eligible reads for a use, duplicate/ambiguous candidates, or an unmatched reuse.
- A temporally unambiguous alternating pattern may still link only if each assignment has exactly one eligible unused preceding read.
- Return the existing typed unavailable/refusal contract; do not guess and do not count a subset while hiding ambiguity.
- Add public tests for one-read/two-use and multi-read/use ambiguity plus a valid unambiguous one-to-one pattern.

### A4 — exact operation names

- Remove `.strip()` and any equivalent whitespace/case normalization from operation-name admission.
- Admit only exact canonical names, optionally after exactly one sanctioned exact prefix removal among `memory_mcp_`, `mcp_`, and `functions.`.
- Leading/trailing whitespace must refuse. Add both adversaries.
- Do not weaken existing chained/repeated/unrelated/case refusal tests.

### A5 — representation-stable fact-set identity

- The same fact multiset must produce the same fact-set digest even when invalid facts tie on `step_index` and `operation_id` and container order is reversed.
- Use a complete canonical serialized fact value as a representation-only tie-breaker, separate from temporal inference, or omit fact-set identity on identity/order-invalid sets if the established schema permits. Prefer preserving a stable domain-separated digest without inventing order.
- Never use the tie-breaker as temporal precedence/link order.
- Add reversal tests for tied invalid facts and prove valid fact-set digest behavior remains unchanged.

## Preserve

- shared `context_operation_content_digest(ContextOperationPayloadV1)` only;
- `ContextOperationFact.step_index` as the sole temporal coordinate;
- missing/duplicate step typed unavailable;
- complete unique non-synthetic tool-call IDs;
- distinct upstream `source_digest` and domain-separated fact-set digest;
- C0/source-only wording and no readiness/certification claim;
- exact existing five-file scope unless a test-only fixture requires otherwise.

## Verification and handoff

Run the focused memory/semantic/feature-governance matrix, `ty`, touched Ruff check/format check, compile, and `git diff --check`. Commit cleanly and page `wH:p9` with:

- exact new head/commit and diff stat;
- exact implementation choice for one-to-one uniqueness and fact-set canonicalization;
- focused test counts including all new adversaries;
- clean status and no-activation/no-model confirmation.

Do not claim approval; the repaired exact head must return to System Architect.
