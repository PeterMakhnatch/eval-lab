# Repo Custodian — integrate approved MemGym source-only ingestion

## Exact target

- Canonical worktree/head: `/private/tmp/eval-lab-staged-spine-integration @ 9768ad60a6d5a0cb90e4ff5dd1fbe116b050cc63`
- Approved source worktree: `/private/tmp/eval-lab-memgym-source-ingestion`
- Source branch/head: `data/memgym-source-ingestion @ eb6e5ecc8527011a886b4b729cb7420863fb5719`
- Source base: exact canonical `9768ad60`
- Approval: `/tmp/system-architect-memgym-source-ingestion-approval-eb6e5ecc.md`
- Upstream pin/tree: `50b404e6ae4e1fcd453d3e07963eb3e6312cbded` / `68c081f0271cfd7951e490afd59457b029ba0535`

Do not edit dirty root, activate/register/promote MemGym, run models/controls/MemGym/install, or integrate unrelated work.

## Exact 11-path landing

Replay the exact full source delta:

- `library/benchmarks/memgym.md`
- `research/inbox/agent-data-memgym-source-ingestion-reply.md`
- `src/evallab/interpretation/producers/__init__.py`
- `src/evallab/interpretation/producers/memgym.py`
- `tests/fixtures/memgym/0_replay.json`
- `tests/fixtures/memgym/0_training.json`
- `tests/fixtures/memgym/ATTRIBUTION.json`
- `tests/fixtures/memgym/LICENSE`
- `tests/fixtures/memgym/NOTICE`
- `tests/fixtures/memgym/result.json`
- `tests/test_producer_memgym.py`

## Preserve exact contract

- Captured exact source bytes/digests and verified Apache-2.0/NOTICE/pin/tree.
- Exact native task identity types and domain-separated collision-free structured trial/operation identities; no caller override.
- `step_index=msg_index`, exact side sessions, exact nonnegative token values including zero.
- Outcome exact string/null only; compaction flags exact bool/null only; all malformed present types refuse.
- No fabricated forgotten indices/payload/content digest, tool IDs, read/use link, verifier validity, or source order.
- C0 step/session/token/outcome/no-compaction source feature only. Compaction, verifier, corpus, certification, registration, measurement, and activation remain explicit HOLD.
- No shared semantic schema or feature-registry activation.

## Verification

Run:

- `tests/test_producer_memgym.py`, memory-continuity, semantic facts, feature-governance contract;
- exact fixture SHA-256 comparison against attribution;
- touched `ty`, Ruff check/format, compile, diff check;
- clean status and exact path/stat.

Commit cleanly and page `wH:p9` with old/new exact heads, commit/stat, test counts, fixture digest confirmation, and no-activation/no-model/no-root-edit confirmation.
