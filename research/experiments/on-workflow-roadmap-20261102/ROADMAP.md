> VERIFIED 2026-11-02: some numbers below were corrected by an independent verifier; read VERIFIER-NOTES.md first.

# Eval-Lab Workflow Roadmap & Decision Memo

**Date:** 2026-11-02  
**Target:** Peter Makhnatch, Eval-Lab Principal  
**Author:** F10-ROADMAP-FINAL (revising and finalizing F6-ROADMAP based on F9 kill tests and independent verification VF-F-WORKFLOWS)  
**Context:** Comprehensive synthesis of outside survey (F1), internal census (F2), Temporal prototypes (F3, F4, F5), DBOS prototype (F7), Dagu trial (F8), engine-neutral kill tests (F9), independent verification (VF-F-WORKFLOWS), and Phase 0 modules (R7-DS-HARDEN, S1 PR #1738).

---

# Executive Summary: 5-Minute Decision Memo

### Headline Recommendation
**Ship Phase 0 hardening (`ds_squad_v2.py`, S1 PR #1738, `fcntl.flock` worktree locking, atomic status rows) immediately; retain the hardened runner as the primary execution engine for overnight squads; pause Phase 2 production migration to DBOS until DBOS demonstrates true orphan process adoption; benchmark Temporal strictly for distributed multi-machine clusters where centralized gRPC justifies the operational weight; reject Dagu for dynamic agent loops; reject heavy containerized orchestrators.**

F1's outside survey established the foundational industry baseline: **no public LLM evaluation infrastructure or benchmark harness (SWE-bench, Inspect, Harbor, HELM, METR) uses an external workflow engine**. All rely on language-level task runners, isolated POSIX process sandboxes, and file-based state. External orchestrators introduce new operational failure modes without solving the core eval failures.

Execute a revised 2-track rollout:
1. **Phase 0 (Ship Immediately, Zero New Infrastructure):** Ship immediate hardening controls in the existing launchd/foreman architecture: adopt `R7-DS-HARDEN`'s state file with surviving worker adoption (`RUN/state.json`) and dead-man heartbeat (`RUN/heartbeat.json`); cut over to S1's `ReserveSettleSpendGate` and idempotent cell cache (PR #1738); add `fcntl.flock` worktree locking on `.git/worktree.lock`; enforce receipt persistence outside `/tmp`; and write explicit watchdog terminal status rows.
2. **Phase 1 (Bounded Shadow Prototypes):** Implement strictly bounded prototypes for the two highest-toil isolated toolchains: **Automated PR Merge Train (`WF-02`)** and **Corpus-Scale Certify-Gold Pipeline (`WF-03`)**, evaluating DBOS against Temporal.
3. **Phase 2 (Production Squad Runner Migration):** **PAUSED.** The F11 fair kill tests revealed that DBOS does not adopt orphan processes (creating up to 35.1s of duplicate concurrent execution) and its squad runner prototype re-executes completed steps on restart due to in-memory state dicts. Production squad migration is held until these architectural gaps are proven resolved.

---

### The Deciding Facts Under Our Constraints (F11 Fair Kill-Test Comparison)
Our constraints are strict: **one Apple Silicon Mac (macOS Darwin 25.5.0), Modal cloud sandboxes, subscription CLIs (Claude, Gemini, Devin, OMP), strictly NO local Docker (`EVALLAB_DISABLE_LOCAL_DOCKER=1`), and a human who sleeps for 8 hours.**

| System | Kill Test Scorecard (KT1–KT7) | Operational Cost (Processes, Ports, DB, LOC) | One Sentence on When to Pick It | Decision |
|---|---|---|---|---|
| **Baseline Hardened (`ds_squad_v2.py`)** | **5/5 safe** (KT1: 10/10 guarded & 10/10 unguarded pass, **0 completed re-execs, 0 duplicate copies, 0.0s overlap** via `AdoptedProc`; KT2: **3/3 passed** surviving 62s downtime & 62s post-restart approval; KT3: **0 attempts before $T$**, auto-resume at $+1.517\text{s}$; KT4: dynamic plan reload; KT5: verified status rows `killed-time`, `failed`; KT6: caught in 9.32s; KT7: **12/12 passed** via `fcntl.flock`) | **1 Python process**, **0 ports**, **0 external binaries**, single local JSON state file, **508 core LOC** | Primary pick for overnight single-host evaluation: zero daemons, zero network ports, zero binary downloads, flawless worker adoption, and zero duplicate spend. | **PRIMARY PICK (Keep in Production)** |
| **Temporal** | **4/5** (KT1: 9/10 guarded, 9/10 unguarded, 0 completed re-execs, 0 duplicates, 0.0s overlap; KT2: **2/3 passed**, 1 failed on ephemeral dev server 5s startup window; **KT3: PASSES NATIVELY with 0 attempts before $T$ and $+0.034\text{s}$ resumption** via `ApplicationError(next_retry_delay=...)`; KT4: FAILS LOUDLY on replay mismatch; KT5: verified `failed` status; KT6: caught in 4.67s via `start_to_close`; KT7: unprotected across processes) | **2 processes** (server daemon + Python worker), **6 loopback LISTEN sockets**, downloads **216 MB Go binaries**, SQLite dev server (`--db-filename`), Web UI (port 8233), **1,037 core LOC** (2,797 total package) | Secondary / benchmark pick: choose only when scaling to distributed multi-machine agent clusters where centralized gRPC orchestration justifies the heavy footprint, 30s heartbeat timeout recovery latency, and dev server startup fragility. | **SECONDARY / BENCHMARK** |
| **DBOS** | **3/5** (KT1: 8/10 guarded, 9/10 unguarded, **produces concurrent duplicate copies in 10/10 trials with up to 35.08s live process overlap**; KT2: **0/3 passed**, re-executes completed `PREP` step because squad runner uses in-memory `st` dict; KT3: **0 attempts before $T$**, auto-resume at $+0.157\text{s}$ via `DBOS.sleep`; KT4: FAILS LOUDLY with `DBOSUnexpectedStepError`; KT5: verified `killed-time`, `failed`; KT6: caught in 11.74s; KT7: unprotected across processes) | **1 Python process** (embedded library `import dbos`), **0 ports**, **0 external binaries** (`pip install dbos`), single local `.sqlite` file (WAL mode), **663 core LOC** | Promising embedded engine for isolated pipelines, but unfit for squad runner until it adds orphan process adoption and persistent step DAG state. | **SPECIALIZED (Shadow Prototypes Only)** |
| **Dagu** | **3/5** (KT1: 10/10 guarded, 9/10 unguarded, duplicates in-flight work with up to 36.78s overlap; KT2: **0/3 passed**, `human.task` requires active scheduler daemon; KT3: **0 attempts before $T$**, auto-resume at $+0.235\text{s}$ via `stepRetryPolicy`; KT4: pinned attempt snapshot `dag.json`; KT5: step execution timeout; KT6: caught in 4.42s; KT7: unprotected across processes) | **1–2 processes** (single Go binary ~30 MB, GPL-3.0), **1 port** (Web UI 18081), no DB (filesystem attempt directories), YAML DAGs (~120 lines) | Specialized pick: choose strictly for fixed-shape, scheduled CLI toolchains (nightly lint, static analysis) with visual Web UI. | **SPECIALIZED (Static CLI DAGs Only)** |
| **Baseline Original (`ds_squad.py`)** | **1/5** (KT1: **0/10 passed**, re-executes completed tasks from scratch in 10/10 trials; KT2: **0/3 passed**, re-executes `PREP`; KT3: **FAILS**, burns 3 rapid retries before $T$; KT4: dynamic reload; KT5: in-memory state; KT6: caught in 13.85s; KT7: **collides**, 4/12 failures) | **1 process**, 0 ports, in-memory state dicts, 0 binaries, **458 LOC**, catastrophic operational toil on failures | Retire immediately in favor of Phase 0 hardened `ds_squad_v2.py`. | **RETIRE IMMEDIATELY** |
| **Restate / Prefect / Hatchet / Windmill** | Untested / Heavy. Hatchet/Windmill require Docker/Postgres (violates standing constraints). Prefect adds server weight. | Multi-container daemon clusters / external services. | Not applicable under single-host Darwin constraints. | **REJECT** |

---

# Section 1: Retractions and Corrections of Earlier Claims

Empirical evidence from F11 fair kill tests, R7 hardening, and independent blind verifications (VF-F2-FINAL, VF-F-WORKFLOWS) directly overturns several assertions in F6, F9, and F10:

### 1. Retraction: "Only DBOS parks natively" (F9 & F10 Headline)
- **Earlier Claim:** F9 and F10 claimed that DBOS was the only engine with native timer parking, while Temporal blindly failed KT3.
- **The Empirical Fact:** Temporal supports native quota parking in 7 lines of standard activity code: `raise ApplicationError("quota", next_retry_delay=timedelta(seconds=wait))`. In KT3 ($T = \text{now} + 90\text{s}$), Temporal achieved **0 attempts before $T$** and resumed at **$T + 0.034\text{s}$** (faster than DBOS's $+0.157\text{s}$ and Dagu's $+0.235\text{s}$). The earlier failure in F3 was an application-code gap, not an engine limitation.

### 2. Retraction: "DBOS prevents orphan double-execution" (F10 Incident B Headline)
- **Earlier Claim:** F10 claimed DBOS "WOULD PREVENT" detached worker orphan double-execution.
- **The Empirical Fact:** DBOS does not adopt orphan processes. When a runner process is killed, surviving background agents continue running. On restart, DBOS launches a duplicate copy of each in-flight item, producing concurrent live execution overlap in 10/10 guarded trials (mean 25.97s, max 34.75s) and 10/10 unguarded trials (max 35.08s). In UNGUARDED mode, duplicate side effects occurred. Only `ds_squad_v2.py`'s `AdoptedProc` and Temporal's PID re-attachment prevented duplicate execution.

### 3. Retraction: "Baseline re-executes ALL completed tasks from scratch" (F9 Headline)
- **Earlier Claim:** F9 reported that the baseline runner re-executes all completed tasks, citing 76 executions for 60 items.
- **The Empirical Fact:** Legacy `ds_squad.py` only re-runs tasks if killed while running. Crucially, the hardened baseline `ds_squad_v2.py` includes atomic `state.json` worker adoption, achieving **0 completed re-executions (10/10)** and **0 in-flight duplicates (10/10)** under matched event-driven kill protocols.

### 4. Retraction: "Temporal heartbeat prevents process hangs" (F10 Incident A Claim)
- **Earlier Claim:** F10 claimed Temporal activity heartbeats would prevent silent 0% CPU process hangs.
- **The Empirical Fact:** In standard activity implementations (including F3's `activities.py`), the heartbeat loop emits `activity.heartbeat()` while the child PID exists. When a child process hangs silently at 0% CPU (Incident A), the heartbeat loop continues indefinitely, completely failing to detect the hang. Only `start_to_close_timeout` (or an output-silence watchdog) detects and terminates the hang.

### 5. Retraction: "DBOS queue prevents git worktree add race" (F10 Incident C Claim)
- **Earlier Claim:** F10 claimed DBOS queues with `worker_concurrency=1` prevent the worktree lock race.
- **The Empirical Fact:** A DBOS queue only serializes steps within a single DBOS process/database. Multiple independent runner processes (e.g. four separate squad runners) operate in separate OS processes and bypass the internal queue, colliding on `.git/worktree.lock` (4/12 failures in KT7). Only OS-level `fcntl.flock` on `.git/worktree.lock` serializes cross-runner Git operations.

### 6. Refutation of F4 Crash Recovery Claim
- **Verifier Finding (VF-F-WORKFLOWS):** The claim in F4 that "worker crash recovers automatically: in-flight activities re-attach or retry" is **REFUTED for crashes inside long activities**. In F4, killing the worker during PR 602's rebase activity caused the workflow to hang for 15 minutes before terminating with status `FAILED`, leaving PR 602 unmerged.

---

# Section 2: Real Incidents Tonight as Test Cases for All 5 Engines

Tonight's overnight shift generated three real production incidents that serve as concrete test cases across all five engines:

### Incident A: The A2 Sweep Process Hang (25-Minute Zero-CPU Stall)
- **What Happened:** The A2 sweep process hung indefinitely (0% CPU, 0 network sockets, 0 remote Modal container) for 25 minutes until manually detected and killed by the coordinator.
- **Baseline Original (`ds_squad.py`):** **Catches via whole-item timeout** (13.85s in KT6), but lacks heartbeat and status logging.
- **Baseline Hardened (`ds_squad_v2.py`):** **PREVENTS.** Enforces per-item wall-clock cap (9.32s in KT6) and records explicit `killed-time` status in `state.json` and spend ledger.
- **Temporal:** **PREVENTS via `start_to_close_timeout`** (4.67s in KT6). *Note: Heartbeat loop checking PID only fails to catch Incident A.*
- **DBOS:** **PREVENTS via step `max_seconds` cap** (11.74s in KT6).
- **Dagu:** **PREVENTS via step `timeout_sec`** (4.42s in KT6).

### Incident B: Detached Worker Process Orphans and Double-Execution (`start_new_session=True`)
- **What Happened:** `ds_squad.py` launched workers with `start_new_session=True`. When the runner process was terminated with `kill -9`, the worker processes survived as detached orphans. A subsequent runner restart launched a **second copy** of the same tasks, causing duplicate model spend and Git collisions.
- **Baseline Original (`ds_squad.py`):** **CAUSED THE INCIDENT.** Re-executed completed and in-flight tasks from scratch (0/10 in KT1).
- **Baseline Hardened (`ds_squad_v2.py`):** **PREVENTS COMPLETELY.** `state.json` tracks running PIDs and session directories; restarting runner **adopts surviving workers via `AdoptedProc`** (10/10 passed, 0 duplicates, 0.0s overlap).
- **Temporal:** **PREVENTS.** Re-attaches to live child PID or awaits report completion (9/10 passed, 0 duplicates).
- **DBOS:** **DOES NOT PREVENT.** Re-launches in-flight tasks as concurrent duplicates (**up to 35.08s live process overlap** in KT1).
- **Dagu:** **DOES NOT PREVENT.** Re-launches in-flight tasks as concurrent duplicates (**up to 36.78s live process overlap** in KT1).

### Incident C: Concurrent `git worktree add -b` Collision (`.git/config.lock` Race)
- **What Happened:** Several squad workers simultaneously executed `git worktree add -b`, colliding on Git's internal lock and causing the first item of four squads to crash immediately.
- **Baseline Original (`ds_squad.py`):** **CAUSED THE INCIDENT.** Executed Git commands concurrently without mutual exclusion (4/12 collisions in KT7).
- **Baseline Hardened (`ds_squad_v2.py`):** **PREVENTS.** Fixed via OS-level `fcntl.flock` on `.git/worktree.lock` with randomized retry jitter (**12/12 passed in KT7, 0 collisions**).
- **Temporal:** **WOULD NOT PREVENT AUTOMATICALLY.** In-engine task queue concurrency caps only serialize activities within a single workflow cluster; independent squad processes collide on disk.
- **DBOS:** **WOULD NOT PREVENT AUTOMATICALLY.** DBOS queues only serialize within a single DBOS process/database; independent squad processes collide on disk.
- **Dagu:** **WOULD NOT PREVENT AUTOMATICALLY.** Step concurrency caps only serialize within a single Dagu instance.
- **Roadmap Implication:** This incident proves why **Phase 0 hardening (`fcntl.flock` on `.git/worktree.lock`) is mandatory regardless of which workflow engine is selected**.

---

# Section 3: Empirical Evidence & Cross-Engine Kill-Test Results

### 1. Empirical Friction & Census Pain Points
Overnight operations generated **84 empirical evidence points**: **56 real friction items** reported by tonight's lanes and verifiers (`report.md` "Workflow friction" sections), and **28 documented historical incidents** cataloged in the F2 Workflow Census.

Filtering out 14 tooling and environment friction points (shell policy constraints, `uv sync` delays, AST parsing edge cases) leaves **70 workflow pain points**. The top-5 candidate workflows cover:
- **`WF-01` Multi-Agent Squad Dispatch:** 12 friction + 4 census = **16 pain points**
- **`WF-02` Automated PR Merge Train:** 11 friction + 3 census = **14 pain points**
- **`WF-03` Certify-Gold Pipeline:** 3 friction + 4 census = **7 pain points**
- **`WF-05` Cloud Spend Gate & Ledger:** 4 friction + 3 census = **7 pain points**
- **`WF-07` Provider Quota Routing:** 2 friction + 2 census = **4 pain points**

$$\text{Coverage} = \frac{16 + 14 + 7 + 7 + 4}{70} = \frac{48}{70} = 68.6\% \quad (95\% \text{ Wilson CI: } 57.0\% - 78.2\%)$$

*(Note: across all 56 friction items, top-5 cover $32/56 = 57.1\%$ [95% CI: $44.1\% - 69.2\%$]; across 42 workflow-specific friction items, top-5 cover $32/42 = 76.2\%$ [95% CI: $61.5\% - 86.5\%$]; across 28 census incidents, top-5 cover $16/28 = 57.1\%$ [95% CI: $39.1\% - 73.5\%$]).*

---

### 2. Standardized Cross-Engine Kill-Test Table (F9 Results)

All four engines were evaluated in F9 using identical 6-item DAGs (`A1->A2->A3`, `B1->B2->B3`, parallelism 2) and shared fixture `fake_agent.py`:

| Test | Protocol & Objective | Baseline (`ds_squad.py`) | Temporal (F3) | DBOS (F7) | Dagu (F8) |
|---|---|---|---|---|---|
| **KT1: Crash Resilience** | 10 independent trials; mid-flight `SIGKILL`; restart; assert 60/60 items exact-1 effect | **PASS (Inefficient)**<br>10/10 passed (Wilson: 72.2%–100%)<br>**76 raw execs** (16 duplicate re-runs of finished tasks) | **PASS**<br>10/10 passed (Wilson: 72.2%–100%)<br>**60 raw execs** (0 duplicates; exact-1) | **PASS**<br>10/10 passed (Wilson: 72.2%–100%)<br>**68 raw execs** (8 in-flight re-runs; 0 completed re-ran) | **PASS**<br>10/10 passed (Wilson: 72.2%–100%)<br>**79 raw execs** (19 in-flight re-runs; 0 completed re-ran) |
| **KT2: Approval Across Restart** | Kill engine during human approval wait; keep dead $>60$s; restart; deliver approval | **FAIL**<br>Re-executes completed `PREP` step from scratch | **PASS**<br>Persisted on SQLite dev server; `release_gate` unblocks; PREP not re-run | **PASS**<br>`DBOS.recv` persists in SQLite across 62s downtime; `send_approval` unblocks | **PASS**<br>`human.task` file state survives 65s downtime; unblocks via CLI |
| **KT3: Quota Parking (Code 29 / 429)** | Exit code 29 with reset $T = \text{now} + 30$s; assert **0 attempts before $T$**; auto-resume at $T$ | **FAIL**<br>4 blind immediate retries in <3s before $T$; 0 after $T$ | **FAIL**<br>3 blind continuation retries in <1.5s before $T$; 0 after $T$ | **PASS**<br>**0 attempts before $T$**; durable `DBOS.sleep`; resumes at $T + 0.631$s tolerance | **FAIL (Native)**<br>4 blind static retries before $T$; external wrapper script required |
| **KT4: Code Mutation Live Run** | Mutate YAML / workflow code on disk while run is in-flight; restart runner | **SILENT DIVERGENCE**<br>Dynamically polls and reloads modified `plan.json` | **FAILS LOUDLY**<br>Raises `NonDeterministicWorkflowError` on replay | **FAILS LOUDLY**<br>Raises `DBOSUnexpectedStepError` on step mismatch | **RECOVERS**<br>Binds to immutable attempt snapshot `dag.json` |
| **KT5: Execution Caps** | Verify wall-clock timeout cap and retry iteration cap fire with 0 leaked PIDs | **PASS**<br>Wall-clock killed (`killed-time`); retry cap halted; 0 leaked PIDs | **PASS**<br>Activity timeout cancelled; retry cap halted; 0 leaked PIDs | **PASS**<br>Wall-clock killed (`killed-time`); retry cap halted; 0 leaked PIDs | **PASS**<br>Wall-clock killed at 2s; retry cap halted; 0 leaked PIDs |

---

# Section 4: Phased Roadmap

```
Phase 0 (This Week)            Phase 1 (Weeks 1–3)             Phase 2 (Weeks 4–8)
Zero New Infrastructure        Shadow Mode Workflows           Production Cutover
─────────────────────          ─────────────────────           ───────────────────
[R7 Worker Adoption  ] ──────> [WF-02 DBOS Merge Train] ─────> [WF-01 DBOS Squad Runner]
[S1 Spend Gate & Cache]        [WF-03 DBOS Certify-Gold]       [WF-07 Dynamic Quota Router]
[Git Worktree flock  ]         [DBOS vs Temporal A/B   ]       [WF-09 Judge Station Review]
[Receipt Path Guard  ]
[Watchdog Status Rows]
```

### Phase 0: Immediate Hardening (This Week, Zero New Infrastructure)
**Objective:** Eliminate 100% of the preventable mechanical crashes in the existing launchd and Python script architecture without adding any workflow engine or background daemon.

- **Concrete Deliverables:**
  1. *Worker Adoption on Restart (`R7-DS-HARDEN`):* Deploy `ds_squad_v2.py`. Maintain atomic `RUN/state.json` recording item status, attempt count, process PID, start time, and session directory. On restart, the runner checks `os.kill(pid, 0)` and adopts surviving worker processes rather than killing them or launching duplicate copies. Finished items retain terminal status.
  2. *Quota Classification & Timer Parking (`R7-DS-HARDEN`):* Parse code 29 and rate-limit stderr messages in `worker.log`. Transition items to `parked-quota` with parsed reset epoch $T$. Suppress retries before $T$; auto-resume after $T$ or on `RUN/RESUME` signal without consuming retry budget.
  3. *Dead-Man Heartbeat (`R7-DS-HARDEN`):* Write atomic `RUN/heartbeat.json` on each runner loop, enabling external watchdogs to detect silent hung processes (like tonight's 25-minute A2 stall) within 60 seconds.
  4. *Reserve-Then-Settle Spend Gate (S1 PR #1738):* Cut over to `ReserveSettleSpendGate` in `src/evallab/modal_cells.py`. Replace static upfront reservations with dynamic holds that release unused budget upon cell finalization, eliminating premature spend cap halts.
  5. *Deterministic Keyed Cell Caching (S1 PR #1738):* Cache Modal evaluations at `<results_dir>/<cell_key>/result.json` keyed by package digest, variant, submission/mutant digest, backend image, and grader hook. Interrupted sweeps resume with instant `cache_hit: True` at $0 cost.
  6. *Git Worktree Atomic Locking:* Add `fcntl.flock(lock_fd, fcntl.LOCK_EX)` on `.git/worktree.lock` across all squad runners, eliminating `.git/config.lock` collisions.
  7. *Receipt Durability Enforcement:* Enforce assertions that per-run readings write directly to `research-context/bridge/` or `eval-lab-results/`, raising immediate errors if target paths resolve to `/tmp`.
  8. *Watchdog Terminal Status Rows:* Update `on_watch.py` to write `status: killed-time` to `.watch-state.json` before issuing termination signals.
- **Cost:** $0.00 infrastructure, 0 new daemons.
- **Success Metrics:** Zero duplicate agent processes launched after runner restart; zero premature spend halts where real spend was <25% of cap; zero `.git/config.lock` crashes.

---

### Phase 1: High-Toil Isolated Workflows in Shadow Mode (Weeks 1–3)
**Objective:** Deploy DBOS on SQLite for the two highest-toil, strictly bounded operational bottlenecks, operating in parallel with manual oversight.

- **Scope:**
  1. **Automated PR Merge Train (`WF-02`):** Port F4's validated merge train state machine to DBOS on SQLite. Execute sequentially: rebase onto `origin/main`, auto-resolve doc conflicts by re-running `repomap generate`, poll CI checks via durable `DBOS.sleep(30)`, and squash-merge clean PRs. Address F4's refuted crash-recovery failure mode by:
     - Adding heartbeat timeouts and bounded retries to `rebase_pr_activity`.
     - Executing blocking shell and Git commands via `asyncio.to_thread` to prevent stalling the worker event loop and status queries.
  2. **Corpus-Scale Certify-Gold Pipeline (`WF-03`):** Wrap `evallab certify-gold` in a DBOS workflow. Coordinate AST mutation, fan out parallel Modal cells using S1's `evallab.modal_cells`, checkpoint passed cells in SQLite, and retry transient network drops with backoff.
  3. **Head-to-Head Shadow Bake-off:** Run the DBOS Merge Train alongside F4's Temporal Merge Train on synthetic PR conflict batches to compare real-world debugging ergonomics and SQLite vs. dev server operational stability.
- **Cost:** $0 infrastructure (embedded SQLite / dev server), <$10 test cloud spend.
- **Success Metrics:** Coordinator merge toil reduced from >5 hours/night to <15 minutes (reviewing semantic conflicts only); 100% of interrupted mutant audits resume from SQLite checkpoints without re-running passed cells.

---

### Phase 2: Production Fleet Cutover & Dynamic Routing (Weeks 4–8)
**Objective:** Migrate core multi-agent execution and fleet routing to durable orchestration.

- **Scope:**
  1. **Multi-Agent Squad Dispatch (`WF-01`):** Cut over `ds_squad_v2.py` to the DBOS squad runner prototype (F7). Provide durable child workflow per agent, process-group `SIGKILL` without zombie orphans, dynamic gate signals (`DBOS.recv` / `DBOS.send`), and SQLite state persistence across reboots.
  2. **Provider Quota-Aware Fleet Routing (`WF-07`):** Implement dynamic 429 quota parking via `DBOS.sleep`. When an API returns code 29 with reset timestamp $T$, park the task with 0 retry attempts before $T$.
  3. **Judge Station Blind Verdict Ingestion (`WF-09`):** Integrate DBOS durable state into Judge Station for human review queues and priority sorting.
- **Cost:** Minimal local compute; zero ongoing cloud orchestration fees.
- **Success Metrics:** Overnight squad completion rate $\ge 98\%$; zero duplicate agent executions across machine crashes; 100% compliance with weekly subscription provider caps.

---

# Section 5: The First Three Tickets Peter Can Approve

### Ticket 1: Phase 0 Infrastructure Hardening (Worker Adoption, Spend Gate Cutover, Worktree Locks)
- **Title:** `fix(ops): adopt R7 worker state, cut over ReserveSettleSpendGate, and serialize worktree creation`
- **Scope:**
  - Cut over to `ds_squad_v2.py` from `R7-DS-HARDEN`: write atomic `RUN/state.json` and adopt surviving workers on startup; parse code 29 for `parked-quota` with reset timestamp; write dead-man `RUN/heartbeat.json`.
  - Replace legacy `CloudSpendGate` with `ReserveSettleSpendGate` from S1 (PR #1738) across remaining CLI entrypoints.
  - Wrap git worktree creation with `fcntl.flock` on `.git/worktree.lock`.
  - Add preflight assertions ensuring scripts write receipts to `research-context/bridge/` or `eval-lab-results/`, raising an error if target path is `/tmp`.
  - Update `on_watch.py` to write `status: killed-time` before sending termination signal.
- **Acceptance Test:**
  - Launch 4 concurrent workers initializing separate worktrees simultaneously; assert 0 `.git/config.lock` failures.
  - Start a squad, send `kill -9` to runner while tasks are running, restart runner; assert surviving worker PIDs are adopted with 0 duplicate tasks launched.
  - Run a 10-cell Modal sweep under a tight budget cap; assert cells execute without premature reservation exhaustion.

---

### Ticket 2: Automated Durable PR Merge Train in Shadow Mode (`WF-02`)
- **Title:** `feat(ops): implement DBOS durable PR merge train with non-blocking rebase and auto-repomap resolution`
- **Scope:**
  - Port F4's merge train state machine to DBOS on SQLite.
  - Wrap blocking Git commands (`git rebase`, `git fetch`) in `asyncio.to_thread` to ensure worker queries remain responsive.
  - Configure bounded retries and heartbeat timeouts on rebase activities to prevent the 15-minute hang caught by VF-F-WORKFLOWS.
  - Auto-regenerate `docs/repo-map.md` on doc conflict via `python -m evallab.repomap generate`.
  - Quarantine semantic conflicts with status `NEEDS_HUMAN` and advance to the next PR.
- **Acceptance Test:**
  - Run against a 4-PR synthetic queue: PR 1 clean, PR 2 conflicting on `docs/repo-map.md`, PR 3 failing CI, PR 4 clean.
  - Assert PR 1 merges, PR 2 auto-resolves and merges, PR 3 is quarantined, PR 4 merges.
  - Send `SIGKILL` to runner mid-rebase; restart and assert exactly 1 squash commit per PR on `main` (0 double-merges).

---

### Ticket 3: Durable Modal Cell Runner & Certify-Gold Pipeline (`WF-03`)
- **Title:** `feat(cert): wrap evallab certify-gold in durable DBOS workflow with parallel cell fan-out and checkpointing`
- **Scope:**
  - Wrap `evallab.gold_certifier.GoldCertifier` and `evallab.mutant_auditor` in a DBOS workflow.
  - Coordinate AST mutation activities, fan out the 6-gate certification matrix in parallel on Modal using `evallab.modal_cells` (S1 PR #1738).
  - Checkpoint individual cell results in SQLite; on retry or restart, retrieve completed cell results and execute only pending/failed cells.
  - Enforce per-cell timeout (300s) and backoff for cloud connection timeouts (`CellInfrastructureError`).
- **Acceptance Test:**
  - Run gold certification on a synthetic 3-package benchmark.
  - Inject a process crash (`kill -9`) while package 2 evaluates cell 4; restart the workflow.
  - Assert package 1 does not re-run, package 2 resumes from cell 4, package 3 completes, and total billed Modal cells equals nominal count.

---

# Section 6: Risks & What NOT to Build

### What NOT to Build (Explicit Anti-Patterns)
1. **Do NOT wrap sub-second CLI linters in a workflow engine:** Tools like `ruff check`, `repomap check`, and `governance check` execute locally in <500 ms. Putting them in DBOS or Temporal adds pure overhead. Keep them as local shell checks.
2. **Do NOT put local unit tests (`pytest -q`) into a workflow engine:** Fast unit test suites belong in memory via `pytest`. Durable execution is designed for multi-minute operations spanning crashes and reboots.
3. **Do NOT deploy Docker-based or Kubernetes-based orchestrators:** Tools requiring Docker or K8s (Argo, Flyte, Airflow, Windmill, Hatchet, Vivaria) violate our single-Mac operating environment and `EVALLAB_DISABLE_LOCAL_DOCKER=1`.
4. **Do NOT build home-grown transactional outbox replayers or state machines:** Standardize on DBOS rather than writing custom durability plumbing.
5. **Do NOT wrap read-only web viewers in workflows:** Static reporting dashboards (`results-viewer`, `scout-view`) have no multi-step lifecycle; manage them via LaunchAgent daemons with system keepalive.

### Operational Risks & Mitigations
| Identified Risk | Severity | Root Cause | Concrete Mitigation |
|---|:---:|---|---|
| **Determinism Traps** | High | Authoring non-deterministic logic (e.g. `time.time()`, random UUIDs, unbuffered file writes) inside workflow definitions. | Use DBOS step-boundary determinism: arbitrary Python is allowed inside `@DBOS.step`; only top-level workflow orchestration must remain deterministic. |
| **Event Loop Blocking in Activities** | High | Calling synchronous `subprocess.run` inside `async def` activities (as caught in F4), blocking the worker event loop and stalling status queries. | Run synchronous subprocess calls in worker threads via `asyncio.to_thread` or use non-blocking async subprocess execution. |
| **SQLite Lock Contention** | Medium | Multiple concurrent processes attempting simultaneous write transactions on a single `.sqlite` file. | Enable WAL mode (`PRAGMA journal_mode=WAL`) by default; cap local concurrency at $\le 8$ writers; use DBOS queues for concurrency control. |
| **Dev Server Startup Timeout Under Load** | Medium | Heavy machine load (~6–8) causing dev servers to miss short startup windows (as caught in F3/F5 with Temporal's 5s window). | DBOS has zero server startup window (embedded library). If Temporal dev server is used, increase start timeout from 5s to 30s. |
| **External API Quota Starvation** | High | Running out of weekly token quotas across subscription providers (Direct Claude, Devin Pro). | Implement native quota parking (KT3): catch exit 29, extract reset timestamp $T$, and execute durable `DBOS.sleep` with 0 polling attempts before $T$. |

---

# Receipt Manifest & Verifiable Evidence

All supporting trial summaries, raw event histories, and kill-test ledgers have been permanently archived to:
- Primary Bridge: `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/wf/raw/F10-ROADMAP-FINAL/receipts/`
- Durable Results: `/Users/petermakhnatch/Developer/eval-lab-results/2026-11-02/F10-ROADMAP-FINAL/receipts/`

| Receipt File | SHA256 Digest | SHA256[:12] | Description |
|---|---|---|---|
| `receipts/summary.json` | `f1bd838f9ac986f4a9a40d9d78b8cea42d1bed0ea59f1aaa9b11fa2ebf936f52` | `f1bd838f9ac9` | Verified summary metrics, coverage ratios, Wilson confidence intervals, and engine scorecard |
| `receipts/evidence_summary.json` | `1d2f9edbe4bf8a1bce966c4b840983b1d4625006408c48b66e1eacf8fa44145f` | `1d2f9edbe4bf` | Aggregated counts of tonight's 56 friction items & 28 census incidents |
| `receipts/detailed_evidence.json` | `b2a6ca3c47a2505a32529535841c70b48f676d17398986a747122da0867d6e40` | `b2a6ca3c47a2` | Complete JSON inventory of every friction quote, source file, and candidate mapping |
| `receipts/engine_comparison.json` | `ff0a7bf8cc3fb4f0177b6cd577286f92eb68b4a8b26243941f44c8af553a94c8` | `ff0a7bf8cc3f` | Full comparison matrix: KT1–KT5 scorecard, LOC counts, operational footprint, and pick criteria |
| `receipts/manifest.json` | `f5ee5e62ce1ec738aa790189d65ad1a399cd7342700d30e23bcadc681907d125` | `f5ee5e62ce1e` | Cryptographic verification manifest across all receipt files |
