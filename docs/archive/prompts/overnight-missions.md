---
status: historical
audience:
  - builder
  - operator
---

> **Archived work order**: Completed historical mission set (retired by M001). Living contracts: agents/missions/ACTIVE.md, agents/OWNERS.md, agents/WORKFLOW.md. Board: agents/missions/ACTIVE.md.

# Overnight missions — 2026-08-14 wave

Seven long-running missions (target: 1–2+ hours of real work each), issued by
Peter. Each mission below is the canonical work order; the chat prompt an agent
receives points here. Read first, in order: `AGENTS.md`,
`agents/WORKFLOW.md`, `agents/STRUCTURE.md`, `agents/ROLES.md`, then your
section.

Difficulty ranking (hardest first): AUTOPILOT, ANALYST, JUDGE, INGEST,
OBSERVER, RUNNER, FORGE. Assign the strongest models to the top of the list.

## Setup and git protocol (every role — run and follow exactly)

```bash
cd ~/Developer/eval-lab
git fetch origin
git worktree add .worktrees/<role> -b role/<role> origin/main
#   (if the worktree already exists: cd .worktrees/<role> && git rebase origin/main)
cd .worktrees/<role>
uv sync
```

- All of your work happens on branch `role/<role>` inside `.worktrees/<role>`.
  Never edit the main checkout, never enter another role's worktree, never
  create anything outside the repository (the one-folder law).
- You may write only your owned paths (your section + `agents/ROLES.md` row)
  plus `agents/handoffs/<role>.md`. Everything else is read-only to you.
- Commit small and often on your branch. Integration sequence, every time:
  `git fetch origin && git rebase origin/main` → verify (`uv run pytest -q`
  and `uv run ruff check .` when you touched repo code; recorded verification
  evidence when you produced content) → `git push -u origin role/<role>` →
  `gh pr create --title "<ROLE>: <summary>" --fill`.
- **Squash self-merge is allowed** only when the PR diff touches nothing
  outside your owned paths and your checks are green. Otherwise leave the PR
  open and say why in your handoff.
- Never push to `main`, never force-push anything, never merge or rebase
  another role's branch, never resolve a conflict that involves files you do
  not own — on any conflict: stop, record it in your handoff, continue with
  other mission work.
- Docker Compose services (Postgres, Phoenix) are managed from the main
  checkout only — they are already running for you; do not restart them.

Rules that bind every mission tonight:

- Work in `.worktrees/<role>` on branch `role/<role>` (WORKFLOW setup block).
  BUILDER-track missions (OBSERVER, ANALYST, AUTOPILOT) also use worktrees —
  `main` belongs to the integrator tonight.
- **Shared files** (`src/evallab/cli.py`, `src/evallab/schemas.py`,
  `compose.yaml`, `pyproject.toml`): additive-only, smallest possible diff
  (one registration line, one model, one service block). New logic goes in
  new modules you own. Rebase onto `origin/main` before every PR.
- Verification before any PR: `uv run pytest -q` and `uv run ruff check .`
  clean; content-only missions record their own verification evidence.
- Billable model calls happen **only** via `evallab submit` through the
  queue, governed by `policy/standing-approvals.yaml` (ceilings: $20/day,
  $3/job). Never edit that file. If the ceiling or a missing credential
  blocks you, record it in your handoff and continue with free work — the
  Claude keychain token is currently ABSENT, so `claude-code` and Anthropic
  LLM-judge calls will defer until Peter stores it; `codex` and free
  `oracle`/`nop` runs work now.
- Update `agents/handoffs/<role>.md` (4-line header) at least every ~30
  minutes of work. PR titles: `ROLE: summary`. Python-only repo (AGENTS.md).
- Finish your acceptance criteria, then take items from your continuation
  list. Do not stop early because a sub-goal got hard: pick the next item and
  record the blocker.

---

## INGEST — the frontier benchmark library

**Owns:** `library/benchmarks/`, `agents/handoffs/ingest.md`.

**Mission.** Bring the top-tier, publicly available benchmarks used to
evaluate frontier models in 2026 into this lab in Harbor-runnable form. Quality
bar: benchmarks a frontier-lab eval team would actually cite — not every
GitHub repo with "bench" in the name.

Phases:

1. **Survey (~30 min).** Enumerate candidates with one paragraph each:
   what it measures, who uses it, why it is (or is not) top-tier, and the
   ingestion lane. Candidates to assess at minimum: terminal-bench (already
   pinned here), SWE-bench Verified / SWE-bench Pro, GAIA/GAIA2, OSWorld,
   BFCL, HLE, GPQA-Diamond, AIME, LiveCodeBench, MLE/ML-dev-bench-class,
   tau-bench-class tool-use suites, plus anything current you find that
   clears the bar. Write `library/benchmarks/SURVEY.md`. Reject loudly:
   the rejected list with reasons is half the value.
2. **Ingest lane per accepted benchmark.** Three lanes, in order of
   preference: (a) Harbor Hub datasets — `harbor dataset list`,
   `harbor download` at a **pinned version**; (b) in-repo Harbor adapters
   (the upstream `harbor/adapters/` tree has 60+ — run the adapter with
   `--limit` to produce a starter slice); (c) write a thin adapter yourself
   only when neither exists (follow `library/adapters/quixbugs` as the
   pattern; self-contained package, own pyproject).
3. **Materialize + verify.** For each accepted benchmark:
   `library/benchmarks/<name>/` containing a `MANIFEST.md` (source, version/
   commit pin, license, task count, ingestion lane, resource needs, what
   subset was materialized and why) and the tasks or the pinned reference.
   Where oracle solutions exist, verify a 3–5 task sample with free
   `oracle`/`nop` runs (`-n 2` max) and record results in the manifest.
   Skip GPU/cloud-only content and say so.
4. **Register.** Nominate which benchmarks should join the canary suite and
   which should be first `registered/*` experiment targets, in
   `library/benchmarks/README.md`.

**Acceptance:** SURVEY.md with ≥12 assessed candidates; ≥4 benchmarks
materialized with manifests and sample verification; rejected list with
reasons; zero unpinned sources.

**Continuation:** materialize the next accepted benchmark; deepen slices of
existing ones (more tasks, more verified samples); draft adapter for one
no-lane benchmark.

---

## OBSERVER — tracing and observability

**Owns:** `src/evallab/tracing.py`, `docs/prompts/08-*.md`,
`agents/handoffs/observer.md`; additive lines in `compose.yaml`,
`cli.py`, `pyproject.toml` (dependency group `observability`).

**Mission.** Brief 08 from `docs/design-additions.md`, built to completion:
every trajectory and researcher call inspectable on a timeline.

1. Phoenix service in `compose.yaml` (image digest pinned, ports
   127.0.0.1-bound, volume; verify env/ports against current Phoenix docs).
2. `evallab trace <trial-or-job>`: convert ATIF via `harbor-atif2otel`,
   ship OTLP to Phoenix. Handle missing/invalid trajectories with a clear
   message, not a stack trace. Use `research/explorations/harbor-021/`
   (RECON's atif2otel demo + fixtures) as your starting material.
3. Auto-ship: completed billable trials get traced during nightly; free
   control runs only with a flag.
4. OpenInference instrumentation wired into any lab code that calls LiteLLM
   or DSPy (dormant until those run — that's fine; prove it with a stub).
5. `docs/observability.md`: what lands where (Phoenix vs digests vs
   `harbor view` vs Streamlit), how to read a trace, retention noted.

**Acceptance:** `docker compose up phoenix` + `evallab trace` on an
existing run in `runs/` (or a control run you produce) shows a span tree in
Phoenix; tests cover the converter path with a fixture trajectory (no live
Phoenix needed in CI); pytest+ruff clean.

**Continuation:** trace the queue executor itself (spans per dispatch);
Grafana-free latency/cost summaries into the digest from trace data.

---

## ANALYST — the analysis engine

**Owns:** `src/evallab/atif.py`, `src/evallab/facts.py`,
`src/evallab/cohort.py`, `research/analysis/`,
`docs/prompts/01–03` copies, `agents/handoffs/analyst.md`.

**Mission.** Briefs 01–03: turn raw runs into queryable facts and auditable
findings, with everything **associated** — experiment → job → trials →
trajectories → analyses must be one join path, so "show me this experiment's
data and what the model thought about it" is a query, not archaeology.

1. **ATIF → Parquet (brief 01).** Deterministic projection: trajectories,
   steps, tool calls, token/cost per step. DuckDB queries over it; original
   ATIF stays canonical. Rebuild-from-raw test proves the contract.
2. **Deterministic facts (brief 02).** Per trial: rewards, exception class,
   durations, token/cost, tool-use counts, command failures, artifact
   digests — extracted into the catalog + Parquet, reproducibly.
3. **Cohort compare (brief 02).** `evallab compare <cohort-spec>`:
   pass@1/pass@k with Wilson intervals, paired-by-task where applicable,
   exceptions reported beside the denominator, machine-readable output +
   readable table. Refuses cohorts that differ in more than the declared
   variable.
4. **Model-assisted stage (brief 03).** The `analysis-loop.md` stage-5
   sidecar, as pydantic models: bounded rubric, structured output, evidence
   citations by path + step id, provenance digests. Invocation via headless
   CLI agent (`codex exec`) with validation-retry-once. Live calls go through
   the queue under `researcher-followups`; build and test with stubs first.
5. Wire the association keys into `sql/schema.sql` idempotently
   (experiment_id on jobs; analysis records referencing trial ids).

**Acceptance:** existing runs in `runs/` + `research/evidence/runs/` are
ingested end-to-end: Parquet exists, `compare` produces a correct table for
the oracle-vs-nop control cohort, one stub-model sidecar validates and lands
in the catalog with its provenance; rebuild-from-raw test passes.

**Continuation:** failure-taxonomy auto-labeling proposals against
`research/calibration/trajectory-labels/` as ground truth (report agreement,
don't overwrite labels); DuckDB views for the dashboard.

---

## RUNNER — real experiments through the queue

**Owns:** `research/experiments/` (specs, journal, findings notes),
`agents/handoffs/runner.md`. Touches no `src/`.

**Mission.** Be the lab's first working scientist: design, submit, monitor,
and interpret real experiment runs — through the queue only.

1. **Design (~45 min).** Write 4–6 experiment specs under
   `research/experiments/specs/`, each with hypothesis, one variable, fixed
   conditions, attempts (≥5 for any comparison), est cost within the $3/job
   ceiling. First studies should use what exists: codex on
   `library/tasks/` + verified `library/curated/` slices (e.g. "codex
   pass@5 across the 5 canary-nominated curated tasks", "attempt-count
   sensitivity on transaction-reconciliation", "instruction-preamble A/B via
   --extra-instruction-path"). Note per spec which policy rule admits it —
   `registered/*` scope questions go in the handoff for Peter, they are not
   yours to stretch.
2. **Submit + monitor.** `evallab submit` each admissible spec; watch
   dispatch/deferral/completion in `queue/events.jsonl`; free oracle/nop
   baselines for every task family you test.
3. **Journal.** `research/experiments/JOURNAL.md`: one entry per submitted
   spec — what, why, status, result summary, links to job dirs and digests.
   This is the "everything associated" thread a human reads top to bottom.
4. **Interpret.** For completed runs: results table (with n and intervals —
   no claims from n=1), trajectory observations from actually reading the
   agent logs, task-vs-agent-vs-harness failure attribution, and the next
   spec each result implies.

**Acceptance:** ≥4 specs written; every admissible one submitted; ≥1 real
codex study completed and interpreted in the journal with links; deferred/
refused specs documented with their reason codes.

**Continuation:** widen to INGEST's newly materialized benchmarks as they
appear on `main`; design the first claude-code comparison to run the moment
the keychain token exists.

---

## AUTOPILOT — the 24/7 discovery loop

**Owns:** `src/evallab/researchers.py`, `digests/DISCOVERIES.md`,
`docs/prompts/12-*.md` (fleet) if untaken, `agents/handoffs/autopilot.md`;
additive edits to `automation.py`/`cli.py` (rebase carefully — the
integrator landed credential-aware health there tonight).

**Mission.** Make the lab produce *nontrivial, compounding findings* while
nobody watches — the researcher roles from `docs/design-additions.md` §2.3,
implemented with hard bounds.

1. **Researcher invocations.** `researchers.py`: analyst / synthesizer /
   proposer roles as headless CLI-agent calls (`codex exec` JSON out,
   pydantic-validated, one retry, wall-clock timeout, per-role daily call
   caps, cost attributed against the daily ceiling). No execution
   capability: output is sidecars and `queue/proposed/` specs only.
2. **Discovery journal.** `digests/DISCOVERIES.md`: append-only, each entry =
   claim + evidence links + which prior entry it builds on (or "new
   thread"). The compounding requirement is structural: the proposer's
   prompt receives the current journal tail and must either extend a thread
   or justify a new one. Entries are drafts until a human or a validated
   analysis confirms them; mark status.
3. **Schedule integration.** Nightly (and tick, if budget allows) runs a
   bounded researcher pass after ingestion: analyze yesterday's completed
   trials → synthesize → propose. Quarantine and STOP semantics inherited —
   never bypass the doctor.
4. **Fleet section in the digest** (brief 12 lite): roles, queue funnel,
   spend vs ceiling, deferrals — so the morning digest shows both what the
   lab did and what it's waiting on.

**Acceptance:** with stub agents, the full loop is tested end-to-end;
with real codex, one bounded researcher pass runs tonight within policy and
its outputs (sidecars, proposals, journal entries) validate; tomorrow's
digest contains the fleet section and any discoveries with evidence links.

**Continuation:** proposal dedup vs config digests; journal-thread aging
(mark threads dormant when evidence stops accruing).

---

## FORGE — engineering quality and performance

**Owns:** `.github/`, `docs/engineering.md`, `agents/handoffs/forge.md`;
config-level PRs to `pyproject.toml` (marked `FORGE:`, not self-merged).

**Mission.** Make the lab's code fast, typed, and coherent — without
colliding with the three missions writing `src/` tonight.

1. **Measure first.** Profile the real paths: ingest of the existing runs
   corpus, digest render, queue tick with 100 synthetic specs, fleet-status.
   Write numbers into `docs/engineering.md` — they are the baseline all
   optimization claims cite.
2. **CI hardening.** GitHub Actions: pytest + ruff + (new) `ty` or mypy type
   check on PRs; cache uv; keep runtime under ~3 min. Type-check config as a
   PR; fix only type errors in files nobody else owns tonight (queue and
   automation are hot — leave them; note findings instead).
3. **Standards doc.** `docs/engineering.md`: the observed conventions
   (pydantic contracts, seam-based DI for tests, immutability rules) plus
   the performance baselines and profiling how-to. Short, factual, cited.
4. **Surgical wins only.** Uncontested files: obvious O(n²)→O(n) or
   redundant-I/O fixes with a micro-benchmark in the PR description.
   Anything structural becomes a held PR titled `FORGE-HOLD:` for daytime
   review.

**Acceptance:** CI runs on PRs with type-checking; engineering.md exists with
real measured baselines; every optimization PR carries before/after numbers;
zero edits to files another role changed tonight.

**Continuation:** pre-commit config PR; test-speed pass (fixture reuse);
coverage report wired into CI artifacts.

---

## JUDGE — calibration and the first DSPy experiment

**Owns:** `src/evallab/calibrate.py`, `docs/prompts/09-*.md`,
`research/calibration/` (additions only — EVIDENCE's corpus and labels are
read-only ground truth), `agents/handoffs/judge.md`.

**Mission.** Brief 09: no judged dimension is reportable until its judge has
a measured agreement number.

1. **`evallab calibrate <family>`.** Run a family's judge rubric over the
   labeled corpus in `research/calibration/`, compare with the sealed answer
   keys, write a calibration record (judge model, rubric digest, corpus
   digest, per-criterion agreement, date) to the catalog +
   `research/calibration/records/`. Stub-model tests prove the plumbing.
2. **Live calibration.** Anthropic-judge calls need the absent keychain
   token, so: prepare the queue specs, and ALSO run the calibration with a
   codex-backed judge (Reward Kit supports agent judges) so a real number
   exists tonight. Report both paths' status honestly in the record.
3. **DSPy experiment 1 (design + dry run).** Recast one rubric as a DSPy
   program with the calibration corpus as metric, held-out controls the
   optimizer never sees. Implement and test with a stub LM; the billable
   optimization run is a queue spec marked ready, executed when budget and
   credentials allow.
4. **Gate wiring.** The `calibrated_judges_only` requirement in the policy
   engine should read your calibration records; coordinate by writing the
   record format into `schemas.py` (additive) and documenting the floor
   (≥0.9 agreement) in `docs/prompts/09`.

**Acceptance:** calibrate CLI tested end-to-end with stubs; ≥1 real
calibration record produced tonight (codex-judge path); DSPy experiment
implemented, dry-run tested, queue spec staged; honest record of what waits
on the Claude token.

**Continuation:** second family calibration; inter-judge agreement (codex vs
claude judge on the same corpus) the day the token exists.
