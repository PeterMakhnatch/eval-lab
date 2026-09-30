---
status: living
audience:
  - builder
  - analyst
  - runner
---

# MiMo task catalog

Pinned intake, static findings, versioning, and outcome joins for the
FineEnvs `MiMo-V2.6-RL-harbor-*` collection (adapter `mimo_harbor 1.1.0`,
source `XiaomiMiMo/MiMo-V2.6-RL-oss @ 639865f...`). This is the TaskCatalog
slice; lineage records belong to TaskVariants (`evallab.task_variants`),
model-traffic capture to ModelCapture.

## Storage

All runtime data lives under the shared checkout root resolved by
`evallab.storage.paths.shared_checkout_root` (never inside a worktree).
Two different `derived/` trees hang off it — snapshots are NOT under the
Parquet derived root:

- `<shared>/derived/task-store/hf/<org>__<repo>@<rev12>/` — pinned
  snapshots, read-only (`555`/`444`), one `provenance.json` per snapshot
  (`ProvenanceMetadata`, zone `01-external`). Resolved by
  `default_task_store_root`, mirroring
  `task_variants.default_variants_root` (`derived/task-store/variants`).
- `<parquet-derived>/external/task_catalog/` — `task_sources`,
  `task_versions`, `task_findings`, `task_lineage` Parquet tables. (The
  resolved derived root already ends in `/parquet`; the catalog path is
  `external/...` relative to it.)
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

# After a hack-probe run: detector over every probe job -> task_exploits.parquet
uv run evallab tasks exploit-collect --cohort research/experiments/mimo-hack-probe/cohort.json runs/mimo-hack-*

# After any nop/control run: per-trial backend health -> task_qualification.parquet
uv run evallab tasks qualify-collect runs/<job>... [--backend-rate-card daytona]
uv run evallab tasks catalog export-broken --backend daytona --out broken-daytona.json
# Census a pool: one health label per task -> task_health.parquet
uv run evallab tasks health-collect --pool research/experiments/har108-python-census/pool.json --jobs-root runs/
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
  (`src/evallab/task_lint.py`), each mapped to the MimoFaultAudit
  finding it carries where statically detectable:
  `mimo-no-oracle`, `mimo-verifier-not-isolated`, `mimo-network-public`
  (H5: root + public = no network guarantee, hosts bypass), `mimo-paid-judge`
  (N1: judge model, hardcoded temperature 1.0, webdev 1500-char QUERY_CAP),
  `mimo-answer-leak` (incl. cyber expected-crash disclosure),
  `mimo-setup-healthcheck-only`, `mimo-manifest-digest-mismatch` (M5 class),
  `mimo-id-case-collision` (M1), `mimo-terminal-hook-planting` (H1),
  `mimo-testmain-plantable` (H2, package-wide noted), `mimo-conftest-plantable`
  (H3), `mimo-git-history-readable` (H4, image-dependent exposure),
  `mimo-cyber-binary-unchecksummed` (H6), `mimo-verify-network-dep`
  (N2 music abcmidi, N3 webdev CDN, N4 code test-time installs),
  plus `split-group-unresolved` when no family key is derivable and the
  builder-level cross-task slug-collision rows (same rule id).
  On top of the lint set, `catalog build` appends curated, evidence-backed
  defect records from `library/task-findings/**/*.json` (schema
  `evallab.task_finding/v1`: digest, task, domain, rule, `error`|`warning`
  severity, message, `{path, sha256}` evidence, recorder and time).
  Invalid records are skipped with a count, never a crash. An
  `error`-severity curated finding is a hard defect: it forces
  `train_eligible=false` with reason `finding: <rule>` in `v_task_audit`
  and lists the version in `export-broken` on every backend (see below).
- `task_lineage`: parent/child digest links from lineage records
  (`variant` origin rows also appear in `task_versions`).
- `task_qualification`: one row per Harbor trial with backend health
  (see "Backend qualification" below). Written by `qualify-collect`,
  never by `catalog build`.
- `task_health`: one row per pool task with a health label (see "Task health
  census" below). Written by `health-collect`, never by `catalog build`.

`grader_kind`/`grader_cost` are derived from task files, never the domain
name: a `*JUDGE*` verifier env or judge-graded `tests/grade.py` means a
paid model judge (`vlm_judge` when vision markers are present, else
`llm_judge`); everything else is a `script`/`free` grader.

## split_group

Stable family key so sibling tasks never straddle train/held-out:

- `code`: repository identity from graded test targets — tier 1:
  module-style `go test` targets (visible command, patch text, and shell
  scripts embedded in the patch as `mimo_build_env.tar.gz.b64`); tier 2: Go
  import paths in the patch (test-helper modules excluded); tier 3: hosted
  code URLs in the instruction and task description. This is the audit's
  go-import / test-import / issue-link union as one rule: test-target
  signals outrank import signals outrank link signals; anything with no
  signal stays a singleton (`split-group-unresolved`). Format-style tasks
  carry no repo signal in any file, so most stay unresolved by design —
  never merged silently.
- `cyber`: ARVO project (first component of `expected_crash.file`). M0:
  the 194 exact-duplicate instruction groups (501 tasks) are all
  project-pure, so the project key already keeps every group together;
  the builder verifies this per build and merges any future
  cross-project dupe group under `cyber:dupe-<inst12>`.
- `terminal`/`webdev`/`music`: task id until near-duplicate analysis lands.

Docker image digests are per-task unique in this collection (64/64
terminal, 1000/1000 cyber, 2698/2698 code), so they carry no family
signal; general/webdev/music share one image per domain (correctly ignored).

## Outcome joins

`storage/attach.py` registers `v_task_outcomes` (per task_version ×
backend × agent/model: n_trials, n_scored, n_errors, mean reward, Wilson 95%
pass-rate interval, verdict) and `v_task_audit` (same grain plus
verifier-stability, exploit, and backend-qualification rollups with
train-eligibility reason codes). `backend` is
`COALESCE(trial_facts.environment_type, 'unknown')`, read from each trial's
Harbor config (`$.environment.type`, with Eval Lab Daytona wrapper import
paths mapped to `daytona`); old trial_facts partitions without the column
read as `'unknown'`, so local and cloud trials never pool into one row.
Verdicts: `learnable` (some pass, some fail), `always_pass`,
`always_fail`, `infra_only` (no scored trial), `untested`. Outcomes are
grained by (task_version × backend × agent/model), so nop/control trials join as
their own rows and never mix into a model's pass rate: they cannot make
a task look learnable, and a uniformly-failing nop row is simply not
learnable (control, not training signal).

`task_stability` comes from `evallab tasks stability-collect`
(docs/task-stability.md) and `task_exploits` from `evallab tasks
exploit-collect`. `exploit-collect` attributes each trial by the package
digest the queue staged, and keeps cohort tasks with no model trial as
`not_probed` (a nop control does not count as a probe). `train_eligible` =
`learnable` ∧ stability evidence that is `stable` (no evidence is
ineligible) ∧ no `confirmed` exploit and no `suspected` one still awaiting
review ∧ not broken on that backend (see below) ∧ no `error`-severity
curated finding (see above). The finding check comes first: a version with
an error finding is train-ineligible with reason `finding: <rule>` whatever
else holds. `export-eligible` also
drops tasks the split file marks `heldout`; its items carry the backend,
so the same task qualified on docker and daytona exports as two rows.

The export's `sha256` covers `schema` + `items` only. Items carry the
agent/model whose verdict made them eligible. So the digest names the
training set: re-exporting the same set gives the same digest. `meta`
records when, from which table digests, and against which split file it
was built. Without `--split` the export is `provisional` and every item's
split is `unassigned`.

## Backend qualification

`evallab tasks qualify-collect <job_dir>...` writes one
`task_qualification.parquet` row per trial: task digests (attributed by the
package digest the queue staged, as `exploit-collect` does), backend and
environment import path, job/trial/agent names, started/finished times,
setup/agent/verifier/trial seconds, `setup_ok`, `verifier_completed`,
reward, `repeat_rewards` (from `verifier/stability.json` when RepeatVerifier
ran, else null), `infra_error_class`/`infra_error_phase` (the existing
`facts.py` exception classification), `grader_error` (the root-cause line
when the grader's own pytest collection is broken, else null), resource
sizes (`cpus`, `memory_mb`, `storage_mb`) from the staged `task.toml` Harbor
ran — overridden by the trial config's `environment.override_cpus` /
`override_memory_mb` / `override_storage_mb` when Daytona staging resized
the sandbox (the overrides also feed `est_cost_usd`) — `sandbox_seconds`
(trial wall time), `est_cost_usd`, `status`, and `reasons`.
`catalog show` lists a task's qualification rows alongside its outcomes.

Reasons (a nop trial with reward exactly 0 and a completed verifier is `ok`):

| Reason | Meaning |
|---|---|
| `setup_failed` | exception during environment setup (incl. healthcheck) or agent setup — the agent never started |
| `backend_quota` | setup-phase exception whose type/message shows a provider quota/limit/rate-limit problem (matched case-insensitively: `quota`, `limit exceeded`, `total cpu`, `memory limit`, rate-limit wording incl. `DaytonaRateLimitError`, HTTP 429/403 with quota wording, or disk-capacity markers: `no space left on device`, `disk quota`, `insufficient disk`/`storage`, `storage limit`/`disk limit`, `exceeds` near `disk`/`storage` — bare `disk`/`storage` never match). Daytona caps every sandbox at 4 vCPU / 8 GiB RAM / 10 GiB disk whatever the org tier, so a setup-phase capacity failure is provider quota, never a task defect |
| `verifier_error` | verifier-phase exception |
| `verifier_timeout` | verifier-phase exception naming a timeout |
| `reward_missing` | no exception explains the trial, yet no usable reward exists |
| `nop_passes` | a nop/oracle control scored above 0 — the grader accepts no work |
| `unstable_verifier` | `repeat_rewards` disagree |
| `grader_broken` | a nop/oracle control's verifier stdout (`<trial>/verifier/test-stdout.txt`, plus any `RepeatVerifier` per-repeat stdout under `<trial>/verifier/repeat/*/`) shows pytest collection failing on a broken test module (`ImportError while importing test module` / `ModuleNotFoundError` / `SyntaxError`), so every run scores 0 whatever the agent does. The row's `grader_error` carries the root-cause line (e.g. `ModuleNotFoundError: No module named 'stevedore'`). Nop guard: when the missing module's top-level name appears as a word in the task's `instruction.md`, the import is expected under nop (the agent is supposed to create it) and does not flag. Workspace guard: an `ImportError` with no missing-module or syntax root cause whose innermost traceback frame is in the task workspace (a rootdir-relative path or `/app/...`, e.g. vendored sources the task asks the agent to repair) does not flag either. For queue runs, whose staged task copy is deleted after the run, `task.toml` and `instruction.md` are read from the job's `experiment-spec.json` source task path |

Status is `ok` (no reasons), `broken` (any task-blaming reason), or
`inconclusive`: a trial whose *only* reason is `backend_quota` failed on
our provider tier (Daytona Tier 1: 10 vCPU / 10 GiB memory / 30 GiB disk
in total — MiMo code/cyber sandboxes need 8 GiB each, terminal sandboxes
need 10 GiB disk), not on the task, so the task is unrun at this tier,
never guilty. `qualify-collect` reports an `inconclusive (backend_quota)`
count per domain.
Broken rule: a task version is broken on a backend when its *latest*
qualification trial on that backend is `broken` (ordered by `finished_at`,
nulls oldest). `evallab tasks catalog export-broken --backend <b> --out
PATH` writes `{schema, backend, items, meta}` where each item names the
task, the latest trial's reasons, and every job/trial on that backend; each
item carries a `source`: `qualification` for trial-driven entries,
`finding` for curated-defect entries. Versions with an `error`-severity
curated finding are listed on every backend (whatever the trials say) with
a `finding:<rule>` reason — merged into the qualification item when the
version is broken both ways. Curated findings export even before any
qualification run on that backend (`meta.table_digest` is then null); with
neither a qualification table nor an error finding the export refuses. The
`sha256` covers `schema` + `backend` + `items` only (same content-addressed
convention as `export-eligible`). In
`v_task_audit` the same latest-broken rule joins per (task version,
backend) and yields train-ineligible reason `broken on <backend>`, unless
an error-severity finding takes precedence (`finding: <rule>`).

Cost model: `est_cost_usd = sandbox_seconds / 3600 × (cpus × 0.0504 +
memory_GiB × 0.0162 + max(0, storage_GiB − 5) × 0.000108)`, from the Daytona
list prices at https://www.daytona.io/pricing (retrieved 2026-09-28; rates
live in `task_qualification.DAYTONA_RATE_CARD`). Cost is computed only for
the rate-card backend (`--backend-rate-card`, default `daytona`); other
backends get null. When `storage_mb` is unset no disk size is invented: it
counts as 0 billable GiB (likewise missing cpus/memory count as 0).


## Task health census

`evallab tasks health-collect --pool <pool.json> --jobs-root <dir>` (repeatable)
writes one `task_health.parquet` row per pool task, next to
`task_qualification.parquet`. It reads the catalog's qualification rows for the
latest nop per `task_version_digest` and that trial's verifier log under the
given job roots; it runs nothing. `--output` and `--summary` override the
default catalog path and print a markdown summary.

Columns: `task_id`, `task_version_digest`, `split`, `split_group`, `category`,
`image_mib` (from the pool entry), `project_key`, `project_key_source`
(`split_group` when `split_group` minus `code:` names a repo rather than a
`format-code-task-` singleton; else `test_import`, the most common top-level
module the hidden tests import, excluding the stdlib and pytest/mock/hypothesis/
numpy/pandas/requests/yaml/six/attr/attrs/pydantic/sqlalchemy/django/flask/
typing_extensions/tests/test/conftest; else `test_path`, the first directory of
the first patched `.py` file; else `task_id`), `label`, `reasons`, `evidence`
(the log line or `file:line` justifying the label), `patch_files`,
`patch_well_formed` (false only when a diff section fails to parse; an empty
new file, a binary file, a mode-only change and a content-free rename parse),
`test_runner` (`pytest`/`unittest`/`bundled`/`django`/`other`/`none`;
`bundled` unpacks `mimo_build_env.tar.gz.b64` and runs
`.build_env/test_command.sh`, `django` is `manage.py test`),
`test_targets_in_patch`, `literal_source_asserts` (a hidden test obtains
project source with `inspect.getsource` or `open`/`Path.read_text` of a literal
`.py` path it does not itself write (a path join such as `Path(tmp) / 'out.py'`
is generated output, not project source), then asserts a substring of that text — not a
`def`/`class` literal in generated output), `undisclosed_names`,
`instruction_chars`, `nop_job_name`, `nop_trial_name`, `nop_reward`,
`nop_finished_at`, `nop_cost_usd`, `nop_tests_applied`, `nop_tests_ran`,
`nop_setup_error`, `nop_setup_error_excused`, `nop_exception_type` (null when
there is no nop trial), `produced_at`.

The markdown summary names the table path and row count. Every count in it is
a group-by of the table: labels, split × label, the top 25 `project_key`s by
task count plus the number of singleton projects, projects with 2 or more
`broken_environment` tasks, exploded reasons, and the sum of `nop_cost_usd`.

Labels, first match wins:

| Label | When |
|---|---|
| `unknown` | no nop trial for the task version, or its trial directory is absent from every job root |
| `broken_environment` | the trial exception is an environment-setup failure, the hidden tests were not applied, no reward exists, or the verifier log matches `SETUP_ERROR` (`ModuleNotFoundError`, `PackageNotFoundError`, `ERROR collecting`, `ERROR at setup`, `command not found`, `ImportError while loading conftest`, `is not correctly installed`, `build_ext`) and the match is not excused. Evidence is always a log excerpt |
| `grader_suspect` | the nop scores 1 (hidden tests pass with no change), the tests never ran and no setup error explains it, or `literal_source_asserts` is non-empty |
| `sound` | everything else: the nop graded and the environment held |

A `SETUP_ERROR` match is excused only when it names the agent's missing work:
`detect_grader_collection_failure` returns nothing, and every name the error
says is missing (a `cannot import name` symbol, or the leaf of a
`No module named` module) appears as a whole word in `instruction.md`. An
environment defect the instruction happens to mention (`CuPy is not correctly
installed`, pandas `build_ext`) names no missing symbol, so it is never excused.

## Current numbers

Full six-domain build (2026-09-28): `task_sources` 6 rows,
`task_versions` 7780 rows (code 2698 / cyber 1000 / general 925 /
terminal 64 / webdev 2093 / music 1000 — exactly the contract counts),
`task_findings` 47017 rows (all `warning`; zero `error` rows — the audit's
finding gate currently flips nothing), `task_lineage` 0 rows (no variant
records exist yet in `library/task-variants/`). Snapshots total 476M on disk
(code 106M, cyber 52M, general 116M, music 71M, terminal 4.8M, webdev
126M); catalog tables ~5M. Zero manifest digest mismatches: every task
directory verifies against the adapter `manifest.json`.
The first curated record — `library/task-findings/terminal/candidate-0260-security-appsec.json`
(`grader-broken`, `error`: the grader's pytest collection fails on the missing
`stevedore` dependency) — materializes into `task_findings` on the next
`catalog build`; the shared catalog above predates it.

Findings per rule per domain:

| rule | code | cyber | general | terminal | webdev | music |
|---|---|---|---|---|---|---|
| mimo-no-oracle | 2698 | 1000 | 925 | 64 | 2093 | 1000 |
| mimo-verifier-not-isolated | 2698 | 1000 | 925 | 64 | 2093 | 1000 |
| mimo-network-public | 2698 | 1000 | 925 | 64 | 2093 | 1000 |
| mimo-setup-healthcheck-only | 2698 | 1000 | 925 | 64 | 2093 | 1000 |
| mimo-paid-judge | 0 | 0 | 925 | 0 | 2093 | 0 |
| mimo-answer-leak | 7 | 1000 | 0 | 0 | 0 | 0 |
| mimo-id-case-collision | 0 | 0 | 0 | 0 | 0 | 1000 |
| mimo-terminal-hook-planting (H1) | 0 | 0 | 0 | 64 | 0 | 0 |
| mimo-testmain-plantable (H2) | 709 | 0 | 0 | 0 | 0 | 0 |
| mimo-conftest-plantable (H3) | 1014 | 0 | 0 | 0 | 0 | 0 |
| mimo-git-history-readable (H4) | 2698 | 0 | 0 | 0 | 0 | 0 |
| mimo-cyber-binary-unchecksummed (H6) | 0 | 1000 | 0 | 0 | 0 | 0 |
| mimo-verify-network-dep (N2/N3/N4) | 236 | 0 | 0 | 0 | 2093 | 1000 |
| split-group-unresolved | 2058 | 0 | 0 | 0 | 0 | 0 |

Cyber instruction-dupe groups (M0): 194 groups / 501 tasks, all sharing
`split_group` via the ARVO project key (builder-verified per build).

Notable: all 1000 cyber tasks disclose the expected crash
(function/file/sanitizer) in the agent-visible instruction and task
metadata; all 1000 music tasks carry a `source_id` differing from the
directory name only by case (`music-gK-…` vs `music-gk-…`); 2058/2698
code tasks (format-style, no repo signal in any file) fall back to the
task id for `split_group` rather than merging silently.
