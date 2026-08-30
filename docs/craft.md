---
status: living
audience:
  - builder
  - analyst
---

# CRAFT: the task-corpus scanner, and where determinism stops

Status: living. Owner: Platform lane. Date: 2026-08-16. Implements the
deterministic half of `docs/build-plan.md` WS-A.

`src/evallab/craft.py` reads task directories and writes one `CraftRecord` per
task to `derived/parquet/craft/craft.parquet`; `sql/craft_views.sql` builds the
DuckDB views over it. No model is called, no network is touched, and nothing is
written inside a scanned corpus.

The half that is **not** built is `craft classify` (the LLM facet pass) and
`craft patterns` (which depends on classify for several facets). The most useful
output of this workstream's first pass is therefore the boundary below: which
facets are reachable from the bytes on disk, and which genuinely require the
model. That table is the specification for the deferred half.

## Running it

```bash
python -m evallab.craft scan --tb3                    # TB3 corpus only
python -m evallab.craft scan --tb4                    # pinned TB4 corpus only
python -m evallab.craft scan --all-local              # TB3 + in-repo library/
python -m evallab.craft scan path/to/corpus --json    # any corpus root
python -m evallab.craft plan --tb4 --tb4-root <v4>    # read-only 74→66 migration plan
python -m evallab.craft compile --tb4-root <v4>       # executable pinned TB4 Harbor job plan
```

Corpus roots and the output root are both injectable:

| Knob | Default | Why |
|---|---|---|
| `--tb3-root`, `$EVALLAB_TB3_ROOT` | `~/Developer/agent-evals/terminal-bench/tasks` | The TB3 corpus is outside this repository; tests must not depend on a developer's host layout (`agents/CHECKS.md`). |
| `--tb4-root`, `$EVALLAB_TB4_ROOT` | `~/Developer/agent-evals/terminal-bench-4/tasks` | The pinned TB4 lane (`v4.0.0`, commit `452bf30`). A TB4 `source_repo` is forced to `terminal-bench/terminal-bench@4.0.0` so its craft rows never share a `(source_repo, task_ref)` key with the TB3 lane. |
| `--out`, `$EVALLAB_DERIVED_ROOT` | `<primary checkout>/derived/parquet/craft` | `paths.derived_root_from_environment`: one derived store per machine, announced when a linked worktree inherits another checkout's. |

The two Terminal-Bench lanes are intentional historical coexistence, not a
compatibility shim. TB3 keeps its existing 74-task `terminal-bench/terminal-bench`
identity untouched; TB4 is pinned to the immutable `v4.0.0` release and reports
the distinct `terminal-bench/terminal-bench@4.0.0` identity. A `craft plan
--tb4` validates the pin (refusing wrong dataset, floating refs, and wrong
versions), scans the 66-task v4 inventory read-only (no Parquet write), and
reports the exact 74→66 delta against the migration record at
`src/evallab/data/terminal-bench-4-migration.json`. A `craft compile --tb4-root
<v4>` turns the pinned adoption lane into an executable Harbor job plan with a
flat 8-hour timeout, resumable per-task job identity, permitted Z.ai provider
selection, and fail-closed upstream drift guards. TB3 and TB4 scores are
**not** comparable.

`scan` refuses to write into any root it is scanning (`ValueError`), so a
mistyped `--out` cannot mutate a read-only corpus.

### CLI wiring left undone

The build plan specifies `evallab craft scan|classify|patterns`. This module is
reachable only as `python -m evallab.craft scan` because `src/evallab/cli.py`
was leased to another mission when it was written. Wiring it in is mechanical:

1. Add a `craft` subparser in `cli.py` next to the existing command groups, with
   a `scan` sub-subcommand taking `directories`, `--tb3`, `--library`,
   `--all-local`, `--tb3-root`, `--out`, `--json` — the same flags
   `craft.build_parser()` already defines.
2. Dispatch to `craft.main(argv)`; it returns a process exit code and prints its
   own summary, so no output plumbing is needed.
3. `craft.build_parser()` is exported for exactly this reason: the flags should
   be declared once, not transcribed.

When `craft classify` is built, it must submit its specs with `purpose="craft"`.
`ExperimentSpec.purpose` becomes required in WS-E item 1, with dispatch-time
rejection of purposeless specs, so a classify pass that omits it fails at
dispatch rather than at review.

## The facet boundary

Legend: **observed** = read from the bytes; **entailed** = derived from another
observation, not independently measured; **LLM** = out of deterministic reach.

| Facet | Status | How, or what the model would have to read |
|---|---|---|
| `task_ref` | observed | `[task].name`, falling back to the path relative to the corpus root. |
| `source_repo` | observed | TB3 names itself in its own `dataset.toml` (`[dataset].name`); the in-repo corpus is `eval-lab/library`. |
| `version` | observed / null | `[task].version` only. **Not** `schema_version`: that describes Harbor's manifest format, not the task, and reporting it as `version` would fill the column with a fact about the file format and call it task provenance. All 74 TB3 tasks carry `schema_version`; none carries `[task].version`, so the column is null across TB3 and populated on 43 of 477 library tasks. |
| `task_digest` | observed | sha256 over the sorted inventory of relative paths, sizes, per-file content digests, directory entries, and symlink targets. Craft's own digest, not the upstream `dataset.toml` pin — the library corpora have no upstream manifest, and idempotence is defined against this value. |
| `instruction_chars` | observed | Characters (not bytes) of `instruction.md`. |
| `instruction_style` | **LLM** | Rhetorical register is a judgement about prose, and `imperative` / `narrative` / `spec` are not mutually exclusive in this corpus: most TB3 instructions open with narrative scene-setting and close with a numbered requirements list, so any verb-initial-sentence ratio mislabels the majority in whichever direction the threshold is set. A model would have to read `instruction.md` whole and pick the dominant register, ideally reporting a mix rather than one label. |
| `env_n_files` | observed | Recursive file count under `environment/`. |
| `env_languages` | observed | Extension table. Excludes `Dockerfile`, YAML, JSON, Markdown: every environment has a Dockerfile, so recording it makes the column constant. `.v` is Coq only with a `_CoqProject` sibling and Verilog only with a SystemVerilog sibling; otherwise it contributes nothing, because the extension alone does not distinguish them. |
| `env_services_n` | observed / null | `services` count in `environment/docker-compose.yaml`, else 1 for a lone Dockerfile, else null. |
| `env_multi_container` | observed / null | `env_services_n > 1`. |
| `verifier_type` | observed / null | Structural; see below. Null on 200 of 551 records, every one of them with a recorded mechanism in `verifier_signals`. |
| `anti_cheat.hidden_tests` | observed | `[verifier].environment_mode == "separate"` — the declaration that the verifier is built from `tests/` into its own image. Absence is recorded as absent, not unknown, because `task_workbench.py:1503` treats a missing mode as `verifier_not_isolated`. |
| `anti_cheat.answer_outside_image` | **entailed** | From the same evidence as `hidden_tests`, not independently observed. In separate mode the agent image's build context is `environment/`, so nothing under `tests/` or `solution/` can enter it. A content-equality test between the trees was implemented and then **rejected as unsound**: fixture applications and input data are legitimately byte-identical across `environment/` and `tests/` in 164 file pairs of this corpus (and `solution/` duplicates `environment/` inputs in 3 TB3 tasks), so equality proves duplicated inputs, not a leaked answer. Distinguishing a genuine leak needs the semantics of the files, i.e. the model. |
| `anti_cheat.digest_check` | observed | `hashlib`/`hmac` imported by a verifier module (AST), or `sha256sum`-class commands in the shell verifier (lexed). |
| `anti_cheat.process_check` | observed | `psutil` import, a `/proc/...` string constant, or `pgrep`/`pidof`/`lsof`/`pmap`/`pstree` in the shell verifier. Deliberately narrow: `subprocess` appears in 41 TB3 verifiers and is not anti-cheat. |
| `answer_hiding` | observed (mechanism) / **LLM** (description) | A `+`-joined subset of `separate_verifier_image`, `reference_artifact_in_tests`, `signed_expectations` — each a structure a reviewer can open. What is *specifically* withheld ("the expected mutation report for the ATRX transcript set") is a reading task and stays with the model. |
| `difficulty_mechanism` | **LLM** | Why a task is hard requires reading the instruction against the solution. `env_n_files` and `instruction_chars` correlate with size, not with mechanism: `takens-embedding-lean` states a 60-hour expert estimate with 1 environment file, and `cumulative-layout-shift` has 190 environment files of largely mechanical UI work. A model would have to read `instruction.md` plus `solution/` and say which of conceptual / clerical / volume dominates. |
| `human_minutes` | observed / null | `[metadata].expert_time_estimate_hours × 60`, rounded. Present on all 74 TB3 tasks and 1 of 477 library tasks. **Deterministic — this facet does not need the model.** The 476 library tasks that state `[metadata].difficulty` (`easy`/`medium`/`hard`) instead are not a time anchor and are not converted into one. |
| `pinned_deps` | observed / null | See "Dependency pinning". Three-valued: the spec types it `bool`, but a task whose environment declares no dependencies has no fact to report, and null there beats a `False` that reads as "declared and unpinned". |
| `facets_schema_version` | constant | `craft/1`. Bump when a facet's meaning or extraction rule changes. |

### Additions to the WS-A field list

Three columns are not in the build plan's list. Each is forced by the rules the
mission was given, and each is marked in the model's docstring:

- `verifier_signals` — the mechanism families actually observed. Without it,
  `verifier_type` is an unsourced label and the mechanisms the enum cannot name
  are invisible. Rule: "a field you cannot determine is null, **and the record
  says so**".
- `unresolved_facets` — which columns are null *because undeterminable*, so a
  `GROUP BY` separates that from "absent from this task". This is the view
  `craft classify` should consume (`v_craft_unresolved`).
- `base_image_pin` — `digest` / `tag` / `bare` for the first `FROM`.
  `pinned_deps` is one bit about package versions and cannot also carry image
  reproducibility, and a tag is not a pin.

There is deliberately **no timestamp column**. A scan-time field would change
every row on every run and destroy idempotence; `test_the_schema_carries_no_timestamp`
asserts the absence rather than trusting it.

## Verifier detection is structural

Text matching loses. The concrete failure it produces here: 59 of the 74 TB3
`test.sh` files name their runner in prose as well as in code — `atrx-vep-crispr`
explains that "the pytest call lives in an `if` condition so `set -e` does not
abort" — so a substring scan cannot distinguish a documented runner from an
invoked one. What craft does instead:

- **Python verifier modules → `ast`.** Imports, string constants, and whether any
  `test*` function or `Test*` class exists. A module named `helpers.py` with a
  `test_reward` function is a pytest verifier; a module named `test_score.py`
  with no test callable is not.
- **Shell verifiers → `shlex`** with `commenters = "#"`, so comments are not
  evidence. A `NAME=value` assignment also yields its right-hand side, which is
  how the three FreeCAD tasks' held-back references are found — they are bound to
  `REFERENCE_BASE_FCSTD` and passed to the scorer as `"$REFERENCE_BASE_FCSTD"`.
  The token itself is always kept: an earlier revision split every `=` and
  thereby shredded `httpx==0.27.2` into two words, reporting 20 pinned
  environments as unpinned.
- **JS/TS runners → `package.json` parsed as JSON.** A runner in the dependency
  table, never a `*.test.ts` file name.
- **Golden references → the actual `tests/` inventory.** A `golden_file` signal
  fires only when a name appearing in the verifier's own strings or shell words
  resolves to a non-code file that is really there. The evidence is always a file
  a reviewer can open, which is what keeps this from degenerating into a keyword
  list.

### Mechanism families and the enum

`verifier_type` is the spec's `pytest | diff | golden_file | judge | hybrid`.
`hybrid` means "more than one mechanism", which stays true when one of them is a
mechanism the enum cannot name. Three families exist in the corpus that the enum
cannot name, and a task whose *only* mechanism is one of them gets
`verifier_type = NULL` rather than a wrong label:

| Family | What it is | Tasks (of 551) |
|---|---|---|
| `unit_js` | A JS/TS test runner (vitest, playwright) decides the reward. | 3 |
| `shell_only` | A shell verifier with none of the four mechanisms in it. In practice it compares an answer file against an expected value written into the script — all 198 `gpqa-diamond` shards do exactly that, as does `memcached-backdoor` (expected function address `0x41a630` inlined in `test.sh`). | 199 |
| `scorer_script` | A Python module that computes the reward itself: no framework, no committed reference, no model call. `kv-live-surgery` scores a relative throughput speedup off a runtime-collected sidecar. | 1 |

**Recommended enum change for WS-A v2:** rename `pytest` to `unit_tests` (or add
`unit_js`) and add `inline_expected` and `scorer_script`. That would move 200
records out of NULL without inventing a single facet. The gap is a finding about
the enum, not about the tasks.

## Dependency pinning

`pinned_deps` is `True` when every dependency declaration site in
`environment/` pins exact versions, `False` when at least one does not, and
`NULL` when no site exists at all. Sites:

1. **Lockfiles** — `uv.lock`, `package-lock.json`, `Cargo.lock`, `go.sum`, and
   the rest of `_LOCKFILES`. Pinned by definition.
2. **`requirements*.txt`** — pinned when every requirement carries `==`.
3. **Installer calls inside `RUN`** — the Dockerfile is parsed into
   instructions (comments dropped, backslash continuations folded), each `RUN`
   is lexed and split at `&&` / `;` / `|`, and each installer call is checked
   against the longest matching prefix in `_INSTALLERS`. `python3 -m pip install`
   is listed explicitly because 4 TB3 environments install that way and a
   `("pip", "install")`-only table misses them. `npm ci`, `pnpm install`,
   `yarn install`, `uv sync`, and `poetry install` are pinned by construction.
   An argument naming a manifest (`-r requirements.txt`) defers to that file,
   which is scored as its own site — otherwise `pip install -r requirements.txt`
   reads as an unpinned package called `requirements.txt`.

The base image is deliberately not a site: it is `base_image_pin`.

## Idempotence

The acceptance criterion is "same digests ⇒ no row churn", and it is enforced at
three levels:

1. `records_digest` — sha256 over the canonical JSON of the sorted record list.
   Two scans of an unchanged corpus print the same value. Independent of the
   Parquet encoding, so the guarantee is not coupled to a pyarrow version.
2. `compute_churn` — a row-level diff against the existing Parquet, keyed on
   `(source_repo, task_ref)`, reporting `added`, `removed`, `digest_changed`,
   and `facets_changed`. The last is the interesting one: a row whose digest
   held but whose facets moved means the *scanner* changed its mind, which is a
   scanner bug or an intended `facets_schema_version` bump, never noise.
3. The writer compares the new Parquet bytes against the existing file and
   **skips the replace** when they match, so an unchanged re-scan leaves the
   bytes *and* the mtime alone. Rewriting identical bytes would still publish a
   new artifact to anything downstream watching for one.

## What this scanner does not claim

- It does not run any task, build any image, or execute any verifier. Every
  facet is a statement about files on disk.
- `answer_outside_image` is entailed, not measured (see the table).
- A `NULL` facet means undeterminable-from-bytes, and `unresolved_facets` says
  which. It never means "false" or "absent".
- The counts are a snapshot of the corpus digests recorded in the same Parquet
  row. Re-run the scan before quoting them.
