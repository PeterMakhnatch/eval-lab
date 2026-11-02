# Eval-Lab Workflow Roadmap & Decision Memo

**Date:** 2026-11-02  
**Target:** Peter Makhnatch, Eval-Lab Principal  
**Author:** F10-ROADMAP-FINAL (revising and finalizing F6-ROADMAP based on F9 kill tests and independent verification VF-F-WORKFLOWS)  
**Context:** Comprehensive synthesis of outside survey (F1), internal census (F2), Temporal prototypes (F3, F4, F5), DBOS prototype (F7), Dagu trial (F8), engine-neutral kill tests (F9), independent verification (VF-F-WORKFLOWS), and Phase 0 modules (R7-DS-HARDEN, S1 PR #1738).

---

# Executive Summary: 5-Minute Decision Memo

### Headline Recommendation
**Adopt DBOS (on SQLite) as the primary lightweight workflow engine for Python-orchestrated overnight operations; keep Temporal dev-server as the secondary benchmark for distributed / multi-machine fleet expansion; reject Dagu for agent loops (retain only for static CLI batch DAGs); retire the hand-rolled squad runner (`ds_squad.py`) after Phase 0 hardening; reject heavy containerized orchestrators (Hatchet, Windmill, Argo, Flyte, Vivaria).**

Execute a 3-phase rollout:
1. **Phase 0 (This Week, Zero New Infrastructure):** Ship immediate hardening controls in the existing launchd/foreman architecture: adopt `R7-DS-HARDEN`'s state file with surviving worker adoption (`RUN/state.json`) and dead-man heartbeat (`RUN/heartbeat.json`); cut over to S1's `ReserveSettleSpendGate` and idempotent cell cache (PR #1738); add `fcntl.flock` worktree locking; enforce receipt persistence outside `/tmp`; and write explicit watchdog terminal status rows.
2. **Phase 1 (Weeks 1–3, Shadow Mode):** Implement the two highest-toil, strictly bounded operational bottlenecks: **Automated PR Merge Train (`WF-02`)** and **Corpus-Scale Certify-Gold Pipeline (`WF-03`)** using DBOS on SQLite (benchmarked against F4's Temporal merge train).
3. **Phase 2 (Weeks 4–8, Production Migration):** Migrate the overnight multi-agent squad runner (`WF-01`) and quota-aware fleet routing (`WF-07`) to DBOS on SQLite.

---

### The Deciding Facts Under Our Constraints
Our constraints are strict: **one Apple Silicon Mac (macOS Darwin 25.5.0), Modal cloud sandboxes, subscription CLIs (Claude, Gemini, Devin, OMP), strictly NO local Docker (`EVALLAB_DISABLE_LOCAL_DOCKER=1`), and a human who sleeps for 8 hours.**

| System | Kill Test Scorecard (KT1–KT5) | Operational Cost (Processes, Ports, DB, LOC) | One Sentence on When to Pick It | Decision |
|---|---|---|---|---|
| **DBOS** | **5/5 safe** (KT1: 10/10 passed, 68 raw execs, 0 duplicate effects; KT2: approval survives 62s reboot downtime in SQLite; KT3: **0 attempts before $T$**, auto-resume at $+0.631$s tolerance in F9 / $+0.243$s in F7; KT4: FAILS LOUDLY with `DBOSUnexpectedStepError`; KT5: 0 leaked PIDs) | **1 Python process** (embedded library `import dbos`), **0 ports**, **0 external binaries** (`pip install dbos`), single local `.sqlite` file (WAL mode), **663 core LOC** | Primary pick for single-host Python overnight squads: zero server daemons, zero network ports, sub-second startup, step-level journaling, and flawless native 0-attempt quota parking. | **PRIMARY PICK** |
| **Temporal** | **4/5** (KT1: 10/10 passed, 60 raw execs, 0 duplicates; KT2: survives dev server restart on SQLite; **KT3: FAILS NATIVELY with 3 blind continuation retries in <1.5s before $T$**, raises `ReportMissing` without timer parking; KT4: FAILS LOUDLY with `NonDeterministicWorkflowError`; KT5: 0 leaked PIDs) | **2 processes** (server daemon + Python worker), **6 loopback LISTEN sockets**, downloads **216 MB Go binaries**, SQLite dev server (`--db-filename`), Web UI (port 8233), **1,037 core LOC** (2,797 total package) | Secondary / benchmark pick: choose only when scaling to distributed multi-machine agent clusters where centralized gRPC orchestration and enterprise tracing justify the heavy operational footprint and determinism tax. | **SECONDARY / BENCHMARK** |
| **Dagu** | **4/5** (KT1: 10/10 passed, 79 raw execs; KT2: `human.task` survives 65s downtime; **KT3: FAILS NATIVELY with 4–6 blind static retries before $T$**, requires external python wrapper; KT4: RECOVERS via pinned `dag.json` attempt snapshot; KT5: 0 leaked PIDs) | **1–2 processes** (single Go binary ~30 MB, GPL-3.0), **1 port** (Web UI 18081), no DB (filesystem attempt directories), YAML DAGs (~120 lines) | Specialized pick: choose strictly for fixed-shape, scheduled CLI toolchains (nightly lint, static analysis, fixed-list benchmark sweeps) and operator approval forms that require a visual web UI. | **SPECIALIZED (Static only)** |
| **Baseline (`ds_squad.py`)** | **2/5** (KT1: 10/10 passed, but **76 raw execs with 16 duplicate re-runs of finished tasks**; **KT2: FAILS**, re-executes completed `PREP` step; **KT3: FAILS**, 4 blind immediate retries in <3s; **KT4: SILENT DIVERGENCE**, dynamic reload; KT5: 0 leaked PIDs) | **1 process**, 0 ports, in-memory state dicts, 0 binaries, **458 LOC**, but catastrophic operational toil (>5–7 hours coordinator time on failures, duplicate execution compute waste) | Retire after Phase 0 hardening: suitable only for short throwaway runs where transient failure is negligible and re-running from scratch is acceptable. | **RETIRE AFTER PHASE 0** |
| **Restate** | Untested in overnight trials. F1: Single Rust binary, RocksDB state, BSL-style license. | 1 binary, network ports, HTTP SDK handlers. | Alternative to DBOS if DBOS fails multi-node expansion; unnecessary network/HTTP IPC layer for local Python scripts. | **REJECT** |
| **Prefect 3** | F1: SQLite lock contention at ~1,500 runs; 9 min zombie detection. | Python server + worker + SQLite. | Heavier than Dagu/DBOS without better durability. | **REJECT** |
| **Hatchet / Windmill / Argo / Flyte / Vivaria** | F1: Needs Docker, PostgreSQL, or Kubernetes. | Multi-container daemon clusters. | Violates `EVALLAB_DISABLE_LOCAL_DOCKER=1`. | **REJECT OUTRIGHT** |

---

# Section 1: What Earlier Statements the Evidence Contradicts

Standardized kill-test results from F9, independent verification from VF-F-WORKFLOWS, and prototype evaluations (F3, F4, F5, F7, F8) directly contradict several earlier claims and assumptions:

### 1. Contradiction of F2's Framing (Workflows Needing Temporal "Immediately")
- **Earlier Claim (F2 Census):** Five candidate workflows (`WF-01`, `WF-02`, `WF-03`, `WF-05`, `WF-04`) suffer from catastrophic failures and require durable orchestration ("Temporal and similar") immediately.
- **What the Evidence Proved:**
  1. **Temporal is NOT strictly required immediately:** The primary overnight incidents cited by F2 (worktree lock races, reservation cap exhaustion, dangling `/tmp` receipts, watchdog timeout drops) are architectural hygiene gaps that Phase 0 hardening (`R7-DS-HARDEN` and S1 `modal_cells.py` PR #1738) resolves completely with **zero new infrastructure**.
  2. **Temporal fails native quota parking (KT3):** When an agent process exits with code 29 without producing `report.md`, Temporal raises `ReportMissing` and immediately exhausts its activity retry cap within 1.5 seconds (3 rapid retries before $T$). Deploying Temporal "immediately" would worsen, rather than solve, 429 rate-limit storms.
  3. **Temporal introduces new failure modes under load:** As demonstrated by VF-F-WORKFLOWS, the Temporal dev server has a strict 5-second startup window. Under fleet load (~6–8), the dev server timed out, causing F3 tests to fail 2/9 and F5 tests to fail up to 7/7, completely independent of workflow logic.

### 2. Confirmation of F1's Prior (DBOS as the Optimal Fit)
- **Earlier Prior (F1 Survey):** F1 hypothesized that Phase 0 hardening must come first, DBOS-on-SQLite is the cleanest fit for Python orchestration ("each external call is a step"), Temporal is a heavy comparison, and Dagu is suitable only for static CLI batches.
- **What Survived:** **F1's prior survived 100% and was decisively validated by F7 and F9.** DBOS was the **only engine to pass all five kill tests natively**, including flawless zero-attempt quota parking (`DBOS.sleep`), while operating with zero server daemons, zero network ports, sub-second startup, and 36.1% less code than Temporal.

### 3. Corrections to F6's Initial Roadmap Claims
- **F6 Headline Coverage:** F6 claimed the top-5 candidate workflows covered "51 of 70 = 72.9% (61.4%–82.0%)". VF-F-WORKFLOWS caught that F6's own rows (16+14+7+7+4) sum to **48 of 70 = 68.6% (95% Wilson CI: 57.0%–78.2%)**. Furthermore, `summary.json`'s reported Wilson interval was computed over 32/42 instead of 32/56.
- **F6 Temporal Scorecard:** F6 claimed Temporal scored "5/5 PASS", citing KT3 activity backoff and KT4 replay error before F9 had executed. F9 proved Temporal **failed KT3 natively** (3 rapid refusals before $T$).
- **F6 PR References:** F6 cited S1's `ReserveSettleSpendGate` as "shipped in S1 `#1737`". PR `#1737` is HAR-254; S1 is **PR #1738 (OPEN)**.
- **F6 Prefect Mischaracterization:** F6 rejected Prefect as Docker-dependent; F1 confirmed Prefect runs as a local Python SQLite server. It is rejected for operational weight, not Docker.

### 4. Corrections to F3, F4, and F5 Author Claims (VF-F-WORKFLOWS Findings)
- **F3 (Temporal Squad):** The claim of "exactly-once agent execution" holds only when the fake agent outlives the killed worker process. When agent processes die with the worker (Scenario S3), items run **twice** (at-least-once). Crash recovery also incurs a ~30s latency waiting for the heartbeat timeout. In addition, the F3 dev server opens **6 loopback sockets** (not 1), requires a **216 MB binary download** on first run, and the F3 author session had 6 off-route messages served by `devin/claude-sonnet-5-5`.
- **F4 (Temporal Merge Train):** The claim that "worker crash recovers automatically: in-flight activities re-attach or retry" is **REFUTED for crashes inside the rebase activity**. Because `rebase_pr_activity` had `maximum_attempts=1`, `start_to_close=15 min`, and no heartbeat, a `kill -9` caused the workflow to hang for 15 minutes and then terminate with status `FAILED`, leaving PR 602 unmerged. Furthermore, calling `subprocess.run` inside `async def` activities blocked the worker's event loop, stalling status queries.
- **F5 (Certify-Gold Prototype):** Activities were deterministic stubs without real Modal or model calls; the metric "0 duplicate executions" is blind to interrupted attempts (ledger rows are written after cell completion); peak Modal concurrency was 1 (not 2); and `test_suite_receipt.json` was a hardcoded literal. F5 does not prove that Temporal eliminates certification failure modes.

---

# Section 2: Real Incidents Tonight as Test Cases for Every Engine

Tonight's overnight shift generated three real production incidents that serve as concrete test cases across all four engines:

### Incident A: The A2 Sweep Process Hang (25-Minute Zero-CPU Stall)
- **What Happened:** The A2 sweep process hung indefinitely (0% CPU, 0 network sockets, 0 remote Modal container) for 25 minutes until manually detected and killed by the coordinator.
- **Hand-rolled Baseline (`ds_squad.py`):** **Would NOT prevent.** Baseline relies on whole-squad wall-clock limits; individual stalled subprocesses block worker concurrency until the entire timeout elapses. (R7-DS-HARDEN's dead-man `heartbeat.json` addresses this by alerting when the loop stalls). *Tested in F9?* KT5 tested sleeping scripts, not zero-CPU silent stalls.
- **Temporal:** **WOULD PREVENT.** Temporal activity definitions enforce `start_to_close_timeout` and require periodic heartbeats (`activity.heartbeat()`). If an activity stops heartbeating, the server cancels it and triggers retry or failure. *Tested in F9?* KT5 verified timeout cancellation; F3 verified heartbeat cancellation. (Note: F4 failed to configure heartbeats on `rebase_pr_activity`, demonstrating that the protection requires correct activity options).
- **DBOS:** **WOULD PREVENT.** DBOS step execution supports per-step timeouts and queue-level execution timeouts. A hanging step is terminated cleanly upon exceeding its timeout cap. *Tested in F9?* KT5 verified wall-clock termination (`killed-time`) with 0 leaked PIDs.
- **Dagu:** **WOULD PREVENT.** Dagu steps enforce `timeout_sec: N`. When elapsed, Dagu sends `SIGTERM`/`SIGKILL` to the process group. *Tested in F9?* KT5 verified `timeout_sec: 2` terminating a 20s script at 2.0s with 0 leaked PIDs.

### Incident B: Detached Worker Process Orphans and Double-Execution (`start_new_session=True`)
- **What Happened:** `ds_squad.py` launched workers with `start_new_session=True` (line 215). When the runner process was terminated with `kill -9`, the worker processes survived as detached orphans. A subsequent runner restart launched a **second copy** of the same tasks, causing duplicate model spend and Git collisions.
- **Hand-rolled Baseline (`ds_squad.py`):** **CAUSED THE INCIDENT.** Hand-rolled state had no concept of surviving worker adoption. (R7-DS-HARDEN directly fixes this via `RUN/state.json`, checking live PIDs and session directories to adopt surviving workers on restart). *Tested in F9?* KT1 confirmed Baseline re-executes all tasks (76 execs vs 60 required).
- **Temporal:** **WOULD PREVENT.** Temporal activities are tracked by the central server. If a worker process dies and restarts, in-flight activities re-attach or time out; completed activities are never double-launched. *Tested in F9?* KT1 verified 10/10 trials with 60 raw executions (0 duplicates across the suite).
- **DBOS:** **WOULD PREVENT.** DBOS steps are journaled in SQLite with unique workflow and step identifiers. On restart, completed steps return immediately from the database; in-flight steps re-execute only the uncommitted step. *Tested in F9?* KT1 verified 10/10 trials with 68 raw executions (only the 8 interrupted in-flight tasks re-ran; 0 completed tasks re-ran).
- **Dagu:** **WOULD PREVENT.** Dagu features an active zombie process detector (`proc.stale_threshold`) and attempt directories that track running PIDs. On restart, completed steps are preserved and orphaned steps are reaped. *Tested in F9?* KT1 verified 10/10 trials with 79 raw executions (0 completed steps re-run).

### Incident C: Concurrent `git worktree add -b` Collision (`.git/config.lock` Race)
- **What Happened:** Several squad workers simultaneously executed `git worktree add -b`, colliding on Git's internal `.git/config.lock` and causing the first item of four squads to crash immediately.
- **Hand-rolled Baseline (`ds_squad.py`):** **CAUSED THE INCIDENT.** Hand-rolled dispatch executed Git commands concurrently without mutual exclusion. *Tested in F9?* No; F9 used mock agent fixtures.
- **Temporal:** **WOULD NOT PREVENT AUTOMATICALLY.** Temporal does not serialize Git operations unless the developer explicitly routes all Git activities through a single-threaded task queue (`max_concurrent_activities: 1`) or implements a mutex workflow. Concurrent activities across independent workflows still collide on disk. *Tested in F9?* No.
- **DBOS:** **PREVENTS WITH NATIVE QUEUES.** DBOS provides native queue rate-limiting and concurrency controls (`QueueRateLimit`, `worker_concurrency=1` on a dedicated `git-ops` queue), allowing clean serialization of worktree creation without ad-hoc file locks. However, unconstrained concurrent steps would still collide. *Tested in F9?* No.
- **Dagu:** **WOULD NOT PREVENT AUTOMATICALLY.** Dagu supports queue concurrency caps, but steps within parallel DAG branches collide unless wrapped in external synchronization. *Tested in F9?* No.
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
