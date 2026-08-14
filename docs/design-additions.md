# Design additions: one repo, the tool stack, and the unattended research loop

> Author: Claude (Opus), 2026-08-13, written at Peter's direction against
> `165ebeb`, Harbor 0.21.0. This file is additive: it changes no existing code
> or doc. It records two decisions Peter has made, validates the chosen tool
> stack, and specifies — as implementation briefs 05–11, continuing the
> `docs/prompts/` numbering — how to build the unattended research loop. An earlier
> draft lived at `agent-evals/learning/CLAUDE_LAB_DESIGN_ADDITIONS.md`; this
> file supersedes it.

## 0. Decisions taken (by Peter, 2026-08-13)

**D1 — One repository.** This repo, `eval-lab`, is the single home
for the lab: infrastructure, tasks, experiments, analysis, dashboards, docs.
The earlier two-repo idea (separate "task foundry") is rejected. Consequences:

- `agent-evals/harbor-practice` is **frozen source material**: nothing new is
  written there. Its durable assets migrate here in one move (brief 11):
  the tasks and datasets (`tasks/`, `datasets/judged-output`,
  `datasets/adversarial-robustness`), the negative-control corpora under
  `research/experiments/`, the keychain auth scripts (`scripts/with-claude-auth`,
  `claude-token-setup.sh`, `auth-status.sh` — the executor needs these for
  headless billable runs), and the reports. History does not need to migrate;
  the files do, with a pointer left behind.
- Everything an agent or the human needs lives under this one root. No
  cross-repo references in specs, docs, or code.

**D2 — The lab runs unattended, on the clock.** Experiments continue without
Peter's supervision. Human involvement is reduced to: reading a daily digest,
editing a standing-approvals policy file, and occasionally approving proposals
that fall outside it. Section 2 specifies the mechanism; the existing
automation policy in `docs/analysis-loop.md` ("may not invoke a paid model
without acknowledgement") is **amended**: the acknowledgement becomes a
versioned, human-committed policy file rather than a per-run interaction.

## 1. The tool stack, validated

Peter's chosen stack, checked against what this lab actually needs. Verdict
first, role second, correction or caveat third.

| Layer | Tool | Verdict | Role in this lab |
|---|---|---|---|
| Execution | **harbor** | Keep. Already the centerpiece. | Environments, agents, verification, rewards, trials. The lab never re-implements any of it. |
| Optimization | **dspy** | Keep, with a corrected role. | Prompt/program *optimizer* used in bounded experiments (judge optimization first — see brief 09). "Synthetic data generator" is a use pattern (bootstrapped demos), not its identity; it is not general lab infrastructure and nothing else may depend on it. |
| Observability | **arize-phoenix** | Keep. Right choice. | Local, single-container, OTel-native trace UI. Receives (a) Harbor ATIF trajectories via `harbor-atif2otel`, (b) DSPy/LiteLLM calls via OpenInference instrumentation. This is where "why did the agent fail" gets answered visually. |
| Memory | **lancedb** | Keep, sequenced later. | Embedded (file-based, serverless) vector index over *analysis sidecars* — failed attempts, findings, proposals — so researcher agents can ask "have we seen this failure before?" before proposing. It is a **derived, rebuildable index**, never a source of truth. Alternative considered: pgvector in the existing Postgres (one store fewer); LanceDB wins on zero-ops and keeping vectors out of the catalog contract. Do not build until analysis sidecars exist in volume (after brief 03 has produced ~100+). |
| Validation | **pydantic** | Keep. Trivially correct. | Every JSON contract in the lab — experiment spec, queue record, analysis sidecar, proposal, calibration record, digest data — is a versioned pydantic model in one module (`src/evallab/schemas.py`). Harbor itself is pydantic v2; match it. |
| Dashboard | **streamlit** | Keep, with a boundary. | One read-only app over Postgres/DuckDB: leaderboards, canary trends, spend vs. ceiling, queue state, calibration history. It renders; it never writes. Approvals happen via CLI/file moves, not dashboard buttons (v1). Phoenix owns traces; `harbor view` owns single-trial drill-down; Streamlit owns the research overview. |

**Additions the stack list implies but does not name** (all already planned or
cheap):

- **DuckDB + Parquet** — analytical queries over ATIF-derived tables (already
  "next" in `architecture.md`). LanceDB does not replace this; embeddings
  answer "what is similar," DuckDB answers "what happened."
- **harbor-atif2otel** — the existing Harbor package that converts ATIF
  trajectories to OTel spans; the bridge into Phoenix.
- **openinference-instrumentation-dspy** (and `-litellm`) — Arize's
  instrumentation packages, so DSPy optimization runs and Reward Kit judge
  calls land in the same Phoenix UI as agent trajectories.
- **launchd** — the clock. macOS-native, survives reboots, runs in the user
  session (required for Keychain access). No Airflow/Prefect/Temporal; the
  scaling gates in `docs/scaling.md` still apply.
- **uv** — already in use; all new dependencies enter through `uv add` and are
  pinned by `uv.lock`.

Dependency setup (brief 05 executes this; versions resolve at implementation
time and are locked — do not hand-pin from this doc):

```bash
uv add pydantic duckdb pyarrow
uv add harbor-atif2otel opentelemetry-sdk opentelemetry-exporter-otlp
uv add --group dashboard streamlit
uv add --group research dspy openinference-instrumentation-dspy openinference-instrumentation-litellm
uv add --group memory lancedb          # deferred until brief 10
```

Phoenix runs beside Postgres in `compose.yaml` (agent implementing this must
verify current image env/ports against Phoenix docs at build time):

```yaml
  phoenix:
    image: arizephoenix/phoenix:<pin-digest-at-implementation>
    restart: unless-stopped
    ports:
      - "127.0.0.1:6006:6006"   # UI + OTLP/HTTP
      - "127.0.0.1:4317:4317"   # OTLP/gRPC
    volumes:
      - evallab-phoenix:/mnt/data
```

## 2. The unattended research loop

### 2.1 Shape

One clock, one executor, N proposer/analyst agents, one policy file, one
digest. Agents **propose and analyze; only the executor executes.**

```text
                       launchd (user session)
              tick: every 30 min      nightly: 02:30
                        |                  |
                        v                  v
                 evallab tick     evallab nightly
                        |                  |
        +---------------+                  +----------------------------+
        |                                  |                            |
        v                                  v                            v
  drain queue/approved            enqueue canary suite          analysis pass
  within standing policy          (pinned tasks/agents)         (briefs 01-03)
  and daily cost ceiling                                        + researcher agents
        |                                                       draft proposals
        v                                                              |
  harbor run (Docker)                                                  v
        |                                                    queue/proposed/*.json
        v                                                              |
  runs/<job> (immutable) -> ingest (Postgres) -> extract (Parquet)     |
        |                                                              |
        v                                                              v
  events.jsonl  <----------------- every state change ---------- policy check
        |                                                              |
        v                                              in-policy -> queue/approved
  digests/YYYY-MM-DD.md  (committed; the human's daily surface)        |
                                                       out-of-policy -> queue/waiting
                                                              (human: evallab approve <id>)
```

Queue states are directories; a spec is one JSON file named
`<agent>-<ulid>.json`; transitions are atomic `mv` on one filesystem —
that is the entire locking model. `pending → approved|waiting|rejected →
running → done|failed`. A `STOP` file at the queue root halts all dispatch
after the current trial; `evallab stop` / `resume` manage it.

### 2.2 The standing-approvals policy (the autonomy contract)

`policy/standing-approvals.yaml`, committed by Peter, versioned like code. The
executor refuses anything the policy does not cover; covered work runs with no
human in the loop. Initial shape:

```yaml
version: 1
daily_cost_ceiling_usd: 20          # hard stop across ALL billable work
per_job_cost_ceiling_usd: 3
quiet_failure_rule: 3               # N consecutive harness failures -> quarantine billable work
auto_run:
  - name: local-controls            # free; always allowed
    agents: [oracle, nop]
  - name: canary                    # the nightly drift suite
    tasks: [canary/*]               # registered, version-pinned members only
    agents: [codex, claude-code]
    max_attempts: 3
  - name: researcher-followups      # proposals drafted by analyst agents
    tasks: [registered/*]           # any task already in the lab registry
    agents: [codex, claude-code]
    max_attempts: 5
    requires: [schema_valid, dedup_pass, calibrated_judges_only]
escalate_to_human:                  # everything else waits in queue/waiting
  - new_task_registration
  - cloud_or_remote_environment
  - anything_exceeding_ceilings
```

This resolves the tension between `analysis-loop.md`'s "no billable work
without acknowledgement" and D2: the acknowledgement is this file. Editing it
is the human's steering wheel. The executor logs, per spec, which policy rule
admitted it — so every unattended dollar is attributable to a line Peter
committed.

### 2.3 Researcher agents on the clock

The "agents continue to do experiments" part. During `nightly` (and optionally
`tick`, budget permitting), the lab invokes headless CLI agents —
`codex exec` / `claude -p` — in three bounded roles:

1. **Analyst** — per-trial analysis of yesterday's completed trials, producing
   the structured sidecar of `analysis-loop.md` stage 5. Read-only evidence
   bundle in, pydantic-validated JSON out (validation failure → one retry with
   the error message → give up and record the failure).
2. **Synthesizer** — cross-trial pass per cohort (stage 6), same contract.
3. **Proposer** — reads the latest digest, catalog summaries, and (once brief
   10 lands) LanceDB similar-failure lookups; drafts experiment specs into
   `queue/proposed/`. Deduplicated by config digest and, later, semantic
   similarity. Each proposal names its source findings and its one variable,
   per the existing proposal contract.

Hard bounds, enforced by the invoking code, not by prompt text: max invocations
per role per day, max tokens per invocation, wall-clock timeout, and **no
execution capability** — researcher agents never touch Docker, `harbor`, or the
queue's `approved/` directory. Their output is JSON; the policy file decides
what runs. All researcher calls are traced to Phoenix and costed into the same
daily ceiling as everything else.

### 2.4 What the human sees

One committed file per day, `digests/YYYY-MM-DD.md`: jobs run and their policy
rule, rewards vs. 7-day canary baseline, exceptions by taxonomy category,
spend vs. ceiling, disk growth, queue depth, proposals waiting with one-line
rationales, calibration status of judged dimensions. Reading it should take
two minutes; `evallab approve <id>` / `reject <id>` is the only routine
action. The Streamlit app is the pull surface behind it.

### 2.5 Failure containment

- Expired credential / dead Docker / full disk → `doctor --headless` fails →
  the night quarantines itself: nothing dispatches, digest says why. An auth
  failure must produce "nothing ran," never a page of zero-reward trials.
- Ceiling reached → billable dispatch stops; free work (ingest, extraction,
  local controls) continues.
- `quiet_failure_rule` trips → billable work quarantined until a human looks.
- Executor crash mid-trial → Harbor's own trial directory remains the record;
  on restart the executor reconciles `running/` against actual job results
  before dispatching anything new.

## 3. Implementation briefs 05–11

Continuation of `docs/prompts/01–04`. Each is a bounded unit with acceptance
criteria; Codex may copy these into `docs/prompts/` files verbatim. Order matters:
05 → 06 → 07 are the unattended backbone; 08–11 attach to it.

### 05 — Queue + executor + policy gate

Build `src/evallab/queue.py` and extend the CLI with `submit`, `tick`,
`approve`, `reject`, `stop`, `resume`. Directory queue as in §2.1; pydantic
`ExperimentSpec` (extend the existing `research/experiments/*.json` schema with
`submitted_by`, `priority`, `est_cost_usd`, `policy_rule`); policy loader for
`policy/standing-approvals.yaml`; cost ledger check against the catalog;
`events.jsonl` appender. The executor wraps the existing `evallab.runner`
and auto-ingests on completion. Acceptance: two agents submit concurrently
without interference; an out-of-policy spec lands in `waiting/`; a spec past
the ceiling is refused with a reason file; `STOP` halts dispatch; every
transition appears in `events.jsonl`; `uv run pytest` covers the state
machine with a stub runner.

### 06 — Headless doctor, launchd, digest

`doctor --headless` (Keychain item readable, `~/.codex/auth.json` present,
Docker reachable, Postgres up, disk headroom — booleans only, never values;
reuse the migrated `with-claude-auth` sourcing pattern). `evallab schedule
install` writes two LaunchAgent plists (`…tick` every 30 min, `…nightly` at
02:30) running `zsh -lc 'cd <repo> && uv run evallab …'` in the user
session. `evallab digest` renders yesterday from the catalog + events into
`digests/`. Acceptance: with launchd loaded and no human present, a queued
oracle control runs, ingests, and appears in the next morning's committed
digest; with the Keychain locked, the digest reports quarantine and zero
dispatch.

### 07 — Canary suite + drift detection

Register 3–5 pinned canaries: the migrated transaction-reconciliation task,
one adapted terminal-bench task (pin `terminal-bench/terminal-bench@<version>`
via `harbor download`; never `@latest` in a comparison), one more local task.
Nightly enqueue under the `canary` policy rule, 3 attempts. A SQL view
computes trailing-7-day mean ± σ per (task, agent); the digest flags
excursions as *harness-drift suspects* (the `harness_failure` taxonomy row),
explicitly not capability news. Acceptance: canaries run two consecutive
nights unattended; an artificial perturbation (e.g. bumping a task version)
is flagged in the digest.

### 08 — Phoenix + trace shipping

Add the Phoenix compose service (§1). `evallab trace <trial>` converts the
trial's ATIF via `harbor-atif2otel` and ships it OTLP → Phoenix; `--job` ships
all trials of a job; nightly ships completed billable trials automatically.
Wire OpenInference instrumentation into researcher-agent invocations and any
DSPy runs so judge calls and optimizer traffic land in the same UI.
Acceptance: open Phoenix, see a Codex trajectory as a span tree with step
timings, and a researcher-analyst call beside it.

### 09 — Judge calibration, then DSPy experiment 1

Prereq for any judged dimension entering the canary or auto-run sets. Migrate
the judged-output negative-control corpora (brief 11 brings the files);
`evallab calibrate <family>` runs the family's judge over the labeled
corpus, writes a `judge_calibrations` record (judge model, rubric digest,
corpus digest, per-criterion agreement, date); policy `calibrated_judges_only`
gates on the latest record passing a stated floor (≥0.9 agreement).
Then DSPy experiment 1: recast one rubric as a DSPy program, optimize
(MIPROv2/GEPA) against the calibration corpus with held-out controls the
optimizer never sees; success = higher held-out agreement at equal-or-lower
judge cost than the hand-written rubric. Optimized prompts are versioned
artifacts; optimization runs go through the queue like any billable work.
Acceptance: calibration history queryable; the DSPy-optimized judge's
held-out number reported beside the baseline in one short report.

### 10 — LanceDB failure memory

After sidecars exist in volume. `src/evallab/memory.py`: embed analysis
sidecars (summary + category + evidence paths) into a LanceDB table under
`memory/` (gitignored; rebuildable by re-embedding sidecars — assert this with
a rebuild test). Proposer agents query top-k similar failures/proposals before
drafting; semantic near-duplicates are cited in the proposal ("similar to
finding X; differs by Y"). Embedding model choice is the implementer's, but
record it in the table metadata and re-embed wholesale on change. Acceptance:
a duplicate-in-spirit proposal is flagged with its neighbor; `rm -rf memory/`
followed by rebuild reproduces retrieval results.

### 11 — Migration from harbor-practice + Streamlit pane

One PR-sized move: `tasks/`, `datasets/`, control corpora, auth scripts,
reports from `agent-evals/harbor-practice` into this repo (adjust paths;
re-run each migrated task's oracle + nop controls here before it may be
registered; leave a README pointer in harbor-practice marking it frozen).
Then the Streamlit app (`dashboard/app.py`, `uv run evallab dashboard`):
read-only over Postgres/DuckDB — leaderboard per cohort, canary trend, spend
vs. ceiling, queue/proposal state, calibration history. No write path.
Acceptance: migrated tasks pass controls under this repo's runner; dashboard
renders from a cold start with only the catalog running.

## 4. Sequencing and ownership

| Order | Brief | Suggested owner |
|---|---|---|
| 1 | 05 queue/executor/policy | Codex (owns `src/evallab`) |
| 2 | 06 doctor/launchd/digest | Codex |
| 3 | 07 canaries | Codex |
| 4 | 08 Phoenix | any agent |
| 5 | 11 migration (tasks + auth scripts; Streamlit can trail) | Codex + Claude review of migrated verifiers |
| 6 | 09 calibration + DSPy-1 | Claude |
| 7 | 10 LanceDB memory | any agent |

After 05–07 land, the lab is autonomous within policy: canaries and approved
experiments run nightly, digests accumulate, and Peter's involvement is the
policy file and the morning read. 08–10 make the unattended output
inspectable, trustworthy, and non-repetitive, in that order.

Standing rules unchanged from `AGENTS.md` and `analysis-loop.md`: immutable
runs, rebuildable stores, deterministic-before-model analysis, one writer per
tree, no secrets in the repo, and no comparison across changed versions of
anything.
