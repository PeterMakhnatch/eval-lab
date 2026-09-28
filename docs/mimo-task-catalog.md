# MiMo task catalog

Pinned intake, static findings, versioning, and outcome joins for the
FineEnvs `MiMo-V2.6-RL-harbor-*` collection (adapter `mimo_harbor 1.1.0`,
source `XiaomiMiMo/MiMo-V2.6-RL-oss @ 639865f...`). This is the TaskCatalog
slice; lineage records belong to TaskVariants (`evallab.task_variants`),
model-traffic capture to ModelCapture.

## Storage

All runtime data lives under the shared checkout root resolved by
`evallab.storage.paths.shared_checkout_root` (never inside a worktree):

- `<derived>/task-store/hf/<org>__<repo>@<rev12>/` — pinned snapshots,
  read-only (`555`/`444`), one `provenance.json` per snapshot
  (`ProvenanceMetadata`, zone `01-external`).
- `<derived>/external/task_catalog/` — `task_sources`, `task_versions`,
  `task_findings`, `task_lineage` Parquet tables. (The resolved derived
  root already ends in `/parquet`; the catalog path is `external/...`
  relative to it.)
- `library/task-variants/**/*.json` — git-tracked lineage records
  (`evallab.task_variant/v1`, owned by TaskVariants).

## Commands

```bash
# Refuses anything but <org>/<repo>@<40-hex-sha>; idempotent reuse on
# matching provenance, refusal (never overwrite) on mismatch.
uv run evallab tasks pull-hf FineEnvs/MiMo-V2.6-RL-harbor-terminal@fe1c2b665aae1ba7a09a270d979724d32269ae6a

uv run evallab tasks catalog build [--derived-root PATH]
uv run evallab tasks catalog show <task_id|digest> [--derived-root PATH]
uv run evallab tasks catalog export-eligible --out train_eligible.json [--split split.json]
```

`pull-hf` verifies every task directory against the adapter's
`manifest.json` per-task sha256 (same `sha256(relpath + NUL + bytes)`
scheme as `registry.harbor_task_digest`) and reports mismatches as
findings, not crashes.

## Tables

- `task_sources`: one row per pulled snapshot (repo, revision, domain,
  manifest/disk/registry counts, license, material digest).
- `task_versions`: one row per task, keyed by `task_version_digest`
  (`registry.task_directory_digest`) with `harbor_digest`
  (`registry.harbor_task_digest`) — the join key to Harbor `lock.json`
  `trials[].task.digest` and to `trial_facts.task_digest`.
- `task_findings`: one row per `(task_version_digest, rule)` with
  severity and message. Rules are the `mimo-*` lint set
  (`src/evallab/task_lint.py`): no solution/oracle, verifier not
  isolated, network public, paid judge, answer leak, setup-only-in-
  healthcheck, manifest digest mismatch, id/source_id case collisions,
  plus `split-group-unresolved` when no family key is derivable.
- `task_lineage`: parent/child digest links from lineage records
  (`variant` origin rows also appear in `task_versions`).

`grader_kind`/`grader_cost` are derived from task files, never the domain
name: a `*JUDGE*` verifier env or judge-graded `tests/grade.py` means a
paid model judge (`vlm_judge` when vision markers are present, else
`llm_judge`); everything else is a `script`/`free` grader.

## split_group

Stable family key so sibling tasks never straddle train/held-out:

- `code`: repository identity from graded test targets — tier 1:
  module-style `go test` targets (visible command + hidden command in
  `tests/test.patch`); tier 2: Go import paths in the patch (test-helper
  modules excluded); tier 3: hosted code URLs in the instruction and task
  description. Falls back to the task id (`split-group-unresolved`).
  Format-style tasks carry no repo signal in any file, so most stay
  unresolved by design — never merged silently.
- `cyber`: ARVO project (first component of `expected_crash.file`).
- `general`: task id minus the trailing `_rl_NNN` sibling suffix.
- `terminal`/`webdev`/`music`: task id until near-duplicate analysis lands.

Docker image digests are per-task unique in this collection (64/64
terminal, 276/276 sampled code), so they carry no family signal.

## Outcome joins

`storage/attach.py` registers `v_task_outcomes` (per task_version ×
agent/model: n_trials, n_scored, n_errors, mean reward, Wilson 95%
pass-rate interval, verdict) and `v_task_audit` (same grain plus
verifier-stability and exploit rollups with train-eligibility reason codes).
Verdicts: `learnable` (some pass, some fail), `always_pass`,
`always_fail`, `infra_only` (no scored trial), `untested`. Outcomes are
grained by (task_version × agent/model), so nop/control trials join as
their own rows and never mix into a model's pass rate: they cannot make
a task look learnable, and a uniformly-failing nop row is simply not
learnable (control, not training signal).

## Current numbers

<!-- FILLED AFTER FULL BUILD -->
