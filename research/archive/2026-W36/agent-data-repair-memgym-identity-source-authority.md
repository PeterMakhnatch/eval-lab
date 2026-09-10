# Engineer Agent Data — repair MemGym identity and exact-source authority

## Exact state

- Worktree: `/private/tmp/eval-lab-memgym-source-ingestion`
- Branch/head: `data/memgym-source-ingestion @ 5c966a960fbdf47810a49b02d50be6640a11cdb3`
- Architect report: `/tmp/system-architect-memgym-source-ingestion-review-5c966a96.md`
- Upstream byte/license/fixture integrity, ordering, zero-compaction behavior, outcome holds, no activation, tests/statics otherwise pass.

Do not rebase/integrate/activate/register/promote, run MemGym/model/control, alter shared schemas, or spawn subagents. Close only the five authority boundaries and regressions.

## M1 — preserve exact native identity types

- Treat native `task_id` as an exact JSON identity scalar using the source-supported types. Do not `str()`-coerce. Exclude bool from integer identity.
- Training integer `0` versus result string `"0"` must refuse conflicting identity.
- Preserve exact identity strings; do not strip or case-normalize domain/task identity.
- Replace colon concatenation with a collision-free domain-separated canonical structured identity. A digest over canonical JSON that preserves field names and JSON types is preferred:
  - trial identity domain binds exact `{domain, task_id}`;
  - operation identity domain binds exact `{trial_identity, side, msg_index}`.
- `(domain='a:b', task_id='c')` and `(domain='a', task_id='b:c')` must yield distinct trial and operation identities.
- The fact’s required string `trial_id`/`operation_id` may be the stable domain-tagged digest identity; retain native domain/task ID separately only where the existing adapter result schema already exposes it without weakening types.

## M2 — remove caller identity substitution

Clean cutover: remove the arbitrary public `trial_id` override. Derive it only from exact native fields. Migrate every candidate caller/test; no alias or compatibility path. If a diagnostic expected identity is useful, it may only be an assertion that must exactly equal the independently derived identity and must never replace it.

Add a public regression proving `path:list-position:7` cannot become trial identity.

## M3 — exact side/session admission

- Require source `side` to be an exact string member of `{"agent", "user"}`.
- Leading/trailing whitespace, case variants, nonstrings, and empty values refuse.
- Remove shared `.strip()` normalization from every identity-bearing helper; preserve nonidentity human text separately if needed.

## M4 — direct prompt token semantics

- Missing or explicit null `summarizer_prompt_tokens` => unavailable (`None`).
- Present strict integer `>= 0` excluding bool => preserve exactly, including `0`.
- Present bool/string/float/negative => fail closed, not unavailable and not coerced.
- Update the card: no unsupported `> 0` rule.

Test `0`, positive, null/absent, bool, numeric string, float, and negative.

## M5 — exact-byte source/provenance authority

Redesign the public ingestion boundary so source digests are computed from the exact bytes actually parsed, not from reserialized mappings or an unverified optional caller digest.

Preferred clean API:

- accept/capture `training_bytes: bytes` and optional `result_bytes: bytes`;
- compute exact SHA-256 before parsing;
- parse those same captured bytes exactly once under strict JSON/schema validation;
- emit training facts with the exact training artifact digest/source reference;
- when result bytes supply outcome values, bind provenance/digest to result bytes; when result is absent and training supplies outcome, bind explicitly to training, never default `provenance_source='result.json'`.

A path convenience may read each regular no-follow file once into bytes and delegate, but paths themselves are not identity. Do not keep a public mapping API that can claim exact-byte provenance without exact bytes.

- Reordered/whitespace-different JSON bytes with equal parsed values must have distinct source digests while canonical fact semantics/order may remain otherwise equal.
- Validate any independently supplied expected digest against captured bytes; never accept it as authority by assertion.
- Migrate all candidate tests/callers to the new exact-byte API and remove obsolete defaults.

## Preserve

- `msg_index` sole total order; exact `side` session;
- no write/read/use/tool identity claims;
- zero-compaction released fixture behavior;
- count-without-indices compaction has no payload/content digest and stays typed HOLD;
- source-only C0 card wording and all certification/measurement/corpus holds;
- exact vendored upstream bytes and attribution.

## Verification/handoff

Run adapter + memory-continuity + semantic/governance matrix, all new M1–M5 adversaries, touched `ty`, Ruff check/format, compile, diff check, clean status, and exact upstream fixture digest comparison. Commit/push cleanly and page `wH:p9` with exact head/stat, identity domains/API cutover, source-digest behavior, tests, and no-activation/no-model/no-subagent confirmation. Return for Architect review; do not claim approval.
