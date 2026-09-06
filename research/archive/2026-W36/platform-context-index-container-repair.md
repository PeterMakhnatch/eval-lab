# Eval Platform: reject unordered forgotten-index inputs

## Target

- Branch: `feat/context-operation-step-index`
- Worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/context-operation-step-index`
- Current head: `fa73aa46c93337b1380e5cfa91bb4c4e01236e4a`
- Review report: `/tmp/system-architect-context-payload-digest-final-review-fa73aa46.md`

Pause the separate Darwin/admissibility branch only long enough for this one narrow repair. Do not alter the already approved typed digest, domain separator, metadata rules, or step-index semantics.

## Required change

Add a `mode="before"` validator for `ContextOperationPayloadV1.forgotten_message_indices` that:

- accepts only an explicit ordered JSON `list` and the typed Python `tuple` form;
- rejects `set`, `frozenset`, generators, `deque`, mappings, strings, bytes, and every other iterable/container;
- preserves input order exactly;
- does not sort or deduplicate;
- retains the current strict nonnegative integer member validation.

Add negative tests for at least set, frozenset, generator, and deque. Keep the existing order-mutation digest test and all current malformed-member/canonicalization tests.

## Acceptance

- `uv run pytest -q tests/test_semantic_facts.py tests/test_memory_continuity_producer.py`
- Ruff check and format check on touched files only.
- `git diff --check` clean; worktree clean after commit.
- Return the new commit hash and exact evidence. No integration, producer migration, digest backfill, model run, or unrelated formatting.
