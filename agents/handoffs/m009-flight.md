Status: review-wanted
Last: Flew M009 end to end on merged main 86380b0; four free control jobs, six trials, two analysis sidecars, one review, all operator surfaces exercised; findings written to docs/checkpoints/2026-08-16-m009-integration-flight.md
Next: Integrator reviews PR, requires fresh exact-head CI, then decides which of F-01/F-02/F-03/F-04 becomes a repair mission
Blockers: none for this mission; the flight itself is blocked at the trajectory hop for every agent subscription-only policy permits (F-05), which is a finding, not an obstacle to merging this record

# M009 — integration flight

Agent/model: Claude Opus 4.6 (Anthropic), worker id `M009Flight`.
Worktree `.worktrees/m009-flight`, branch `role/m009-flight`, based on
`origin/main` @ `86380b0`.

## What this mission changed

Two files, both inside the declared lease:

- `docs/checkpoints/2026-08-16-m009-integration-flight.md` — the flight record.
- `agents/handoffs/m009-flight.md` — this file.

Nothing under `src/`, `tests/`, `policy/`, `library/`, `research/`,
`dashboard/`, `sql/`, `scripts/`, or `.github/` was touched. This mission
proves the system; it does not change it. `agents/missions/ACTIVE.md` was
deliberately left alone — the board belongs to the integrator, and a
concurrent Integration sweep was editing it.

Generated state lives in this worktree's gitignored `runs/`, `queue/`, and
`derived/`. The verbatim transcript is `runs/m009/transcript.log` (36 recorded
commands with exit statuses).

## Result in one line

The end-to-end path is **not `proven live` as a single chain**. Seven of eight
hops work on merged main. The trajectory hop is structurally `blocked`:
`oracle` and `nop` write `agent/oracle.txt`, never an ATIF
`agent/trajectory.json`, so trajectory documents, trajectory Parquet, and
Phoenix spans are all empty and `experiment_trial_analysis_path
.trajectory_document_id` is `NULL` for all six trials.

## Every command that failed, and whether a human would have known what to do next

Six non-zero exits out of 36 recorded commands.

| # | Command | Exit | Would a human know what to do next? |
|---|---|---|---|
| 1 | `python -m evallab.task_workbench plan library/tasks/event-summary` | 2 | **Yes.** Argparse usage error — my own mistake, three `--source-*` arguments are required and the error names all three. Not a defect. |
| 2 | `python -m evallab.task_workbench plan library/tasks/event-summary --source-uri … --source-ref 86380b0 --license proprietary` | 1 | **Partly.** It emits a full JSON report with `static_passed: false` and six labelled diagnostics, three `task_defect` and three `harness_defect`, and the harness ones say in prose that the limitation is the workbench's, not the task's. What a human would *not* know: that this outcome is expected for every one of the four in-repo tasks, so there is currently no task the workbench can certify. Nothing tells them that. |
| 3 | `evallab trace runs/m009-flat-event-summary-nop` | 1 | **Yes.** `control agent (oracle/nop); pass include_controls to trace` names the exact flag. Best failure message in the repository. |
| 4 | `evallab trace runs/m009-flat-event-summary-nop --include-controls` | 1 | **Yes, and no.** The message is perfect — `no ATIF trajectory at …/agent/trajectory.json (oracle/nop controls write agent/oracle.txt instead)` — so a human knows exactly *why*. But there is no next step available to them: the only fix is an agent that costs money, which policy forbids. Correct message, dead end. |
| 5 | `evallab analyze worker-run-one efa8c08c02a88d57` (before staging) | 2 | **No. This is the worst failure in the flight.** A raw `[Errno 2] No such file or directory: …/derived/analyses/worker/requests/efa8c08c02a88d57/request.json`, one command after `analyze worker-plan` printed that same id as `eligible`. Nothing says requests must be staged first, and no CLI can stage them — `AnalysisWorker.stage()` is called only by `_nightly_analysis_stager` (`src/evallab/cli.py:1387`). I had to read `cli.py`, find the private call site, and invoke the library from Python. An operator stops here. |
| 6 | `evallab analyze worker-run-one efa8c08c02a88d57` (after staging) | 1 | **Yes.** `{"state": "deferred", "reason": "policy_requirement_unmet:calibrated_judges_only"}`. This is the M006 calibration gate refusing exactly as designed, with zero model calls. Correct behaviour, correctly reported. Recorded as a success, not a failure. |

Two further defects produced **no** error at all, which makes them worse than
the ones above:

- **The run explorer silently drops runs whose `jobs_dir` is nested.** Four of
  my six trials were invisible, `index.notes` was empty, and the job appeared
  in the list with zero trials. I proved the cause with a controlled A/B: an
  identical oracle spec differing only in `jobs_dir` (`runs` instead of
  `runs/m009/jobs`) appeared immediately. A human would conclude their run
  failed. `build_index` walks exactly two levels
  (`src/evallab/explorer.py:560-568`) while `jobs_dir` is a free-form spec
  field that `agents/WORKFLOW.md` encourages varying per worktree.
- **`evallab analyze review` writes a file that the explorer then reports as
  corrupt.** `_analysis_views` does `rglob("*.json")` over `derived/analyses/`
  and parses `reviews/*.json` as sidecars, pinning
  `analysis <review-uuid>.json: unreadable (ValidationError)` to the top of
  every explorer tab, permanently, once per review. The same command also fails
  to index the review: `analysis_reviews` stayed at zero rows until I ran
  `ingest-sidecar` a second time, which is documented nowhere.

Full severity-ranked list of all fourteen findings (F-01 … F-14) is in the
checkpoint.

## Isolation — what I did so this flight touched nobody else

- **Throwaway database.** The default `DATABASE_URL` is the shared `evallab`
  catalog (69 jobs / 83 trials, with a doctor invariant asserting
  catalog/Parquet parity). I created `m009_flight` on the already-running
  Postgres over TCP via `psycopg` — no `docker compose`, no container started
  or stopped — applied the schema with `evallab db init`, and dropped the
  database afterwards. Verified: the shared catalog still reports 69 jobs /
  83 trials and `doctor` still prints `catalog=69 projected=69`.
- **Derived root pinned.** `derived_root_from_environment`
  (`src/evallab/paths.py:37-61`) deliberately resolves a linked worktree's
  derived root against the **primary checkout**. Left at its default,
  `evallab ingest` from here would have written Parquet into
  `~/Developer/eval-lab/derived/`. I set `EVALLAB_DERIVED_ROOT` to an absolute
  path inside this worktree. Verified afterwards that nothing under the primary
  checkout's `derived/`, `runs/`, or `queue/` was modified.
- **Queue and jobs inside this worktree**, confirmed by `evallab status`
  Health, which prints both paths.
- **Primary checkout untouched.** `git status --porcelain` there still shows
  only the three pre-existing entries (`M digests/DISCOVERIES.md`,
  `?? docs/prompts/Untitled`, `?? docs/repo_overview.html`). I never `cd`'d
  there to write, and ran no `pull`/`reset`/`clean`/`checkout`.
- **Two Streamlit servers** (dashboard 8791, explorer 8792) were launched
  read-only and both stopped. Nothing is left running. Postgres and Phoenix
  were left exactly as found.
- **No paid call, no adapter, no gate opened, nothing promoted to
  `research/evidence/runs/`, no `policy/` change.**

## Verification recorded for this PR

This mission changed two Markdown files and no code, so `pytest`/`ruff` cover
nothing it touched; per `agents/WORKFLOW.md` step 3, content-only work records
its own verification instead. The verification *is* the flight: 36 recorded
commands with exit statuses in `runs/m009/transcript.log`, four Harbor jobs,
six trials, two durable analysis sidecars, one review, six catalog join rows,
and headless renders of both Streamlit surfaces. Every claim in the checkpoint
is traceable to a command in that log, a file path, or a source line.

## For the integrator

Four findings are candidate repair missions, in the order I would fix them:

1. **F-01** — give the analysis lifecycle a CLI (`analyze stage`), so
   `worker-run-one` is reachable without running `evallab nightly` or calling
   the library directly. Owning path: `src/evallab/cli.py`.
2. **F-03 + F-02** — stop the explorer parsing reviews as sidecars, and make
   `analyze review` index itself. Owning paths: `src/evallab/explorer.py`,
   `src/evallab/facts.py`, `src/evallab/cli.py`.
3. **F-04** — make explorer discovery agree with `status` discovery so nested
   `jobs_dir` layouts are not silently dropped. Owning path:
   `src/evallab/explorer.py`.
4. **F-06** — decide whether the M007 workbench should be able to certify the
   four existing in-repo tasks, or whether that is out of its v1 scope and
   should be said so in `docs/task-workbench.md`.

F-05 (no trajectory from free controls) is not repairable under
subscription-only policy. It is the strongest argument I found for M010: until
a qualified stage-5 runtime exists, the lab can demonstrate that a verifier
discriminates, and nothing at all about agent behaviour.

I did not re-derive the integration follow-ups already on record (container
verifier build, Harbor in CI, `initialize()` inside the perf timed region, the
observatory calibration residual). None of them changed as a result of this
flight.
