# Engineer Agent Data — repair memory continuity ingestion on integrated step index

## Exact writer target

- Worktree: `/Users/petermakhnatch/Developer/eval-lab/.worktrees/agent-data-locomo-ingestion`
- Branch: `data/locomo-atif-memory-ingest`
- Clean blocked checkpoint: `6171a6a275103ab03a716055f0286e9693227d94`
- Canonical integration worktree: `/private/tmp/eval-lab-staged-spine-integration`
- Exact canonical base to rebase/migrate onto: `1ecfc4a587be1301fa1e5a3ddf4e4bf8a942c3ee`
- Integrated context source: `c95d67fb`, replayed as `1ecfc4a5`
- Prior original branch base: `6eebed8751c74133bc76400a5c2d69a4911a2be3`

Do not edit canonical integration, the dirty root, or another worktree. Do not run LoCoMo, models, controls, promotion, registration, or measurement activation.

## Rebase/migration rule

Rebase/migrate the blocked memory-ingestion branch onto exact `1ecfc4a5`. For overlapping files, the integrated canonical context-ordering contract is authoritative. Reapply only the reviewed memory-continuity semantics; do not overwrite `ContextOperationFact.step_index`, exact-type payload digest validation, unordered-container refusal, or current isolation/admissibility/external-lineage changes.

Expected branch scope remains focused on:

- `src/evallab/interpretation/feature_registry.py`
- `src/evallab/interpretation/producers/__init__.py`
- `src/evallab/interpretation/producers/memory_continuity.py`
- `tests/test_memory_continuity_producer.py`
- existing handoff note only if the repository workflow requires updating it

## Blocking repairs A1–A5

### A1 — strict declared content identity

Any declared operation/content digest must be present where the schema requires it, syntactically canonical, and exactly equal to the domain-separated canonical digest of the exact typed payload. Missing, malformed, recomputed-under-another-domain, or mismatched declarations must yield typed unavailable/refusal. Never silently recompute and accept.

### A2 — complete unique operation identity

First-class linking requires complete operation identity, including `tool_call_id` where applicable. Refuse partial identity and duplicate `tool_call_id`/operation identity. A read request and its later realized use must join one-to-one; ambiguous, duplicated, missing, or many-to-one joins are unavailable, not guessed.

### A3 — integrated total ordering only

Use the integrated `ContextOperationFact.step_index` as the sole operation-order coordinate. Do not order same-step operations by operation name, `tool_call_id`, lexical ID, sorted map/set order, or output container iteration. Missing/duplicate/ambiguous step index is typed `missing_step_order`/unavailable. Preserve real trajectory source order only where the integrated contract explicitly supplies it.

### A4 — exact operation-name admission

Admit only exact supported operation names after the single explicitly sanctioned exact prefix removal. Do not use suffix/substring matching, repeated prefix stripping, case folding, fuzzy aliases, or arbitrary namespace removal. Near matches and extra prefixes refuse.

### A5 — separate digest domains

Keep source/corpus identity and derived fact-set identity separate. `fact_set_digest` must be a distinct domain-separated digest over the canonical emitted fact set, not an alias of source digest, content digest, raw file hash, or container hash. Stable source bytes with changed facts must change the fact-set digest; representation-only nondeterminism must not.

## Required observable behavior

- Memory read, write, and realized-use facts preserve exact operation identity, source step index, typed payload identity, and evidence citation.
- Read-use credit is assigned only to the first-class matching result/use, never the request merely because names or surrounding text resemble it.
- Duplicate, incomplete, unordered, content-mismatched, and unsupported operations produce deterministic typed unavailable/refusal facts/status; no partial positive fact leaks through.
- Feature registry metadata accurately labels this as source ingestion/feature availability, not certification, registration, promotion, benchmark validity, or LoCoMo measurement readiness.
- MemGym/LoCoMo source status remains unchanged and inactive.

## Verification

Add/retain public tests for all A1–A5 boundaries and plausible bypasses. Run:

- `uv run pytest tests/test_memory_continuity_producer.py tests/test_semantic_facts.py`
- focused feature-registry tests for the touched feature declarations;
- touched Ruff check/format and `git diff --check`;
- exact diff/symbol/test preservation audit against rebased canonical base.

Commit cleanly and page `wH:p9` with exact old checkpoint, canonical base, new head, rebase/conflict catalog, changed paths/stat, test counts, and no-model/no-LoCoMo/no-integration confirmation. Do not integrate.
