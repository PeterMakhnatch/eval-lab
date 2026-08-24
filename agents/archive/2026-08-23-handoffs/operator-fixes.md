Status: done
Last: merged as PR #57 (`fdc22f8`)
Next: none
Blockers: none

# OPERATOR-FIXES handoff

Agent/model: Claude Opus 4.5 (Anthropic), Oh My Pi harness, subscription-only.
Worktree `.worktrees/operator-fixes` on `role/operator-fixes`, branched from
`origin/main` `ad67126`. No paid model call, cloud sandbox, deploy, or
publication. No API-key environment variable read or introduced. Nothing under
`policy/` touched or weakened. `oracle`/`nop` were not executed; no Harbor run,
no Docker build, no `docker compose up/down`, no `evallab tick`. M006's
calibration gate was not opened and no live analysis adapter was wired.

The primary checkout `~/Developer/eval-lab` was read only — `runs/` was read to
prove F-08 against real job directories, and nothing there was written, moved,
or deleted. No `git pull`, `reset`, `clean`, `checkout`, or `stash` was run in
it.

## Lease, and what was written

Leased: `src/evallab/` except `task_workbench.py` and `analysis_worker.py`, the
matching files under `tests/`, `agents/handoffs/operator-fixes.md`, and a short
note in `docs/operations.md`. `git diff --name-only origin/main...HEAD`:

```
agents/handoffs/operator-fixes.md
docs/operations.md
src/evallab/cli.py
src/evallab/database.py
src/evallab/explorer.py
src/evallab/facts.py
src/evallab/schemas.py
src/evallab/status.py
tests/fixtures/explorer/analyses/badstep/analysis.json
tests/fixtures/explorer/analyses/broken/analysis.json
tests/fixtures/explorer/analyses/valid/analysis.json
tests/test_explorer.py
tests/test_operator_surfaces.py
```

`task_workbench.py` and `analysis_worker.py` are absent, as required. The three
fixture paths are `git mv` renames of `analyses/<name>.json` into the real
on-disk layout `analyses/<name>/analysis.json`. `dashboard/` was not touched —
see "Deliberately not done" below.

## The six fixes

Commits, oldest first. Each names its F-number.

| commit | F | one line |
|---|---|---|
| `878953b` | F-03 | explorer discovers sidecars by filename, not `*.json` |
| `504e269` | F-02 | `analyze review --index` indexes its own review |
| `7de814e` | F-11 | doctor names the database it inspected |
| `b818caa` | F-09 | `submit` prints the bare `spec_id`, labelled |
| `eb55da8` | F-08 | `.executor` is no longer listed as a job |
| `a4c1900` | F-12 | `analyze stub --index` says what it indexed, and where |

### F-03 — `analyze review` output permanently corrupted the explorer

`_analysis_views` globbed `analyses_dir.rglob("*.json")` and parsed every hit as
a `TrialAnalysisSidecar`, so each `derived/analyses/<id>/reviews/<review_id>.json`
became a permanent `unreadable (ValidationError)` banner line on every tab.

Fixed with a positive rule rather than an exclusion: a sidecar **is** the file
named `analysis.json`. `ANALYSIS_SIDECAR_FILENAME` and `ANALYSIS_REVIEWS_DIRNAME`
now live beside the models in `schemas.py`, documented with the full on-disk
contract, and `explorer.py`, `facts.py`, and `status.py` all use them — the
latter two already hardcoded exactly this rule, so the explorer was the outlier.
A new artifact type added under an analysis directory cannot break discovery.
Unreadable-sidecar notes now name the analysis directory, because the filename
is `analysis.json` for every sidecar and would otherwise be ambiguous.

Test: `tests/test_explorer.py::test_review_beside_a_sidecar_is_not_parsed_as_one`
writes a real review with `facts.write_analysis_review` next to a real sidecar
and asserts the sidecar renders fully (id, trial key, validation, category,
resolved citation) with no `unreadable` note and no mention of the review id.

Negative control, discovery reverted to `rglob("*.json")`:
`AssertionError: assert not ['analysis 11111111-…/reviews/9966deba-….json: unreadable (ValidationError)']`.
Fixed: passes.

Proven live, not only by fixture. Against two sidecars each carrying a real
review under this worktree's `derived/analyses`:

```
notes: ()                                    # after the fix
pre-fix notes:                               # same data, discovery rule reverted
  analysis d34bfe9b-…/reviews/cca95a46-….json: unreadable (ValidationError)
  analysis e38ea97b-…/reviews/7f8dce08-….json: unreadable (ValidationError)
```

**Does the operator now know what to do next?** There is nothing to do — the
failure is gone rather than explained.

### F-02 — `analyze review` did not index the review

Indexing stays a separate concern, deliberately: the catalog is a derived,
rebuildable index (`AGENTS.md`), and `analyze stub` and `compare` already gate
catalog writes behind `--index`. Adding a mandatory database round-trip to a
filesystem-only command would have been the wrong cutover. So `analyze review`
gained the same `--index` / `--database-url` pair, and — the actual defect — the
command now always states which of the two states the operator is in:

```
--index    review: <path>
           disposition: accepted
           indexed review: <review_id> -> analysis_reviews
           catalog: localhost:54329/evallab_opfix

default    indexed: no (the catalog is a derived index, written on request)
           next: uv run evallab analyze ingest-sidecar <sidecar path>
```

A missing sidecar path now refuses with the path shape it expected instead of a
bare `FileNotFoundError`.

Tests: `tests/test_operator_surfaces.py::test_analyze_review_index_populates_analysis_reviews`
(asserts the `INSERT INTO analysis_reviews` row carries the review id read off
disk, the disposition, and the reviewer),
`…::test_analyze_review_without_index_names_the_command_that_indexes` (asserts
zero catalog statements and the exact next command), and
`…::test_analyze_review_on_a_missing_sidecar_says_what_to_pass`.

Negative control, `cli.py` reverted to the pre-fix branch: all three fail —
`SystemExit: 2` (argparse rejects `--index`), `assert 'indexed: no' in 'review: …\ndisposition: accepted\n'`,
and `assert 'no analysis sidecar at' in "error: [Errno 2] No such file or directory: …"`.
Fixed: all three pass.

**Proven live, end to end**, in a throwaway database (`evallab_opfix`, created
and dropped; the shared `evallab` catalog was never written):

```
== analysis_reviews BEFORE ==   0
$ uv run evallab analyze review <sidecar> --disposition accepted \
    --rationale "…" --reviewer operator-fixes --index --database-url <opfix>
indexed review: cca95a46-b951-4f26-b058-a32926a56467 -> analysis_reviews
== analysis_reviews AFTER ==
cca95a46-…|d34bfe9b-…|accepted|operator-fixes
```

Shared catalog verified unchanged afterwards: `trajectory_documents=23`,
`jobs=72`, `analysis_reviews=0`, and `evallab_opfix` gone from `pg_database`.

**Does the operator now know what to do next?** Yes, in both branches: either
the review is indexed and the command says so and where, or it says it is not
and prints the command that indexes it.

### F-11 — doctor never said which database it inspected

`catalog-parquet` now ends with `db=<host>:<port>/<dbname>` on **every** branch:
green, projection-check failure, and Postgres unreachable — the last of those
matters most, since "catalog unavailable" is exactly when an operator is trying
to work out which catalog. Counts stay first so existing readers still parse.

`database.identity()` parses the connection string with
`psycopg.conninfo.conninfo_to_dict` and reads only host, port, and dbname, so a
password in `DATABASE_URL` cannot reach the terminal or a log. An unparsable
string degrades to `unparsable connection string` rather than raising. (The
helper landed in the F-02 commit, which needed it first; F-11 is its use in
doctor.)

Tests: `tests/test_operator_surfaces.py::test_doctor_names_the_catalog_it_inspected_without_credentials`,
`…::test_doctor_names_the_catalog_even_when_postgres_is_unreachable` — both
assert `db=catalog.test:54329/evallab` appears on the `catalog-parquet` line and
that the password substring `local-development-only` appears on neither stdout
nor stderr — and `…::test_database_identity_never_returns_a_password` for URI
form, keyword form, and garbage.

Negative control, identity dropped from the detail strings: both fail —
`assert 'db=catalog.test:54329/evallab' in 'ok    catalog-parquet catalog=1 projected=1 exceptions=0 missing=0 extra=0'`
and `… in 'FAIL  catalog-parquet catalog unavailable'`. Fixed: pass.

Proven live against the real shared Postgres (`EVALLAB_DERIVED_ROOT` pointed at
an empty scratch dir inside this worktree, so nothing in the primary checkout
was read or written; the FAIL is that deliberate empty root, not a defect):

```
ok    postgres       PostgreSQL 18.4 on aarch64-unknown-linux-musl, …
FAIL  catalog-parquet catalog=72 projected=0 exceptions=0 missing=72 extra=0 db=localhost:54329/evallab
```

**Does the operator now know what to do next?** Yes — the line now answers the
question a disagreeing count raises.

### F-09 — `submit` printed something no command takes

It printed `f"{path.parent.name}: {path}"`: the queue *state* directory looking
like a label, then a filename. The ULID `approve`, `reject`, and the catalog's
`experiment_id` column all want appeared only as a suffix inside that filename.
Now:

```
spec_id: <ULID>
state: waiting
path: <queue/waiting/oracle-<ULID>.json>
<policy decision message>
next: uv run evallab approve <ULID> --actor <you>     # only when parked in waiting/
```

The id is read back off the spec that was actually written
(`executor.queue.load(path)`), not reconstructed by string surgery.

Test: `tests/test_operator_surfaces.py::test_submit_prints_the_bare_spec_id_approve_wants`
submits a real spec through the real policy gate, parses the `spec_id:` line,
and feeds it to `DirectoryQueue.locate()` — the same lookup `approve` uses — so
the assertion is that the printed value is *consumable*, not merely present.

Negative control, output reverted to `f"{path.parent.name}: {path}"`:
`StopIteration` — there is no `spec_id:` line to parse. Fixed: passes.

**This test was not hermetic on its first push and red CI on both Python
versions (`assert 2 == 0`); fixed in the last commit on this branch.** The
cause was *not* the missing task package, which was the first hypothesis.
Proven by isolating each variable:

| workspace holds | `DATABASE_URL` reachable | result |
|---|---|---|
| `policy/` only, no task package | yes | **exit 0** — the gate never touches the task path for a non-`registered/` task |
| `policy/` + the task package | no | **exit 2**, `error: cannot enforce cost policy because the catalog is unavailable` |

`Executor.submit` calls `_effective_spend_today()` and
`_consecutive_harness_failures()`, which reach `database.daily_cost_usd` and
`database.consecutive_harness_failures` (`queue.py:1128-1145`); both re-raise as
`RuntimeError` when the catalog is unreachable, and `run_cli` maps that to exit
2. It passed locally only because Postgres is running here. `.github/workflows`
provides a `postgres` service to `perf.yml` and to nothing else, so the quality
job has no database — correctly, since no other test needs one.

The fix stubs exactly those two probes to a clean, admitting state, and also
copies the task package into the workspace so the fixture is self-describing
even though it is not load-bearing today. Production code was not touched:
F-09's behaviour is unchanged and its negative control still holds. Verified by
running the file, and then the whole suite, with `DATABASE_URL` pointed at
`127.0.0.1:1`.

**Does the operator now know what to do next?** Yes, and for a parked spec the
next command is printed with the id already substituted.

### F-08 — the explorer listed `.executor` as a job

`build_index` iterated every directory under a jobs root. `_is_job_dir` now
requires a non-dot-prefixed directory, which also covers `.tombstones` and
anything similar added later. Trial discovery is unchanged; it already required
`result.json`.

Test: `tests/test_explorer.py::test_executor_bookkeeping_is_not_listed_as_a_job`
builds a jobs root holding one real job plus `.executor/` (with contents) and
`.tombstones/`.

Negative control, predicate reverted to `p.is_dir()`: fails with
`+ '.executor'`, `+ '.tombstones'` in the diff. Fixed: passes.

Proven live against the primary checkout's real `runs/`, which does contain
`.executor`: 42 trials indexed, `.executor listed: False`.

**Does the operator now know what to do next?** There is nothing to do — the
misleading row is gone.

### F-12 — `analyze stub --index` indexed silently

Output was byte-identical to the un-indexed form; the M009 flight had to query
`analysis_invocations` by hand to confirm the row. Now symmetric with F-02:
`indexed analysis: <id>` plus `catalog: <identity>`, or `indexed: no` plus the
exact `analyze ingest-sidecar` command.

`analyze ingest-sidecar` got the same treatment plus `indexed reviews: <n>` —
that command silently sweeps in `reviews/*.json` beside the sidecar, which is
the behaviour F-02 depended on and which nothing previously surfaced.

Tests: `tests/test_operator_surfaces.py::test_analyze_stub_index_reports_what_it_indexed`,
`…::test_analyze_stub_without_index_says_it_did_not_index`,
`…::test_ingest_sidecar_reports_the_reviews_it_swept_in`.

Negative control, the report block removed: both stub tests fail —
`assert 'indexed: no' in 'analysis: …\nvalidation: valid\n'` and
`assert 'indexed analysis: 05c1b87b-…' in 'analysis: …\nvalidation: valid\n'`.
Fixed: pass.

Proven live:

```
$ uv run evallab analyze stub <trial> --response … --index --database-url <opfix>
analysis: …/derived/analyses/d34bfe9b-…/analysis.json
validation: valid
indexed analysis: d34bfe9b-b8e9-4730-9dd6-901ed6e37174
catalog: localhost:54329/evallab_opfix
```

**Does the operator now know what to do next?** Yes — the indexed/not-indexed
distinction is stated, and the un-indexed branch prints the command.

## Capability labels

| F | label | basis |
|---|---|---|
| F-03 | `proven live` | real sidecars + real reviews on disk, `notes: ()`, reverted-rule control reproduces both banner lines |
| F-02 | `proven live` | `analysis_reviews` 0 -> 1 from the single documented command, throwaway DB, row read back |
| F-11 | `proven live` | real shared Postgres, `db=localhost:54329/evallab` on the real doctor line |
| F-09 | `fixture-proven only` | printed id round-tripped through `DirectoryQueue.locate()`; no live queue submission was made, because that would write to a queue |
| F-08 | `proven live` | primary checkout's real `runs/`, which contains `.executor`; 42 trials, no dot-prefixed job |
| F-12 | `proven live` | real CLI invocation, `indexed analysis:` + catalog identity, row present in the throwaway DB |

## Verification

Scoped first, then the full suite once at the end. No project-wide formatter,
never `scripts/premerge.sh`.

```
uv run pytest tests/test_analysis_worker.py tests/test_canary.py \
  tests/test_cli_audit.py tests/test_explorer.py tests/test_fetch.py \
  tests/test_operator_surfaces.py tests/test_pipeline.py \
  tests/test_profile_harness.py tests/test_provenance.py tests/test_queue.py \
  tests/test_registry.py tests/test_repository_contract.py tests/test_runner.py \
  tests/test_status.py tests/test_trajectory_queries.py tests/test_truth.py \
  tests/test_unattended.py                                    -> 311 passed
uv run pytest tests/test_smoke.py tests/test_gc.py tests/test_results.py \
  tests/test_paths.py                                         -> 14 passed
uv run ruff check src/evallab tests/ dashboard/               -> All checks passed

uv run pytest                                                 -> 539 passed
DATABASE_URL=postgresql://evallab:x@127.0.0.1:1/evallab \
  uv run pytest                                               -> 539 passed
```

The scoped set is every test file importing a module this branch changed
(`cli`, `database`, `explorer`, `facts`, `schemas`, `status`), plus the adjacent
four. The full suite was then run twice: once normally, and once with
`DATABASE_URL` pointed at a closed port, which is the condition that broke the
first CI run. Both are green, so nothing on this branch depends on a reachable
catalog.

## Deliberately not done

**The seventh instance of the same class: `compare --index` is silent.**
`src/evallab/cli.py`, the `compare` branch, calls
`index_comparison_associations(...)` and then prints only the paired statements,
the JSON path, and the Markdown path. Nothing distinguishes an indexed run from
an un-indexed one — exactly F-12 wearing different clothes. Recorded, not
fixed, per this mission's instructions. The fix is three lines and should mirror
`analyze stub`.

**F-01 and F-04 untouched**, as instructed — no partial address of either. The
explorer's two-level walk (F-04) is still the reason a nested `jobs_dir` is
invisible; nothing here changes that, and the F-08 predicate deliberately only
filters, never recurses.

**The explorer still does not render a review's disposition.** That is item (b)
of the flight report's explorer section, not an F-number, and it is not
reachable from this lease: `AnalysisView` is rendered by `dashboard/explorer.py`
(lines 158-178), and `dashboard/` is a separate top-level entry owned by the
Platform lane per `agents/STRUCTURE.md`, outside this mission's paths. Adding an
unrendered field to `AnalysisView` would have been dead code. F-03's fix means
the review is now *ignored cleanly* rather than misread; showing it is a
separate, small change that must touch `dashboard/` in the same PR.

**No `dashboard/` change, no `docs/analysis-loop.md` change.** The latter still
does not mention that reviews reach the catalog only through indexing; it is
outside the lease. The equivalent note went into `docs/operations.md`, which the
lease permits, in the "Ingest and query" section alongside the doctor identity
note and in the queue section for `submit`.

## Notes for the integrator

- `sql/schema.sql` is unchanged. `analysis_reviews` already existed; F-02 only
  made a shipped command reach it.
- `tests/test_operator_surfaces.py` is new. It is the home for "what a command
  tells the operator is part of its contract" — F-02, F-09, F-11, F-12. F-03 and
  F-08 are explorer-internal and stayed in `tests/test_explorer.py`.
- The explorer fixture rename is behaviour-relevant, not cosmetic: the old flat
  `analyses/*.json` layout could not exist in production, and it was the reason
  the `*.json` glob looked correct.
- `database.identity()` is now used by `doctor`, `analyze stub`,
  `analyze review`, and `analyze ingest-sidecar`. It is the one place that
  decides what a connection string may reveal.
