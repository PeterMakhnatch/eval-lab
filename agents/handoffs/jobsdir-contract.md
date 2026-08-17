Status: review-wanted
Last: constrained `jobs_dir` on both declarations, excluded bookkeeping dirs from `discover_job_dirs`, 685 tests + ruff clean, PR opened
Next: Integrator review and merge; two residual defects listed below need owners
Blockers: none

# SCHEMA: constrain jobs_dir to the layout every reader assumes

Branch `role/jobsdir-contract`, worktree `.worktrees/jobsdir-contract`, based on
`origin/main` = `0960eea`.

## Corrections to the brief (evidence first)

1. **`origin/main` is `0960eea`, not `7456ac8`.** `7456ac8` (#66) is one commit
   behind; `0960eea` is a build-plan doc commit on top of it. Verified with
   `git rev-parse origin/main` after `git fetch origin`.
2. **"Every reader assumes a two-level layout" is false.** `results.py`
   `discover_job_dirs` is depth-agnostic — `rglob("result.json")` plus a
   *positive* job test (`results.py:275-285`) — and `load_job:204-209` already
   required `task_name`+`trial_name` before accepting a trial. Proven: given
   `runs/nightly/jobs/j-nest`, `discover_job_dirs` returns it while
   `build_index` does not. The two-level assumption is real in
   `explorer.py:_discover_jobs` and in Harbor's viewer, not in the module I lease.
3. **Depth is not the rule; being a scanned root is.** Discovery roots are
   hardcoded (`dashboard/explorer.py:80`: `runs`, `research/evidence/runs`) and
   are never read from any spec. Proven: a *flat, single-segment* `my-runs/j`
   is exactly as invisible to `build_index` as a nested path. A single-segment
   validator would have satisfied the letter of the brief and still lost runs;
   `tests/test_jobs_dir_contract.py::test_a_flat_root_the_readers_do_not_scan_is_refused_too`
   fails against that naive rule (verified by mutation).
4. **Nesting *is* legitimately used today — by the lab itself.** `evallab smoke`
   sets `jobs_dir = "runs/_smoke/<job>/jobs"` (`smoke.py:167,222`) and reads it
   back by direct path (`smoke.py:232`). The brief asked me to grep committed
   specs; the nested writer is in *code*, not a spec. A single-segment rule
   would have broken `evallab smoke`, and `smoke.py` is outside every lease this
   round. It is admitted as a named reservation, not a special case: nothing
   browses that area, and its jobs carry the reserved `smoke-` prefix already
   excluded from the digest (`digest.py:47-60`).
5. **There is no `harbor/runner.py`.** The `runner.py:601` cited in the brief and
   in `explorer.py:807` is eval-lab's own `src/evallab/runner.py:601`. Harbor is
   not an importable dependency at all — it is a subprocess resolved from PATH
   (`runner.py:635-636`), installed as a uv tool at
   `~/.local/share/uv/tools/harbor/…`, version 0.21.0.

## Writer / reader inventory

**Writers of the job directory shape**

| Where | What it writes |
|---|---|
| `runner.py:601,638,644` (eval-lab) | `jobs_dir / name`, refuses reuse |
| `queue.py:892,1041` | resolves `spec.jobs_dir`, addresses `jobs_dir / name` |
| `queue.py:1053` | **moves** a whole job dir to `.transient-attempts/<name>/attempt-N` |
| `runner.py:540` | `.executor/<name>.log` |
| `smoke.py:222` | nested `runs/_smoke/<job>/jobs` (see correction 4) |
| `harbor/job.py:115,626-628` | `jobs_dir / job_name`, `mkdir(parents=True)`, unvalidated |
| `harbor/job.py:402,532` + `trial/trial.py:102` | trials flat inside the job dir |
| `harbor/job.py:644` / `harbor/models/trial/paths.py:270` | `result.json` at **both** levels |
| `harbor/trial/regrade.py:175` + `download/downloader.py:118` | `.sources/<uuid>/<job>` — a complete job at depth 3 |

**Readers, by how many levels they assume**

| Reader | Depth | Nested run? |
|---|---|---|
| `results.py:275` `discover_job_dirs` | any (`rglob`) | **found** |
| `report.py:370`, `queue.py:1141`, `smoke.py:232`, `cli.py:666` | spec-driven join | **found** |
| `explorer.py:821` `_discover_jobs` / `build_index:1080-1098` | exactly 2 | not found, noted |
| `harbor/viewer/scanner.py:50,86` | exactly 2 | not found |
| `harbor/analyze/analyzer.py:52`, `cli/jobs.py:1752`, `cli/upload.py:110`, `cli/view.py:133`, `viewer/server.py:1160,1569` | exactly 2 | not found |

Harbor's readers disagree on the *predicate* (`config.json OR result.json` at
`scanner.py:87`; `trial.log`/`job.log` at `analyzer.py:35,39`; no predicate at all
at `viewer/server.py:1569`), so no single marker file identifies a level. Harbor
never validates `--jobs-dir` (`cli/jobs.py:354-365`, applied at `:1197`); its only
path check is containment, not depth (`viewer/server.py:1311-1313`).

**Is nesting used anywhere today?** In committed specs, **no**: all 17
`ExperimentSpec`/`ExperimentMatrix` documents under `research/experiments/` and
`research/calibration/records/queue-specs/` set `jobs_dir: "runs"`. Two nested
values exist outside this schema: `library/adapters/quixbugs/run_quixbugs.yaml:1`
(`runs/quixbugs-adapter/full`) is a **Harbor** run config, not an
`ExperimentSpec`, and is unaffected; and `smoke.py`'s scratch, above.

## What changed

- `schemas.py`: `EXPLORATION_JOBS_ROOT`, `SELF_TEST_JOBS_SCRATCH`, and
  `validated_jobs_dir()` shared by a `field_validator` on **both** declarations.
  Two declarations exist because `ExperimentMatrix` is not built from
  `ExperimentSpec` — `runner.request_from_matrix` expands it straight into
  `RunRequest` (`runner.py:710-716`) and `load_matrix` validates it from its own
  file — so any rule must be applied twice or the contracts drift.
- **Hole closed as a side effect:** `ExperimentMatrix` had *no* path validation.
  It accepted `/etc` and `../../escape`, which `runner.py:716` resolves outside
  the repository, against `agents/WORKFLOW.md:42`. Now refused.
- `results.py`: `_is_bookkeeping()` excludes dot-prefixed components from
  `discover_job_dirs`. **Proven defect:** one real job with two retried attempts
  was reported as **three** completed jobs, because `queue.py:1053` archives an
  attempt by moving the whole job dir, `result.json` included. That inflated
  `evallab status` (`status.py:166`), the #64 consumption ledger
  (`quota.py:717`), `compare`, `report.py`, and `cohort.py`.
  `explorer.py:_is_job_dir` already excluded these; `results.py` did not.
- `docs/architecture.md` §3: the layout documented as a contract with citations.
- `tests/test_jobs_dir_contract.py`: 16 tests. Mutation-checked — reverting the
  `results.py` filter fails two of them; a naive single-segment rule fails a third.

## What this does and does not fix

- **Fixes:** a *new* nested or unscanned `jobs_dir` cannot be submitted, on
  either declaration, refused at validation with the layout and the command.
- **Does not fix:** any job directory **already** written in a nested shape. No
  migration is performed and none is attempted; `discover_job_dirs` deliberately
  still finds such runs, and the explorer still names them in a note.
- **Does not fix:** Harbor's scanner. It remains two-level and dotfile-blind
  (`scanner.py:50` has no dot filter, unlike `cli/view.py:139`), so `harbor view
  runs` renders eval-lab's `.executor` and `.transient-attempts` as phantom jobs.
  External tool, not ours to change.
- **Committed evidence in a shape the validator would reject:** yes, but not
  bindingly. Five `research/evidence/runs/*/config.json` record `jobs_dir` as an
  **absolute** path, two of them under a retired predecessor checkout whose name
  `tests/test_repository_contract.py:198` forbids repeating outside
  `research/evidence/runs/` (the paths are in those configs; I did not copy them
  here). These are Harbor's own `JobConfig` (`job_name`, `jobs_dir`,
  `n_concurrent_trials`, `tasks`) — never parsed by `ExperimentSpec` — and are
  immutable promoted evidence. Not edited. No committed spec or fixture breaks.

## Residual defects found, needing an owner (not in my lease)

1. **`evallab run --jobs-dir` bypasses the schema entirely.** `cli.py:247`
   accepts any `Path` and `cli.py:643-666` builds a `RunRequest` directly, never
   an `ExperimentSpec`. The validator covers `submit`, not `run`. Owner: `cli.py`
   (QuotaGate holds that lease this round).
2. **`evallab smoke` writes runs the explorer cannot render.** Proven live: given
   `runs/_smoke/<job>/jobs/<job>/<trial>`, `build_index([runs])` returns zero
   trials and emits the F-04 note verbatim. Correct behaviour post-#66, but it
   means the lab's own self-test generates that note on every run. Fixing it
   means either moving the scratch to a dot-prefixed name (`runs/.smoke/`, which
   `explorer._is_job_dir` already skips) or pointing smoke at `runs`. Owner:
   `smoke.py` + `digest.py` — unowned and owned this round respectively.
3. **`ExperimentMatrix.task` is still unvalidated** for traversal, unlike
   `ExperimentSpec.task`. Same class as the `jobs_dir` hole I closed; left alone
   to keep this diff scoped to `jobs_dir`. Owner: `schemas.py` (next mission).

## Verification

Run in `.worktrees/jobsdir-contract`:

- `uv run pytest` — **685 passed** (669 before, 16 new). Includes
  `test_queue.py`, `test_runner.py`, `test_results.py`, `test_registry.py`,
  `test_smoke.py`, `test_unattended.py`, `test_status.py`, `test_quota.py`,
  `test_profile_harness.py`, all of which construct specs directly.
- `uv run ruff check .` — All checks passed.
- `uvx ty@0.0.71 check src/ --output-format=concise` — 28 diagnostics, under the
  33 ratchet; **zero** in `schemas.py` or `results.py`.
- Shared catalog read-only and at the expected counts: `select count(*)` reports
  **72 jobs / 23 `trajectory_documents`**, matching the brief. Checked on
  completion only — nothing in this mission opens a database connection, so
  there was no write to guard against. No paid agent executed; no `launchctl`;
  no writes to the primary checkout; no `docker compose`.

GitHub, PR #68, head `d0c07c2` — **all five checks pass**: `lint`, `ty`,
`profile`, `test (3.12)`, `test (3.14)` (run 31984467050). Head `5b51e12` was
also fully green (run 31984332420). Per `agents/CHECKS.md` green is a property of
the exact head, so any further commit needs its own confirmation before merge.

**One flake observed, and it is a test-contract defect rather than bad luck.**
The first head `efa9aba` failed
`test_profile_harness.py::test_injected_slowdown_raises_named_path_median` on
`test (3.12)` only: `assert 82.315 >= 94.207 + 40.0`. The *baseline* median
(94.2 ms) exceeded the run carrying an injected 80 ms delay (82.3 ms).

`PerfRebaseline` diagnosed it further and **corrected a fix I had suggested**;
their analysis is the one to act on. `_inject_delay`
(`scripts/profile/harness.py:180-183`) does `time.sleep(80/1000)`, so every
injected rep has an 80 ms floor and the injected side is noise-immune by
construction. `slow = 82.315` therefore means the real digest work was ~2.3 ms —
within 2.9% of the theoretical minimum. Only `base` was noisy, absorbing ~92 ms.
Consequently **every** formulation comparing `slow` against `base` fails on this
data — additive, relative, even a zero margin — because `base > slow`. My
suggestion of a relative margin or more reps was wrong and I withdrew it. Their
fix drops `base` from the assertion: `assert slow >= 0.9 * injected_ms`, which
holds by construction and still catches an injection that is dropped or misrouted.
The principled version injects the sleeper as a seam. Either way the current test
depends on a shared runner's wall clock, which `agents/CHECKS.md`
("Deterministic-test rule") already forbids.

Not attributable to this diff, structurally: the failing run measured ~2.3 ms of
actual digest work, so the digest path was *fast* in the run that failed — a
`results.py` slowdown cannot produce that. Independently, `_time_digest`
(`harness.py:267-293`) installs a stub `trial_loader` and never calls
`discover_job_dirs`, the only function I changed there; and the second commit
edited only this handoff file, after which the test passed. It passed 4/4 locally.

Ownership: `tests/` and `scripts/profile/` are outside my lease and outside
`PerfRebaseline`'s (mission closed, worktree sunset). Reported to
`PerfRebaseline`, who routed the spec to `Main` for a single owner. **Not**
reported to `PerfGateDiag` — I messaged only `PerfRebaseline`.

Capability labels: the schema refusal and the discovery fix are **proven live**
(exercised directly against the real readers, not fixtures). The Harbor inventory
is **source-verified, not executed** — no Harbor command was run.
