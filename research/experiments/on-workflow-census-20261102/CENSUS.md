# Eval-Lab Internal Workflow Census: Durable Orchestration Candidates

**Date:** 2026-11-02  
**Context:** Eval-Lab overnight fleet operations, research pipelines, and production services.  
**Purpose:** Evidence-backed census of internal workflows to identify the highest-ROI candidates for durable workflow orchestration (Temporal and similar), prototype selection (F3-F5), and strategic roadmap (F6).

---

## Executive Summary

Eval-lab runs dozens of complex, distributed, multi-hour operations across local worker fleets, cloud sandboxes (Modal), model providers, and background daemons. Today, almost all orchestration is **hand-rolled** in custom Python scripts (`ds_squad.py`, `factory.py`, `regrade.py:CloudSpendGate`, `queue_keeper.py`).

An exhaustive audit of overnight logs (`_ds*.log`), morning reports, verifier receipts (`VF-OV1`, `VF-OV2`), and spend ledgers reveals systematic operational failure modes:
1. **Concurrency Race Conditions:** Git lock contention (`.git/config`) crashing parallel worker initialization (`_ds-ao.log:2-10`).
2. **Timeout Kills Without Reports:** Long batch operations (e.g. `W3B-EXHAUSTIVE-GOLD-CERTIFIER`) killed at wall-clock limits with zero report and unrecovered spend (`.watch-state.json:39`).
3. **Premature Spend Gate Halts:** Inflexible upfront reservation arithmetic in `CloudSpendGate` reserving $0.05/cell, halting batches when only $0.0089 is actually spent (`T3-SAFEGUARD-BENCH/report.md:73`).
4. **Ephemeral Receipt Storage:** Critical per-run readings left in `/tmp` that cannot survive a reboot (`VF-OV1/report.md:42,93`).
5. **Severe Merge Train Toil:** Coordinators spending 7+ hours manually babysitting 22 PR rebases and CI polling loops because ad-hoc merge agents fail (`fable-2/_ds-fb2.log:10-12`).

Durable workflow orchestration directly eliminates these failure modes by providing state persistence across process crashes, first-class timers/signals, deterministic retry policies, and centralized quota management.

---

## Census & Scoring Matrix (16 Workflows)

Each workflow is evaluated across seven 0-3 quantitative dimensions (Total Score max 21):
- **Fail (Failure Frequency):** 0 = rare, 1 = weekly, 2 = multiple per shift, 3 = near-constant/every run.
- **Toil (Human Toil):** 0 = fire-and-forget, 1 = occasional, 2 = active polling/restarts, 3 = constant babysitting.
- **Cost (Cost of Failure):** 0 = trivial $0, 1 = <10m lost, 2 = $10-$50 or hours lost, 3 = ruined shift/quota blowout.
- **State (Statefulness/Duration):** 0 = <1m, 1 = 1-15m, 2 = 15-120m, 3 = multi-hour / continuous daemon.
- **Retry (Retries, Timers & Signals):** 0 = linear script, 1 = basic retry, 2 = timeouts+retries, 3 = complex signals/approvals/timers.
- **Limits (Parallelism & Quota Limits):** 0 = unconstrained, 1 = simple pool, 2 = multi-provider caps, 3 = dynamic quota/rate throttling.
- **Audit (Auditability & Provenance):** 0 = ephemeral stdout, 1 = basic logs, 2 = structured receipts, 3 = cryptographic hashes/Wilson CIs.

| Rank | ID | Workflow Name | Owner Today | Fail | Toil | Cost | State | Retry | Limits | Audit | Total |
|:---:|:---:|:---|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **1** | `WF-01` | Multi-Agent Squad Dispatch & Worker Lifecycle | Overnight Coordinator / ds_squad.py daemon | 3 | 3 | 3 | 3 | 3 | 3 | 3 | **21** |
| **2** | `WF-03` | Corpus-Scale Certify-Gold & STING Mutation Pipeline | CLI evallab certify-gold / overnight subagents | 3 | 2 | 3 | 3 | 3 | 3 | 3 | **20** |
| **3** | `WF-02` | Automated PR Merge Train & Spec Harmonization | Research Coordinator / manual gh CLI loops | 3 | 3 | 3 | 2 | 3 | 2 | 3 | **19** |
| **4** | `WF-05` | Cloud Spend Gate & Dynamic Budget Ledger | evallab.regrade.CloudSpendGate / spend.jsonl | 3 | 2 | 3 | 3 | 2 | 3 | 3 | **19** |
| **5** | `WF-04` | Continuous Causal Trace Ingestion & Outbox Relay | LaunchAgent prod-factory / ContinuousWorkQueue | 2 | 2 | 3 | 3 | 3 | 2 | 3 | **18** |
| **6** | `WF-07` | Provider Quota-Aware Fleet Routing & Rate Throttling | ds_throttle.py, ds_budget_guard.py, queue_keeper.py | 3 | 2 | 2 | 3 | 3 | 3 | 2 | **18** |
| **7** | `WF-08` | Scheduled Morning Report & Recomputation Verifier (VF Fleet) | Coordinator / ad-hoc VF subagents (VF-OV1, VF-OV2) | 2 | 3 | 2 | 2 | 2 | 2 | 3 | **16** |
| **8** | `WF-09` | Judge Station Signal Collection & Human Calibration | FastAPI control_plane.py / human reviewer | 1 | 3 | 2 | 3 | 3 | 1 | 3 | **16** |
| **9** | `WF-11` | Multi-Model Verifier Fan-Out & Consensus Adjudication | swarm_coordinator.py / swarm_bench.py | 2 | 2 | 2 | 2 | 2 | 3 | 3 | **16** |
| **10** | `WF-12` | Live Supervisor & Alarm Actuator | live_supervisor.py / launchd services | 2 | 2 | 2 | 3 | 3 | 1 | 3 | **16** |
| **11** | `WF-10` | Agent Rollout Filtering & SFT Hack-Drop Pipeline | Research scripts / C1-HACKDROP | 2 | 2 | 3 | 2 | 1 | 2 | 3 | **15** |
| **12** | `WF-13` | Task Hardening Package Synthesis & Canary Validation | evallab harden / package_anatomy.py | 2 | 2 | 2 | 2 | 2 | 2 | 3 | **15** |
| **13** | `WF-06` | Live Trace Auto-Review & Cascading Tripwire Monitor | evallab.production.monitor_worker / MisalignmentLiveWorker | 2 | 1 | 2 | 2 | 2 | 2 | 3 | **14** |
| **14** | `WF-14` | Public Corpus Mirror Sync & CAS Blob Ingestion | cloud_trace_retriever.py / asset_store.py | 2 | 1 | 2 | 2 | 2 | 1 | 3 | **13** |
| **15** | `WF-15` | Corpus Clearance, Split Locking & Anti-Leak Audit | corpus_clearance.py / CorpusRegistry | 1 | 2 | 3 | 1 | 1 | 1 | 3 | **12** |
| **16** | `WF-16` | Local Production Factory Deploy, Upgrade & Rollback | install-prod-factory.sh / operator bash | 2 | 2 | 2 | 1 | 2 | 1 | 2 | **12** |

---

## Top 5 Candidates: Deep Rationale & Impact

### 1. `WF-01`: Multi-Agent Squad Dispatch & Worker Lifecycle (Score: 21/21)
**Current Implementation:** ds_squad.py loop executing headless omp -p CLI subprocesses, polling status files, git worktrees  
**Owner Today:** Overnight Coordinator / ds_squad.py daemon  

**Rationale:** Temporal replaces the 458-line ds_squad.py poll-loop with durable Child Workflows per agent, built-in Activity heartbeating, dynamic cancellation signals, deterministic retry policies, and persistent execution state surviving host reboots.

**Smallest Useful Slice:** A single SquadWorkflow managing 2-3 child agent activities with dependency gating, automatic timeout enforcement, and structured result aggregation.

**What Would NOT Benefit:** Ad-hoc single-turn interactive CLI prompts or fast throwaway diagnostic commands (e.g. omp --version).

**Concrete Incidents & Evidence:**
- `research-context/bridge/2026-10-31-overnight/ag-opus/_ds-ao.log:2-10 (Git config lock collision killed 2 tasks simultaneously at start)`
- `research-context/bridge/2026-10-31-overnight/.watch-state.json:39 (W3B-EXHAUSTIVE-GOLD-CERTIFIER killed-time with 0 report)`
- `research-context/bridge/2026-10-30-overnight/OVERNIGHT.md:18-20 (ds_squad runner meter undercounting 9x compared to guard balance)`
- `research-context/bridge/2026-11-02-overnight/CHARTER.md:20-30 (Fragile routing constraints across 5 model pools)`

### 2. `WF-03`: Corpus-Scale Certify-Gold & STING Mutation Pipeline (Score: 20/21)
**Current Implementation:** Synchronous Python CLI scripts driving Modal container sandboxes sequentially or in ad-hoc threads with CloudSpendGate  
**Owner Today:** CLI evallab certify-gold / overnight subagents  

**Rationale:** A CertifyGoldWorkflow coordinates AST mutation activities, fans out parallel Modal execution activities with individual retries, tracks cumulative metered spend rather than static block reservations, and checkpoints progress so crashes resume mid-batch.

**Smallest Useful Slice:** A 1-task 6-gate certification workflow executing reference, nop, and 5 mutant cells in parallel on Modal with durable retry.

**What Would NOT Benefit:** Static AST inspections (evallab tasks anatomy) that complete in <10 ms without containers or spend.

**Concrete Incidents & Evidence:**
- `research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV2/report.md:115 (Gold certifier timed out, leaving 8 trials without report/PR)`
- `research-context/bridge/2026-10-31-overnight/ag-sonnet/raw/T3-SAFEGUARD-BENCH/report.md:73 (CloudSpendGate reservation exhausted $2.50 cap on $0.0089 real spend)`
- `research-context/bridge/2026-10-31-overnight/ag-opus/ds-squad-ao.md:51 (26 cells refused before execution by spend-gate estimate flaw)`

### 3. `WF-02`: Automated PR Merge Train & Spec Harmonization (Score: 19/21)
**Current Implementation:** Manual gh pr checks polling loops, ad-hoc worker prompts (W2-MERGE-TRAIN, T2A-REBASE), manual git rebase and repomap generate  
**Owner Today:** Research Coordinator / manual gh CLI loops  

**Rationale:** A MergeTrainWorkflow queues reviewed PRs, provisions isolated worktrees, runs git rebase origin/main, auto-regenerates repo-map.md, pushes with lease, polls CI activities with backoff, and squash-merges atomically without human presence.

**Smallest Useful Slice:** A 2-PR linear merge train workflow with automatic repo-map regeneration, CI check polling activity, and auto-merge execution.

**What Would NOT Benefit:** Single trivial documentation-only PRs that can already use native GitHub auto-merge safely.

**Concrete Incidents & Evidence:**
- `research-context/bridge/2026-10-31-overnight/fable-2/_ds-fb2.log:10-12 (W2-MERGE-TRAIN failed after 1.3 min, leaving retry loop broken)`
- `research-context/bridge/2026-10-31-overnight/fable-2/ds-squad-fb2.md:1-25 (T2A-REBASE required manual rebase of PR #1691 after #1693 merged)`
- `research-context/bridge/2026-10-31-overnight/MORNING-REPORT-2026-11-01.md:5 (22 PRs required continuous manual coordinator babysitting and merge gating)`

### 4. `WF-05`: Cloud Spend Gate & Dynamic Budget Ledger (Score: 19/21)
**Current Implementation:** In-process threading.Lock + append-only JSON/JSONL ledgers on shared disk  
**Owner Today:** evallab.regrade.CloudSpendGate / spend.jsonl  

**Rationale:** A SpendGateWorkflow/Actor maintains live reservations, receives settlement signals with actual metered costs, releases expired reservations automatically, and dynamically alerts operators before hard failure.

**Smallest Useful Slice:** A centralized budget workflow tracking Modal and LLM reservations with settlement updates and automatic lease expiration.

**What Would NOT Benefit:** Free local zero-cost checks (ruff, pytest, git commands).

**Concrete Incidents & Evidence:**
- `research-context/bridge/2026-11-02-overnight/NARRATIVE.md:41 (CloudSpendGate flat reservation exhausts caps at 1/4 of real spend)`
- `src/evallab/regrade.py:241-298 (Reservations are estimates kept even on failed/short executions)`
- `research-context/bridge/2026-10-17-v7/classifiers-v7-canonical-incomplete-receipt.json:251-280 (49 cumulative reservations stalled runs on $0.26 shortfall)`

### 5. `WF-04`: Continuous Causal Trace Ingestion & Outbox Relay (Score: 18/21)
**Current Implementation:** Local Python service managed by launchd, polling SQLite journal, custom outbox relay to NATS/HTTP  
**Owner Today:** LaunchAgent prod-factory / ContinuousWorkQueue  

**Rationale:** Temporal replaces custom transactional outbox tables and polling replayers with event-driven Signal methods, durable event buffering, and native activity retries, eliminating SQLite write lock contention.

**Smallest Useful Slice:** A TraceIngestWorkflow that ingests raw ATIF trace steps, writes to CAS storage, and reliably signals the live monitor.

**What Would NOT Benefit:** Static offline analysis of a completed trace JSON file on local disk.

**Concrete Incidents & Evidence:**
- `src/evallab/production/factory.py:22-31 (Documented 33 items re-queued 12x and quadratic start-up recovery)`
- `src/evallab/production/outbox_relay.py:1-8 (Hand-rolled at-least-once transactional outbox relay with JetStream pub-ack)`
- `src/evallab/production/corpus_queue.py:20-22 (Leases requiring heartbeat renewal and unguessable fencing tokens)`

---

## Suggested Build Order (Prototypes F3-F5 & Beyond)

1. **Phase 1: Multi-Agent Squad Dispatch & Worker Lifecycle (`WF-01`, Item F3 Prototype)**
   - *Why first:* Solves immediate operational bleeding in nightly fleet runs. Eliminates git worktree race conditions, process-kill zombies, and silent timeout deaths without reports.
   - *Key primitive:* Temporal Child Workflows + Activity Heartbeating + Signal-based continuation prompts.

2. **Phase 2: Automated PR Merge Train & Spec Harmonization (`WF-02`, Item F4 Prototype)**
   - *Why second:* Unblocks developer and coordinator throughput. Replaces hours of manual CI polling and git rebase commands with a reliable, sequential merge queue that automatically updates `repo-map.md`.
   - *Key primitive:* Queued workflow activities polling GitHub Actions status, executing rebase in isolated worktrees, and squash-merging.

3. **Phase 3: Corpus-Scale Certify-Gold & STING Mutation Pipeline (`WF-03`, Item F5 Prototype)**
   - *Why third:* Scales the core eval-lab scientific value proposition (certifying tasks and detecting weak graders). Replaces brittle multi-hour CLI scripts with resilient, parallel Modal sandbox execution and checkpointed progress.
   - *Key primitive:* Dynamic parallel Activity fan-out across Modal sandboxes with per-cell timeout and retry.

4. **Phase 4: Cloud Spend Gate & Dynamic Budget Ledger (`WF-05`)**
   - *Why fourth:* Decouples static reservation estimates from actual metered cloud bills, preventing premature batch shutdowns while maintaining strict financial safety.
   - *Key primitive:* Stateful Workflow / Entity actor maintaining balance, reservations, and settlement signals.

5. **Phase 5: Continuous Causal Trace Ingestion & Outbox Relay (`WF-04`)**
   - *Why fifth:* Upgrades the 24/7 background production infrastructure from hand-rolled SQLite transactional outbox polling to robust event streaming with zero loss.
   - *Key primitive:* Event-driven Signal ingestion, durable message deduplication, and streaming outbox dispatch.

---

## Explicit Non-Candidates (Do NOT Put in a Workflow Engine)

Not every task benefits from durable orchestration. Simple scripts, sub-second CLI commands, and pure in-memory operations are actively harmed by the latency and overhead of a workflow engine.

### Deterministic CLI Linters & Repo Checks (ruff, repomap, governance)
- **Why Not:** These tools run locally in sub-second to a few seconds, have zero state, zero external network dependencies, and require no retry logic or signals. Wrapping them in a durable orchestrator introduces pure latency and complexity overhead with zero reliability benefit.
- **Recommended Runtime:** Local shell execution or pre-commit hook via uv run.

### Read-Only Dashboard & Viewer Servers (results-viewer, scout-view)
- **Why Not:** These are read-only HTTP/Streamlit servers serving static files or querying SQLite. They do not have multi-step lifecycles, long-running batch executions, or persistent workflows. Standard system service managers (launchd / systemd) handle keep-alive and restart on failure adequately.
- **Recommended Runtime:** LaunchAgent / background daemon with system keepalive.

### Local Fast Unit Tests (pytest unit tests)
- **Why Not:** Targeted unit tests run in seconds in memory. Durable execution is designed for operations that survive process crashes, reboots, and multi-hour network calls, not short test suites.
- **Recommended Runtime:** Direct CLI (pytest -q).

### Static Package & Vulnerability Parser (evallab tasks anatomy)
- **Why Not:** Model-free, container-free AST and regex parser running in 3.44 ms p50. It produces deterministic JSON cards in-process with no statefulness.
- **Recommended Runtime:** Standard Python in-process library call.

### Single-Branch Git Mirror Sync (sync-primary.sh)
- **Why Not:** A 10-line bash script running git fetch && git fast-forward on a 60-second cron interval. A failure is simply retried on the next tick; there is no intermediate state to preserve.
- **Recommended Runtime:** Simple cron / launchd interval timer.

---

## Complete Candidate Registry (All 16 Workflows)

### `WF-01`: Multi-Agent Squad Dispatch & Worker Lifecycle
- **Owner:** Overnight Coordinator / ds_squad.py daemon
- **Runs On Today:** ds_squad.py loop executing headless omp -p CLI subprocesses, polling status files, git worktrees
- **Total Score:** 21/21 (Fail=3, Toil=3, Cost=3, State=3, Retry=3, Limits=3, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* Git worktree lock race (.git/config lock) crashed simultaneous launches of T1-BUILD-ANATOMY and T2-UNIFIED-HARDEN (_ds-ao.log:2-10); worker timeout kills without reports (.watch-state.json:39).
  - *human_toil:* Coordinator must babysit runs all night, watch swap memory, manually restart dead workers, and append continue items to plan.json.
  - *cost_of_failure:* Killed workers lose up to 60m of model time ($5-$10/run), exhaust subscriber quotas, and risk uncollected PR deliverables.
  - *statefulness:* Squad runs 4-8 hours across waves with sequential dependency graphs (needs, needs_report).
  - *need_for_retries_timers_signals:* Requires external file-based kill signals (RUN/STOP, gate files), wall-clock timeouts (max_minutes), and retry continuation prompts.
  - *parallelism_limits:* Strict limits: at most 4 concurrent omp -p processes, swap memory <12 GB, model budget caps, provider token ceilings.
  - *auditability:* Must record attested provider/model per assistant turn, token usage, cost accounting in spend.jsonl, and cryptographic prompt/receipt hashes.
- **Durable Workflow Value:** Temporal replaces the 458-line ds_squad.py poll-loop with durable Child Workflows per agent, built-in Activity heartbeating, dynamic cancellation signals, deterministic retry policies, and persistent execution state surviving host reboots.
- **Smallest Useful Slice:** A single SquadWorkflow managing 2-3 child agent activities with dependency gating, automatic timeout enforcement, and structured result aggregation.
- **What Would Not Benefit:** Ad-hoc single-turn interactive CLI prompts or fast throwaway diagnostic commands (e.g. omp --version).
- **Citations & Incidents:**
  - `research-context/bridge/2026-10-31-overnight/ag-opus/_ds-ao.log:2-10 (Git config lock collision killed 2 tasks simultaneously at start)`
  - `research-context/bridge/2026-10-31-overnight/.watch-state.json:39 (W3B-EXHAUSTIVE-GOLD-CERTIFIER killed-time with 0 report)`
  - `research-context/bridge/2026-10-30-overnight/OVERNIGHT.md:18-20 (ds_squad runner meter undercounting 9x compared to guard balance)`
  - `research-context/bridge/2026-11-02-overnight/CHARTER.md:20-30 (Fragile routing constraints across 5 model pools)`

### `WF-03`: Corpus-Scale Certify-Gold & STING Mutation Pipeline
- **Owner:** CLI evallab certify-gold / overnight subagents
- **Runs On Today:** Synchronous Python CLI scripts driving Modal container sandboxes sequentially or in ad-hoc threads with CloudSpendGate
- **Total Score:** 20/21 (Fail=3, Toil=2, Cost=3, State=3, Retry=3, Limits=3, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* W3B-EXHAUSTIVE-GOLD-CERTIFIER timed out at 60m (.watch-state.json:39); spend gate reservation exceeded ledger cap prematurely (T2-UNIFIED-HARDEN/report.md:44).
  - *human_toil:* Requires manual continuation ledger creation (e.g. t3-safeguard-bench-ledger-continuation.json) and setting up rerun matrices.
  - *cost_of_failure:* Aborted runs lose container sandbox charges, leaving tasks half-certified (e.g. K6 left task 000232 unproven in NARRATIVE.md:42).
  - *statefulness:* Multi-phase pipeline runs 1-4 hours (AST mutation -> reference run -> nop run -> witness run -> synthesized tests -> held-out evaluation).
  - *need_for_retries_timers_signals:* Requires per-cell retry with exponential backoff for Modal network hiccups, per-task timeout timers (300s), and completion signals.
  - *parallelism_limits:* Bounded by Modal concurrent container quotas, CPU/RAM container allocations, and spend reservation rate limits.
  - *auditability:* Must record immutable SHA-256 package digests, mutant AST patches, execution exit codes, and 95% Wilson confidence intervals.
- **Durable Workflow Value:** A CertifyGoldWorkflow coordinates AST mutation activities, fans out parallel Modal execution activities with individual retries, tracks cumulative metered spend rather than static block reservations, and checkpoints progress so crashes resume mid-batch.
- **Smallest Useful Slice:** A 1-task 6-gate certification workflow executing reference, nop, and 5 mutant cells in parallel on Modal with durable retry.
- **What Would Not Benefit:** Static AST inspections (evallab tasks anatomy) that complete in <10 ms without containers or spend.
- **Citations & Incidents:**
  - `research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV2/report.md:115 (Gold certifier timed out, leaving 8 trials without report/PR)`
  - `research-context/bridge/2026-10-31-overnight/ag-sonnet/raw/T3-SAFEGUARD-BENCH/report.md:73 (CloudSpendGate reservation exhausted $2.50 cap on $0.0089 real spend)`
  - `research-context/bridge/2026-10-31-overnight/ag-opus/ds-squad-ao.md:51 (26 cells refused before execution by spend-gate estimate flaw)`

### `WF-02`: Automated PR Merge Train & Spec Harmonization
- **Owner:** Research Coordinator / manual gh CLI loops
- **Runs On Today:** Manual gh pr checks polling loops, ad-hoc worker prompts (W2-MERGE-TRAIN, T2A-REBASE), manual git rebase and repomap generate
- **Total Score:** 19/21 (Fail=3, Toil=3, Cost=3, State=2, Retry=3, Limits=2, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* W2-MERGE-TRAIN failed within 1.3m without report (_ds-fb2.log:10-12); conflicting PRs on docs/repo-map.md and cli.py stall whole queues.
  - *human_toil:* Coordinator manually reviewed and merged 22 PRs across 7 hours (MORNING-REPORT-2026-11-01.md:5), manually rebasing and polling CI.
  - *cost_of_failure:* Blocked merge train stalls all dependent downstream workers (e.g., W2-TB4-UNION-QA blocked waiting for #1691 in fable-2/ds-squad-fb2.md:112).
  - *statefulness:* Sequential train runs 30-120 minutes per batch, waiting for multi-minute GitHub Actions CI checks on every rebase step.
  - *need_for_retries_timers_signals:* Requires polling GitHub checks every 30s, timeout handling for flaky CI, webhook/signal upon CI pass, and automated conflict recovery.
  - *parallelism_limits:* Strictly serialized single-writer queue for origin/main to prevent race conditions on repo-map.md.
  - *auditability:* Needs immutable records of PR head SHA, squash commit SHA, CI run check details URLs, and governance verification.
- **Durable Workflow Value:** A MergeTrainWorkflow queues reviewed PRs, provisions isolated worktrees, runs git rebase origin/main, auto-regenerates repo-map.md, pushes with lease, polls CI activities with backoff, and squash-merges atomically without human presence.
- **Smallest Useful Slice:** A 2-PR linear merge train workflow with automatic repo-map regeneration, CI check polling activity, and auto-merge execution.
- **What Would Not Benefit:** Single trivial documentation-only PRs that can already use native GitHub auto-merge safely.
- **Citations & Incidents:**
  - `research-context/bridge/2026-10-31-overnight/fable-2/_ds-fb2.log:10-12 (W2-MERGE-TRAIN failed after 1.3 min, leaving retry loop broken)`
  - `research-context/bridge/2026-10-31-overnight/fable-2/ds-squad-fb2.md:1-25 (T2A-REBASE required manual rebase of PR #1691 after #1693 merged)`
  - `research-context/bridge/2026-10-31-overnight/MORNING-REPORT-2026-11-01.md:5 (22 PRs required continuous manual coordinator babysitting and merge gating)`

### `WF-05`: Cloud Spend Gate & Dynamic Budget Ledger
- **Owner:** evallab.regrade.CloudSpendGate / spend.jsonl
- **Runs On Today:** In-process threading.Lock + append-only JSON/JSONL ledgers on shared disk
- **Total Score:** 19/21 (Fail=3, Toil=2, Cost=3, State=3, Retry=2, Limits=3, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* CloudSpendGate reserves fixed $0.05/call estimates, halting batches when 25% of dollar budget is spent (NARRATIVE.md:41, T3-SAFEGUARD-BENCH/report.md:73).
  - *human_toil:* Operators must write manual continuation ledgers with synthetic approval strings to resume aborted batches.
  - *cost_of_failure:* Aborts large experiment sweeps mid-flight, leaving incomplete cells that cannot be compared scientifically.
  - *statefulness:* Budgets span across hours and multiple squad waves, needing consistent multi-process tracking.
  - *need_for_retries_timers_signals:* Needs reservation expiration timers, settlement updates from real cloud provider meters, and budget adjustment signals.
  - *parallelism_limits:* High contention across concurrent worktrees competing for shared file locks on spend.jsonl and ledger files.
  - *auditability:* Must strictly comply with standing spend policy (policy/standing-approvals.yaml) and produce unforgeable receipts.
- **Durable Workflow Value:** A SpendGateWorkflow/Actor maintains live reservations, receives settlement signals with actual metered costs, releases expired reservations automatically, and dynamically alerts operators before hard failure.
- **Smallest Useful Slice:** A centralized budget workflow tracking Modal and LLM reservations with settlement updates and automatic lease expiration.
- **What Would Not Benefit:** Free local zero-cost checks (ruff, pytest, git commands).
- **Citations & Incidents:**
  - `research-context/bridge/2026-11-02-overnight/NARRATIVE.md:41 (CloudSpendGate flat reservation exhausts caps at 1/4 of real spend)`
  - `src/evallab/regrade.py:241-298 (Reservations are estimates kept even on failed/short executions)`
  - `research-context/bridge/2026-10-17-v7/classifiers-v7-canonical-incomplete-receipt.json:251-280 (49 cumulative reservations stalled runs on $0.26 shortfall)`

### `WF-04`: Continuous Causal Trace Ingestion & Outbox Relay
- **Owner:** LaunchAgent prod-factory / ContinuousWorkQueue
- **Runs On Today:** Local Python service managed by launchd, polling SQLite journal, custom outbox relay to NATS/HTTP
- **Total Score:** 18/21 (Fail=2, Toil=2, Cost=3, State=3, Retry=3, Limits=2, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* 2026-10-24 service re-queued 33 items 12 times (factory.py:24); quadratic recovery on startup forced minutes of rehydration (factory.py:31).
  - *human_toil:* Requires custom watchdog scripts, LaunchAgent plist maintenance, and manual database recovery after crash.
  - *cost_of_failure:* Duplicate processing, dropped causal trace events, corrupted outbox publication state, and monitor desynchronization.
  - *statefulness:* Runs continuously (24/7 daemon) streaming thousands of trace steps across multi-step agent rollouts.
  - *need_for_retries_timers_signals:* Requires at-least-once outbox delivery with broker pub-ack, deduplication tokens, and lease renewal heartbeats.
  - *parallelism_limits:* Single-writer constraint on SQLite database requires careful serialization of global sequence allocation.
  - *auditability:* Every event must have an immutable sequence number, SHA-256 event ID, and strict quarantine handling for poison messages.
- **Durable Workflow Value:** Temporal replaces custom transactional outbox tables and polling replayers with event-driven Signal methods, durable event buffering, and native activity retries, eliminating SQLite write lock contention.
- **Smallest Useful Slice:** A TraceIngestWorkflow that ingests raw ATIF trace steps, writes to CAS storage, and reliably signals the live monitor.
- **What Would Not Benefit:** Static offline analysis of a completed trace JSON file on local disk.
- **Citations & Incidents:**
  - `src/evallab/production/factory.py:22-31 (Documented 33 items re-queued 12x and quadratic start-up recovery)`
  - `src/evallab/production/outbox_relay.py:1-8 (Hand-rolled at-least-once transactional outbox relay with JetStream pub-ack)`
  - `src/evallab/production/corpus_queue.py:20-22 (Leases requiring heartbeat renewal and unguessable fencing tokens)`

### `WF-07`: Provider Quota-Aware Fleet Routing & Rate Throttling
- **Owner:** ds_throttle.py, ds_budget_guard.py, queue_keeper.py
- **Runs On Today:** Ad-hoc background Python watchdog scripts polling process trees and sleeping
- **Total Score:** 18/21 (Fail=3, Toil=2, Cost=2, State=3, Retry=3, Limits=3, Audit=2)
- **Score Evidence:**
  - *failure_frequency:* Direct Claude 7-Day quota hit 100% at 12:57 AM ET (MORNING-REPORT-2026-11-01.md:84); queue keeper made 91 blocked attempts in 41 min (OVERNIGHT.md:44-45).
  - *human_toil:* Coordinator must inspect quotas manually before overnight shifts and re-route workers away from depleted accounts.
  - *cost_of_failure:* Abrupt 429 or quota stops kill running agent sessions without saving partial work.
  - *statefulness:* Runs across multi-day rolling windows (7-day Claude quota, weekly Devin quota, monthly balance).
  - *need_for_retries_timers_signals:* Requires scheduled sleep timers (waiting for off-peak hours at 6 AM ET), rate-limiting backoff, and quota refresh signals.
  - *parallelism_limits:* Complex matrix: max 4 OMP processes, max 16 nested model calls, provider concurrency limits.
  - *auditability:* Needs attested provider/model tracking to prove no unauthorized model fallback occurred.
- **Durable Workflow Value:** A FleetRouterWorkflow manages global provider rate-limiters, queues worker dispatches during off-peak windows, pauses when quotas near ceilings, and dynamically shifts routes.
- **Smallest Useful Slice:** A token-bucket rate limiter workflow for OpenRouter/DeepSeek with sleep-until-reset capability.
- **What Would Not Benefit:** Static local CPU-only script executions.
- **Citations & Incidents:**
  - `research-context/bridge/2026-10-30-overnight/OVERNIGHT.md:44-45 (Queue keeper looped 91 blocked attempts in 41 minutes on stale routes)`
  - `research-context/bridge/2026-10-31-overnight/MORNING-REPORT-2026-11-01.md:80-84 (Claude 7-day 100% quota burnout mid-shift)`

### `WF-08`: Scheduled Morning Report & Recomputation Verifier (VF Fleet)
- **Owner:** Coordinator / ad-hoc VF subagents (VF-OV1, VF-OV2)
- **Runs On Today:** Morning coordinator compilation, ad-hoc python scripts in /tmp (VF-OV2/report.md:196)
- **Total Score:** 16/21 (Fail=2, Toil=3, Cost=2, State=2, Retry=2, Limits=2, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* Verifiers caught wrong significance tests (p=0.031 vs p=0.109 in VF-OV1/report.md:45), post-hoc definitions in headlines, and lost /tmp receipts.
  - *human_toil:* Coordinator spends 2+ hours every morning gathering numbers, verifying hashes, and reconciling prose claims.
  - *cost_of_failure:* Publishing unverified or post-hoc numbers damages scientific credibility and violates strict reporting contracts.
  - *statefulness:* Runs for 1-2 hours aggregating artifacts produced over the entire 8-hour overnight shift.
  - *need_for_retries_timers_signals:* Requires cron-style scheduling (08:30 ET trigger), collecting outputs from completed squads, and verification fan-out.
  - *parallelism_limits:* Requires independent model verification (e.g. Claude Opus blind recomputation).
  - *auditability:* Extreme: every number in prose must match raw receipt hashes; discordant McNemar pairs; 95% Wilson intervals.
- **Durable Workflow Value:** A MorningReportWorkflow automatically triggers at shift end, runs deterministic metric recomputation activities, executes independent verification checks, validates receipt durability, and formats the report.
- **Smallest Useful Slice:** An automated verification workflow that scans raw receipts, computes McNemar p-values and Wilson intervals, and checks hash consistency.
- **What Would Not Benefit:** Ad-hoc free-form qualitative notes or exploratory scratchpad discussions.
- **Citations & Incidents:**
  - `research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/report.md:42-54 (Caught 2 cited receipts in /tmp/t1pf and post-hoc cascade rules)`
  - `research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV2/report.md:196-198 (Throwaway recomputation scripts left in /tmp/vf-ov2/)`

### `WF-09`: Judge Station Signal Collection & Human Calibration
- **Owner:** FastAPI control_plane.py / human reviewer
- **Runs On Today:** FastAPI ASGI app with SSE stream /api/stream and local SQLite, manual browser UI
- **Total Score:** 16/21 (Fail=1, Toil=3, Cost=2, State=3, Retry=3, Limits=1, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* Human calibration backlog stall (69 uncompleted verdicts in NARRATIVE.md:39); SSE cursor disconnects failing with HTTP 409 (control_plane.py:10).
  - *human_toil:* High manual effort required for humans to review blinded runs and submit verdicts without session state loss.
  - *cost_of_failure:* Without human ground truth calibration, automated LLM monitors cannot be statistically validated.
  - *statefulness:* Human review tasks can remain pending for hours or days waiting for operator availability.
  - *need_for_retries_timers_signals:* Classic human-in-the-loop workflow: needs external Signals to unblock execution, reminder timers, and timeout escalations.
  - *parallelism_limits:* Bounded by human operator bandwidth.
  - *auditability:* Blinded review protocol requires strict redaction of model identities and benchmark labels until submission.
- **Durable Workflow Value:** Temporal handles human-in-the-loop workflows natively: workflows wait durable days for human verdict Signals without consuming server resources or risking state loss on server restarts.
- **Smallest Useful Slice:** A HumanReviewWorkflow that serves a blinded case bundle, waits for an operator verdict Signal, and updates calibration metrics.
- **What Would Not Benefit:** Fully automated batch heuristic scoring with no human in the loop.
- **Citations & Incidents:**
  - `research-context/bridge/2026-11-02-overnight/NARRATIVE.md:39 (69 Judge Station human verdicts pending, stalling calibration)`
  - `src/evallab/production/control_plane.py:8-14 (Cursor reconnection conflicts on HTTP 409, manual intervention tokens)`

### `WF-11`: Multi-Model Verifier Fan-Out & Consensus Adjudication
- **Owner:** swarm_coordinator.py / swarm_bench.py
- **Runs On Today:** Python asyncio/threading fan-out calling Anthropic, Google, and DeepSeek APIs concurrently
- **Total Score:** 16/21 (Fail=2, Toil=2, Cost=2, State=2, Retry=2, Limits=3, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* Out-of-schema responses from Sonnet/Opus required manual rerun (ds-squad-ao.md:234); judge disagreement moves gated arm by ±2 keys (VF-OV2/report.md:241).
  - *human_toil:* Reconciling conflicting model adjudications, patching JSON schema parser errors.
  - *cost_of_failure:* Unreliable consensus scores invalidate expensive benchmark comparisons.
  - *statefulness:* Runs for 20-60 minutes across multi-model ensembles.
  - *need_for_retries_timers_signals:* Needs per-provider timeout, schema validation retries, and fallback to tie-breaking adjudicators.
  - *parallelism_limits:* High: distributes across 3+ providers with distinct concurrency caps.
  - *auditability:* Requires full capture of raw prompt/completion tokens, latency, and consensus voting records.
- **Durable Workflow Value:** A ConsensusAdjudicationWorkflow fans out parallel evaluation activities to multiple model providers, enforces per-provider timeouts, and routes discordant cases to a tie-breaker activity.
- **Smallest Useful Slice:** A 3-model parallel jury workflow that validates outputs against a strict Pydantic schema and returns a majority verdict.
- **What Would Not Benefit:** Single-model prompt evaluations with deterministic output formats.
- **Citations & Incidents:**
  - `src/evallab/production/swarm_coordinator.py:1-60 (Swarm coordinator managing multi-agent round consensus)`
  - `research-context/bridge/2026-10-31-overnight/ag-opus/ds-squad-ao.md:234 (D0009 and D0017 required second attempt due to scorer schema failure)`

### `WF-12`: Live Supervisor & Alarm Actuator
- **Owner:** live_supervisor.py / launchd services
- **Runs On Today:** LaunchAgent polling cycle executing live_supervisor.py, reading SQLite, writing JSONL findings
- **Total Score:** 16/21 (Fail=2, Toil=2, Cost=2, State=3, Retry=3, Limits=1, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* Silence trigger kill ratio issues (15m threshold vs 45m latency gap); unhandled LaunchAgent restarts.
  - *human_toil:* Investigating false-positive alarms, manually managing shadow vs act modes.
  - *cost_of_failure:* Uncaught runaway agent burns API credits; erroneous kill stops valid long-running benchmark trials.
  - *statefulness:* Continuous background surveillance over hours of fleet execution.
  - *need_for_retries_timers_signals:* Needs cooldown timers (DEFAULT_COOLDOWN = 30m), trigger prioritization, and kill signals.
  - *parallelism_limits:* Low concurrency, but monitors all active fleet subjects simultaneously.
  - *auditability:* Appends trace-findings/v1 records with complete evidence lists and safety guard verifications.
- **Durable Workflow Value:** A SupervisorWorkflow tracks live fleet subjects as stateful entities, receives heartbeat signals, schedules cooldown timers natively, and executes kill actions safely.
- **Smallest Useful Slice:** A watchdog workflow monitoring a single running job and signaling a kill if no heartbeat is received within 15 minutes.
- **What Would Not Benefit:** Stateless post-mortem log parsers.
- **Citations & Incidents:**
  - `src/evallab/production/live_supervisor.py:33-51 (Priority triggers, silence kill ratios, and 30m cooldown timers)`
  - `scripts/ops/launchd/com.petermakhnatch.evallab.live-supervisor.plist (Launchd configuration)`

### `WF-10`: Agent Rollout Filtering & SFT Hack-Drop Pipeline
- **Owner:** Research scripts / C1-HACKDROP
- **Runs On Today:** Ad-hoc Python scripts reading raw CAS sqlite databases, filtering steps, generating training JSONL
- **Total Score:** 15/21 (Fail=2, Toil=2, Cost=3, State=2, Retry=1, Limits=2, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* Historical projection dropped 212M chars across 98.1% of Goodfire DEV runs (MORNING-REPORT-2026-11-01.md:26); deep_swe had 0 runs in cohort.
  - *human_toil:* Requires manual dataset verification, spot-checking regex rules, and re-running failed exports.
  - *cost_of_failure:* Reward-hacked or truncated trajectories leaking into SFT/RL training sets corrupt downstream model behavior.
  - *statefulness:* Runs for 30-90 minutes processing gigabytes of raw trajectory data.
  - *need_for_retries_timers_signals:* Requires checkpointed chunk processing so multi-GB trajectory exports do not start over on OOM.
  - *parallelism_limits:* Disk I/O and SQLite read lock contention across multi-GB database files.
  - *auditability:* Must record exact sha256 manifests of included/dropped runs and provide reproducible filter provenance.
- **Durable Workflow Value:** A HackDropWorkflow executes chunked ingestion activities with deterministic resumption, applies verified AST and tripwire filters, and commits export bundles atomically.
- **Smallest Useful Slice:** A batch filtering workflow that takes 100 raw trajectories, runs L0 tripwire checks in parallel, and emits a clean JSONL dataset.
- **What Would Not Benefit:** Filtering a small local CSV file with <1,000 rows.
- **Citations & Incidents:**
  - `research-context/bridge/2026-10-31-overnight/fable/raw/T1-AUDIT-PARSE/report.md:24-28 (212.4M chars dropped, slicing 10,931 file mutations)`
  - `research-context/bridge/2026-11-02-overnight/train/raw/C1-HACKDROP/prompt.md:1-25 (Tonight's hack-drop pipeline requirements)`

### `WF-13`: Task Hardening Package Synthesis & Canary Validation
- **Owner:** evallab harden / package_anatomy.py
- **Runs On Today:** CLI scripts parsing task packages, injecting AST fixtures, deploying Modal canaries
- **Total Score:** 15/21 (Fail=2, Toil=2, Cost=2, State=2, Retry=2, Limits=2, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* grader-hardening@1 crashed with FileNotFoundError on general Harbor packages; failed on Python 2.7 images (T2-UNIFIED-HARDEN.md:6).
  - *human_toil:* Manual inspection of Dockerfile layers and container environment debugging.
  - *cost_of_failure:* Deploying flawed hardening creates false positives for honest agents or leaves exploits unblocked.
  - *statefulness:* 15-45 minutes per task package batch.
  - *need_for_retries_timers_signals:* Needs isolated canary test runs with timeouts and failure rollbacks.
  - *parallelism_limits:* Bounded by Modal container concurrency and local Docker build isolation.
  - *auditability:* Must verify 5-cell composition proof (1/0/0/0/0) and record package digests.
- **Durable Workflow Value:** A TaskHardeningWorkflow coordinates L1-L4 test generation, packages Docker images, runs canary verification trials, and commits hardened specs only upon 100% gate pass.
- **Smallest Useful Slice:** A workflow hardening a single task package across L1-L4 layers with canary validation.
- **What Would Not Benefit:** Static regex analysis of a single shell script.
- **Citations & Incidents:**
  - `src/evallab/full_hardening.py:1-50 (Unified L1-L4 container hardening implementation)`
  - `research-context/bridge/2026-10-31-overnight/ag-opus/briefs/T2-UNIFIED-HARDEN.md:5-9 (Historical hardening failures across 5 impossible tasks)`

### `WF-06`: Live Trace Auto-Review & Cascading Tripwire Monitor
- **Owner:** evallab.production.monitor_worker / MisalignmentLiveWorker
- **Runs On Today:** In-memory PrefixScanner, ThreadPoolExecutor for cheap LLM calls, LRU finished run cache
- **Total Score:** 14/21 (Fail=2, Toil=1, Cost=2, State=2, Retry=2, Limits=2, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* Inverted false-alarm rate on TW DEV (20.5% clean FA vs 6.3% hack in MORNING-REPORT-2026-11-01.md:15); memory leak from unbounded finished runs.
  - *human_toil:* Manual tuning of cascade thresholds, debugging detector memory growth.
  - *cost_of_failure:* False alarms kill innocent runs; missed exploits contaminate training datasets or benchmark scores.
  - *statefulness:* Maintains incremental prefix state for dozens of active runs over 10-60 minutes.
  - *need_for_retries_timers_signals:* Requires event streaming, Stage-2 asynchronous follow-up triggers, and stop/quarantine signals to running agents.
  - *parallelism_limits:* Limits on concurrent LLM judge calls to avoid provider 429 rate limits.
  - *auditability:* Requires 100% CoT-free verification, zero-leak dev gates, and deterministic causal tripwire logging.
- **Durable Workflow Value:** A MonitorWorkflow processes step events via Signals, evaluates L0 rules, schedules Stage-2 LLM reviews via activities with rate-limiting, and issues durable pause/quarantine commands.
- **Smallest Useful Slice:** A single-run monitor workflow receiving step signals and executing L0 prefix tripwires with state persistence.
- **What Would Not Benefit:** Single-turn static regex matching on completed patches.
- **Citations & Incidents:**
  - `src/evallab/production/monitor_worker.py:1-68 (Event-driven causal monitor with complex gate rules)`
  - `research-context/bridge/2026-10-31-overnight/fable/raw/T1-LIVE-FIX/report.md:11-20 (Fixing LRU memory bound and inverted false alarms)`

### `WF-14`: Public Corpus Mirror Sync & CAS Blob Ingestion
- **Owner:** cloud_trace_retriever.py / asset_store.py
- **Runs On Today:** Python scripts downloading parquet/tarballs into local SQLite / CAS directories
- **Total Score:** 13/21 (Fail=2, Toil=1, Cost=2, State=2, Retry=2, Limits=1, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* Multi-gigabyte SQLite database contention; .opus audio file corruption during redaction (PR #1710).
  - *human_toil:* Verifying download integrity, cleaning up failed partial downloads.
  - *cost_of_failure:* Corrupted or incomplete corpus sync breaks evaluation reproducibility.
  - *statefulness:* 30-60 minutes downloading and unpacking gigabytes of external traces.
  - *need_for_retries_timers_signals:* HTTP download retries with range headers and chunk validation.
  - *parallelism_limits:* Network bandwidth and disk I/O.
  - *auditability:* Content-addressed storage (CAS) with SHA-256 verification of every stored object.
- **Durable Workflow Value:** A CorpusSyncWorkflow downloads, validates checksums, unpacks, and registers datasets with resume capability on interrupted transfers.
- **Smallest Useful Slice:** An ingestion activity that downloads a single parquet shard and writes CAS objects atomically.
- **What Would Not Benefit:** Local file copies of small test fixtures.
- **Citations & Incidents:**
  - `src/evallab/production/cloud_trace_db.py:1-40 (4.0 GB SQLite trace database management)`
  - `research-context/bridge/2026-10-31-overnight/ag-opus/briefs/VF-OV1.md:8 (PR #1710 fixing .opus audio extension corruption and ATIF unpacking)`

### `WF-15`: Corpus Clearance, Split Locking & Anti-Leak Audit
- **Owner:** corpus_clearance.py / CorpusRegistry
- **Runs On Today:** In-memory validation in Python loaders and pre-action check scripts
- **Total Score:** 12/21 (Fail=1, Toil=2, Cost=3, State=1, Retry=1, Limits=1, Audit=3)
- **Score Evidence:**
  - *failure_frequency:* Historical leak risks where Goodfire 95 held SWE tasks had to be guarded by runtime exceptions (SplitLockError).
  - *human_toil:* Manual verification that no TEST tasks leaked into DEV cohorts or prompt contexts.
  - *cost_of_failure:* Catastrophic: contamination of evaluation benchmarks invalidates research results.
  - *statefulness:* 1-10 minutes per audit sweep.
  - *need_for_retries_timers_signals:* Fail-closed validation with hard error propagation.
  - *parallelism_limits:* Low.
  - *auditability:* Strict audit trails proving zero answer-key reads and zero split seal violations.
- **Durable Workflow Value:** A ClearanceWorkflow runs automated audit activities across workspaces and outputs cryptographic clearance tokens before jobs can be dispatched.
- **Smallest Useful Slice:** An automated pre-flight audit activity validating cohort IDs against the frozen registry.
- **What Would Not Benefit:** Pure in-memory unit tests.
- **Citations & Incidents:**
  - `src/evallab/production/corpus_clearance.py:1-50 (Corpus clearance checks and split isolation)`
  - `src/evallab/production/corpus_queue.py:54-56 (SplitLockError preventing sealed task access)`

### `WF-16`: Local Production Factory Deploy, Upgrade & Rollback
- **Owner:** install-prod-factory.sh / operator bash
- **Runs On Today:** Bash scripts executing git archive, uv sync, and launchctl bootout/bootstrap
- **Total Score:** 12/21 (Fail=2, Toil=2, Cost=2, State=1, Retry=2, Limits=1, Audit=2)
- **Score Evidence:**
  - *failure_frequency:* Stale launchd process locks; lingering SQLite WAL files during release switches; heartbeat stall on startup.
  - *human_toil:* Manual shell execution of install scripts and checking launchctl status.
  - *cost_of_failure:* Failed deploy halts background trace ingestion and monitoring.
  - *statefulness:* 5-15 minutes to archive, build venv, and restart LaunchAgents.
  - *need_for_retries_timers_signals:* Requires health check polling and automatic rollback on verification failure.
  - *parallelism_limits:* Strictly single instance.
  - *auditability:* Git commit SHA releases stored under releases/<sha>.
- **Durable Workflow Value:** A FactoryDeployWorkflow orchestrates release preparation, health-checks the new service, and rolls back cleanly on watchdog failure.
- **Smallest Useful Slice:** A deployment script with automated post-start heartbeat verification and rollback.
- **What Would Not Benefit:** Local hot-reloading development servers.
- **Citations & Incidents:**
  - `scripts/ops/launchd/install-prod-factory.sh:1-60 (Bash script managing immutable releases and launchctl bootstrap)`
  - `scripts/ops/launchd/com.petermakhnatch.evallab.prod-factory-watchdog.plist (Watchdog monitoring service heartbeat)`
