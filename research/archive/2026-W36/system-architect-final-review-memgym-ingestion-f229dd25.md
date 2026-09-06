# System Architect — final MemGym source-ingestion closure review

## Exact target

- Worktree: `/private/tmp/eval-lab-memgym-source-ingestion`
- Branch/head: `data/memgym-source-ingestion @ f229dd2500a746d843250dfba3ef25f6f2a32765`
- Base: `9768ad60a6d5a0cb90e4ff5dd1fbe116b050cc63`
- Prior blocked report: `/tmp/system-architect-memgym-source-ingestion-review-5c966a96.md`
- Exact upstream/pin/tree/license/fixtures unchanged and previously passed.

Read-only. Do not edit/rebase/integrate/activate/register/promote, run MemGym/model/control, or spawn subagents.

## Closure probes

1. **Exact native identity types:** training integer task ID versus result string task ID refuses; bool/float IDs refuse; identity strings are not trimmed/case-normalized.
2. **Collision-free composites:** domain-separated canonical structured trial/operation digests preserve JSON types and distinguish delimiter adversaries such as `(a:b,c)` vs `(a,b:c)`.
3. **No override:** arbitrary `trial_id` parameter is removed; optional `expected_trial_id` can only assert exact independently derived equality and never replace identity.
4. **Exact side:** only literal `agent`/`user`; whitespace/case/nonstring/empty values refuse.
5. **Prompt tokens:** missing/null unavailable; strict integer >=0 preserved including zero; bool/string/float/negative refuse.
6. **Exact source bytes:** public API accepts captured raw bytes, hashes before parsing, parses those same bytes, rejects mapping input, validates expected digest, and produces different source digests for byte-distinct equivalent JSON. Outcome provenance binds result bytes when present and training bytes only when result absent.
7. Reconfirm prior passing ordering/zero-compaction/token/outcome/hold/card/license behavior, exact 11-path scope, no shared schema, no activation.

Run exact public adversaries plus adapter/memory/semantic/governance matrix, fixture digest comparison, touched `ty`, Ruff/format, compile, diff check, clean status.

Write `/tmp/system-architect-memgym-source-ingestion-final-review-f229dd25.md` beginning `APPROVE` or `BLOCK`; page `wH:p9` with verdict/evidence. No edits/model/subagents.
