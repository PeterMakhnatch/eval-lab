# Engineer Agent Data — prepare MemGym source-only C0 ingestion

## Exact base and upstream authority

Create a dedicated isolated worktree/branch from canonical:

- Eval Lab base: `9768ad60a6d5a0cb90e4ff5dd1fbe116b050cc63`
- Suggested worktree: `/private/tmp/eval-lab-memgym-source-ingestion`
- Suggested branch: `data/memgym-source-ingestion`
- Librarian report: `/tmp/librarian-memgym-local-source-verification.md`
- Author repo: `https://github.com/WujiangXu/MemGym`
- Exact upstream commit: `50b404e6ae4e1fcd453d3e07963eb3e6312cbded`
- Tree: `68c081f0271cfd7951e490afd59457b029ba0535`
- Repo code/fixture license: Apache-2.0, LICENSE digest `sha256:04a6dfa6a8e2222a1dc9959758c94e29335eaa7bb782da9470788396aa5bf64f`, NOTICE digest `sha256:67866b5f1f5c41843e68f5c435e529d5ec86af31c2fe5265de868fd7986fe989`.

Do not edit dirty root/canonical worktree, spawn subagents, run MemGym/install.sh/Docker/browser/network benchmark/model/control, inspect/adopt the unverified HF corpus, register/promote/activate, or modify shared semantic schemas.

## Exact scope

Source/package adoption is GO only for locally verified Apache-2.0 repository fixtures. C0 ingestion splits:

- **GO:** step/session/token/outcome/no-compaction path from the exact released tau2 fixture.
- **HOLD:** compaction payload, verifier-evidence, memory read/use, certification, registration, and measurement.

Implement the smallest existing-convention adapter and source card. Expected scope:

1. `src/evallab/interpretation/producers/memgym.py`
2. producer export only if repository convention requires it
3. `tests/fixtures/memgym/` containing exact three upstream files plus machine-readable upstream commit/tree/path/digest/license attribution and the required LICENSE/NOTICE copies
4. `tests/test_producer_memgym.py`
5. `library/benchmarks/memgym.md` benchmark card (documentation is explicitly requested for this program)

Avoid a second fact schema. If the integrated types cannot honestly represent a source field/status, leave it typed unavailable in the adapter result or omit the positive fact; do not alter `semantic_facts.py` in this branch.

## Source mapping

Use exact released fields only:

- trial identity from direct `domain` + `task_id` source fields under a documented domain; never a path name;
- `session_id <- steps[].side`;
- `step_index <- steps[].msg_index` (strict integer, globally unique total order after sorting);
- never use `steps[].step` as total order: it restarts per side and collides;
- operation identity may be a canonical domain-separated composite of exact `(task_id, side, msg_index)` only for the source step/session fact; it is not a tool-call ID and must not establish memory read/use identity;
- direct before/after token counts from `original_tokens`/`filtered_tokens`;
- direct summarizer prompt tokens only when present and exact-type valid;
- outcome from exact `episode_reward`, `episode_outcome`, `result.reward`, and `result.success`, with source provenance/digests; null evaluation fields remain unavailable and reward 0 must not become verifier validity;
- `context_position_tokens`, configured/realized sizes, tool-call IDs, memory read/use, and verifier evidence remain typed unavailable.

Treat one step row as `session_boundary` only if that enum’s integrated semantics genuinely cover a source message/context boundary; document and test the interpretation. Do not label arbitrary source steps as writes/reads/uses/compactions.

## Compaction hard boundary

The released output emits `forgotten_count`, not ordered forgotten-message indices. Ordered indices exist only transiently upstream and are not serialized. Therefore:

- Do not construct `ContextOperationPayloadV1` for a compaction event.
- Never pass `forgotten_message_indices=()` for a positive forgotten count.
- Never compute a content digest from summary/count alone.
- When a source event indicates compaction but lacks ordered indices, return a deterministic typed unavailable/refusal reason such as `ordered_forgotten_indices_unavailable`; do not emit a positive digest-bound compaction fact.
- The exact released fixture has zero compactions. Test the observed no-compaction branch from that fixture, but do not invent a MemGym-shaped positive compaction fixture. A minimal malformed/count-without-indices unit object may test refusal only if clearly labeled an adapter negative, never source validation.

## Determinism and validation

- Parse exact JSON schema fail-closed: bool-vs-int, duplicate/missing `msg_index`, invalid side, non-finite/nonnumeric reward/tokens, conflicting training/result task identity, changed bytes/digest, unexpected extra identity fields where the contract is strict.
- Source row representation order must not change canonical fact order/identity.
- Same step coordinate across sides is permitted only because total order uses `msg_index`; duplicate `msg_index` refuses.
- Do not allow raw paths, labels, or list position to fill missing identity/order.
- Feed canonical facts through the integrated producer only if its status remains honest; assert the no-write/read/use fixture does not create positive memory linkage or measurement-readiness claims.

## Card requirements

Record exact paper `arXiv:2605.20833`, repo/commit/tree, LICENSE/NOTICE digests, fixture paths/digests, the paper MIT-vs-repo Apache discrepancy, missing corpus license, no lockfile/network-cloning install, evaluator determinism per track, mapping/holds, and explicit non-activation. Do not claim corpus adoption or benchmark validity.

## Verification/handoff

Run focused adapter/producer/semantic/feature-governance tests, touched `ty`, Ruff check/format, compile, diff check, clean status. Commit cleanly and page `wH:p9` with exact base/head/stat, fixture digests, mapping/status behavior, tests, and no-model/no-activation/no-subagent confirmation. Return for Architect review; do not claim approval.
