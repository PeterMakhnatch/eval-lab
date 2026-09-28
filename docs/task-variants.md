---
status: living
audience:
  - builder
  - analyst
  - operator
---

# Task variants: versioning modified tasks without touching the original

Status: normative. Owner: DATA-STRATEGY (module: `evallab.task_variants`).
Date: 2026-09-28.

## Why variants exist

Two very different workflows both produce a *modified task*:

1. **Optimizer rewrites** — GEPA/DSPy propose a new instruction for a task the
   model keeps failing. The instruction is the treatment under study.
2. **Hand fixes of broken tasks** — an audit finds a verifier that crashes
   before grading or an environment that never becomes healthy. Fixing it
   changes the task's meaning and must not silently replace the original.

In both cases the original task stays the control. Every reward, error rate,
or pass-rate comparison is only attributable if each side of the comparison
resolves to one exact, content-addressed task package. So a modified task is
never edited in place: it becomes a **variant** — a full derived package plus
a small git-tracked **lineage record** linking it to its parent (which may
itself be a variant; chains are allowed).

## Storage split

| What | Where | Git |
|---|---|---|
| Lineage record (schema `evallab.task_variant/v1`) | `library/task-variants/<task_slug>/<digest12>.json` | tracked, reviewable |
| Materialized variant package | `<derived root>/task-store/variants/<task_slug>/<digest12>/` | ignored (rebuildable) |

`<task_slug>` is the task name with `/` replaced by `__` (e.g.
`mimo-v2.6-rl__candidate-0109-science-robotics`). The variants store resolves
through `evallab.storage.paths` to the primary checkout's `derived/`
directory, so every worktree shares one store; the records live in whatever
checkout derives the variant and are committed from there. Only changed files
are inlined in the record (≤1 MiB, UTF-8), which keeps records reviewable
even for large packages.

## The record

One JSON object per variant: variant and parent digests (both
`registry.task_directory_digest`, plus the Harbor lock digest
`registry.harbor_task_digest` for each), parent provenance (`hf` with repo +
40-hex revision + path, `variant` with the parent record's repo-relative
path, or `local`), the transform as `name@version`, the changed components
(`task_toml`/`instruction`/`environment`/`verifier`/`solution`, classified by
comparing `registry.compute_task_digests` before and after), per-file
before/after digests with the after-bytes inline, rationale, free-form
inputs, author, UTC timestamp, and a `status` of `candidate`, `validated`, or
`rejected`.

Integrity invariant: `materialize(parent bytes + record files)` reproduces
`variant_digest` exactly — this is proven on every rebuild, so a deleted
store directory is recoverable from Git plus the parent package alone.

## Guarantees

- **Never overwrite.** Deriving refuses if the record or the package already
  exists; identical inputs deterministically produce the same digest, so a
  re-derive is a refusal, not a fork.
- **Refusals.** Binary or >1 MiB changed files, symlinks in the parent,
  relative paths that escape the package, deletions of files the parent does
  not have, and no-op derivations (variant digest == parent digest) are all
  rejected with explicit errors.
- **Append-only history.** `evallab tasks variant-status` appends evidence
  and moves `candidate → validated|rejected`. Identity fields are immutable
  and verdicts are final; a reversed decision requires a new variant derived
  from the same parent, so scores stay attributable to one exact package.

## Commands

```bash
# Derive: changed files come from --set REPATH=FILE (repeatable) and --delete
evallab tasks derive \
  --parent /private/tmp/mimo-data/terminal/tasks/<task_id> \
  --set instruction.md=/tmp/new-instruction.md \
  --transform instruction-candidate@1 \
  --rationale "clarify the output schema" \
  --created-by gepa-proposer \
  --parent-source '{"kind":"hf","repo":"FineEnvs/MiMo-V2.6-RL-harbor-terminal","revision":"<40-hex>","path":"tasks/<task_id>"}'

# Lineage: chain back to the original, with per-file digest changes
evallab tasks lineage library/task-variants/<slug>/<digest12>.json

# Validation verdict: append evidence, never rewrite
evallab tasks variant-status <record-or-digest> validated --evidence runs/<job>
```

Python API: `derive_task`, `materialize`, `verify`, `load_records`,
`load_variant_records` (plain dicts for catalog builders), `lineage_chain`,
`append_status_evidence` in `evallab.task_variants`.

## Relationship to task candidates (HAR-67)

`evallab.task_candidate.build_instruction_candidate` is now a policy on top
of variants: it computes the candidate instruction (original verbatim +
bounded preamble) and derives it with transform `instruction-candidate@1`,
so `components_changed` is always exactly `["instruction"]`. Its stricter
mutation-boundary validation (frozen subtrees, no solution leak, no
forbidden tokens) remains as `validate_task_candidate`, run against the
materialized package. There is exactly one way to create a derived task.

## Zone

Variants are zone `03-synthetic` material (see
[`docs/data-architecture.md`](data-architecture.md)): machine- or
agent-produced tasks derived from declared inputs, candidate material until
independently validated. The validation evidence appended by
`variant-status` is the admission trail.
