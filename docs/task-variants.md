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
- **Read-only parents.** Pinned snapshots under `derived/task-store/hf` are
  read-only; the copy a variant starts from is made owner-writable, and the
  parent is never touched.

MiMo code tasks run `environment/setup/setup.sh` from a gzip+base64 tar
embedded in `task.toml`'s `[environment.healthcheck]` command, not from the
files on disk, so a setup change must re-embed that payload in the same
variant. `research/experiments/har105-exploration/setup_payload.py` encodes
it byte for byte as the adapter does (`gzip.GzipFile` with `mtime=0` and an
empty filename; `gzip.compress` stamps a different OS byte on macOS) and
checks that the on-disk setup reproduces the embedded one before a change.

## Separate-verifier variants (MiMo code tasks)

Two transforms move hidden tests into a Harbor 0.24 separate verifier
environment (`[verifier] environment_mode = "separate"` plus a
`tests/Dockerfile`, so the agent container never sees `/tests`). Both keep
the `@1` lineage records: `@2` supersedes the grading path but deletes
nothing.

* `separate-verifier@1` snapshots the agent workspace **plus** the agent's
  `.git` and base sha, restores all three in the verifier, and `exec`s the
  original grading script. Container-root tamper (tracked conftest edits,
  `sitecustomize`, `PATH` shadowing, background writers, in-source
  exit/`pytest` hooks) still grades 1 without fixing the bug.
* `separate-verifier@2` ("patch-only verifier") trusts nothing from the
  agent environment except repo file bytes. The verifier reruns the bundled
  clean setup itself (`tests/_verifier-setup/`, the parent's
  `environment/setup/*` at derive time), computes `BASE` there, diffs the
  snapshot files with its own git under the BASE tree's ignore rules, drops
  test-infra paths (`conftest.py`, `pytest.ini`, `tox.ini`,
  `sitecustomize.py`, `usercustomize.py`, `*.pth`, `*_test.go`, Go files
  declaring `TestMain`, every hidden-test-patch path, pytest-related
  `setup.cfg`/`pyproject.toml` hunks), gates added lines against the single
  `TAMPER_SIGNATURES` constant in `evallab.separate_verifier`, then applies
  the hidden test patch and runs the original test command with
  `PYTEST_ADDOPTS=--junitxml=<path outside repo>` (plus `PYTHONUNBUFFERED=1`
  so the pytest session header survives an import-time kill). Reward 1 iff
  the command exits 0 and (the report has no failure/error/skip and contains
  every pytest node id named in the command, or no report exists while pytest
  demonstrably did *not* run with our addopts). When the output shows a
  pytest session started for a pytest command that keeps `PYTEST_ADDOPTS`
  (base64-encoded command forms decoded), a missing or unparsable report
  grades 0 (`junit missing after pytest start`): the report was suppressed,
  not unconfigured. Commands that unset `PYTEST_ADDOPTS` or never start
  pytest (unittest, custom) keep the exit-code fallback.

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

## Task-health metadata

`evallab tasks health-tags` derives a metadata-only variant for **every Python
ledger task**, including review/discarded tasks. It does not admit, run, repair,
or relabel a trial. The parent package must match the ledger's full
`run_digest`; an unavailable or changed parent fails rather than disappearing
from the output. Only `[metadata].tags` changes: other tags, instructions,
environment, verifier and solution bytes are preserved.

```bash
evallab tasks health-tags \
  --variants-root derived/task-health/variants \
  --view-root derived/task-health/view \
  --output derived/task-health/manifest.json
harbor view derived/task-health/view --tasks
```

Lineage records use the existing `library/task-variants/` schema and the
`task-health-tags@1` transform. They bind the source-file hashes and assessment;
repeating the command reuses matching records and rebuilds missing packages.
The flat viewer collection links to those packages, not modified originals.
Use `--records-dir` as well as `--variants-root` to isolate a smoke run.
`--task format-code-task-001269` selects an explicit subset; without it all
ledger rows are processed.

Sources are `python-task-ledger/ledger.csv`, HAR-146's locked-nop CSV and
`python-task-ledger/task_history.csv`, with optional `--exploit-verdicts` JSON
from `probe-exploit verdict`. Probe outcomes must bind to the parent through
the retained Lab provenance or native task lock; a different or unknown
package cannot decide its health. No supplied probe means **not probed**, not
clean. The manifest exposes the binding and the original evidence rows.

- `health:cracked` / `health:leak-found` take precedence over favorable evidence.
  Ledger image-leak diagnoses therefore retain `health:leak-found` even when
  an old nop was sound (including 001269's `build/lib` leak).
- `health:repaired` denotes a validated ledger repair. A locked-nop failure
  explicitly bound to that same repair still wins. `health:sound` denotes
  a usable ledger task with a sound locked nop, **not semantic certification
  or proof against every exploit**. Other states remain explicit:
  `health:review`, `health:discarded`, `health:unchecked`,
  `health:broken-environment`, and `health:grader-suspect`.
- `solve:never-run`, `solve:0-of-n`, `solve:mixed`, and `solve:always` summarize
  the recorded history's **clean-pass + fail** denominator. `n` is reported as
  `known_attempts` in the manifest, not embedded in the tag. Copies and infra
  are excluded, not failures; an all-excluded history is `solve:unscored`, and
  missing history is `solve:unknown`. These are task-level historical labels
  across the recorded packages/models/harnesses, not a current-package
  capability estimate or a replacement for canonical counted verdicts.

Harbor 0.24 natively filters these tags in **task-definition mode** (`--tasks`).
Its jobs-mode `TrialSummary` has no metadata-tag filter. For historical jobs,
use the existing read-only [`evallab view` adapter](harbor-view.md) with
`--task-health derived/task-health/manifest.json --tag health:sound`.
It joins retained task-package identities, selects trials before building the
viewer projection, then launches the unchanged Harbor jobs UI. Repeated tags
are ANDed; unbound or unresolvable-package trials are explicitly excluded.
The manifest carries both Lab and Harbor parent/variant digests for this join.
For derived verifier packages, the filter walks retained `VariantRecord`
parent digests transitively to the nearest manifest identity, retaining each
hop's record hash. The launcher reads `library/task-variants`; use repeatable
`--task-variants DIR` for additional canonical record trees. Inherited tags
describe the ancestor's evidence, not fresh validation of the descendant.
Neither command changes original task packages, trials, locks, or frozen specs.


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
