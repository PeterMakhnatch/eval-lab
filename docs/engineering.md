---
status: living
audience:
  - builder
  - analyst
  - runner
  - operator
---

# Engineering standards and performance baselines

Owner: FORGE. Scope: how the lab's code is written, checked, and measured.

Every performance claim made anywhere in this repository must cite a number
from the Baselines section below, or supersede it with a new measurement taken
the same way. "Faster" without a before/after pair is not a claim, it is an
opinion.

---

## 1. Observed conventions

These are descriptions of what the code already does, not new rules.

**Pydantic contracts at the boundary.** Everything that crosses a process or
file boundary is a `ContractModel` subclass in `src/evallab/schemas.py`
(`ExperimentSpec`, `QueueEvent`, `JobRecord`, …). Parsing is
`Model.model_validate_json(path.read_text())`; serialization is
`model_dump_json()`. Consequence: a malformed queue file fails at read time
with a located error, not three frames later. Follow this for any new file
format — do not hand-roll `json.loads` into a dict.

**Seam-based dependency injection.** Collaborators that touch the outside
world are constructor parameters defaulting to the real implementation:

```python
Executor(..., runner=None, ingester=None, spent_today=None,
         consecutive_harness_failures=None)
# self._runner = runner or self._run_harbor
```

This is why the test suite runs without Docker or PostgreSQL, and it is the
mechanism the profiling harness below uses to separate our code cost from
database latency. New I/O gets a seam.

**Immutability.** Models are updated with `model_copy(update={...})`, never
mutated in place (`queue.py` `submit`/`transition`). Completed run
directories are immutable once written (`AGENTS.md`); the digest and the
PostgreSQL catalog are *derived* and rebuildable from them.

**The queue is the only dispatch boundary.** `Executor` is documented as "the
sole application boundary allowed to start Harbor experiments". Billable work
goes through `evallab submit` and the policy gate. Nothing else shells out
to Harbor.

**Python only.** Per `AGENTS.md`: application code, adapters, verifiers, and
tasks are Python. Shell, SQL, Dockerfiles, and config formats are supporting
files.

---

## GREENLINE

Default `uv run pytest` (and therefore CI `quality` plus `scripts/premerge.sh`)
collects every **repository-owned unit suite**:

- `tests/`
- `dashboard/tests/`
- `research/analysis/tests/`
- `research/calibration/tests/`
- `research/experiments/tests/`

`tests/test_ci_coverage.py` fails if a committed `test_*.py` under those
directories is not collected.

**Not** in the default suite (and must not be silently skipped):

- Harbor task verifiers under `library/tasks/**/tests/`,
  `library/benchmarks/**/tests/`, and `library/adapters/*/generated/**/tests/`.
  Those run inside Harbor, not in lab CI.
- Any future live/container/benchmark integration suite belongs in
  `tests/live/` or `tests/integration/` and is invoked by an explicit path
  (for example `uv run pytest tests/live`). Those directories are absent
  today; do not add them to `testpaths` until they exist and stay
  Docker/network-free or are documented as opt-in.

The PROGRAM ledger is gated by `tests/test_program_contract.py`, which
imports `research/experiments/validate_program.py` and rejects malformed
inputs. Production `load_job` still requires `finished_at` on job
`result.json`; synthetic fixtures must include that field rather than
weakening the loader.

---

## 2. Checks

### Supported Python versions

Eval Lab requires Python 3.12 or newer. Python 3.12 is the
development and lint floor; CI runs the test suite on Python 3.12 and 3.14.
Keep `requires-python`, `.python-version`, `uv.lock`, and the CI version matrix
aligned whenever the supported range changes.

| Check | Command | Where | Blocking |
|---|---|---|---|
| Lint | `uv run ruff check .` | `.github/workflows/ci.yml` | yes |
| Tests | `uv run pytest` | `.github/workflows/ci.yml` | yes |
| Types | `uvx ty@0.0.71 check src/` | `.github/workflows/typecheck.yml` | **ratchet — see below** |

`typecheck.yml` is a separate workflow file on purpose. `ci.yml` was being
rewritten concurrently while this landed (by `codex/restore-green-ci`, since
merged as `65ef29c`, and by a second uncommitted edit in the FORGE worktree —
see `agents/handoffs/forge.md`). A separate workflow merges cleanly regardless
of who touches `ci.yml`, and runs in parallel so it costs no extra wall-clock.

Consolidating the two into one workflow is a reasonable daytime follow-up, but
only after the ty-versus-mypy question in the FORGE handoff is settled. Do not
ship two type checkers.

### The type-check baseline

`ty` 0.0.71 reports **zero diagnostics** on `src/` as of 2026-08-25. CI keeps
`TY_BASELINE: 0`; any diagnostic is therefore a blocking regression.

The zero-diagnostic cutover narrowed nullable ATIF/fact payloads, made optional
Harbor and observability imports explicit dynamic boundaries, typed trusted SQL
inputs, and removed stale casts. Do not restore a positive baseline or suppress
module-wide diagnostics. If an optional integration is absent, preserve its
runtime fallback while keeping the statically imported core clean.

`ty` remains pinned because it is pre-1.0. An unpinned checker can turn CI red
on its own release schedule, which is indistinguishable from a code regression.

---

## 3. Baselines

Measured 2026-08-14 on the FORGE worktree, rebased onto `d0d6760` (after the
wave-1 merge). **Cite these numbers, or re-measure and supersede them.**

**Machine and versions.** Apple Silicon (arm64), macOS 25.5; Python 3.13
(worktree venv), uv 0.9.24, Harbor 0.21.0; PostgreSQL 18.4 in Docker on
`localhost:54329`; `ty` 0.0.71; ruff and pytest per `uv.lock`.

**Corpus shape.** 10 Harbor job directories, 24 `result.json` files,
263,439 bytes, drawn from `runs/` + `research/evidence/runs/`. All numbers
scale with this corpus; re-state the shape whenever you re-measure.

Method: median of N repetitions after one warmup, `time.perf_counter()`.

### Checks

| Path | Median | Notes |
|---|---|---|
| `uv run ruff check .` | 0.12 s | whole repo, clean |
| `uv run pytest -q` | 0.82 s | historical 2026-08-14 capture; no Docker, no DB. Live collection: `uv run pytest --collect-only -q` |
| `uvx ty@0.0.71 check src/` | 0.10 s | warm; excludes the uvx download |

These figures are historical. On the M4 Max hardening workstation, the
2026-08-25 baseline exercised the full suite serially in 230.91 s and exposed a
fast-process lease-heartbeat race. The unchanged suite passed with four xdist
workers and load-scope distribution in 101.84 s. After fixing that race, the
hardened suite passed in 107.38 s: a 53.5% wall-clock reduction from the serial
baseline. The 2026-08-25 capture is a dated wall-clock measurement, not a
current test census; observe live collection with `uv run pytest --collect-only -q`.
The bounded four-worker default keeps every test and Hypothesis setting in the
lane.

CI dependency installation is narrowed by job instead of cached: quality jobs
omit the observability group, and performance installs only project runtime
dependencies. Typecheck installs the default development and observability
groups because it analyzes every source module, including optional integrations.
`docs/operations.md` records why no cache trust root is added.

### Application paths

| Path | Median | Reps |
|---|---|---|
| `results.load_jobs(corpus)` — parse only | **12.46 ms** | 7 |
| `database.ingest` — cold insert into an empty schema | **58.72 ms** | 1 |
| `database.ingest` — steady-state upsert | **54.03 ms** | 5 |
| `DigestRenderer.write` — catalog seams stubbed | **4.10 ms** | 7 |
| `DigestRenderer.write` — real catalog queries | **34.24 ms** | 5 |
| `scripts/fleet-status.sh` — full report | **1287 ms** | 5 |

Reading: parsing the corpus is cheap (~1.2 ms per job dir). Ingest is
dominated by the database, and re-ingest costs ~92% of a cold insert — the
upsert does not short-circuit on unchanged jobs. Digest rendering is ~4 ms of
our code plus ~30 ms of PostgreSQL. `fleet-status.sh` is ~1.3 s of subprocess
`git`/`gh` calls; fine for a human at a terminal, too slow to call from
anything automated.

### Queue tick — the one that matters

`Executor.tick()`, N approved specs, `runner`/`ingester` stubbed so nothing
dispatches and nothing is billed. Two configurations: catalog seams stubbed
(our queue code alone) versus the production seams wired in.

| N | queue scan only | production seams | ratio |
|---|---|---|---|
| 10 | 17.57 ms | **149.53 ms** | 8.5x |
| 50 | 40.61 ms | **772.18 ms** | 19x |
| 100 | 70.26 ms | **1594.27 ms** | 23x |

The queue's own scan is linear and cheap: ~0.53 ms per additional spec between
N=10 and N=100, and that includes the JSON parse, the state-directory
transition, and the `events.jsonl` append. (The N=10 figure carries a fixed
~12 ms of setup, which is why the ratio climbs with N rather than being flat.)

The overhead is `tick()` calling `self._spent_today()` and
`self._consecutive_harness_failures()` **inside** the per-spec dispatch loop
(`src/evallab/queue.py:438-439`). Each is a separate PostgreSQL round-trip
opening its own connection:

| Seam | Median per call |
|---|---|
| `database.daily_cost_usd` | 6.18 ms |
| `database.consecutive_harness_failures` | 6.39 ms |

That is ~15.2 ms of database work per approved spec, so at N=100 roughly 96%
of tick wall-clock is catalog round-trips rather than queue work.

**This was measured, not fixed, and the obvious fix is wrong.** Spend changes
as jobs dispatch *within* a single tick, so re-reading it per iteration is
plausibly deliberate: hoisting the query out of the loop would let a long tick
overrun the `$20/day` ceiling in `policy/standing-approvals.yaml`. The safe
optimization is **connection reuse** — one connection held for the tick, or a
small pool — which removes the per-call connect cost while preserving
read-per-iteration semantics. A correctness argument, and a test that a tick
stops dispatching once the ceiling is reached mid-tick, must come before any
change here.

---

## 4. Reproducing these numbers

The harness lives at `runs/_forge/profile.py` — under the gitignored `runs/`
tree, deliberately not committed, because `scripts/` is BUILDER-owned. Rebuild
it from this recipe:

1. **Corpus.** Point at `runs/` and `research/evidence/runs/`. Resolve job directories
   with `results.discover_job_dirs`, then **filter out anything under your
   scratch directory**. Writing scratch copies inside `runs/` silently doubled
   the corpus between two of my runs and inflated the first ingest numbers by
   ~2.4x before I caught it.
2. **Isolate the database.** Create a scratch database
   (`CREATE DATABASE evallab_forge_prof`), point `DATABASE_URL` at it, and
   run `database.initialize` before ingesting. Never profile writes against
   the shared `evallab` catalog. Drop it afterwards.
3. **Never dispatch.** Build `Executor` with `runner=lambda spec, job_dir:
   job_dir` and `ingester=lambda job_dir: None`. A profiling run must not
   start Harbor jobs or spend money.
4. **Measure the seams separately.** Run each path twice — once with catalog
   seams stubbed, once with them real. The difference is the database's
   contribution, and it is where all the surprises were.
5. **Report median of >= 5 reps after a warmup**, plus min and max. Single
   timings on a laptop are noise.

The committed harness is `scripts/profile/` (SPEED, 2026-08-14). It replaces
the gitignored `runs/_forge/profile.py` recipe:

```bash
uv run python scripts/profile/harness.py
uv run python scripts/profile/check_budgets.py runs/_speed/profile-report.json
```

It profiles ingest, projection, facts, digest render, a 100-spec queue tick,
and `scripts/fleet-status.sh`. Harbor dispatch is stubbed. Ingest writes only
to a scratch database named `evallab_speed_prof` (or
`$EVAL_LAB_PROFILE_DATABASE_URL`). The shared `evallab` catalog is refused.
`gh` is stubbed so fleet-status does not hit the network. CI runs the same
command in `.github/workflows/perf.yml` and fails when a median exceeds the
committed budget by more than `tolerance_pct`.

Its corpus is **pinned** (2026-08-16): `harness.DEFAULT_CORPUS` names the two
`event-summary` control job directories rather than the directory
`research/evidence/runs`, so promoting evidence cannot move the gate. See
"Corpus pin" below. `--corpus` still points the harness anywhere for ad-hoc
profiling.

Synthetic specs need `hypothesis`, `name` matching `^[a-z0-9][a-z0-9-]+$`, and
`policy_rule="human-approval"` to sit in `approved/` without a live gate.

---

## 5. SPEED before / after (2026-08-14)

Harness: `uv run python scripts/profile/harness.py`. Method: median of 5
reps after 1 warmup, `time.perf_counter()`. Machine: Apple Silicon arm64,
macOS 26.5, Python 3.12.11, scratch Postgres `evallab_speed_prof` on
`127.0.0.1:54329`. Harbor dispatch stubbed. Two consecutive local runs on 2026-08-14 agreed on the same six paths
(times jitter). Re-run `scripts/profile/harness.py` to supersede.

**Corpus shape (this table).** 2 Harbor job directories,
4 `result.json` files, 31,716 bytes, from committed
`research/evidence/runs` (oracle + nop event-summary). This is *not* the
FORGE §3 corpus (10 jobs / 24 `result.json` / 263,439 bytes from mixed
`runs/` + evidence). Do not ratio the two columns as a speedup.

| Path | FORGE §3 (10-job mix) | SPEED harness (2-job evidence) | Notes |
|---|---:|---:|---|
| ingest (`database.ingest`, scratch DB) | 54.03 ms steady / 58.72 ms cold | **29.01 / 31.08 ms** | Smaller corpus; same seam |
| projection (`atif.export_trajectories`) | not separately timed | **2.69 / 2.65 ms** | Python + Parquet write |
| facts (`facts.export_facts`) | not separately timed | **3.45 / 3.91 ms** | Python + Parquet write |
| `DigestRenderer.write` (catalog stubbed) | 4.10 ms | **0.80 / 0.80 ms** | Smaller digest input |
| `Executor.tick` N=100, stubs | 70.26 ms | **44.73 / 43.45 ms** | Same N, stubbed runner |
| `scripts/fleet-status.sh` | 1287 ms | **1858 / 1446 ms** | git + stubbed `gh`; host-bound |

Reading: on the committed fixture, separate ingest+projection is ~33 ms.

### Post-PIPELINE ingest+projection (2026-08-14, `3ba570c`)

`origin/main` now includes `PIPELINE: unify catalog ingest and Parquet
projection` (#17). The shipped seam is `atif.ingest_and_project`: catalog
`database.ingest`, then `facts.ingest_catalog`, then per-job Parquet
(`jobs.parquet` + `rebuild_from_raw`). Same machine, same 2-job evidence
corpus, same harness, scratch DB `evallab_speed_prof`, Harbor stubbed.

| Path | Pre-PIPELINE SPEED §5 | Post-PIPELINE | Notes |
|---|---:|---:|---|
| ingest (`database.ingest`) | 29.01 / 31.08 ms | **30.47 ms** | Same seam; jitter |
| projection (`export_trajectories`) | 2.69 / 2.65 ms | **3.20 ms** | Same seam; jitter |
| **ingest+projection** (`ingest_and_project`) | n/a (did not exist) | **46.77 ms** (42.61–53.18) | Unified path does ingest + fact catalog + full rebuild |

**Finding: already optimal for this corpus. Polars was not adopted.**

The Python-side transforms (`project_trial`, `export_trajectories`,
`export_facts`) are 3–4 ms. The unified path's extra ~14 ms versus
ingest+projection separately is `ingest_catalog` plus
`rebuild_from_raw`, not a loop that Polars can shrink. Two jobs / a few
dozen rows is below the break-even for a DataFrame library; converting
to Polars and back would add import and copy cost. DuckDB is not on this
path (it is used later for analysis queries) and was not touched.

A real win would need a much larger fixture (hundreds of jobs) or a
change to catalog round-trips, which is the FORGE open item on
`queue.py`, not this grant. Re-measure with `scripts/profile/harness.py`
(the `ingest+projection` row) if the committed corpus grows.

CI ratchet: `scripts/profile/budgets.json` + `.github/workflows/perf.yml`.
An injected `digest=500` slowdown against a 5 ms budget fails with
`digest: median 653.170 ms exceeds budget 5.000 ms + 10%`.

### `ingest` re-baselined against CI evidence (2026-08-15)

The `profile` check failed on a docs-only PR (#52) with `ingest: median
125.239 ms exceeds budget 80.000 ms + 50% (ceiling 120.000 ms)`; a rerun of
the identical commit passed. The budgets above were calibrated at ~3x an
Apple Silicon laptop capture with a local PostgreSQL, but they are enforced on
`ubuntu-latest` against a PostgreSQL service container. Of the paths whose cost
scales with machine class, `ingest` had the least headroom against the §5
laptop capture (80.0 / 30.47 = 2.6x, versus 7.2x–18.8x for projection, facts,
and digest), so it broke first. It is also the path most exposed to the
substitution: `_time_ingest` re-runs the whole `sql/schema.sql` DDL through
`initialize()` and opens two fresh connections per repetition, so it is
dominated by connection setup and server-side catalog work.

CI-measured `ingest` distribution, from the `speed-profile-report` artifact of
the last 14 **successful** perf runs on `ubuntu-latest`:

| n | min | median | mean | stdev | max |
|---:|---:|---:|---:|---:|---:|
| 14 | 51.4 ms | 72.3 ms | 75.9 ms | 13.1 ms | 96.1 ms |

Samples: 51.4, 66.9, 67.2, 67.4, 67.9, 68.9, 72.0, 72.5, 72.6, 85.2, 88.5,
91.3, 95.1, 96.1 ms. One further run measured **125.2 ms** and failed the old
120.0 ms ceiling; the rerun of that same commit passed, so it is runner
variance, not a regression.

**New budget: `ingest` = 115.0 ms**, `tolerance_pct` unchanged at 50, so the
ceiling is **172.5 ms**. That is ~1.2x the observed successful max (96.1 ms),
1.79x that max at the ceiling, and 1.38x the 125.2 ms variance spike — a
repeat of the worst observed runner event now passes. The gate still fails any
median above 2.39x the current CI median, so a ~2.4x regression is caught and
a 2x one (~145 ms) is not; that is the deliberate price of making the check
trustworthy. The other five budgets were **not** widened and remain
laptop-anchored.

**Rule.** Any future re-baseline of a perf budget must cite CI artifact
samples — run count, spread, and median from `speed-profile-report` — not a
single laptop timing.

### Corpus pin: the gate measures code, not the size of the evidence set (2026-08-16)

The harness profiled the *directory* `research/evidence/runs`, so the six
budgets were enforced against however much evidence happened to be committed.
**Three of the six paths are corpus-coupled** — `_time_ingest`,
`_time_projection`, and `_time_facts` all take the loaded `list[JobRecord]`.
(`digest` takes only a repo root, `queue-tick-100` a synthetic `tick_n`, and
`fleet-status` is git/`gh`-bound.) A promotion from 2 jobs to 5 pushed
`projection` and `facts` past their 37.5 ms ceiling on unchanged code and
turned the gate red for a reason that has nothing to do with performance.

`harness.DEFAULT_CORPUS` now names the two control job directories
(`research/evidence/runs/event-summary-nop-evidence` and
`.../event-summary-oracle-evidence`) instead of their parent.
`results.discover_job_dirs` returns a named job directory verbatim, so the
profiled set cannot grow by discovery. `--corpus` still overrides it for
ad-hoc profiling.

**No budget and no tolerance changed.** The pinned set *is* the 2-job corpus
every committed number was measured on, so pinning preserves the meaning of
those budgets instead of resetting them; a re-baseline here would have been a
loosening with nothing behind it. Three local runs on 2026-08-16
(macOS-26.5-arm64, Python 3.12.11, scratch Postgres `evallab_speed_prof`,
median of 5 reps after 1 warmup each) confirm every median sits inside its
ceiling: `ingest` 28.6/30.4/31.2 (ceiling 172.5), `projection` 2.8/2.9/3.2 and
`facts` 3.9/3.9/4.0 (37.5), `digest` 0.88/0.93/0.96 (22.5), `queue-tick-100`
74.7/74.8/78.2 (225), `fleet-status` 2234/2241/2250 ms (7500). This is a local
sample, not CI artifact evidence; it justifies *not* moving a budget, which is
the only claim it can support.

Pinning also makes the 2026-08-15 `ingest` re-baseline meaningful in
retrospect: those 14 CI samples were drawn on the 2-job corpus, and that corpus
is now the one the budget is enforced against.

Two mechanisms keep the coupling out. `scripts/profile/budgets.json` carries a
`corpus` block (`roots`, `jobs`, `result_json`) and
`check_budgets.assert_corpus_shape` fails any report that does not match it, so
a *data* change is caught at the gate. `tests/test_profile_harness.py` fails if
the default resolves to `research/evidence/runs` or to anything that is not a
job directory, so a *code* change is caught in the suite.

**Rule (extends the 2026-08-15 rule).** A perf re-baseline may only cite
samples from runs whose reported `corpus_roots` and `corpus_jobs` match the
pinned shape declared in `budgets.json`. A sample measured on a different
corpus is not a weaker sample; it is a measurement of something else. This
matters for any pending work that will need a re-baseline — the pin must land
before the samples are drawn, or the new number is calibrated against a corpus
that is not the enforced one.

### Anti-pattern: freezing the composition of a growing corpus (2026-08-16)

Three separate checks failed on the same day for the same reason, in three
different mechanisms:

| Check | What it froze |
|---|---|
| `scripts/profile/budgets.json` via the harness default | six perf budgets calibrated on "whatever is under `research/evidence/runs`" |
| `research/analysis/tests/test_facts.py:25` | `set(facts) == {"oracle", "nop"}` — the evidence set is exactly these two |
| `research/analysis/tests/test_analysis.py:331` | `report["n_labels"] == 25` |

Each was correct when written and each turned red on a change that added
intended, reviewed content. The cost is worse than the red build: it pressures
the author of the *growth* to edit the *check*, which is how a gate gets
quietly re-scoped to make a PR pass.

**The pattern.** A gate or test that hardcodes the size or membership of a
corpus the project intends to grow.

**The rule.** Assert invariants and relationships, not census counts. Prefer
"every label resolves to a known task", "the oracle control outscores the nop
control", "the profiled corpus is exactly the declared pinned set" over
"there are 25 labels" or "there are 2 evidence jobs". When a fixed count really
is the contract — as it is for a perf corpus, where the number *must* not move
— pin it explicitly in committed data, name it as a pin, and make the mismatch
message say that the number changing invalidates the measurement.

---

## 6. Open items

Recorded, not acted on, because the files belong to other roles:

- **Queue tick connection reuse** — the finding above. Needs a
  ceiling-enforcement test first.
- **`evallab fleet` does not exist.** `agents/WORKFLOW.md:45` and the header
  of `scripts/fleet-status.sh` both reference it as the successor command.
  Either build it or correct the references. Live fleet reporting remains
  `scripts/fleet-status.sh`.
- **Python 3.13 is not in the CI matrix.** Supported range is Python 3.12 or
  newer (`pyproject.toml` `requires-python = ">=3.12"`). CI quality/tests run
  3.12 and 3.14; typecheck runs 3.12. A local 3.13 venv is therefore off-matrix,
  not a 3.11 support gap. The former open item that `pyproject.toml` /
  `uv.lock` / `ci.yml` still advertised 3.11 and broke CI is superseded.
- **The `ty` ratchet is zero, not 33.** §2 records `ty` 0.0.71 / `TY_BASELINE: 0`
  as of 2026-08-25. Do not restore a positive diagnostic census as the live
  status; re-measure with `uvx ty@0.0.71 check src/` before claiming a new
  baseline.
- **Ingest does not short-circuit.** Re-ingesting an unchanged corpus costs
  ~92% of a cold insert. A content hash per job directory would make repeat
  ingests near-free; worth it only once the corpus is much larger than 10 jobs.
