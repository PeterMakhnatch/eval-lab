# Eval Lab instructions

## Engineering rules

- Do not write repository unit tests. Add deterministic simulation scenarios and invariants instead. Preserve benchmark graders and reference controls.
- Use seeded, single-threaded simulation. Simulate time and randomness. Inject faults at external service boundaries. Record every failing seed and replay it in every gate.
- Do not add code comments or docstrings. Keep functional directives, shebangs, and license notices. Express critical assumptions as assertions.
- Use [TigerStyle](https://github.com/tigerbeetle/tigerbeetle/blob/main/docs/TIGER_STYLE.md). Assert preconditions, postconditions, and invariants. Bound loops and queues. Prefer simple control flow. New functions must have at most 70 lines.
- Use the standard library or small in-repo code. Keep essential native and service dependencies. Get Peter's approval before adding a dependency. Keep hashes locked and apply the release-age delay.
- No documentation directories, repository skill directories, or nested agent instructions. This file is the only agent-facing instruction file.
- Put new systems and infrastructure in top-level packages in this repository, never in new repositories. Update the placement list when you add a root.
- Use Python for application code, adapters, verifiers, and first-party task authoring. Shell, SQL, Dockerfiles, and configuration are allowed.

## Change and land

1. Keep the primary checkout on `main`. Create an owned branch and Git worktree from current `origin/main`. Work only in that worktree.
2. Run `make sync`. Edit only the assigned scope. Do not reset, stash, overwrite, or delete another writer's work.
3. Run `make gate`. For a failing simulation seed, reproduce it with `uv run python -m evallab.sim check` and retain its regression entry.
4. Commit only your changes. Run `make land` from the clean worktree. It serializes landing, rebases onto `origin/main`, runs the gate, and pushes to `main`.

On demand:
- `scripts/gates/lessons` checks statistical evidence, lineage, and freshness.
- `make line-replay` runs the frozen detector replay. For changes that affect Line decisions, run `make line-gate` with the private replay set. Missing data is not a pass. Use `make line-gate ARGS=--paths` for path discovery only.
- `make smoke` checks the local services and execution path. `make controls` runs local Oracle and nop controls. Use both only in an owned environment.
- `uv run python -m evallab.sim hunt --seconds N` searches for new failing seeds. Set a bounded time budget.

## Placement and contracts

- `src/` holds the platform. Register CLI routes in domain parser modules, not `cli.py`. Keep expensive imports in command handlers. Report usage errors without a traceback.
- Keep existing module placement unless the change specifies a migration. Do not create parallel implementations in reserved namespaces.
- `src/evallab/data/` contains small, immutable runtime assets. Do not store downloads or local state there.
- `src/evallab/evidence/` validates ATIF and extracts deterministic facts. Never run models or interpret results there. Interpretation cites bounded, immutable evidence. Machine judgments are not acceptance decisions.
- `src/evallab/schemas/` holds strict, side-effect-free contracts. Preserve persisted formats unless you explicitly version or migrate them. `sql/schema.sql` owns the catalog schema. Ingestion is deterministic and idempotent.
- `src/evallab/storage/paths.py` owns storage paths and zones. Attachment and disposition give explicit reasons. Recovery records redact secrets before serialization and hashing; missing required evidence blocks certification.
- `library/` holds versioned task supply. Never change registered task bytes or pinned verifier payloads in place. Make a new version. Preserve imported benchmark contracts.
- `research/` holds studies and their provenance. Cite the source runs and corpus digest. Preserve frozen prompts, thresholds, labels, cohorts, and held-out exclusions unless the change explicitly authorizes them.
- `policy/` holds operating policy and gate inputs. `scripts/` holds operator commands. `sql/` holds the schema and views. `containers/` holds service definitions.
- `tools/` holds isolated, locked uv projects that the platform invokes as subprocesses. Do not import them into the platform. `authoring/` holds templates; `grids/` holds declared experiment inputs; `digests/` holds curated reports.
- `tidegauge/` reads Harbor job directories and reports audits (with seat health checks), scores with rerun and task error bars, paired comparisons, rank intervals, run plans, team decompositions, run timelines against a plan (`process`), and snapshots of running jobs (`live`). It is standard library only, read only, makes no model or network call, and never imports Harbor or `evallab`. `src/` must not import it either; call it as `uv run tidegauge ...`. `tidegauge/stats.py` stays pure, and `tidegauge/load.py` and `tidegauge/atif.py` are the only modules that open Harbor files. `python -m tidegauge.sim check` replays its recorded seeds in the gate.
- `muster/` runs and checks multi-seat Harbor tasks: control matrices of scripted seat policies (`muster matrix`), real-agent rosters (`muster run`), exact replay grading (`muster regrade`), and seat attribution and message measures (`muster attribute`, `muster events`). It is standard library only and never imports `evallab`; `src/` must not import it. Only `matrix` and `run` start trials, through the `harbor` command of the Harbor fork named by `MUSTER_HARBOR`. `muster/tasks/` is task supply: the world kit, family templates, generators and built tasks are the exact bytes of the recorded experiments, are excluded from ruff and ty, and change only by a new family version; the gate runs each family's `generate.py --check` and `python -m muster.sim check`. Its studies live in `research/experiments/muster-boxes/`.
- `campaign/` runs a paid multi-arm Harbor campaign from 1 strict plan file (`campaign check|run|status|fill|merge PLAN`): lanes of arms in order, a balance floor plus a reserve per arm that fails closed, reserve and settle lines in the plan's spend ledger, a recorded-cost cap, a STOP file and a no-reward streak, a plan sha256 that must not change while a lane runs, 1 lane lock per lane, and 1 rerun round of unmeasured trials. It is standard library only and never imports Harbor, `evallab`, `muster` or `tidegauge`; it starts trials only through the plan's command template as a subprocess. `src/` must not import it. Example plans live in `campaign/plans/`. `python -m campaign.sim check` replays its recorded seeds in the gate. An arm's processes are found in `ps` only by the forms the arm produces (`--out <out>` for muster, `run -c <out>.job.json` for Harbor, which runs in its own session); a reader that only names the path is ignored, and the runner never signals its own group or an ancestor. Stops never block the tick: a reason for all lanes (STOP file, cap, cap root, streak, signal) sends 1 TERM to the top process of every running arm at once (not to its group, because `uv run` forwards the signal), the runner waits `stop_grace_seconds` (at least 180, above muster's 120 s grace) before it signals Harbor directly, and a lane never starts or settles while such a process lives (`STOP … harbor-alive` / `orphan`). The API key can have its own spend limit below the account credits: the optional `money.key_limit_probe` prints the key's remaining limit or `none`, a start needs the smaller of the 2 numbers, and a key probe that fails stops the lane. The money lock is `<ledger>.lock`, and open reserves of other writers in the same ledger count for `foreign_reserve_window_hours` (default 6); a line without a writer or provider counts, and an amount that is not a number counts as the plan's largest reserve. The lock binds only `campaign` runners: a bash lane that writes the same ledger takes no lock, so `floor_usd` must cover its next reserve (`campaign check` prints the rules for a paid run). Every event line is also appended to `<state_root>/campaign.log`.

## Execution and spend

- Read `task.toml`, `environment/Dockerfile`, and `policy/standing-approvals.yaml` before any trial. Check the task, harness, model, and backend together.
- On Apple Silicon, tasks with GPUs or memory above the current Docker VM limit need a remote backend. An explicit `linux/amd64` image runs under emulation.
- Treat tasks above 8 GiB of memory or at least 3 hours of agent time as local-heavy. Do not use them as canaries. Check actual capacity before dispatch.
- Run free, bounded local Oracle and nop controls without model credentials. Oracle and nop measure validity, not model capability. Do not run local generative inference.
- Get current authorization for paid calls, remote compute, shared service changes, task registration, and external publication. Credentials, old caps, and a successful land are not authorization. Do not relax the standing policy.
- Use a bounded watchdog, exact resource limits, approved isolation, and a fresh workspace per trial. Do not retry on a different backend after execution starts.
- For a paid campaign, use one persistent spend ledger and uniquely tagged owned resources. Reserve costs before allocation. Do not treat unknown billing or cleanup state as zero.
- Pin the task version, agent/model, Harbor revision, and changed variable. Do not expose hidden tests, solutions, or sealed data to the evaluated agent.
- Report missing rewards, timeouts, and infrastructure errors as unmeasured, not as reward zero. Do not start a paid run to verify a code change without approval.

## Evidence retention

- Raw Harbor jobs, registered task versions, and promoted evidence are immutable. `evallab process-job` publishes finished jobs under `~/Developer/eval-lab-results/` outside worktrees.
- Preserve unique raw jobs in `runs/`, durable blobs in `derived/evidence-cas/`, queue leases and append-only events in `queue/`, backups, and drafts. Ignored paths are not disposable.
- Remove a projection only when the source bytes and exact regeneration procedure are available. Do not assume model judgments can be regenerated.
- Never commit secrets, runtime credentials, volumes, unredacted private prompts, or arbitrary raw run directories. Put small approved immutable evidence under `research/evidence/`.
- Retire a worktree only when it is clean, unlocked, inactive, and not in use by a service. Verify its commits and unique ignored evidence survive elsewhere. Never force removal or purge a whole evidence root.
