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
file boundary is a `ContractModel` subclass in `src/harbor_lab/schemas.py`
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

This is why the test suite runs in ~0.7 s with no Docker and no PostgreSQL,
and it is the mechanism the profiling harness below uses to separate our code
cost from database latency. New I/O gets a seam.

**Immutability.** Models are updated with `model_copy(update={...})`, never
mutated in place (`queue.py` `submit`/`transition`). Completed run
directories are immutable once written (`AGENTS.md`); the digest and the
PostgreSQL catalog are *derived* and rebuildable from them.

**The queue is the only dispatch boundary.** `Executor` is documented as "the
sole application boundary allowed to start Harbor experiments". Billable work
goes through `harbor-lab submit` and the policy gate. Nothing else shells out
to Harbor.

**Python only.** Per `AGENTS.md`: application code, adapters, verifiers, and
tasks are Python. Shell, SQL, Dockerfiles, and config formats are supporting
files.

---

## 2. Checks

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

`ty` 0.0.71 reports **33 diagnostics** on `src/` as of the wave-1 merge
(`d0d6760`). Distribution:

| File | Count | Owner tonight |
|---|---|---|
| `atif.py` | 14 | other role |
| `facts.py` | 7 | other role |
| `cohort.py` | 5 | other role |
| `database.py` | 3 | BUILDER |
| `tracing.py` | 2 | other role |
| `queue.py` | 2 | hot — do not touch |

By rule: 14 `unresolved-attribute`, 10 `invalid-argument-type`, 3
`unresolved-import`, 3 `not-subscriptable`, 1 `not-iterable`, 1
`no-matching-overload`, 1 `redundant-cast`.

Three of these are not code defects: `tracing.py` imports `litellm` and `dspy`,
which are optional runtime dependencies absent from the locked dev
environment. Those want a dependency-group entry or a per-module ignore, not a
code change.

FORGE fixed none of them. Every file above belongs to another role
(`agents/WORKFLOW.md`), and `queue.py` was under active work the night this
was written.

**The job is a non-regression ratchet, not a pass/fail gate on zero.**
`typecheck.yml` sets `TY_BASELINE: 33`. The job fails only if ty reports *more*
than the baseline, and emits a notice when the count drops so the baseline can
be lowered. This was the only design that satisfies all three constraints at
once: type checking runs on every PR, new type errors are caught, and FORGE
does not have to edit files owned by other roles to make CI green.

An earlier revision used job-level `continue-on-error: true`. That was wrong in
practice — GitHub still reports the job's conclusion as *failure* to the checks
API, so the PR showed a red check indistinguishable from a real break, which is
the precise failure mode a reporting-only job is supposed to avoid.

Lower `TY_BASELINE` as modules are cleaned. Raising it should require a
sentence in the PR saying why.

An earlier draft of this document recorded 4 diagnostics; that was measured
before the wave-1 modules (`atif.py`, `facts.py`, `cohort.py`, `tracing.py`,
`calibrate.py`, `credentials.py`, `researchers.py`) merged. The number above
supersedes it.

`ty` is pre-1.0 and therefore pinned. An unpinned checker can turn CI red on
its own release schedule, which is indistinguishable from a real regression.

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
| `uv run pytest -q` | 0.82 s | 49 tests, no Docker, no DB |
| `uvx ty@0.0.71 check src/` | 0.10 s | warm; excludes the uvx download |

Total local check time is ~1 s. The ~3 min CI target is therefore dominated by
runner startup and `uv sync`, not by our checks — optimize the workflow, not
the test suite. The practical levers are the uv cache (already enabled) and
`concurrency.cancel-in-progress` (already set on both workflows).

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
(`src/harbor_lab/queue.py:438-439`). Each is a separate PostgreSQL round-trip
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
   (`CREATE DATABASE harbor_lab_forge_prof`), point `DATABASE_URL` at it, and
   run `database.initialize` before ingesting. Never profile writes against
   the shared `harbor_lab` catalog. Drop it afterwards.
3. **Never dispatch.** Build `Executor` with `runner=lambda spec, job_dir:
   job_dir` and `ingester=lambda job_dir: None`. A profiling run must not
   start Harbor jobs or spend money.
4. **Measure the seams separately.** Run each path twice — once with catalog
   seams stubbed, once with them real. The difference is the database's
   contribution, and it is where all the surprises were.
5. **Report median of >= 5 reps after a warmup**, plus min and max. Single
   timings on a laptop are noise.

Synthetic specs need `hypothesis`, `name` matching `^[a-z0-9][a-z0-9-]+$`, and
`policy_rule="human-approval"` to sit in `approved/` without a live gate.

---

## 5. Open items

Recorded, not acted on, because the files belong to other roles:

- **Queue tick connection reuse** — the finding above. Needs a
  ceiling-enforcement test first.
- **33 `ty` diagnostics** — distribution in section 2; flip the job to blocking
  as modules reach zero. 3 of them are missing optional deps, not defects.
- **`harbor-lab fleet` does not exist.** `agents/WORKFLOW.md:45` and the header
  of `scripts/fleet-status.sh` both reference it as the successor command.
  Either build it or correct the references.
- **`pyproject.toml`, `uv.lock`, and `ci.yml` disagree about Python 3.11, and
  it breaks CI today.** `pyproject.toml` declares `requires-python = ">=3.11"`
  and `uv.lock` repeats it, but the lock's `supported-markers` are
  `python_full_version >= '3.12'`. So `uv sync --locked` on 3.11 fails outright:

  ```
  error: The current Python platform is not compatible with the lockfile's
  supported environments: `python_full_version >= '3.12'`
  ```

  `ci.yml` pins the `lint` job to 3.11 and includes 3.11 in the `test` matrix,
  so **two of main's three CI jobs cannot pass** regardless of the code. FORGE
  hit the same wall by copying the 3.11 pin and moved `typecheck.yml` to 3.12.

  The real fix is one of: drop 3.11 from `pyproject.toml` and the CI matrix, or
  re-lock so 3.11 is genuinely supported. Both touch BUILDER-owned files
  (`pyproject.toml` / `uv.lock`, per `agents/WORKFLOW.md`), so this is reported
  rather than fixed.

- **`tests/test_canary.py::test_canaries_run_two_consecutive_nights_with_three_attempts`
  fails on CI** (`assert 3 == 0` — enqueued 3, dispatched 0) while passing
  locally. Appeared with the per-agent default-model change (`177b20d`).
  Local runs are green because they see a different credential/Docker
  environment than the runner. Not a FORGE-owned file.

- **Python 3.13 is untested.** The worktree venv resolves to 3.13, while the CI
  matrix tests 3.11 (broken, above) and 3.14. Local development therefore runs
  on a version CI never exercises.
- **Ingest does not short-circuit.** Re-ingesting an unchanged corpus costs
  ~92% of a cold insert. A content hash per job directory would make repeat
  ingests near-free; worth it only once the corpus is much larger than 10 jobs.
