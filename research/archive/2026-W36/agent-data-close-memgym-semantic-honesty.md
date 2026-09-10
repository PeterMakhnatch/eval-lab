# Engineer Agent Data — close last MemGym semantic-honesty blockers

## Exact state

- Worktree: `/private/tmp/eval-lab-memgym-source-ingestion`
- Branch/head: `data/memgym-source-ingestion @ f229dd2500a746d843250dfba3ef25f6f2a32765`
- Report: `/tmp/system-architect-memgym-source-ingestion-final-review-f229dd25.md`
- M1–M5, upstream bytes/license, ordering, token semantics, exact-byte provenance, hold/card behavior all pass.

Do not rebase/integrate/activate, run MemGym/model/control, change schemas, or spawn subagents.

## S1 — exact outcome string or null

Remove `str(raw_outcome)` coercion.

- Missing or explicit null `episode_outcome` => unavailable/None according to the existing outcome schema.
- Present exact string => preserve byte-decoded value exactly; apply only the schema’s existing nonempty/exactness rule, with no trim/case normalization unless already source-defined.
- Present bool/int/float/list/dict => fail closed.
- Never manufacture Python repr text that was absent from captured JSON.

Add public adversaries for bool, int, float, list, dict, whitespace-decorated strings if exactness requires refusal, plus valid string/null.

## S2 — exact compaction marker

- When `steps[].memory.new_compaction` is present and non-null, require exact JSON bool (`type(value) is bool`).
- `True` maps only to the existing digestless/typed-HOLD compaction behavior; `False` maps to session boundary.
- Present integer `0/1`, strings (`"true"`/`"false"`), floats, lists, dicts refuse before operation mapping.
- Define absent/null policy explicitly and test it; never let malformed data become a negative boundary.

Retain the hard rule: no ordered forgotten indices/payload/content digest are fabricated.

## Verification/handoff

Run complete MemGym + memory-continuity + semantic/governance matrix including all M1–M5 and new S1/S2 adversaries; touched `ty`, Ruff check/format, compile, diff check, fixture byte comparison, clean status. Commit/push cleanly and page `wH:p9` with exact head/test results and no-activation/no-model/no-subagent confirmation. Return for final Architect approval.
