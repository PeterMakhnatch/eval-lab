# Eval Lab system cartography — 2026-08-15

Cartographer: Grok 4.6 (xAI). Tree: `origin/main` @ `903abe4`
(`INTEGRATION: add system cartographer mission`). Read-only inspection
except this file, `docs/system-cartography.html`, and
`agents/handoffs/system-cartographer.md`. No Harbor, Docker, model, or
paid invocation.

Companion visual: [system-cartography.html](../system-cartography.html).

---

## One-page overview

Eval Lab is a **local evaluation R&D workbench** around Harbor. It is
not a second agent harness and it is **not yet a post-training data
platform**. Harbor executes and verifies. This repo owns task
definitions, experiment intent, the standing-policy queue, immutable
job evidence, a rebuildable PostgreSQL catalog, Parquet facts, bounded
analysis sidecars, and human approval. That is the design in
`docs/architecture.md` and the executable CLI in `src/evallab/cli.py`
(`uv run evallab --help`).

**What Peter can do today (safe, no paid calls).** Select a lab task
under `library/tasks/` (four runnable packages). Submit a free
`oracle`/`nop` spec through `evallab submit`; policy admits
`local-controls` (`policy/standing-approvals.yaml`). `evallab tick`
dispatches. Evidence lands as a Harbor job directory. `ingest` /
`ingest_and_project` index catalog + Parquet. `evallab status` and the
dashboard/explorer render a typed read-only snapshot. Stage-5 analysis
on main is **plan / stub / sidecar / review** — a saved-response stub,
not a live judge. Next experiments live in
`research/experiments/PROGRAM.json` and `STATUS.md`.

**What is live vs not.** Proven live (integrator-flown or committed
promoted jobs): oracle/nop through the queue, canary suite pin, CLI
surface (`doctor`, `submit`, `tick`, `ingest`, `trajectories`,
`compare`, `analyze`, `registry`, `status`, `dashboard`, `trace`,
`fetch`, `gc`, …). Fixture-proven only: smoke compose path, explorer,
compare, registry admission logic, profiles, pipeline invariant.
**Pending in PR (not merged):** M006 analysis worker (`#47` head
`1f4cf6f`), M007 task workbench (`#49` head `c6c35a4`). Blocked: any
`registered/*` billable researcher-followup (registry has **zero**
records). Designed only: FOUNDRY, LADDER, Zone-04 training exports,
cloud/GKE.

**The join path.** `ExperimentSpec.spec_id` → job
`lab-metadata.experiment.spec_id` (`facts.experiment_id`) → Harbor
`jobs.id` / `trials.id` UUIDs → ATIF `document_id` / `source_sha256` →
analysis sidecar + `analysis_invocations.source_trial_id` → proposal
spec in `queue/proposed/`. Task identity is `task_checksum` /
registry digests. Phoenix traces **do not** carry those IDs today
(`src/evallab/tracing.py` has no `trial_id`/`spec_id` attributes).

**Do not call this a post-training platform.** There is no registered
held-out suite, no Zone-04 export product, no training loop, and
`allowed_uses` includes `"training"` only as a schema token
(`docs/task-registry.md`). The GLM-style road in
`docs/path-forward-2026-08.md` is a staged goal (S0–S5). Present
tense: **benchmark engineering + honest measurement**.

---

## 1. What Peter can do today

End-to-end loop that exists on this checkout:

| Step | What | How | Status |
|---|---|---|---|
| Select a task | Four local Harbor packages | `library/tasks/{event-summary,transaction-reconciliation,terminal-bench-html-js-filter,query-optimize}` | proven live (task.toml present) |
| State a hypothesis | Checked-in spec | `research/experiments/specs/**/*.json` as `ExperimentSpec` (`src/evallab/schemas.py`) | fixture-proven + committed specs |
| Admit work | Policy gate | `evallab submit` → `PolicyGate` (`src/evallab/queue.py`); `local-controls` admits oracle/nop | proven live (`tests/test_queue.py`; `docs/operations.md`) |
| Execute | Harbor via executor | `evallab tick` / `run` (oracle/nop only for direct run) | proven live (`docs/checkpoints/2026-08-14.md` integrator flight; `tests/test_smoke.py`) |
| Persist evidence | Immutable job dir | `runs/<job>/` (gitignored) or promoted `research/evidence/runs/` | proven live (two promoted jobs) |
| Index | Catalog + Parquet | `evallab ingest` / `trajectories` → `atif.ingest_and_project` | fixture-proven (`tests/test_pipeline.py`); live compose depends on local Postgres |
| See what ran | Status / explorer / digest | `evallab status`; `dashboard/`; `digests/` | fixture-proven (`tests/test_status.py`, `tests/test_explorer.py`); dashboard UI is visual |
| Analyze | Stage-5 stub path | `evallab analyze plan\|stub\|ingest-sidecar\|review` | fixture-proven (`research/analysis/stub-oracle-analysis.json`; `tests/test_smoke.py`) |
| Compare | Declared cohorts | `evallab compare` | fixture-proven (`tests/test_truth.py`, `research/analysis/control-oracle-vs-nop.json`) |
| Next experiment | Program ledger | `research/experiments/PROGRAM.json` + `STATUS.md` | designed + reviewed notes; several next cells **blocked** |

**Observation.** `uv run evallab --help` on this tree lists 27 command
groups including `dashboard`, `fetch`, `gc`, `registry`, `status`,
`analyze`. That **supersedes** the 2026-08-14 checkpoint claim that
`gc`/`fetch`/`dashboard` were missing from the CLI
(`docs/checkpoints/2026-08-14.md` §Broken). Inference: MENDER/later
waves wired those entry points; the old checkpoint is historical.

**What Peter cannot do today without further work or his own
authority.** Dispatch `registered/*` researcher-followups (zero
registry files; `uv run evallab registry list` prints “No task
records”). Self-register a task. Run a live stage-5 LLM analysis from
main (M006 worker is **pending in PR**). Use M007 workbench (also
pending). Treat runtime `runs/canary-*-20260815/` as a Git-retained
scientific reference (`STATUS.md` says they were reviewed in the
primary store; `PROGRAM.json` `jobs: []`).

---

## 2. Capability matrix (status vocabulary is closed)

Allowed labels: **proven live**, **fixture-proven only**, **pending in
PR**, **blocked**, **designed**.

| Capability | Status | Implementation | Test or persisted evidence |
|---|---|---|---|
| CLI control plane | proven live | `src/evallab/cli.py` | `uv run evallab --help`; `tests/test_cli_audit.py` |
| Oracle/nop queue dispatch | proven live | `queue.py` `Executor`; `runner.py` | `tests/test_queue.py`; `tests/test_smoke.py`; promoted `research/evidence/runs/event-summary-*-evidence/` |
| Standing policy ceilings | proven live | `policy/standing-approvals.yaml` + `PolicyGate` | `tests/test_queue.py`; file itself |
| Canary pin (3 tasks, Codex, k=3) | proven live (pin) / mixed (runs) | `policy/canary-suite.yaml`; `src/evallab/canary.py` | `tests/test_canary.py`; 2026-08-15 scores in `STATUS.md` (runtime `runs/`, **not** Git) |
| Promoted control evidence | proven live | `research/evidence/runs/` | two job trees; `CONTROL_RESULTS.md` |
| Catalog + Parquet unify | fixture-proven only | `atif.ingest_and_project` | `tests/test_pipeline.py` |
| Docker-free smoke compose | fixture-proven only | `src/evallab/smoke.py` | `tests/test_smoke.py`; `docs/operator-demo.md` |
| `evallab status` snapshot | fixture-proven only | `src/evallab/status.py` | `tests/test_status.py` |
| Run/analysis explorer | fixture-proven only | `src/evallab/explorer.py`, `dashboard/explorer.py` | `tests/test_explorer.py`; `docs/run-explorer.md` |
| Dashboard overview | fixture-proven only | `dashboard/app.py`, `projection.py` | `dashboard/tests/`; `docs/operator-demo.md` |
| Cohort compare + power | fixture-proven only | `src/evallab/cohort.py` | `tests/test_truth.py`; `research/analysis/control-oracle-vs-nop.json` |
| Stage-5 stub analysis | fixture-proven only | `src/evallab/facts.py` analyze helpers; CLI `analyze` | stub JSON; smoke test |
| Registry admission logic | fixture-proven only | `src/evallab/registry.py` | `tests/test_registry.py` |
| Registry **contents** | blocked | `library/registry/` is `.gitkeep` only | `evallab registry list` → 0 records |
| Researcher loop | fixture-proven only | `src/evallab/researchers.py` | tests under researchers/unattended; live LLM **blocked** by empty registry + `calibrated_judges_only` |
| Fetch Hub datasets | fixture-proven only | `src/evallab/fetch.py` | `tests/test_fetch.py` (no live Hub in this session) |
| GC plan/apply | fixture-proven only | `src/evallab/gc.py` | `tests/test_gc.py` |
| Trace convert | fixture-proven only | `src/evallab/tracing.py` | `tests/test_tracing.py`; `--dry-run` allowed |
| Phoenix as store | designed (derived view) | `compose.yaml` phoenix service; `docs/observability.md` | compose file; **not** canonical evidence |
| Post-trial analysis worker | pending in PR | GitHub PR #47 (not on this tree) | pending; PR claims worker tests |
| Task-quality workbench | pending in PR | GitHub PR #49 (not on this tree) | pending; PR claims workbench tests |
| FOUNDRY / LADDER | designed | `docs/path-forward-2026-08.md` only | unproven in `src/` |
| Training / SFT export | designed | Zone 04 in `docs/data-architecture.md` | no `derived/curated/` product; unproven |
| Codex capability on canaries | mixed | scored 2026-08-15 jobs (runtime) | `research/experiments/STATUS.md` + baseline md; **not** promoted to `research/evidence/` |
| Oracle/nop as model skill | — | — | **Not model evidence** (`AGENTS.md`, stub analysis text) |

M006 `#47` and M007 `#49` are **open**. This map does not treat their
heads as merged behavior.

---

## 3. Artifacts, IDs, and joins

```text
task package (library/tasks/<name>/)
  task.toml + instruction + environment + tests
  identity: task_checksum (Harbor) / TaskDigests SHA-256 (registry)
        |
        v
ExperimentSpec  (research/experiments/specs/ or queue/*.json)
  spec_id (ULID), name, hypothesis, task, agent, attempts, policy_rule
        |
        v  evallab submit / tick
Harbor job directory  (runs/<job-name>/)
  jobs.id UUID, lab-metadata.experiment.spec_id
        |
        +--> trials/<task>__<id>/
              trials.id UUID, result.task_checksum, agent_info, rewards
              agent/trajectory.json  (ATIF; often absent on oracle/nop)
              document_id, source_sha256, step_id, tool_call_id
        |
        v  ingest_and_project
PostgreSQL catalog (rebuildable)     Parquet derived/parquet/job_id=*/
  experiments.id = spec_id           trajectories, steps, tool_calls,
  jobs.experiment_id ──► jobs.id     observations, trial_facts, …
  trials.job_id ──► trials.id
        |
        v  analyze stub / (M006 pending) worker
analysis sidecar JSON + analysis_invocations.source_trial_id
        |
        v  researchers proposer (fixture) / human
queue/proposed ExperimentSpec  (new spec_id; dedup on config digest)
```

| Hop | Artifact | Stable ID | Owner of truth |
|---|---|---|---|
| Task | `task.toml` tree | `task_checksum` / package digest | Git package; registry record if present |
| Experiment | `ExperimentSpec` JSON | `spec_id` | Git spec + queue copy |
| Job | Harbor `result.json` | `jobs.id` UUID | Job directory |
| Trial | trial `result.json` | `trials.id` UUID | Trial directory |
| Trajectory | ATIF JSON | `document_id`, `source_sha256` | File in trial; Parquet is derived |
| Analysis | sidecar + reviews | `analysis_invocations.id`, source trial UUID | Sidecar file; catalog index |
| Proposal | new spec | new `spec_id`; digest of frozen fields | `queue/proposed/` |

**Implemented join.** `sql/schema.sql` view links
`experiments` → `jobs.experiment_id` → `trials` →
`trajectory_documents` → `analysis_invocations.source_trial_id`.
`facts.experiment_id` reads `lab-metadata.experiment.spec_id`.
`status.py` exposes experiment/job/trial/analysis ids on snapshot
items. Explorer resolves citations to step/tool or marks them invalid
(`docs/run-explorer.md`).

**Missing or weak joins (observation).**

1. `library/registry/` empty → `registered/<id>` cannot resolve.
2. `PROGRAM.json` `references.jobs` is `[]` while STATUS cites runtime
   `runs/`; the scientific ledger does not point at retained job paths.
3. Phoenix OTel conversion (`tracing.py`) has **no** `spec_id` /
   `job_id` / `trial_id` resource attributes — traces cannot join the
   catalog without path archaeology.
4. Proposal specs do not carry `predecessor_analysis_id` in
   `ExperimentSpec` (`schemas.py`).
5. Oracle/nop often lack `agent/trajectory.json` (they write
   `oracle.txt`); trajectory joins are empty by design on controls
   (`docs/observability.md`).

---

## 4. Who interacts with what

```text
Peter
  policy/standing-approvals.yaml, registry approval, DISCOVERIES,
  publication, research direction  (agents/OWNERS.md)
        |
Development agents (lanes)
  Integration | Research | Tasks | Platform
  write only leased paths; never loosen policy
        |
evallab CLI / queue / Executor
        |                      \
        |                       evaluated agents (codex, claude-code, …)
        v                                 |
     Harbor 0.21  ---- Docker env ---- verifier (separate image)
        |
   immutable job dirs ---- ingest_and_project ----+
        |                                          |
        +--> PostgreSQL catalog (index)            +--> Parquet
        +--> optional evallab trace --> Phoenix (derived spans)
        |
GitHub  PRs, CI (quality, typecheck, perf), mission board
        |
Future training infra   NOT present
  would consume a Zone-04 export; no writer exists
```

**Trust / approval.**

| Actor | May | May not |
|---|---|---|
| Peter | Change policy/ceilings; register tasks; accept discoveries; publish | Need to review every diff |
| Integrator | Merge after exact-head green checks; edit ACTIVE.md | Loosen policy; register tasks |
| Lane workers | Write leased paths | Merge themselves; resolve foreign conflicts; paid Harbor run outside queue |
| Evaluated agent | Act inside Harbor env | See `tests/` or `solution/` |
| Policy gate | Admit `local-controls`, `canary/*`, `registered/*`+requires | Admit cloud env or over-ceiling jobs without human |
| Analysis agent (main) | Stub/plan | Call a model; approve a spec |
| M006 worker (pending) | Saved-response / admitted profile | Self-approve; nightly live call |
| Phoenix | Display spans | Be the evidence store |
| Postgres | Index | Be canonical |

---

## 5. Doc vs source contradictions

| Doc claim | Observed on this tree | Call |
|---|---|---|
| `docs/checkpoints/2026-08-14.md`: `gc`/`fetch`/`dashboard` not in CLI | `evallab --help` lists all three | Checkpoint is **stale**. Observation: wiring exists now. |
| `agents/missions/ACTIVE.md`: M006/M007 `ready`, “no worktree/PR” | GitHub `#47` and `#49` **OPEN** | Board is **stale**. Treat PRs as pending, not ready-unstarted. |
| `docs/task-registry.md` example JSON is `state: registered` for event-summary | `library/registry/` has only `.gitkeep`; `registry list` is empty | Example is **normative**, not a live record. |
| `docs/architecture.md` “Next: ingest ATIF… Parquet… analysis gates” | `ingest_and_project`, compare, analyze, registry modules exist | Architecture “now/next” is **behind the code**. |
| `docs/path-forward-2026-08.md` REGISTER/ROSTER/NIGHTLY/FOUNDRY “pending dispatch” | REGISTER CLI exists, empty; nightly CLI exists; FOUNDRY/LADDER/ROSTER **not** in `src/` | Split: REGISTER **fixture-proven, empty**; FOUNDRY **designed**. |
| `agents/ROLES.md` still describes 2026-08-13 role table | `OWNERS.md` is the four-lane system (M001) | ROLES is compatibility; OWNERS wins. |
| `docs/operator-demo.md` “do not compose from `.worktrees/m002-operability`” | That worktree name is historical | Instruction still right in spirit: compose from **main checkout**. |
| RUNNER journal “no Codex trials” | `STATUS.md` records 2026-08-15 scored Codex canaries | Journal is worktree-local and **stale lab-wide** (STATUS already says this). |

---

## 6. If the end goal is real-environment post-training

**Challenge.** Calling Eval Lab a “post-training data platform” today is
a category error. The repo is an evaluation instrument. Path-forward S3
(data product) and S4 (QLoRA pilot) are explicit **future gates**, not
shipped surfaces.

**What remains if that goal is real.**

| Keep | Why |
|---|---|
| Harbor execution + separate verifier | Verifiable rewards |
| Immutable job/ATIF evidence | Train-set parents |
| Four-zone provenance | Stop leaking eval into train |
| Registry `allowed_uses` + heldout | Contamination control |
| Policy / human registration | No silent benchmark mutation |
| Honest compare (TRUTH) | Know whether S4 moved anything |

**Exact versioned data product that would bridge (absent).** A Zone-04
export: versioned transform `name@version`, parent digests of Zone-02
ATIF + rewards, declared selection/redaction contract, schema pin,
license carry-forward (`docs/data-architecture.md` §Zone 04). Output
shape should be Harbor-trace→SFT-compatible JSONL **plus** a
manifest of excluded trials (harness failures, leaked tests). No such
tree exists under `derived/curated/` (and `derived/` is gitignored).

**Absent.** Held-out registered suite; SFT exporter; training loop;
difficulty-band foundry; contamination audit beyond cards; any GPU
path.

---

## 7. One hour / 30 days / 90 days

**One-hour operator session (today, no Compose from a worktree).**

1. `uv run evallab --help` and `uv run evallab doctor --help`
2. `uv run evallab registry list` and `registry audit` (see 0 records)
3. `uv run python -m evallab.smoke --help` then
   `uv run python -m evallab.smoke --docker-free` if you want the
   fixture compose (writes under a smoke dir; no paid model)
4. `uv run evallab status --help`
5. Read `research/experiments/STATUS.md` and PROGRAM EXP-S01
6. Read this page + open `docs/system-cartography.html`
7. `gh pr view 47` and `gh pr view 49` — review, do not merge as author

**30 days.** Independent review + integrator merge of M006/M007.
Peter registers the three canary tasks **after** candidate packs exist
(or rejects). Harbor version pin in doctor. Trace attributes carry
IDs. First retained eval card from the 2026-08-15 scored set (copy
reviewed extracts into Git; do not pretend `runs/` is archival).
Decide claude-code keychain separately.

**90 days.** Only if 30-day gates hold: foundry qualification **rate**
on mutations (not a flood of unregistered tasks); one Zone-04 dry SFT
export from promoted oracle ATIF; optional TB4 submission of a
qualified task. Still no training unless S4 money + held-out suite
exist.

---

## 8. Harbor skill/contract and Phoenix bridge

### Harbor executable skill / contract — **recommend, now-sized**

**Form.** A version-aware **lab contract**, not a copied Harbor wiki:

- Pin the Harbor **executable** the lab doctor already probes
  (`evallab doctor` reports `harbor` version; 2026-08-14 evidence used
  Harbor 0.21.0).
- Every billable or canary spec records `harbor` version in
  `lab-metadata` (runner already writes tool versions —
  `src/evallab/runner.py`) and fails closed if it drifts from the pin.
- A short `docs/` contract page (or an addition Peter can commission)
  listing **only** the flags this lab actually uses: `--path`,
  `--agent`, `--n-attempts`, `--env docker`, `--extra-instruction-path`
  (exists in Harbor; **not** forwarded by `ExperimentSpec` — Study 03
  blocker).
- Optional later: a Grok/Gemini **skill file** that wraps `evallab
  submit`/`tick`/`summarize` and forbids raw `harbor run` for paid
  agents. Timing: **after** the pin exists, not before.

**Do not** ingest Harbor’s documentation tree into this repo.

### Phoenix trace-evidence bridge — **recommend later, narrow**

Phoenix is installed (`compose.yaml`, `docs/observability.md`) and
`evallab trace --dry-run` is fixture-tested. That is **not** a reason
to make Phoenix the system of record.

**Form when built.** A join bridge, not a second warehouse:

1. On convert, stamp OTel resource attributes `eval.trial_id`,
   `eval.job_id`, `eval.spec_id`, `eval.task_checksum` from the trial
   directory and `lab-metadata`.
2. Keep ATIF + job dir canonical; Phoenix remains a derived span UI.
3. Dry-run in CI; live POST only from main checkout with Phoenix up.

**Timing.** After M006 (so analysis and traces share the same trial
id) and after at least one promoted Codex job with a real
`trajectory.json`. Not this week’s unattended work.

---

## 9. At most six implementation missions

Dependency order. Each is 3–8 hours. Integrator merges; authors stop
at review.

### M-C1 — Canary registry candidates
- **Outcome:** Three `library/registry/<id>.json` files in `candidate`
  state for the canary members, audit-green, **no** `approved_by`.
- **Lease:** `library/registry/`, `docs/task-registry.md` (additive
  note only if needed), a new handoff under `agents/handoffs/` (unproven
  until that mission writes it).
- **Deps:** none (uses existing promoted event-summary evidence;
  txn/html-js may cite documented control paths or stop if evidence is
  not promoted).
- **Acceptance:** `evallab registry list --state candidate` shows 3;
  `audit` has 0 errors; no `state=registered`.
- **Failure:** Missing promoted oracle=1/nop=0 → do not invent jobs.
- **Stop:** Before setting `approved_by` (Peter).

### M-C2 — Harbor pin contract
- **Outcome:** Doctor + a unit test fail if the Harbor executable
  version ≠ a committed pin; job metadata already records the version.
- **Lease:** `src/evallab/runner.py` or `src/evallab/smoke.py` pin
  helper, a new pin test under `tests/` (unproven until written),
  `docs/operations.md` one paragraph, handoff.
- **Deps:** none.
- **Acceptance:** pytest injects a fake `harbor --version` and fails
  the pin; real doctor still prints the version.
- **Failure:** Cannot run Harbor binary in CI → pin check is
  skippable there, required locally.
- **Stop:** Do not vendor Harbor source.

### M-C3 — Trace↔catalog join
- **Outcome:** `evallab trace --dry-run` OTel JSON contains
  `eval.trial_id` / `eval.job_id` / `eval.spec_id` when those exist.
- **Lease:** `src/evallab/tracing.py`, `tests/test_tracing.py`,
  `docs/observability.md` (one subsection), handoff.
- **Deps:** M-C2 useful but not required.
- **Acceptance:** fixture trajectory + fake job metadata; dry-run
  only; no Phoenix POST in tests.
- **Failure:** Missing trajectory → skip with existing one-line
  message, not a crash.
- **Stop:** Do not change Phoenix retention or point it at Postgres.

### M-C4 — Zone-04 SFT dry export
- **Outcome:** A documented, parent-digested JSONL (or Parquet) of
  **promoted oracle** ATIF + reward=1, written under a research path
  or a clearly named derived recipe; no training.
- **Lease:** a new export module under `src/evallab/` (unproven until
  written), tests/fixtures, `docs/data-architecture.md` additive
  section, handoff.
- **Deps:** none (uses `research/evidence/runs` only).
- **Acceptance:** rebuild-from-raw test; manifest lists parent
  SHA-256; oracle-only; nop excluded.
- **Failure:** No ATIF on oracle trial → export empty with reason,
  do not fake steps from `oracle.txt`.
- **Stop:** No GPU, no Hub upload, no `allowed_uses` change.

### M-C5 — Retained canary extracts
- **Outcome:** PROGRAM EXP-S01 `references.jobs` points at
  **committed** digest records (not `runs/`), so the first scored
  Codex night is citable without the workstation disk.
- **Lease:** `research/experiments/` (PROGRAM + a small extracts
  JSON), handoff. No `src/`.
- **Deps:** none.
- **Acceptance:** `validate_program.py` still passes; STATUS links
  match extract digests already quoted in
  `baselines/codex-canary-20260815.md`.
- **Failure:** If baseline file lacks digests, stop and record.
- **Stop:** Do not git-add raw `runs/` trees.

### M-C6 — Post-M006 saved-response flight (blocked until #47 merges)
- **Outcome:** On **main**, one docker-free worker cycle on the smoke
  fixture produces exactly the documented pending/completed records
  and they appear in `evallab status` Analysis.
- **Lease:** handoff + `docs/operator-demo.md` additive paragraph
  only; no worker rewrite.
- **Deps:** M006 merged green.
- **Acceptance:** commands in the M006 handoff reproduced on main;
  zero live model calls.
- **Failure:** If #47 is rejected, this mission is cancelled.
- **Stop:** No nightly live analysis enablement.

---

## Do not build yet

- LADDER 24/7 elicitation generator
- FOUNDRY batch mutation factory
- Kubernetes / ClickHouse / object-store canonicalization
- Any training or QLoRA
- A documentation wiki / Harbor-docs ingest
- Phoenix as system of record
- Live `researcher-followups` while registry is empty
- EXP-N1 (html-js official tests in the agent image) — `STATUS.md`
  withdrew it
- Self-approving analysis or nightly model calls
- Copying curated cards into `registered/` without Peter

---

## Safe demo commands (this mission’s bar)

```bash
uv run evallab --help
uv run evallab doctor --help
uv run evallab status --help
uv run evallab registry list
uv run evallab registry audit
uv run evallab analyze --help
uv run evallab analyze plan --help
uv run evallab compare --help
uv run evallab report --help
uv run evallab trace --help
uv run evallab trajectories --help
uv run python -m evallab.smoke --help
uv run python -m evallab.smoke --docker-free   # fixture compose; no paid model
```

Do **not** from a role worktree: `docker compose up`, `harbor run` of a
paid agent, `evallab tick` against the shared queue, or `evallab
research` hoping for a live judge.

---

## 20-minute teach-back checklist

A mid-level engineer should answer without opening `src/`:

1. What ran? → STATUS EXP-S01 + promoted oracle/nop; runtime canaries
   are not Git.
2. What is running? → board vs `gh pr list` (M006/M007 open); queue
   empty on the STATUS snapshot.
3. What comes next? → PROGRAM NEXT; do not submit withdrawn N1.
4. What tasks exist? → four `library/tasks`; 17 curated **cards**; 0
   registry records; 3 canary pins.
5. Where does analysis appear? → stub sidecar, `analyze *`, explorer
   Analysis, STATUS briefs; worker is pending #47.
6. Who authorizes what? → Peter: policy, register, publish, direction.
   Integrator: merge. Policy file: spend.
7. How could this later produce training data? → Zone-04 export of
   **our** Zone-02 ATIF + rewards, held-out registered suite, then
   rented S4 — none of that is shipped.

If they say “Eval Lab is a post-training platform,” correct them.

---

## Sources (primary)

- `src/evallab/cli.py`, `queue.py`, `atif.py`, `facts.py`, `status.py`,
  `registry.py`, `tracing.py`, `smoke.py`, `schemas.py`
- `sql/schema.sql`
- `policy/standing-approvals.yaml`, `policy/canary-suite.yaml`
- `tests/test_smoke.py`, `test_pipeline.py`, `test_registry.py`,
  `test_status.py`, `test_explorer.py`, `test_queue.py`, `test_tracing.py`
- `docs/architecture.md`, `analysis-loop.md`, `data-architecture.md`,
  `operator-demo.md`, `observability.md`, `operating-manual.md`,
  `path-forward-2026-08.md`, `task-registry.md`, `run-explorer.md`,
  `checkpoints/2026-08-14.md`
- `agents/OWNERS.md`, `agents/missions/ACTIVE.md`, `agents/STRUCTURE.md`
- `research/experiments/PROGRAM.json`, `STATUS.md`
- `research/evidence/runs/`, `research/analysis/stub-oracle-analysis.json`
- GitHub PR `#47`, `#49` (open; pending)
