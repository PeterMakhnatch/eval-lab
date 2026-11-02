# Eval-Lab Workflow Roadmap & Decision Memo

**Date:** 2026-11-02  
**Target:** Peter Makhnatch, Eval-Lab Principal  
**Author:** F6-ROADMAP Synthesis Agent  
**Context:** Synthesis of external survey (F1), internal census (F2), Temporal prototypes (F3, F4), DBOS prototype (F7), Dagu trial (F8), and overnight fleet friction (56 report items, 28 census incidents).

---

# Executive Summary: 5-Minute Decision Memo

### Headline Recommendation
**Adopt DBOS (on SQLite) as the primary lightweight workflow engine for Python-orchestrated overnight operations; keep Temporal dev-server as the distributed / multi-machine benchmark; reject Dagu for agent loops (retain only for static CLI batch DAGs); reject heavy orchestrators (Hatchet, Windmill, Argo, Flyte, Prefect).**

Execute a 3-phase rollout:
1. **Phase 0 (This Week, $0 Infrastructure):** Five immediate script and policy fixes in the existing launchd/foreman architecture (git worktree atomic locks, `ReserveSettleSpendGate` cutover, receipt persistence outside `/tmp`, watchdog terminal status rows, CI polling backoff).
2. **Phase 1 (Weeks 1–3, Shadow Mode):** Implement the two highest-toil, strictly bounded operational workflows: **Automated PR Merge Train (`WF-02`)** and **Corpus-Scale Certify-Gold Pipeline (`WF-03`)** using DBOS on SQLite (benchmarked against F4's Temporal merge train).
3. **Phase 2 (Weeks 4–8, Production Migration):** Migrate the overnight multi-agent squad runner (`WF-01`) and quota-aware routing (`WF-07`) to DBOS on SQLite.

---

### The Deciding Facts Under Our Constraints
Our constraints are strict and non-negotiable: **one Apple Silicon Mac, Modal cloud sandboxes, subscription CLIs (Claude, Gemini, Devin, OMP), strictly NO local Docker (`EVALLAB_DISABLE_LOCAL_DOCKER=1`), and a human who sleeps for 8 hours.**

| System | Kill Test Scorecard (KT1–KT5) | Footprint (Processes, Ports, DB) | Determinism & Code Tax | Fit for Our Mac | Decision |
|---|---|---|---|---|---|
| **DBOS** | **5/5 PASS** (KT1: 10/10 crash trials, 0 duplicate effects; KT2: approval survives reboot; KT3: 0 attempts before $T$, wakes up at $+0.243$ s; KT4: fails loudly; KT5: 0 leaked PIDs) | **1 process**, **0 ports**, **0 external binaries** (pure `pip install dbos`, single `.sqlite` file) | Step-boundary only. Arbitrary Python inside steps. | **Perfect.** Zero daemon overhead, embedded in existing venv. | **PRIMARY PICK** |
| **Temporal** | **5/5 PASS** (KT1: verified in F3 & F4; KT2: signals survive restart; KT3: activity backoff; KT4: replay error; KT5: budget/timeout caps) | **2 processes** (server + worker), **1–2 TCP ports** (7233, 8233), downloads ~100MB Go binary | Heavy determinism tax: sandbox bans `time.time()`, direct I/O, `asyncio.wait()`. Deadlock traps. | **Viable but Heavy.** 1,037 LOC scaffolding vs 663 LOC DBOS. | **SECONDARY / BENCHMARK** |
| **Dagu** | **4/5 PASS** (KT1: 10/10; KT2: survives reboot; KT4: snapshot pinning; KT5: 0 leaked PIDs). **FAILS KT3 NATIVE** (6 blind retries before $T$). | **1–2 processes** (Go binary), **1 port** (Web UI 18081), no DB (YAML/JSON files) | Zero determinism tax, but **static DAG only**. Shell string interpolation only. No dynamic fan-out. | **Good for static CLI tasks, unusable for dynamic agents.** | **SPECIALIZED (Static only)** |
| **Restate** | Untested in overnight trials. F1: Single Rust binary, RocksDB state, BSL-style license. | 1 binary, network ports, HTTP SDK handlers. | Journaled RPC handlers over HTTP. | Unnecessary network/HTTP IPC layer for local Python scripts. | **REJECT** |
| **Prefect 3** | F1: SQLite lock contention at ~1,500 runs; 9 min zombie detection. | Python server + worker + SQLite. | Flow/task decorators. | Heavier than Dagu/DBOS without better durability. | **REJECT** |
| **Hatchet / Windmill / Argo / Flyte** | F1: Needs Docker, PostgreSQL, or Kubernetes. | Multi-container daemon clusters. | Heavy distributed infrastructure. | Violates `EVALLAB_DISABLE_LOCAL_DOCKER=1`. | **REJECT OUTRIGHT** |
| **Plain Scripts + launchd** | **0/5 PASS** on durability. Crashes lose memory; restart re-executes all items from scratch. | 1 process, 0 ports, text logs. | None, but 7+ hours of coordinator toil. | Current state; unsustainable operational bleeding. | **PHASE 0 HARDENING ONLY** |

---

### What Evidence Would Flip This Decision
1. **Multi-Machine Expansion:** If Eval-Lab scales beyond Peter's laptop to a distributed fleet of worker machines, DBOS on SQLite cannot coordinate across nodes (requires Postgres). Temporal's native clustering would become mandatory.
2. **Interactive UI Requirement:** If Peter requires an interactive real-time web dashboard to pause, signal, or inspect DAGs while running, Temporal or Dagu have built-in web UIs, whereas open-source DBOS currently lacks a free UI.
3. **SQLite Lock Contention:** If concurrent worker activity creates persistent SQLite write locks under high load (>10 concurrent workers), Temporal's dedicated state store would win.
4. **Zero-Port Temporal Embedded Mode:** If Temporal releases an in-process, zero-port SQLite storage driver for Python without a background daemon, Temporal's rich tooling might outweigh its determinism tax.

---

# Section 1: Empirical Evidence Table (Tonight's Friction & Historical Incidents)

Overnight operations generated **84 empirical evidence points**: **56 real friction items** reported by tonight's lanes and verifiers (`report.md` "Workflow friction" sections), and **28 documented historical incidents** cataloged in the F2 Workflow Census (`research/experiments/on-workflow-census-20261102/CENSUS.md`).

### Mapping Evidence to Candidate Workflows

| Candidate Workflow ID | Workflow Name & Current Implementation | Tonight's Friction Points | Census Incidents | Total Pain Points | Primary Failure Modes Observed |
|---|---|:---:|:---:|:---:|---|
| **`WF-01`** | **Multi-Agent Squad Dispatch & Worker Lifecycle** (`ds_squad.py`, LaunchAgent) | **12** | **4** | **16** | Git worktree config lock collision (`_ds-ao.log:2-10`); worker timeout kills without reports (`.watch-state.json:39`); foreground jobs pushed to background after 60s; zombie process leaks on Darwin. |
| **`WF-02`** | **Automated PR Merge Train & Spec Harmonization** (Manual `gh` CLI loops, `tools/merge_train.py`) | **11** | **3** | **14** | Coordinator spending 7+ hours babysitting 22 PRs (`MORNING-REPORT-2026-11-01.md:5`); CI polling loops triggering tool repetition limits; `repo-map.md` merge collisions stalling queues; red CI PRs blocking clean PRs. |
| **`WF-03`** | **Corpus-Scale Certify-Gold & STING Mutation Pipeline** (`evallab certify-gold`, Modal CLI) | **3** | **4** | **7** | Modal 200-app quota silently truncating sweeps ($0.0004 cost, 0 reward); unrun trials cached as failures; 60m certifier timeouts losing 8 trials without report/PR (`VF-OV2:115`). |
| **`WF-05`** | **Cloud Spend Gate & Dynamic Budget Ledger** (`CloudSpendGate`, `spend.jsonl`) | **4** | **3** | **7** | Inflexible static $0.05 reservations exhausting $2.50 caps on $0.0089 real spend (`T3-SAFEGUARD-BENCH:73`); non-reentrant lock deadlocks; double reservation on admission; 49 reservations stalling runs on $0.26 shortfall. |
| **`WF-07`** | **Provider Quota-Aware Fleet Routing & Rate Throttling** (`ds_throttle.py`, `queue_keeper.py`) | **2** | **2** | **4** | Quota exhaustion exit 29 causing repeated blind retry storms (F1, F8); Direct Claude 7-day quota hitting 100% at 12:57 AM ET without auto-parking; timezone parsing bugs turning 90s sleeps into hours. |
| **`WF-04`** | **Continuous Causal Trace Ingestion & Outbox Relay** (`prod-factory`, `ContinuousWorkQueue`) | **2** | **3** | **5** | 33 items re-queued 12x on quadratic restart (`factory.py:22-31`); hand-rolled outbox replayers; unexposed connection internals. |
| **`WF-14`** | **Public Corpus Mirror Sync & CAS Blob Ingestion** (`asset_store.py`) | **4** | **1** | **5** | APFS directory traversal latency across tens of thousands of run dirs; broad glob timeouts exceeding 300s; inconsistent 6-digit task IDs. |
| **`WF-08`** | **Morning Report & Recomputation Verifier** (`VF-OV1`, `VF-OV2`) | **1** | **2** | **3** | Receipts left in `/tmp` wiped on reboot; scattered receipts across repo and bridge requiring manual inspection of 10 JSON schemas. |
| **`WF-10`** | **Agent Rollout Filtering & SFT Hack-Drop Pipeline** (`hack_drop.py`) | **2** | **1** | **3** | Runner receipts (`grade.json`) colliding with target surface patterns; null variant label ingestion bugs. |
| **`WF-06`, `WF-09`, `WF-11`, `WF-12`, `WF-13`** | Other specialized pipelines (Live monitor, Judge Station, Swarm bench, Live supervisor, Task hardening) | **1** | **5** | **6** | Watchdog false alarms, judge station blind verdict leaks, thread starvation in consensus judges. |
| **Tooling & Environment** | Non-engine harness issues (shell policy, venv sync, repomap generator, browser hangs) | **14** | **0** | **14** | Shell policy blocking heredocs; `uv sync --extra laminar` delays in fresh worktrees; headless browser flags. |
| **TOTAL** | | **56** | **28** | **84** | **Top 5 Candidate Workflows account for 51 of 70 workflow pain points (72.9%, 95% Wilson CI: 61.4%–82.0%).** |

*Receipts: `receipts/evidence_summary.json` (SHA256: `1d2f9edbe4bf`), `receipts/detailed_evidence.json` (SHA256: `b2a6ca3c47a2`).*

---

# Section 2: Engine Decision & Architectural Trade-offs

### The Core Dilemma: Temporal vs DBOS vs Dagu
Tonight's overnight trials produced direct head-to-head empirical data across the three non-Docker options:

#### 1. DBOS on SQLite (The Winner for Python Orchestration)
- **Strengths Demonstrated in F7 (`F7-DBOS-SQUAD/report.md`):**
  - **Zero Server Daemon:** Runs as an embedded Python library (`import dbos`) journaling directly to a local SQLite database file. Zero external binaries to download, zero TCP ports to allocate or guard.
  - **100% Crash Durability (KT1):** Passed 10/10 mid-execution `SIGKILL` trials (95% Wilson: 72.2%–100.0%) with **exactly 1 effect per item** across 60 item completions. Completed steps never re-ran; interrupted steps resumed seamlessly.
  - **Perfect Quota Parking (KT3):** Parked on an exit 29 quota reset timestamp for 90s, making **0 execution attempts before $T$**, and waking up at $T + 0.243$ s tolerance.
  - **Fails Loudly on Code Edit (KT4):** Modifying a step definition between crash and restart immediately raised `DBOSUnexpectedStepError`, preventing silent divergence.
  - **Minimal Code Surface:** Full durable squad runner implemented in **663 LOC** (single file), representing a **36.1% LOC reduction** compared to Temporal's 1,037 LOC.
- **Weaknesses:**
  - OSS edition has no built-in web UI (monitoring via SQLite queries or CLI).
  - Single-machine only when backed by SQLite (requires Postgres for multi-node).
  - Step functions must be declared at module scope with stable names.

#### 2. Temporal Dev Server (The Viable Heavyweight)
- **Strengths Demonstrated in F3 (`F3-TEMPORAL-SQUAD`) and F4 (`F4-MERGETRAIN-WF`):**
  - **Rich Web UI & Ecosystem:** Full execution graph, step timings, and history inspection out of the box on port 8233.
  - **Time-Skipping Testing:** `WorkflowEnvironment.start_time_skipping()` executes multi-hour timers and backoff loops in sub-second wall time, enabling incredibly thorough unit tests.
  - **First-Class Signals & Queries:** Dynamic DAG controls (`approve`, `skip`, `abort`, `board`) work seamlessly without custom database polling.
- **Weaknesses:**
  - **Operational Footprint:** Requires running the separate Temporal dev server binary (auto-downloaded ~100MB Go binary) listening on TCP ports (7233, 8233). If the server is started without `--db-filename`, all state is lost on process restart.
  - **Severe Determinism Tax:** Workflow code executes inside a strict sandbox. Calling `time.time()`, `datetime.now()`, standard file I/O, or `asyncio.wait()` triggers non-determinism exceptions.
  - **Scaffolding Overhead:** 1,037 LOC for squad runner, 814 LOC for merge train. Requires managing `continue_as_new` counters manually to prevent event history exhaustion.

#### 3. Dagu (The Static Tool Specialist)
- **Strengths Demonstrated in F8 (`F8-DAGU-TRIAL`):**
  - Single Go binary, zero database, file-backed YAML DAGs, clean Web UI on port 18081.
  - Passed KT1 (10/10 crash trials), KT2 (human approval across reboot), and KT4 (pins immutable `dag.json` snapshots).
- **Fatal Weaknesses for Dynamic Agents:**
  - **Failed KT3 Natively:** Native retry policy issued 6 blind retry attempts before quota reset timestamp $T$.
  - **No Dynamic Fan-Out / Fan-In:** Cannot generate a dynamic array of tasks from step 1 and fan out workers; DAG graph is static.
  - **String-Only Data Passing:** Steps can only communicate via shell environment variable string interpolation (`${step.output.val}`). No Python objects, no structured state.
  - **No Budget / Token Accounting:** Zero native dollar tracking or rate semaphores.

#### Verdict & Architectural Selection
**DBOS on SQLite is selected as the primary workflow engine.** It gives us 100% of the crash recovery, approval persistence, and quota parking of Temporal, with **zero background daemons, zero network ports, and 36% less code**. Temporal is retained as a reference architecture for distributed expansion. Dagu is reserved exclusively for static CLI batches (such as scheduled lint and benchmark runs).

*Receipts: `receipts/engine_comparison.json` (SHA256: `0b9259246ee0`), `receipts/summary.json` (SHA256: `62b118583645`).*

---

# Section 3: Phased Roadmap

```
Phase 0 (This Week)           Phase 1 (Weeks 1–3)            Phase 2 (Weeks 4–8)
Zero New Infrastructure       Shadow Mode Workflows          Production Cutover
─────────────────────         ─────────────────────          ───────────────────
[Git Worktree Locks ] ──────> [WF-02 Merge Train WF] ──────> [WF-01 Squad Runner]
[Spend Gate Cutover ]         [WF-03 Certify-Gold WF]        [WF-07 Quota Router]
[Receipt Durability ]         [DBOS vs Temporal A/B]         [WF-09 Judge Station]
[Watchdog Status Row]
```

### Phase 0: Immediate Hardening (This Week, Zero New Infrastructure)
**Objective:** Stop operational bleeding in the existing launchd and Python script layer with zero new dependencies or background daemons.

- **Scope (5 Concrete Fixes):**
  1. *Git Worktree Atomic Locking:* Add `fcntl.flock` to worktree initialization in `ds_squad.py` and helpers to eliminate `.git/config.lock` race conditions.
  2. *Reserve-Settle Spend Gate Cutover:* Replace legacy `CloudSpendGate` with `ReserveSettleSpendGate` (shipped in S1 `#1737`) across all remaining CLI entrypoints to eliminate premature $0.05 reservation halts.
  3. *Receipt Durability Enforcer:* Enforce writing per-run readings directly to durable directories (`research-context/bridge/` and `eval-lab-results`), banning `/tmp` for authoritative evidence.
  4. *Watchdog Terminal Status Rows:* Update `on_watch.py` so that wall-clock timeout kills write an explicit `killed-time` status row before sending `SIGKILL`, eliminating silent drops.
  5. *CI Polling Backoff with Jitter:* Add 5-second backoff increments to CLI polling loops to avoid triggering agent tool loop guards.
- **Cost:** $0.00 infrastructure, 0 new daemons.
- **Owner Role:** Ops / Eval-Lab Maintainer.
- **Success Metric:** Zero `.git/config.lock` crashes; zero batch shutdowns where real spend was <25% of cap; zero silent timeout kills without status rows.
- **Kill Criteria:** If git worktree collisions or spend reservation stalls recur after these patches, freeze squad concurrency to 1 until Phase 1.

---

### Phase 1: High-Toil Isolated Workflows in Shadow Mode (Weeks 1–3)
**Objective:** Deploy DBOS-on-SQLite for the two highest-toil, strictly bounded operational bottlenecks, operating in parallel with manual oversight.

- **Scope:**
  1. **Automated PR Merge Train (`WF-02`):** Implement the merge train on DBOS (translating F4's validated 10-scenario Temporal state machine). Run sequentially: rebase onto `origin/main`, auto-regenerate `docs/repo-map.md` on doc conflict, wait for CI via durable sleep, squash-merge clean PRs, quarantine semantic conflicts with `NEEDS_HUMAN`.
  2. **Corpus-Scale Certify-Gold Pipeline (`WF-03`):** Wrap `evallab certify-gold` in a DBOS workflow. Coordinate AST mutation, fan out 6 Modal verification cells in parallel, checkpoint passed cells in SQLite, and retry cloud disconnects with exponential backoff.
  3. **Head-to-Head Shadow Bake-off:** Run the DBOS Merge Train alongside F4's Temporal Merge Train on synthetic conflict suites to verify real-world developer ergonomics.
- **Cost:** $0 infrastructure (embedded SQLite / dev server), <$10 test cloud spend.
- **Owner Role:** Research Coordinator / Eval-Lab Core Engineer.
- **Success Metric:** Coordinator merge toil reduced from >5 hours/night to <15 minutes (reviewing semantic conflicts only); 100% of interrupted mutant audits resume without re-running passed cells.
- **Kill Criteria:** Any workflow engine crash or deadlock halting PR throughput for >30 minutes triggers immediate rollback to manual `tools/merge_train.py`.

---

### Phase 2: Production Fleet Cutover & Dynamic Routing (Weeks 4–8)
**Objective:** Migrate core multi-agent execution and fleet routing to durable orchestration.

- **Scope:**
  1. **Multi-Agent Squad Dispatch (`WF-01`):** Cut over `ds_squad.py` to the DBOS squad runner prototype (F7). Provide durable child workflow per agent, process-group `SIGKILL` without zombie orphans, dynamic gate signals, and resume on reboot.
  2. **Provider Quota-Aware Fleet Routing (`WF-07`):** Implement dynamic 429 quota parking via `DBOS.sleep`. When an API returns code 29 with reset timestamp $T$, park the task with 0 retry attempts before $T$.
  3. **Judge Station Blind Verdict Ingestion (`WF-09`):** Integrate DBOS durable state into Judge Station for human review queues and priority sorting.
- **Cost:** Minimal local compute; zero ongoing cloud orchestration fees.
- **Owner Role:** Core Infrastructure Team.
- **Success Metric:** Overnight squad completion rate $\ge 98\%$; zero duplicate agent executions across machine crashes; 100% compliance with weekly subscription provider caps.
- **Kill Criteria:** If step non-determinism bugs corrupt research metrics, disable engine wrapping and revert to Phase 0 hardened scripts.

---

# Section 4: The First Three Tickets Peter Can Approve

### Ticket 1: Phase 0 Infrastructure Hardening (Git Locks, Spend Gate Cutover, Receipt Paths)
- **Title:** `fix(ops): serialize worktree creation, cut over ReserveSettleSpendGate, and enforce durable receipt paths`
- **Scope:**
  - Wrap git worktree creation in `ds_squad.py` with `fcntl.flock(lock_fd, fcntl.LOCK_EX)` on `.git/worktree.lock`.
  - Replace all remaining imports of `CloudSpendGate` in `research/experiments/**` and `src/evallab/` with `ReserveSettleSpendGate`.
  - Add preflight assertions ensuring scripts write receipts to `research-context/bridge/` or `eval-lab-results/`, raising an error if target path is `/tmp`.
  - Update `on_watch.py` to write `status: killed-time` to `.watch-state.json` before sending termination signal.
- **Acceptance Test:**
  - Run a stress script launching 4 concurrent workers initializing separate worktrees simultaneously; assert 0 `.git/config.lock` failures.
  - Run a 10-cell Modal mock sweep with $0.005 actual cost per cell under a $0.10 budget cap; assert all 10 cells execute without premature reservation exhaustion.
  - Assert simulated timeout leaves a readable terminal JSON receipt.

---

### Ticket 2: Automated Durable PR Merge Train in Shadow Mode (`WF-02`)
- **Title:** `feat(ops): implement DBOS durable PR merge train with repo-map auto-resolution and CI polling`
- **Scope:**
  - Port the tested state machine from F4 (`src/mergetrain/workflows.py`) to DBOS on SQLite.
  - Implement activities for `git fetch origin/main`, isolated worktree rebase, auto-regenerating `docs/repo-map.md` via `python -m evallab.repomap generate` on doc conflict, polling GitHub Actions CI status via durable `DBOS.sleep(30)`, and squash-merging via `gh pr merge --squash --auto`.
  - Quarantine PRs with source code conflicts (`src/**`) or failing CI by setting status to `NEEDS_HUMAN` and advancing to the next PR.
  - Provide CLI command `evallab merge-train run --prs 1735,1737,1740`.
- **Acceptance Test:**
  - Execute against a test git repo with a 4-PR queue: PR 1 clean, PR 2 conflicting on `docs/repo-map.md`, PR 3 failing CI, PR 4 clean.
  - Assert PR 1 merges cleanly; PR 2 resolves repo map, commits, and merges; PR 3 is quarantined without blocking PR 4; PR 4 merges cleanly.
  - Send `SIGKILL` to runner mid-way through PR 2; restart runner and assert exactly 1 squash commit per merged PR on `main` (0 double-merges).

---

### Ticket 3: Durable Modal Cell Runner & Certify-Gold Pipeline (`WF-03`)
- **Title:** `feat(cert): wrap evallab certify-gold in durable DBOS workflow with parallel cell fan-out and checkpointing`
- **Scope:**
  - Wrap `evallab.gold_certifier.GoldCertifier` and `evallab.mutant_auditor` in a DBOS workflow.
  - Coordinate AST mutation activities, fan out the 6-gate certification matrix (reference, nop, witness, 3 mutants) in parallel on Modal using `evallab.modal_cells`.
  - Checkpoint individual cell results in SQLite; on retry or restart, retrieve completed cell results and execute only pending/failed cells.
  - Enforce per-cell timeout (300s) and exponential backoff for cloud connection timeouts (`CellInfrastructureError`).
- **Acceptance Test:**
  - Run gold certification on a synthetic 3-package benchmark.
  - Inject a process crash (`kill -9`) while package 2 is evaluating cell 4; restart the workflow.
  - Assert package 1 does not re-run, package 2 resumes from cell 4, package 3 completes, and total billed Modal cells equals exactly the nominal cell count.

---

# Section 5: Risks & What NOT to Build

### What NOT to Build (Explicit Anti-Patterns)
1. **Do NOT wrap sub-second CLI linters in a workflow engine:** Tools like `ruff check`, `evallab.repomap check`, and `governance check` execute locally in <500 ms with zero external state. Orchestrating them via Temporal or DBOS adds pure serialization overhead with zero reliability benefit. Keep them as local pre-commit hooks or direct shell commands.
2. **Do NOT put local unit tests (`pytest -q`) into a workflow engine:** Fast unit test suites belong in memory via `pytest`. Durable execution is designed for operations that span process crashes, host reboots, and multi-minute cloud calls.
3. **Do NOT deploy Docker-based or Kubernetes-based orchestrators:** Tools requiring Docker daemons or K8s clusters (Argo, Flyte, Airflow, Windmill, Hatchet) violate our single-Mac operating environment and `EVALLAB_DISABLE_LOCAL_DOCKER=1`.
4. **Do NOT build home-grown transactional outbox replayers or custom state stores:** Eval-lab already suffered from bugs in custom leasing logic, unguessable fencing tokens, and quadratic restart re-queuers (`src/evallab/production/factory.py:22-31`). Standardize on a tested library engine (DBOS) rather than writing custom durability plumbing.
5. **Do NOT wrap read-only web viewers in workflows:** Static reporting dashboards (`results-viewer`, `scout-view`) have no multi-step lifecycle; manage them via LaunchAgent daemons with system keepalive.

### Operational Risks & Mitigations
| Identified Risk | Severity | Root Cause | Concrete Mitigation |
|---|:---:|---|---|
| **Determinism Traps** | High | Authoring non-deterministic logic (e.g. `time.time()`, random UUIDs, unbuffered file writes) inside workflow definitions. | Use DBOS step-boundary determinism: arbitrary Python is allowed inside `@DBOS.step`; only top-level workflow orchestration must remain deterministic. |
| **SQLite Lock Contention** | Medium | Multiple concurrent processes attempting simultaneous write transactions on a single `.sqlite` file. | DBOS uses WAL mode (`PRAGMA journal_mode=WAL`) by default. Cap local concurrency at $\le 8$ writers; use queued step execution. |
| **macOS Zombie Process Table** | Medium | Terminated child subprocesses lingering in Darwin's process table as `Z` until reaped, reporting false alive status to `os.kill(pid, 0)`. | Enforce process group termination (`os.killpg(pgrp, signal.SIGKILL)`) and verify termination via `ps -o state= -p <pid>`. |
| **External API Quota Starvation** | High | Running out of weekly token quotas across subscription providers (Direct Claude, Devin Pro). | Implement native quota parking (KT3): catch exit 29, extract reset timestamp $T$, and execute durable sleep with 0 polling attempts before $T$. |

---

# Receipt Manifest & Verifiable Evidence

All supporting trial summaries, raw event histories, and kill-test ledgers have been permanently archived to:
- Primary Bridge: `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F6-ROADMAP/receipts/`
- Durable Results: `/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F6-ROADMAP/receipts/`

| Receipt File | SHA-256 Digest | SHA256[:12] | Description |
|---|---|---|---|
| `receipts/summary.json` | `62b11858364508edf8191a7a724a83ae43e04304345dee2cc8e7e3dcd0567954` | `62b118583645` | Key metrics, coverage ratios, and Wilson confidence intervals |
| `receipts/evidence_summary.json` | `1d2f9edbe4bf8a1bce966c4b840983b1d4625006408c48b66e1eacf8fa44145f` | `1d2f9edbe4bf` | Aggregated counts of tonight's 56 friction items & 28 census incidents |
| `receipts/detailed_evidence.json` | `b2a6ca3c47a2505a32529535841c70b48f676d17398986a747122da0867d6e40` | `b2a6ca3c47a2` | Complete JSON inventory of every friction quote, source file, and candidate mapping |
| `receipts/engine_comparison.json` | `0b9259246ee093c3646cf076adc728a21582f7512547bb8e66b04e6dbe6dfb71` | `0b9259246ee0` | Full comparison matrix: KT1–KT5 scorecard, LOC counts, and operational footprint |
| `receipts/manifest.json` | Computed upon generation | Manifest | Cryptographic verification manifest across all receipt files |
