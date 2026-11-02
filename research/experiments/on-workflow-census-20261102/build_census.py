#!/usr/bin/env python3
"""Build and validate the Eval-Lab Workflow Census.

Generates:
- census.json (structured scores, evidence citations, and metadata)
- CENSUS.md (complete engineering report with tables, rationales, and build order)
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent


@dataclass
class WorkflowScore:
    failure_frequency: int  # 0-3
    human_toil: int  # 0-3
    cost_of_failure: int  # 0-3
    statefulness: int  # 0-3
    need_for_retries_timers_signals: int  # 0-3
    parallelism_limits: int  # 0-3
    auditability: int  # 0-3

    @property
    def total_score(self) -> int:
        return (
            self.failure_frequency
            + self.human_toil
            + self.cost_of_failure
            + self.statefulness
            + self.need_for_retries_timers_signals
            + self.parallelism_limits
            + self.auditability
        )


@dataclass
class WorkflowCandidate:
    id: str
    name: str
    owner_today: str
    runs_on_today: str
    scores: WorkflowScore
    score_evidence: dict[str, str]
    what_durable_workflow_changes: str
    smallest_useful_slice: str
    what_would_not_benefit: str
    incident_citations: list[str]


CANDIDATES: list[WorkflowCandidate] = [
    WorkflowCandidate(
        id="WF-01",
        name="Multi-Agent Squad Dispatch & Worker Lifecycle",
        owner_today="Overnight Coordinator / ds_squad.py daemon",
        runs_on_today="ds_squad.py loop executing headless omp -p CLI subprocesses, polling status files, git worktrees",
        scores=WorkflowScore(
            failure_frequency=3,
            human_toil=3,
            cost_of_failure=3,
            statefulness=3,
            need_for_retries_timers_signals=3,
            parallelism_limits=3,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "Git worktree lock race (.git/config lock) crashed simultaneous launches of T1-BUILD-ANATOMY and T2-UNIFIED-HARDEN (_ds-ao.log:2-10); worker timeout kills without reports (.watch-state.json:39).",
            "human_toil": "Coordinator must babysit runs all night, watch swap memory, manually restart dead workers, and append continue items to plan.json.",
            "cost_of_failure": "Killed workers lose up to 60m of model time ($5-$10/run), exhaust subscriber quotas, and risk uncollected PR deliverables.",
            "statefulness": "Squad runs 4-8 hours across waves with sequential dependency graphs (needs, needs_report).",
            "need_for_retries_timers_signals": "Requires external file-based kill signals (RUN/STOP, gate files), wall-clock timeouts (max_minutes), and retry continuation prompts.",
            "parallelism_limits": "Strict limits: at most 4 concurrent omp -p processes, swap memory <12 GB, model budget caps, provider token ceilings.",
            "auditability": "Must record attested provider/model per assistant turn, token usage, cost accounting in spend.jsonl, and cryptographic prompt/receipt hashes.",
        },
        what_durable_workflow_changes="Temporal replaces the 458-line ds_squad.py poll-loop with durable Child Workflows per agent, built-in Activity heartbeating, dynamic cancellation signals, deterministic retry policies, and persistent execution state surviving host reboots.",
        smallest_useful_slice="A single SquadWorkflow managing 2-3 child agent activities with dependency gating, automatic timeout enforcement, and structured result aggregation.",
        what_would_not_benefit="Ad-hoc single-turn interactive CLI prompts or fast throwaway diagnostic commands (e.g. omp --version).",
        incident_citations=[
            "research-context/bridge/2026-10-31-overnight/ag-opus/_ds-ao.log:2-10 (Git config lock collision killed 2 tasks simultaneously at start)",
            "research-context/bridge/2026-10-31-overnight/.watch-state.json:39 (W3B-EXHAUSTIVE-GOLD-CERTIFIER killed-time with 0 report)",
            "research-context/bridge/2026-10-30-overnight/OVERNIGHT.md:18-20 (ds_squad runner meter undercounting 9x compared to guard balance)",
            "research-context/bridge/2026-11-02-overnight/CHARTER.md:20-30 (Fragile routing constraints across 5 model pools)",
        ],
    ),
    WorkflowCandidate(
        id="WF-02",
        name="Automated PR Merge Train & Spec Harmonization",
        owner_today="Research Coordinator / manual gh CLI loops",
        runs_on_today="Manual gh pr checks polling loops, ad-hoc worker prompts (W2-MERGE-TRAIN, T2A-REBASE), manual git rebase and repomap generate",
        scores=WorkflowScore(
            failure_frequency=3,
            human_toil=3,
            cost_of_failure=3,
            statefulness=2,
            need_for_retries_timers_signals=3,
            parallelism_limits=2,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "W2-MERGE-TRAIN failed within 1.3m without report (_ds-fb2.log:10-12); conflicting PRs on docs/repo-map.md and cli.py stall whole queues.",
            "human_toil": "Coordinator manually reviewed and merged 22 PRs across 7 hours (MORNING-REPORT-2026-11-01.md:5), manually rebasing and polling CI.",
            "cost_of_failure": "Blocked merge train stalls all dependent downstream workers (e.g., W2-TB4-UNION-QA blocked waiting for #1691 in fable-2/ds-squad-fb2.md:112).",
            "statefulness": "Sequential train runs 30-120 minutes per batch, waiting for multi-minute GitHub Actions CI checks on every rebase step.",
            "need_for_retries_timers_signals": "Requires polling GitHub checks every 30s, timeout handling for flaky CI, webhook/signal upon CI pass, and automated conflict recovery.",
            "parallelism_limits": "Strictly serialized single-writer queue for origin/main to prevent race conditions on repo-map.md.",
            "auditability": "Needs immutable records of PR head SHA, squash commit SHA, CI run check details URLs, and governance verification.",
        },
        what_durable_workflow_changes="A MergeTrainWorkflow queues reviewed PRs, provisions isolated worktrees, runs git rebase origin/main, auto-regenerates repo-map.md, pushes with lease, polls CI activities with backoff, and squash-merges atomically without human presence.",
        smallest_useful_slice="A 2-PR linear merge train workflow with automatic repo-map regeneration, CI check polling activity, and auto-merge execution.",
        what_would_not_benefit="Single trivial documentation-only PRs that can already use native GitHub auto-merge safely.",
        incident_citations=[
            "research-context/bridge/2026-10-31-overnight/fable-2/_ds-fb2.log:10-12 (W2-MERGE-TRAIN failed after 1.3 min, leaving retry loop broken)",
            "research-context/bridge/2026-10-31-overnight/fable-2/ds-squad-fb2.md:1-25 (T2A-REBASE required manual rebase of PR #1691 after #1693 merged)",
            "research-context/bridge/2026-10-31-overnight/MORNING-REPORT-2026-11-01.md:5 (22 PRs required continuous manual coordinator babysitting and merge gating)",
        ],
    ),
    WorkflowCandidate(
        id="WF-03",
        name="Corpus-Scale Certify-Gold & STING Mutation Pipeline",
        owner_today="CLI evallab certify-gold / overnight subagents",
        runs_on_today="Synchronous Python CLI scripts driving Modal container sandboxes sequentially or in ad-hoc threads with CloudSpendGate",
        scores=WorkflowScore(
            failure_frequency=3,
            human_toil=2,
            cost_of_failure=3,
            statefulness=3,
            need_for_retries_timers_signals=3,
            parallelism_limits=3,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "W3B-EXHAUSTIVE-GOLD-CERTIFIER timed out at 60m (.watch-state.json:39); spend gate reservation exceeded ledger cap prematurely (T2-UNIFIED-HARDEN/report.md:44).",
            "human_toil": "Requires manual continuation ledger creation (e.g. t3-safeguard-bench-ledger-continuation.json) and setting up rerun matrices.",
            "cost_of_failure": "Aborted runs lose container sandbox charges, leaving tasks half-certified (e.g. K6 left task 000232 unproven in NARRATIVE.md:42).",
            "statefulness": "Multi-phase pipeline runs 1-4 hours (AST mutation -> reference run -> nop run -> witness run -> synthesized tests -> held-out evaluation).",
            "need_for_retries_timers_signals": "Requires per-cell retry with exponential backoff for Modal network hiccups, per-task timeout timers (300s), and completion signals.",
            "parallelism_limits": "Bounded by Modal concurrent container quotas, CPU/RAM container allocations, and spend reservation rate limits.",
            "auditability": "Must record immutable SHA-256 package digests, mutant AST patches, execution exit codes, and 95% Wilson confidence intervals.",
        },
        what_durable_workflow_changes="A CertifyGoldWorkflow coordinates AST mutation activities, fans out parallel Modal execution activities with individual retries, tracks cumulative metered spend rather than static block reservations, and checkpoints progress so crashes resume mid-batch.",
        smallest_useful_slice="A 1-task 6-gate certification workflow executing reference, nop, and 5 mutant cells in parallel on Modal with durable retry.",
        what_would_not_benefit="Static AST inspections (evallab tasks anatomy) that complete in <10 ms without containers or spend.",
        incident_citations=[
            "research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV2/report.md:115 (Gold certifier timed out, leaving 8 trials without report/PR)",
            "research-context/bridge/2026-10-31-overnight/ag-sonnet/raw/T3-SAFEGUARD-BENCH/report.md:73 (CloudSpendGate reservation exhausted $2.50 cap on $0.0089 real spend)",
            "research-context/bridge/2026-10-31-overnight/ag-opus/ds-squad-ao.md:51 (26 cells refused before execution by spend-gate estimate flaw)",
        ],
    ),
    WorkflowCandidate(
        id="WF-04",
        name="Continuous Causal Trace Ingestion & Outbox Relay",
        owner_today="LaunchAgent prod-factory / ContinuousWorkQueue",
        runs_on_today="Local Python service managed by launchd, polling SQLite journal, custom outbox relay to NATS/HTTP",
        scores=WorkflowScore(
            failure_frequency=2,
            human_toil=2,
            cost_of_failure=3,
            statefulness=3,
            need_for_retries_timers_signals=3,
            parallelism_limits=2,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "2026-10-24 service re-queued 33 items 12 times (factory.py:24); quadratic recovery on startup forced minutes of rehydration (factory.py:31).",
            "human_toil": "Requires custom watchdog scripts, LaunchAgent plist maintenance, and manual database recovery after crash.",
            "cost_of_failure": "Duplicate processing, dropped causal trace events, corrupted outbox publication state, and monitor desynchronization.",
            "statefulness": "Runs continuously (24/7 daemon) streaming thousands of trace steps across multi-step agent rollouts.",
            "need_for_retries_timers_signals": "Requires at-least-once outbox delivery with broker pub-ack, deduplication tokens, and lease renewal heartbeats.",
            "parallelism_limits": "Single-writer constraint on SQLite database requires careful serialization of global sequence allocation.",
            "auditability": "Every event must have an immutable sequence number, SHA-256 event ID, and strict quarantine handling for poison messages.",
        },
        what_durable_workflow_changes="Temporal replaces custom transactional outbox tables and polling replayers with event-driven Signal methods, durable event buffering, and native activity retries, eliminating SQLite write lock contention.",
        smallest_useful_slice="A TraceIngestWorkflow that ingests raw ATIF trace steps, writes to CAS storage, and reliably signals the live monitor.",
        what_would_not_benefit="Static offline analysis of a completed trace JSON file on local disk.",
        incident_citations=[
            "src/evallab/production/factory.py:22-31 (Documented 33 items re-queued 12x and quadratic start-up recovery)",
            "src/evallab/production/outbox_relay.py:1-8 (Hand-rolled at-least-once transactional outbox relay with JetStream pub-ack)",
            "src/evallab/production/corpus_queue.py:20-22 (Leases requiring heartbeat renewal and unguessable fencing tokens)",
        ],
    ),
    WorkflowCandidate(
        id="WF-05",
        name="Cloud Spend Gate & Dynamic Budget Ledger",
        owner_today="evallab.regrade.CloudSpendGate / spend.jsonl",
        runs_on_today="In-process threading.Lock + append-only JSON/JSONL ledgers on shared disk",
        scores=WorkflowScore(
            failure_frequency=3,
            human_toil=2,
            cost_of_failure=3,
            statefulness=3,
            need_for_retries_timers_signals=2,
            parallelism_limits=3,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "CloudSpendGate reserves fixed $0.05/call estimates, halting batches when 25% of dollar budget is spent (NARRATIVE.md:41, T3-SAFEGUARD-BENCH/report.md:73).",
            "human_toil": "Operators must write manual continuation ledgers with synthetic approval strings to resume aborted batches.",
            "cost_of_failure": "Aborts large experiment sweeps mid-flight, leaving incomplete cells that cannot be compared scientifically.",
            "statefulness": "Budgets span across hours and multiple squad waves, needing consistent multi-process tracking.",
            "need_for_retries_timers_signals": "Needs reservation expiration timers, settlement updates from real cloud provider meters, and budget adjustment signals.",
            "parallelism_limits": "High contention across concurrent worktrees competing for shared file locks on spend.jsonl and ledger files.",
            "auditability": "Must strictly comply with standing spend policy (policy/standing-approvals.yaml) and produce unforgeable receipts.",
        },
        what_durable_workflow_changes="A SpendGateWorkflow/Actor maintains live reservations, receives settlement signals with actual metered costs, releases expired reservations automatically, and dynamically alerts operators before hard failure.",
        smallest_useful_slice="A centralized budget workflow tracking Modal and LLM reservations with settlement updates and automatic lease expiration.",
        what_would_not_benefit="Free local zero-cost checks (ruff, pytest, git commands).",
        incident_citations=[
            "research-context/bridge/2026-11-02-overnight/NARRATIVE.md:41 (CloudSpendGate flat reservation exhausts caps at 1/4 of real spend)",
            "src/evallab/regrade.py:241-298 (Reservations are estimates kept even on failed/short executions)",
            "research-context/bridge/2026-10-17-v7/classifiers-v7-canonical-incomplete-receipt.json:251-280 (49 cumulative reservations stalled runs on $0.26 shortfall)",
        ],
    ),
    WorkflowCandidate(
        id="WF-06",
        name="Live Trace Auto-Review & Cascading Tripwire Monitor",
        owner_today="evallab.production.monitor_worker / MisalignmentLiveWorker",
        runs_on_today="In-memory PrefixScanner, ThreadPoolExecutor for cheap LLM calls, LRU finished run cache",
        scores=WorkflowScore(
            failure_frequency=2,
            human_toil=1,
            cost_of_failure=2,
            statefulness=2,
            need_for_retries_timers_signals=2,
            parallelism_limits=2,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "Inverted false-alarm rate on TW DEV (20.5% clean FA vs 6.3% hack in MORNING-REPORT-2026-11-01.md:15); memory leak from unbounded finished runs.",
            "human_toil": "Manual tuning of cascade thresholds, debugging detector memory growth.",
            "cost_of_failure": "False alarms kill innocent runs; missed exploits contaminate training datasets or benchmark scores.",
            "statefulness": "Maintains incremental prefix state for dozens of active runs over 10-60 minutes.",
            "need_for_retries_timers_signals": "Requires event streaming, Stage-2 asynchronous follow-up triggers, and stop/quarantine signals to running agents.",
            "parallelism_limits": "Limits on concurrent LLM judge calls to avoid provider 429 rate limits.",
            "auditability": "Requires 100% CoT-free verification, zero-leak dev gates, and deterministic causal tripwire logging.",
        },
        what_durable_workflow_changes="A MonitorWorkflow processes step events via Signals, evaluates L0 rules, schedules Stage-2 LLM reviews via activities with rate-limiting, and issues durable pause/quarantine commands.",
        smallest_useful_slice="A single-run monitor workflow receiving step signals and executing L0 prefix tripwires with state persistence.",
        what_would_not_benefit="Single-turn static regex matching on completed patches.",
        incident_citations=[
            "src/evallab/production/monitor_worker.py:1-68 (Event-driven causal monitor with complex gate rules)",
            "research-context/bridge/2026-10-31-overnight/fable/raw/T1-LIVE-FIX/report.md:11-20 (Fixing LRU memory bound and inverted false alarms)",
        ],
    ),
    WorkflowCandidate(
        id="WF-07",
        name="Provider Quota-Aware Fleet Routing & Rate Throttling",
        owner_today="ds_throttle.py, ds_budget_guard.py, queue_keeper.py",
        runs_on_today="Ad-hoc background Python watchdog scripts polling process trees and sleeping",
        scores=WorkflowScore(
            failure_frequency=3,
            human_toil=2,
            cost_of_failure=2,
            statefulness=3,
            need_for_retries_timers_signals=3,
            parallelism_limits=3,
            auditability=2,
        ),
        score_evidence={
            "failure_frequency": "Direct Claude 7-Day quota hit 100% at 12:57 AM ET (MORNING-REPORT-2026-11-01.md:84); queue keeper made 91 blocked attempts in 41 min (OVERNIGHT.md:44-45).",
            "human_toil": "Coordinator must inspect quotas manually before overnight shifts and re-route workers away from depleted accounts.",
            "cost_of_failure": "Abrupt 429 or quota stops kill running agent sessions without saving partial work.",
            "statefulness": "Runs across multi-day rolling windows (7-day Claude quota, weekly Devin quota, monthly balance).",
            "need_for_retries_timers_signals": "Requires scheduled sleep timers (waiting for off-peak hours at 6 AM ET), rate-limiting backoff, and quota refresh signals.",
            "parallelism_limits": "Complex matrix: max 4 OMP processes, max 16 nested model calls, provider concurrency limits.",
            "auditability": "Needs attested provider/model tracking to prove no unauthorized model fallback occurred.",
        },
        what_durable_workflow_changes="A FleetRouterWorkflow manages global provider rate-limiters, queues worker dispatches during off-peak windows, pauses when quotas near ceilings, and dynamically shifts routes.",
        smallest_useful_slice="A token-bucket rate limiter workflow for OpenRouter/DeepSeek with sleep-until-reset capability.",
        what_would_not_benefit="Static local CPU-only script executions.",
        incident_citations=[
            "research-context/bridge/2026-10-30-overnight/OVERNIGHT.md:44-45 (Queue keeper looped 91 blocked attempts in 41 minutes on stale routes)",
            "research-context/bridge/2026-10-31-overnight/MORNING-REPORT-2026-11-01.md:80-84 (Claude 7-day 100% quota burnout mid-shift)",
        ],
    ),
    WorkflowCandidate(
        id="WF-08",
        name="Scheduled Morning Report & Recomputation Verifier (VF Fleet)",
        owner_today="Coordinator / ad-hoc VF subagents (VF-OV1, VF-OV2)",
        runs_on_today="Morning coordinator compilation, ad-hoc python scripts in /tmp (VF-OV2/report.md:196)",
        scores=WorkflowScore(
            failure_frequency=2,
            human_toil=3,
            cost_of_failure=2,
            statefulness=2,
            need_for_retries_timers_signals=2,
            parallelism_limits=2,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "Verifiers caught wrong significance tests (p=0.031 vs p=0.109 in VF-OV1/report.md:45), post-hoc definitions in headlines, and lost /tmp receipts.",
            "human_toil": "Coordinator spends 2+ hours every morning gathering numbers, verifying hashes, and reconciling prose claims.",
            "cost_of_failure": "Publishing unverified or post-hoc numbers damages scientific credibility and violates strict reporting contracts.",
            "statefulness": "Runs for 1-2 hours aggregating artifacts produced over the entire 8-hour overnight shift.",
            "need_for_retries_timers_signals": "Requires cron-style scheduling (08:30 ET trigger), collecting outputs from completed squads, and verification fan-out.",
            "parallelism_limits": "Requires independent model verification (e.g. Claude Opus blind recomputation).",
            "auditability": "Extreme: every number in prose must match raw receipt hashes; discordant McNemar pairs; 95% Wilson intervals.",
        },
        what_durable_workflow_changes="A MorningReportWorkflow automatically triggers at shift end, runs deterministic metric recomputation activities, executes independent verification checks, validates receipt durability, and formats the report.",
        smallest_useful_slice="An automated verification workflow that scans raw receipts, computes McNemar p-values and Wilson intervals, and checks hash consistency.",
        what_would_not_benefit="Ad-hoc free-form qualitative notes or exploratory scratchpad discussions.",
        incident_citations=[
            "research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/report.md:42-54 (Caught 2 cited receipts in /tmp/t1pf and post-hoc cascade rules)",
            "research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV2/report.md:196-198 (Throwaway recomputation scripts left in /tmp/vf-ov2/)",
        ],
    ),
    WorkflowCandidate(
        id="WF-09",
        name="Judge Station Signal Collection & Human Calibration",
        owner_today="FastAPI control_plane.py / human reviewer",
        runs_on_today="FastAPI ASGI app with SSE stream /api/stream and local SQLite, manual browser UI",
        scores=WorkflowScore(
            failure_frequency=1,
            human_toil=3,
            cost_of_failure=2,
            statefulness=3,
            need_for_retries_timers_signals=3,
            parallelism_limits=1,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "Human calibration backlog stall (69 uncompleted verdicts in NARRATIVE.md:39); SSE cursor disconnects failing with HTTP 409 (control_plane.py:10).",
            "human_toil": "High manual effort required for humans to review blinded runs and submit verdicts without session state loss.",
            "cost_of_failure": "Without human ground truth calibration, automated LLM monitors cannot be statistically validated.",
            "statefulness": "Human review tasks can remain pending for hours or days waiting for operator availability.",
            "need_for_retries_timers_signals": "Classic human-in-the-loop workflow: needs external Signals to unblock execution, reminder timers, and timeout escalations.",
            "parallelism_limits": "Bounded by human operator bandwidth.",
            "auditability": "Blinded review protocol requires strict redaction of model identities and benchmark labels until submission.",
        },
        what_durable_workflow_changes="Temporal handles human-in-the-loop workflows natively: workflows wait durable days for human verdict Signals without consuming server resources or risking state loss on server restarts.",
        smallest_useful_slice="A HumanReviewWorkflow that serves a blinded case bundle, waits for an operator verdict Signal, and updates calibration metrics.",
        what_would_not_benefit="Fully automated batch heuristic scoring with no human in the loop.",
        incident_citations=[
            "research-context/bridge/2026-11-02-overnight/NARRATIVE.md:39 (69 Judge Station human verdicts pending, stalling calibration)",
            "src/evallab/production/control_plane.py:8-14 (Cursor reconnection conflicts on HTTP 409, manual intervention tokens)",
        ],
    ),
    WorkflowCandidate(
        id="WF-10",
        name="Agent Rollout Filtering & SFT Hack-Drop Pipeline",
        owner_today="Research scripts / C1-HACKDROP",
        runs_on_today="Ad-hoc Python scripts reading raw CAS sqlite databases, filtering steps, generating training JSONL",
        scores=WorkflowScore(
            failure_frequency=2,
            human_toil=2,
            cost_of_failure=3,
            statefulness=2,
            need_for_retries_timers_signals=1,
            parallelism_limits=2,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "Historical projection dropped 212M chars across 98.1% of Goodfire DEV runs (MORNING-REPORT-2026-11-01.md:26); deep_swe had 0 runs in cohort.",
            "human_toil": "Requires manual dataset verification, spot-checking regex rules, and re-running failed exports.",
            "cost_of_failure": "Reward-hacked or truncated trajectories leaking into SFT/RL training sets corrupt downstream model behavior.",
            "statefulness": "Runs for 30-90 minutes processing gigabytes of raw trajectory data.",
            "need_for_retries_timers_signals": "Requires checkpointed chunk processing so multi-GB trajectory exports do not start over on OOM.",
            "parallelism_limits": "Disk I/O and SQLite read lock contention across multi-GB database files.",
            "auditability": "Must record exact sha256 manifests of included/dropped runs and provide reproducible filter provenance.",
        },
        what_durable_workflow_changes="A HackDropWorkflow executes chunked ingestion activities with deterministic resumption, applies verified AST and tripwire filters, and commits export bundles atomically.",
        smallest_useful_slice="A batch filtering workflow that takes 100 raw trajectories, runs L0 tripwire checks in parallel, and emits a clean JSONL dataset.",
        what_would_not_benefit="Filtering a small local CSV file with <1,000 rows.",
        incident_citations=[
            "research-context/bridge/2026-10-31-overnight/fable/raw/T1-AUDIT-PARSE/report.md:24-28 (212.4M chars dropped, slicing 10,931 file mutations)",
            "research-context/bridge/2026-11-02-overnight/train/raw/C1-HACKDROP/prompt.md:1-25 (Tonight's hack-drop pipeline requirements)",
        ],
    ),
    WorkflowCandidate(
        id="WF-11",
        name="Multi-Model Verifier Fan-Out & Consensus Adjudication",
        owner_today="swarm_coordinator.py / swarm_bench.py",
        runs_on_today="Python asyncio/threading fan-out calling Anthropic, Google, and DeepSeek APIs concurrently",
        scores=WorkflowScore(
            failure_frequency=2,
            human_toil=2,
            cost_of_failure=2,
            statefulness=2,
            need_for_retries_timers_signals=2,
            parallelism_limits=3,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "Out-of-schema responses from Sonnet/Opus required manual rerun (ds-squad-ao.md:234); judge disagreement moves gated arm by ±2 keys (VF-OV2/report.md:241).",
            "human_toil": "Reconciling conflicting model adjudications, patching JSON schema parser errors.",
            "cost_of_failure": "Unreliable consensus scores invalidate expensive benchmark comparisons.",
            "statefulness": "Runs for 20-60 minutes across multi-model ensembles.",
            "need_for_retries_timers_signals": "Needs per-provider timeout, schema validation retries, and fallback to tie-breaking adjudicators.",
            "parallelism_limits": "High: distributes across 3+ providers with distinct concurrency caps.",
            "auditability": "Requires full capture of raw prompt/completion tokens, latency, and consensus voting records.",
        },
        what_durable_workflow_changes="A ConsensusAdjudicationWorkflow fans out parallel evaluation activities to multiple model providers, enforces per-provider timeouts, and routes discordant cases to a tie-breaker activity.",
        smallest_useful_slice="A 3-model parallel jury workflow that validates outputs against a strict Pydantic schema and returns a majority verdict.",
        what_would_not_benefit="Single-model prompt evaluations with deterministic output formats.",
        incident_citations=[
            "src/evallab/production/swarm_coordinator.py:1-60 (Swarm coordinator managing multi-agent round consensus)",
            "research-context/bridge/2026-10-31-overnight/ag-opus/ds-squad-ao.md:234 (D0009 and D0017 required second attempt due to scorer schema failure)",
        ],
    ),
    WorkflowCandidate(
        id="WF-12",
        name="Live Supervisor & Alarm Actuator",
        owner_today="live_supervisor.py / launchd services",
        runs_on_today="LaunchAgent polling cycle executing live_supervisor.py, reading SQLite, writing JSONL findings",
        scores=WorkflowScore(
            failure_frequency=2,
            human_toil=2,
            cost_of_failure=2,
            statefulness=3,
            need_for_retries_timers_signals=3,
            parallelism_limits=1,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "Silence trigger kill ratio issues (15m threshold vs 45m latency gap); unhandled LaunchAgent restarts.",
            "human_toil": "Investigating false-positive alarms, manually managing shadow vs act modes.",
            "cost_of_failure": "Uncaught runaway agent burns API credits; erroneous kill stops valid long-running benchmark trials.",
            "statefulness": "Continuous background surveillance over hours of fleet execution.",
            "need_for_retries_timers_signals": "Needs cooldown timers (DEFAULT_COOLDOWN = 30m), trigger prioritization, and kill signals.",
            "parallelism_limits": "Low concurrency, but monitors all active fleet subjects simultaneously.",
            "auditability": "Appends trace-findings/v1 records with complete evidence lists and safety guard verifications.",
        },
        what_durable_workflow_changes="A SupervisorWorkflow tracks live fleet subjects as stateful entities, receives heartbeat signals, schedules cooldown timers natively, and executes kill actions safely.",
        smallest_useful_slice="A watchdog workflow monitoring a single running job and signaling a kill if no heartbeat is received within 15 minutes.",
        what_would_not_benefit="Stateless post-mortem log parsers.",
        incident_citations=[
            "src/evallab/production/live_supervisor.py:33-51 (Priority triggers, silence kill ratios, and 30m cooldown timers)",
            "scripts/ops/launchd/com.petermakhnatch.evallab.live-supervisor.plist (Launchd configuration)",
        ],
    ),
    WorkflowCandidate(
        id="WF-13",
        name="Task Hardening Package Synthesis & Canary Validation",
        owner_today="evallab harden / package_anatomy.py",
        runs_on_today="CLI scripts parsing task packages, injecting AST fixtures, deploying Modal canaries",
        scores=WorkflowScore(
            failure_frequency=2,
            human_toil=2,
            cost_of_failure=2,
            statefulness=2,
            need_for_retries_timers_signals=2,
            parallelism_limits=2,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "grader-hardening@1 crashed with FileNotFoundError on general Harbor packages; failed on Python 2.7 images (T2-UNIFIED-HARDEN.md:6).",
            "human_toil": "Manual inspection of Dockerfile layers and container environment debugging.",
            "cost_of_failure": "Deploying flawed hardening creates false positives for honest agents or leaves exploits unblocked.",
            "statefulness": "15-45 minutes per task package batch.",
            "need_for_retries_timers_signals": "Needs isolated canary test runs with timeouts and failure rollbacks.",
            "parallelism_limits": "Bounded by Modal container concurrency and local Docker build isolation.",
            "auditability": "Must verify 5-cell composition proof (1/0/0/0/0) and record package digests.",
        },
        what_durable_workflow_changes="A TaskHardeningWorkflow coordinates L1-L4 test generation, packages Docker images, runs canary verification trials, and commits hardened specs only upon 100% gate pass.",
        smallest_useful_slice="A workflow hardening a single task package across L1-L4 layers with canary validation.",
        what_would_not_benefit="Static regex analysis of a single shell script.",
        incident_citations=[
            "src/evallab/full_hardening.py:1-50 (Unified L1-L4 container hardening implementation)",
            "research-context/bridge/2026-10-31-overnight/ag-opus/briefs/T2-UNIFIED-HARDEN.md:5-9 (Historical hardening failures across 5 impossible tasks)",
        ],
    ),
    WorkflowCandidate(
        id="WF-14",
        name="Public Corpus Mirror Sync & CAS Blob Ingestion",
        owner_today="cloud_trace_retriever.py / asset_store.py",
        runs_on_today="Python scripts downloading parquet/tarballs into local SQLite / CAS directories",
        scores=WorkflowScore(
            failure_frequency=2,
            human_toil=1,
            cost_of_failure=2,
            statefulness=2,
            need_for_retries_timers_signals=2,
            parallelism_limits=1,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "Multi-gigabyte SQLite database contention; .opus audio file corruption during redaction (PR #1710).",
            "human_toil": "Verifying download integrity, cleaning up failed partial downloads.",
            "cost_of_failure": "Corrupted or incomplete corpus sync breaks evaluation reproducibility.",
            "statefulness": "30-60 minutes downloading and unpacking gigabytes of external traces.",
            "need_for_retries_timers_signals": "HTTP download retries with range headers and chunk validation.",
            "parallelism_limits": "Network bandwidth and disk I/O.",
            "auditability": "Content-addressed storage (CAS) with SHA-256 verification of every stored object.",
        },
        what_durable_workflow_changes="A CorpusSyncWorkflow downloads, validates checksums, unpacks, and registers datasets with resume capability on interrupted transfers.",
        smallest_useful_slice="An ingestion activity that downloads a single parquet shard and writes CAS objects atomically.",
        what_would_not_benefit="Local file copies of small test fixtures.",
        incident_citations=[
            "src/evallab/production/cloud_trace_db.py:1-40 (4.0 GB SQLite trace database management)",
            "research-context/bridge/2026-10-31-overnight/ag-opus/briefs/VF-OV1.md:8 (PR #1710 fixing .opus audio extension corruption and ATIF unpacking)",
        ],
    ),
    WorkflowCandidate(
        id="WF-15",
        name="Corpus Clearance, Split Locking & Anti-Leak Audit",
        owner_today="corpus_clearance.py / CorpusRegistry",
        runs_on_today="In-memory validation in Python loaders and pre-action check scripts",
        scores=WorkflowScore(
            failure_frequency=1,
            human_toil=2,
            cost_of_failure=3,
            statefulness=1,
            need_for_retries_timers_signals=1,
            parallelism_limits=1,
            auditability=3,
        ),
        score_evidence={
            "failure_frequency": "Historical leak risks where Goodfire 95 held SWE tasks had to be guarded by runtime exceptions (SplitLockError).",
            "human_toil": "Manual verification that no TEST tasks leaked into DEV cohorts or prompt contexts.",
            "cost_of_failure": "Catastrophic: contamination of evaluation benchmarks invalidates research results.",
            "statefulness": "1-10 minutes per audit sweep.",
            "need_for_retries_timers_signals": "Fail-closed validation with hard error propagation.",
            "parallelism_limits": "Low.",
            "auditability": "Strict audit trails proving zero answer-key reads and zero split seal violations.",
        },
        what_durable_workflow_changes="A ClearanceWorkflow runs automated audit activities across workspaces and outputs cryptographic clearance tokens before jobs can be dispatched.",
        smallest_useful_slice="An automated pre-flight audit activity validating cohort IDs against the frozen registry.",
        what_would_not_benefit="Pure in-memory unit tests.",
        incident_citations=[
            "src/evallab/production/corpus_clearance.py:1-50 (Corpus clearance checks and split isolation)",
            "src/evallab/production/corpus_queue.py:54-56 (SplitLockError preventing sealed task access)",
        ],
    ),
    WorkflowCandidate(
        id="WF-16",
        name="Local Production Factory Deploy, Upgrade & Rollback",
        owner_today="install-prod-factory.sh / operator bash",
        runs_on_today="Bash scripts executing git archive, uv sync, and launchctl bootout/bootstrap",
        scores=WorkflowScore(
            failure_frequency=2,
            human_toil=2,
            cost_of_failure=2,
            statefulness=1,
            need_for_retries_timers_signals=2,
            parallelism_limits=1,
            auditability=2,
        ),
        score_evidence={
            "failure_frequency": "Stale launchd process locks; lingering SQLite WAL files during release switches; heartbeat stall on startup.",
            "human_toil": "Manual shell execution of install scripts and checking launchctl status.",
            "cost_of_failure": "Failed deploy halts background trace ingestion and monitoring.",
            "statefulness": "5-15 minutes to archive, build venv, and restart LaunchAgents.",
            "need_for_retries_timers_signals": "Requires health check polling and automatic rollback on verification failure.",
            "parallelism_limits": "Strictly single instance.",
            "auditability": "Git commit SHA releases stored under releases/<sha>.",
        },
        what_durable_workflow_changes="A FactoryDeployWorkflow orchestrates release preparation, health-checks the new service, and rolls back cleanly on watchdog failure.",
        smallest_useful_slice="A deployment script with automated post-start heartbeat verification and rollback.",
        what_would_not_benefit="Local hot-reloading development servers.",
        incident_citations=[
            "scripts/ops/launchd/install-prod-factory.sh:1-60 (Bash script managing immutable releases and launchctl bootstrap)",
            "scripts/ops/launchd/com.petermakhnatch.evallab.prod-factory-watchdog.plist (Watchdog monitoring service heartbeat)",
        ],
    ),
]

NON_CANDIDATES = [
    {
        "name": "Deterministic CLI Linters & Repo Checks (ruff, repomap, governance)",
        "why_not": "These tools run locally in sub-second to a few seconds, have zero state, zero external network dependencies, and require no retry logic or signals. Wrapping them in a durable orchestrator introduces pure latency and complexity overhead with zero reliability benefit.",
        "best_runtime": "Local shell execution or pre-commit hook via uv run.",
    },
    {
        "name": "Read-Only Dashboard & Viewer Servers (results-viewer, scout-view)",
        "why_not": "These are read-only HTTP/Streamlit servers serving static files or querying SQLite. They do not have multi-step lifecycles, long-running batch executions, or persistent workflows. Standard system service managers (launchd / systemd) handle keep-alive and restart on failure adequately.",
        "best_runtime": "LaunchAgent / background daemon with system keepalive.",
    },
    {
        "name": "Local Fast Unit Tests (pytest unit tests)",
        "why_not": "Targeted unit tests run in seconds in memory. Durable execution is designed for operations that survive process crashes, reboots, and multi-hour network calls, not short test suites.",
        "best_runtime": "Direct CLI (pytest -q).",
    },
    {
        "name": "Static Package & Vulnerability Parser (evallab tasks anatomy)",
        "why_not": "Model-free, container-free AST and regex parser running in 3.44 ms p50. It produces deterministic JSON cards in-process with no statefulness.",
        "best_runtime": "Standard Python in-process library call.",
    },
    {
        "name": "Single-Branch Git Mirror Sync (sync-primary.sh)",
        "why_not": "A 10-line bash script running git fetch && git fast-forward on a 60-second cron interval. A failure is simply retried on the next tick; there is no intermediate state to preserve.",
        "best_runtime": "Simple cron / launchd interval timer.",
    },
]


def generate_markdown(candidates: list[WorkflowCandidate], top_5: list[WorkflowCandidate]) -> str:
    md: list[str] = []
    md.append("# Eval-Lab Internal Workflow Census: Durable Orchestration Candidates")
    md.append("")
    md.append("**Date:** 2026-11-02  ")
    md.append("**Context:** Eval-Lab overnight fleet operations, research pipelines, and production services.  ")
    md.append("**Purpose:** Evidence-backed census of internal workflows to identify the highest-ROI candidates for durable workflow orchestration (Temporal and similar), prototype selection (F3-F5), and strategic roadmap (F6).")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## Executive Summary")
    md.append("")
    md.append("Eval-lab runs dozens of complex, distributed, multi-hour operations across local worker fleets, cloud sandboxes (Modal), model providers, and background daemons. Today, almost all orchestration is **hand-rolled** in custom Python scripts (`ds_squad.py`, `factory.py`, `regrade.py:CloudSpendGate`, `queue_keeper.py`).")
    md.append("")
    md.append("An exhaustive audit of overnight logs (`_ds*.log`), morning reports, verifier receipts (`VF-OV1`, `VF-OV2`), and spend ledgers reveals systematic operational failure modes:")
    md.append("1. **Concurrency Race Conditions:** Git lock contention (`.git/config`) crashing parallel worker initialization (`_ds-ao.log:2-10`).")
    md.append("2. **Timeout Kills Without Reports:** Long batch operations (e.g. `W3B-EXHAUSTIVE-GOLD-CERTIFIER`) killed at wall-clock limits with zero report and unrecovered spend (`.watch-state.json:39`).")
    md.append("3. **Premature Spend Gate Halts:** Inflexible upfront reservation arithmetic in `CloudSpendGate` reserving $0.05/cell, halting batches when only $0.0089 is actually spent (`T3-SAFEGUARD-BENCH/report.md:73`).")
    md.append("4. **Ephemeral Receipt Storage:** Critical per-run readings left in `/tmp` that cannot survive a reboot (`VF-OV1/report.md:42,93`).")
    md.append("5. **Severe Merge Train Toil:** Coordinators spending 7+ hours manually babysitting 22 PR rebases and CI polling loops because ad-hoc merge agents fail (`fable-2/_ds-fb2.log:10-12`).")
    md.append("")
    md.append("Durable workflow orchestration directly eliminates these failure modes by providing state persistence across process crashes, first-class timers/signals, deterministic retry policies, and centralized quota management.")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## Census & Scoring Matrix (16 Workflows)")
    md.append("")
    md.append("Each workflow is evaluated across seven 0-3 quantitative dimensions (Total Score max 21):")
    md.append("- **Fail (Failure Frequency):** 0 = rare, 1 = weekly, 2 = multiple per shift, 3 = near-constant/every run.")
    md.append("- **Toil (Human Toil):** 0 = fire-and-forget, 1 = occasional, 2 = active polling/restarts, 3 = constant babysitting.")
    md.append("- **Cost (Cost of Failure):** 0 = trivial $0, 1 = <10m lost, 2 = $10-$50 or hours lost, 3 = ruined shift/quota blowout.")
    md.append("- **State (Statefulness/Duration):** 0 = <1m, 1 = 1-15m, 2 = 15-120m, 3 = multi-hour / continuous daemon.")
    md.append("- **Retry (Retries, Timers & Signals):** 0 = linear script, 1 = basic retry, 2 = timeouts+retries, 3 = complex signals/approvals/timers.")
    md.append("- **Limits (Parallelism & Quota Limits):** 0 = unconstrained, 1 = simple pool, 2 = multi-provider caps, 3 = dynamic quota/rate throttling.")
    md.append("- **Audit (Auditability & Provenance):** 0 = ephemeral stdout, 1 = basic logs, 2 = structured receipts, 3 = cryptographic hashes/Wilson CIs.")
    md.append("")
    md.append("| Rank | ID | Workflow Name | Owner Today | Fail | Toil | Cost | State | Retry | Limits | Audit | Total |")
    md.append("|:---:|:---:|:---|:---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|")

    for rank, cand in enumerate(candidates, 1):
        s = cand.scores
        md.append(
            f"| **{rank}** | `{cand.id}` | {cand.name} | {cand.owner_today} | "
            f"{s.failure_frequency} | {s.human_toil} | {s.cost_of_failure} | "
            f"{s.statefulness} | {s.need_for_retries_timers_signals} | "
            f"{s.parallelism_limits} | {s.auditability} | **{s.total_score}** |"
        )

    md.append("")
    md.append("---")
    md.append("")
    md.append("## Top 5 Candidates: Deep Rationale & Impact")
    md.append("")

    for rank, cand in enumerate(top_5, 1):
        md.append(f"### {rank}. `{cand.id}`: {cand.name} (Score: {cand.scores.total_score}/21)")
        md.append(f"**Current Implementation:** {cand.runs_on_today}  ")
        md.append(f"**Owner Today:** {cand.owner_today}  ")
        md.append("")
        md.append(f"**Rationale:** {cand.what_durable_workflow_changes}")
        md.append("")
        md.append(f"**Smallest Useful Slice:** {cand.smallest_useful_slice}")
        md.append("")
        md.append(f"**What Would NOT Benefit:** {cand.what_would_not_benefit}")
        md.append("")
        md.append("**Concrete Incidents & Evidence:**")
        for inc in cand.incident_citations:
            md.append(f"- `{inc}`")
        md.append("")

    md.append("---")
    md.append("")
    md.append("## Suggested Build Order (Prototypes F3-F5 & Beyond)")
    md.append("")
    md.append("1. **Phase 1: Multi-Agent Squad Dispatch & Worker Lifecycle (`WF-01`, Item F3 Prototype)**")
    md.append("   - *Why first:* Solves immediate operational bleeding in nightly fleet runs. Eliminates git worktree race conditions, process-kill zombies, and silent timeout deaths without reports.")
    md.append("   - *Key primitive:* Temporal Child Workflows + Activity Heartbeating + Signal-based continuation prompts.")
    md.append("")
    md.append("2. **Phase 2: Automated PR Merge Train & Spec Harmonization (`WF-02`, Item F4 Prototype)**")
    md.append("   - *Why second:* Unblocks developer and coordinator throughput. Replaces hours of manual CI polling and git rebase commands with a reliable, sequential merge queue that automatically updates `repo-map.md`.")
    md.append("   - *Key primitive:* Queued workflow activities polling GitHub Actions status, executing rebase in isolated worktrees, and squash-merging.")
    md.append("")
    md.append("3. **Phase 3: Corpus-Scale Certify-Gold & STING Mutation Pipeline (`WF-03`, Item F5 Prototype)**")
    md.append("   - *Why third:* Scales the core eval-lab scientific value proposition (certifying tasks and detecting weak graders). Replaces brittle multi-hour CLI scripts with resilient, parallel Modal sandbox execution and checkpointed progress.")
    md.append("   - *Key primitive:* Dynamic parallel Activity fan-out across Modal sandboxes with per-cell timeout and retry.")
    md.append("")
    md.append("4. **Phase 4: Cloud Spend Gate & Dynamic Budget Ledger (`WF-05`)**")
    md.append("   - *Why fourth:* Decouples static reservation estimates from actual metered cloud bills, preventing premature batch shutdowns while maintaining strict financial safety.")
    md.append("   - *Key primitive:* Stateful Workflow / Entity actor maintaining balance, reservations, and settlement signals.")
    md.append("")
    md.append("5. **Phase 5: Continuous Causal Trace Ingestion & Outbox Relay (`WF-04`)**")
    md.append("   - *Why fifth:* Upgrades the 24/7 background production infrastructure from hand-rolled SQLite transactional outbox polling to robust event streaming with zero loss.")
    md.append("   - *Key primitive:* Event-driven Signal ingestion, durable message deduplication, and streaming outbox dispatch.")
    md.append("")
    md.append("---")
    md.append("")
    md.append("## Explicit Non-Candidates (Do NOT Put in a Workflow Engine)")
    md.append("")
    md.append("Not every task benefits from durable orchestration. Simple scripts, sub-second CLI commands, and pure in-memory operations are actively harmed by the latency and overhead of a workflow engine.")
    md.append("")

    for nc in NON_CANDIDATES:
        md.append(f"### {nc['name']}")
        md.append(f"- **Why Not:** {nc['why_not']}")
        md.append(f"- **Recommended Runtime:** {nc['best_runtime']}")
        md.append("")

    md.append("---")
    md.append("")
    md.append("## Complete Candidate Registry (All 16 Workflows)")
    md.append("")

    for cand in candidates:
        md.append(f"### `{cand.id}`: {cand.name}")
        md.append(f"- **Owner:** {cand.owner_today}")
        md.append(f"- **Runs On Today:** {cand.runs_on_today}")
        md.append(f"- **Total Score:** {cand.scores.total_score}/21 (Fail={cand.scores.failure_frequency}, Toil={cand.scores.human_toil}, Cost={cand.scores.cost_of_failure}, State={cand.scores.statefulness}, Retry={cand.scores.need_for_retries_timers_signals}, Limits={cand.scores.parallelism_limits}, Audit={cand.scores.auditability})")
        md.append("- **Score Evidence:**")
        for k, v in cand.score_evidence.items():
            md.append(f"  - *{k}:* {v}")
        md.append(f"- **Durable Workflow Value:** {cand.what_durable_workflow_changes}")
        md.append(f"- **Smallest Useful Slice:** {cand.smallest_useful_slice}")
        md.append(f"- **What Would Not Benefit:** {cand.what_would_not_benefit}")
        md.append("- **Citations & Incidents:**")
        for inc in cand.incident_citations:
            md.append(f"  - `{inc}`")
        md.append("")

    return "\n".join(md)


def main() -> None:
    # Sort candidates by total score descending
    sorted_candidates = sorted(CANDIDATES, key=lambda c: c.scores.total_score, reverse=True)
    top_5 = sorted_candidates[:5]

    # 1. Output census.json
    census_data = {
        "generated_at_utc": "2026-11-02T06:30:00Z",
        "total_candidate_count": len(sorted_candidates),
        "scoring_dimensions": [
            "failure_frequency",
            "human_toil",
            "cost_of_failure",
            "statefulness",
            "need_for_retries_timers_signals",
            "parallelism_limits",
            "auditability",
        ],
        "top_5_ids": [c.id for c in top_5],
        "candidates": [
            {
                "id": c.id,
                "name": c.name,
                "owner_today": c.owner_today,
                "runs_on_today": c.runs_on_today,
                "total_score": c.scores.total_score,
                "scores": asdict(c.scores),
                "score_evidence": c.score_evidence,
                "what_durable_workflow_changes": c.what_durable_workflow_changes,
                "smallest_useful_slice": c.smallest_useful_slice,
                "what_would_not_benefit": c.what_would_not_benefit,
                "incident_citations": c.incident_citations,
            }
            for c in sorted_candidates
        ],
        "non_candidates": NON_CANDIDATES,
    }

    json_path = ROOT / "census.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(census_data, f, indent=2)
    print(f"Wrote {json_path} ({len(sorted_candidates)} candidates)")

    # 2. Output CENSUS.md
    md_content = generate_markdown(sorted_candidates, top_5)
    md_path = ROOT / "CENSUS.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
