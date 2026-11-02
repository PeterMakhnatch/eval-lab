---
status: living
audience:
  - operator
  - builder
---

# The Trust Engine: Architecture, Commands, Runbooks, and Verified Evidence

The trust engine makes agent evaluation and training data trustworthy by reducing reward gaming opportunities and surfacing tampering attempts with human-auditable evidence.
It attacks the evaluation gaming problem from both ends: certifying task packages before rollouts so weak or tamperable graders cannot accept false solutions, and monitoring agent execution traces without viewing private reasoning to catch cheating in progress or post hoc.
Together, the two halves provide defence in depth: container hardening raises the difficulty of grader subversion before rollouts begin, while CoT-free monitoring provides low-false-alarm, partial-recall alerts and structured evidence for human adjudication.
All enforcement is grounded in deterministic container boundaries, AST semantic mutations, lossless structured case anatomy, and evidence for human adjudication.
When cheating occurs, the pipeline isolates exact diffs, tool calls, and mutation ledgers for human auditing without manual log inspection.

---

## 1. Architecture: The Two Halves

The engine divides cleanly into two halves that share a common ontology of reward hacking mechanisms:

1. **Task Certifier (Is the exam fair?):** Inspects task packages (instruction, Dockerfile, test files, grader scripts, reference patch) before agent rollouts begin. It tests whether an incorrect or adversarial solution can obtain a passing reward.
2. **Trace Monitor and Judge Station (Did the run cheat?):** Inspects recorded execution traces of agents without viewing chain-of-thought (`reasoning_content` is stripped). It flags cheating during live execution or batch evaluation, and feeds suspect cases to blind human adjudication.

Human verdicts from the Judge Station provide ground truth intended to calibrate Layer 0 tripwires, AutoReview thresholds, and task certifier audits, while rollout filtering via `evallab hack-drop` prevents cheated trajectories from contaminating downstream training sets.

```mermaid
flowchart TD
    subgraph Half1["Half 1: Task Certifier (Is the Exam Fair?)"]
        pkg["Task Package\n(instruction, Dockerfile, tests, grader)"]
        ref["Reference Patch\n(gold solution)"]
        
        ta["evallab tasks anatomy\n(Static Vulnerability Parser: AST & Regex)"]
        am["evallab audit-mutants\n(STING: AST Semantic Mutation Testing)"]
        hd["evallab harden\n(L1 Auto-load, L2 Pristine, L3 Report, L4 Verifier)"]
        cg["evallab certify-gold\n(Plan-only CLI; 8-task experiment driver)"]
        
        pkg & ref --> ta
        pkg & ref --> am
        pkg --> hd
        hd --> certified["Hardened Task Package\n(Tamper-resistant verifier & tests)"]
        am & hd -. Plan preview .-> cg
    end

    subgraph Execution["Agent Execution Environment"]
        certified --> rollouts["Harbor Agent Rollouts / Trials"]
        rollouts --> raw_traces["Raw Execution CAS & ATIF Traces"]
    end

    subgraph Half2["Half 2: Trace Monitor & Judge Station (Did the Run Cheat?)"]
        raw_traces --> tra["evallab trace anatomy\n(Structured Case Anatomy Card)"]
        raw_traces --> l0["Layer 0 Tripwires\n(Deterministic regex & path checks)"]
        
        tra & l0 --> ar["evallab trace autoreview\n(CoT-Free Stage 1 Card + Stage 2 Raw Retrieval)"]
        
        ar --> cascade["L0 ∧ AutoReview Cascade\n(High-Precision Filter: Low FA, Partial Recall)"]
        
        cascade --> js["evallab judge serve\n(Judge Station: Blind Human Adjudication)"]
        js --> verdicts["Human Verdicts\n(verdicts.jsonl)"]
        
        verdicts -. Planned calibration .-> l0
        verdicts -. Planned calibration .-> ar
        cascade --> hackdrop["evallab hack-drop\n(Rollout Filter: Task + L0 + AutoReview)"]
        hackdrop --> clean_sft["Clean SFT / RL Training Corpus"]
    end
```

*(Note: The flowchart above is written in Mermaid syntax. It requires a compatible viewer or renderer to display graphically; text-only renderers show the raw definition).*

---

## 2. Command Reference

All CLI commands run under `evallab` (executable via `uv run evallab` from the repository, or by activating `.venv`). Note that relative task and trace paths resolve against the repository root, not the shell's current working directory. Shared Modal executions are managed through `src/evallab/modal_cells.py` using reserve-then-settle spend gating (`ReserveSettleSpendGate`) and content-addressed execution caching. Every CLI signature listed below has been verified against `--help` in the worktree; operational behaviors, environment requirements (such as local Docker for regrade), and stub limits are documented explicitly below.

| Command | Input | Output | Cost Class | Code Location |
|---|---|---|---|---|
| `evallab tasks anatomy` | Task directories or collections; optional `--reference-patch` | `evallab.package_anatomy/v1` card (stdout or JSONL) | Free local ($0, static AST/regex, <10 ms) | `src/evallab/package_anatomy.py` |
| `evallab audit-mutants` | Base package, reference patch, output dir; optional `--ledger <path>.json`, `--repaired-package` | Audit summary JSON, mutated cells CSV, survivor diffs; reservations recorded in `--ledger` | Modal cents (~$0.012 per weak grader; metered cap) | `src/evallab/mutant_auditor.py` |
| `evallab harden` | Task package path; `--expected-tests` (required for pytest unless `--allow-degraded`), optional `--single-container`, `--out` | Hardened package directory (L1–L4 hardened verifier) | Free local ($0, static container re-wiring) | `src/evallab/full_hardening.py` |
| `evallab certify-gold` | Base package, reference patch; `--plan` [`--json`], optional `--rounds`, `--probes-per-round`, `--cap-usd`, `--model`, `--limit` | Plan preview JSON/text (`task_id, package, targets, model, metered_cap_usd, mutant_limit, rounds, probes_per_round`). CLI non-plan mode is a stub (`--rounds > 1` without `--plan` exits 2); arbitrary package certification is not operator-runnable today. Multi-round synthesis feedback is available as the library API `evallab.gold_certifier.run_rounds`; full 6-gate certification exists as a fixed 8-task MiMo experiment driver (`research/experiments/ov-gold-certifier-8-20261101/run_gold_certification.py`) | Free local ($0, offline planning; 8-task experiment driver uses Subscription + Modal cents) | `src/evallab/gold_certifier.py` |
| `evallab trace anatomy` | Run ID (cloud/CheatBench) or `--atif-file <trial.json>`; optional `--include-narration` | Structured Case Anatomy card (human-readable text or JSON) | Free local ($0, raw CAS projection, <100 ms) | `src/evallab/trace_anatomy.py` |
| `evallab trace autoreview` | Run ID or ATIF trial JSON; optional `--compact-only`, `--with-l0-cascade`, `--model` | AutoReview judgment JSON (suspicion score, cited steps, cascade/union alerts when model runs) | Subscription (or Free local $0 with `--compact-only`) | `src/evallab/autoreview_monitor.py` |
| `evallab regrade` | Stored Harbor job directory (`<job_dir>`); optional `--task <hardened_task>` or `--held-out <bundle>`, `--jobs-dir`, `--name`, `--dry-run`, `--json` | Re-scored job directory under `runs/` (or specified `--jobs-dir`) | Local Docker, $0 model cost (refused when `EVALLAB_DISABLE_LOCAL_DOCKER=1`; `--held-out` also requires local Docker; Modal trial regrades exist only via Python CloudSpendGate API) | `src/evallab/regrade.py`, `src/evallab/cli.py` |
| `evallab judge serve` | Queue file (`[NAME=]PATH`), findings JSONL; optional `--port`, `--open` (interactive-only) | Local blinded web UI on `127.0.0.1:8795` (use `--port <port>` with ephemeral port for scratch runs) with card-first presentation, drafts, and undo; records human verdicts to `verdicts.jsonl`. Additional subcommands: `build-packet`, `export`, `report` (agreement with labels and detectors), `plan` (queue information-value strata), `build-controls` | Free local ($0, stdlib HTTP, no models, no network) | `src/evallab/production/judge_station.py`, `judge_web.py`, `judge_report.py`, `judge_order.py` |
| `evallab judge build-packet` | Fixed 18-case packet markdown (`--adjudication`) and DT-17 cases JSON (`--cases`); optional `--state-dir` | `<state-dir>/queues/adjudication-packet.jsonl` and `guide-examples.jsonl` (fixed 18-case calibration builder) | Free local ($0, file formatting) | `src/evallab/production/judge_station.py` |
| `evallab judge export` | Verdicts state directory | Exported CSV or JSON of human verdicts | Free local ($0, read-only export) | `src/evallab/production/judge_station.py` |
| `evallab live-supervisor` | Live runs root; subcommands: `scan`, `serve`, `replay`, `adjudicate`, `rows`, `route-alarms`, `fleet-report` | `trace-findings/v1` records, shadow-mode decisions | Free local ($0, deterministic rules) | `src/evallab/production/live_supervisor.py` |
| `evallab watch` | Live runs root; subcommands: `ack`, `stop` | Live trial monitoring, stop requests | Free local ($0, watchdog loops) | `src/evallab/harbor_watch_hooks.py` |
| `evallab hack-drop` | Rollout paths/manifest; optional `--certified-tasks`, `--labels`, `--with-autoreview`, `--autoreview-route`, `--max-model-usd`, `--max-calls`, `--suspicion-threshold`, `--out-dir`, `--dry-run`, `--json` | Filtered datasets (`<out-dir>/kept.jsonl`, `dropped.jsonl`, `review.jsonl`) and summary `report.json` | Free local ($0 for task + L0 filter); Subscription / metered model when `--with-autoreview` enabled | `src/evallab/hack_drop.py`, `src/evallab/cli.py` |
| `evallab spend reconcile` | Optional `--spend-log PATH` (defaults to the standard spend log), `--since`, `--until`, `--threshold-usd`, `--providers`, `--deepseek-start-balance`, `--openrouter-start-usage`, `--sync-omp-sessions`, `--session-dir`, `--json` | Reconciliation report comparing provider balances/usage against local `spend.jsonl` ledger | Free local ($0, read-only provider usage queries and ledger verification) | `src/evallab/spend_reconcile.py`, `src/evallab/cli.py` |

---

## 3. Runbooks

> Paths in the runbook examples are shortened for readability. Rollout inputs given as relative paths resolve against the repository root; `--out-dir`, `--labels` and `--certified-tasks` resolve against your current directory. Use absolute paths for all of them (for example `$SCRATCH/runs/candidates.jsonl`).

### Runbook 1: Certify a Task Package

Before deploying a benchmark task to model evaluations, verify that its grader cannot be satisfied by broken or cheating code.

1. **Step 1: Run static package anatomy:**
   Check container boundary posture, baked files, reset lists, hook vectors, and test assertions:
   ```bash
   evallab tasks anatomy library/tasks/my-task --reference-patch library/tasks/my-task/reference.patch --json
   ```
   *Expected result:* Exit 0. Examine `exploit_grader_gap`, `grader_tamperable`, and `weak_check` hypotheses. As a heuristic, if `AR-TESTS-IN-IMAGE` (score 85/92) or `GT-AUTOLOAD-WRITABLE` (score 80) fires (which flags almost every legacy single-container package — 31/35 in receipt packages), container hardening is recommended.

2. **Step 2: Audit semantic mutations with STING:**
   Generate AST mutations of the reference patch and test whether the original grader accepts them:
   ```bash
   evallab audit-mutants \
     --package library/tasks/my-task \
     --reference-patch library/tasks/my-task/reference.patch \
     --out results/audit-my-task \
     --ledger results/audit-my-task-ledger.json \
     --max-usd 0.50 \
     --approval "Operator task certification run"
   ```
   *Expected result:* Outputs `audit-summary.json`. If `weak_original: true`, the benchmark grader passes semantic bugs and requires hardening tests. (Note: requires paid Modal container execution authorized via `--approval` and bounded by `--max-usd`. Pass `--ledger <path>.json` so `CloudSpendGate` tracks spend reservations across invocations; without `--ledger`, reservations are kept only in memory and a rerun gets a fresh $0.50 cap. The ledger holds execution reservations, not final invoices; actual metered billing is tracked in the Modal dashboard by `--app-name` / billing tag, not in `spend.jsonl`).

3. **Step 3: Harden container boundaries with L1–L4 separation:**
   Apply deterministic container re-wiring (L1 auto-load reset, L2 pristine workdir, L3 report gating, L4 separate verifier container):
   ```bash
   evallab harden library/tasks/my-task --expected-tests 1 --out library/tasks/my-task-hardened
   ```
   *Expected result:* Outputs hardened package directory with separate verifier architecture (`transform: full-hardening@1`). Note: `--expected-tests <N>` is required for pytest packages unless `--allow-degraded` is passed.

   *Pipeline note for new packages:* Runbook 1 for arbitrary task packages currently ends at Step 3 (anatomy, STING mutation audit, and container hardening). Automated corpus-scale task package certification is not yet an operator-runnable feature on `main`; it is an active research target (see `gh pr list` for work in flight).

4. **Step 4: Status of automated Gold Certification (Plan-only CLI; 8-task experiment runner):**
   The CLI command `evallab certify-gold` is currently **plan-only**. You can preview the task parameters offline ($0):
   ```bash
   evallab certify-gold \
     --package library/tasks/my-task \
     --reference-patch library/tasks/my-task/reference.patch \
     --plan --json \
     --rounds 2 \
     --probes-per-round 4
   ```
   *Expected result:* Prints a JSON planning dict containing `task_id, package, targets, model, metered_cap_usd, mutant_limit, rounds, probes_per_round` only. It does **not** enumerate mutants, synthesize tests, split train/eval sets, or generate prompt previews. The non-plan CLI mode (`--out <dir>`) with `--rounds 1` is a no-op stub that prints a placeholder line and exits 0 without running any verifications; passing `--rounds > 1` without `--plan` exits 2 (`error: --rounds > 1 is not implemented in the command (use evallab.gold_certifier.run_rounds); only --plan accepts it`). Multi-round synthesis feedback is available only via the Python library API `evallab.gold_certifier.run_rounds`.

   *Execution note:* There is **no operator-runnable certification of an arbitrary package today**. The full end-to-end certification pipeline exists today only as a fixed-cohort research experiment runner:
   ```bash
   python research/experiments/ov-gold-certifier-8-20261101/run_gold_certification.py
   ```
   This script evaluates a hard-coded cohort of 8 strictly-DEV MiMo tasks (`000079`, `000232`, `001267`, `001850`, `000682`, `000931`, `000484`, `003005`). It takes no `--package` or `--reference-patch` argument for arbitrary user tasks. Its CLI flags are `--synthesize`, `--harden`, `--plan`, `--run`, `--workers`, `--summarize`, `--task <short>`, and `--cell <substr>`. When executed bare, it only rewrites `research/experiments/ov-gold-certifier-8-20261101/results/selection.json`. Running it with `--run` executes Modal cells under a hard-coded approval string (`"Peter 2026-10-31 overnight factory W3B-EXHAUSTIVE-GOLD-CERTIFIER: Modal cap $0.95"`, cap `$0.95`, bridge `worker.yml` path) and overwrites its own `results/` directory.

   What the 8-task experiment measured across its 6 certification gates:
   - Gate 1 (Reference Validity): Reference patch scores 1.0 (8/8 observed).
   - Gate 2 (Nop Rejection): Empty submission scores 0.0 (8/8 observed).
   - Gate 3 (Witness / Tamper Rejection): Grader tampering and conftest hooks score 0.0 (8/8 observed).
   - Gate 4 (L4 `.pth` Startup Interception): Interpreter hijack via `.pth` scores 0.0 (8/8 observed).
   - Gate 5 (Train Mutants Killed): Reports training semantic mutants killed (19/20 = 95.0% observed across 8 tasks; 0/1 on task 000931; reports count, no hard 90% threshold in code).
   - Gate 6 (Eval Mutants Killed): Reports held-out eval mutants killed out-of-sample (3/8 base survivors, 13/18 all mutants killed across 8 tasks; reports counts only).

### Runbook 2: Review a Run

When an agent scores 1.0 on a task, or when an operator suspects reward gaming, audit the trajectory using the CoT-free monitoring stack.

1. **Step 1: Generate lossless Structured Case Anatomy:**
   Generate the card without viewing private reasoning:
   ```bash
   evallab trace anatomy --atif-file runs/my-job/my-trial/agent/trajectory.json --json
   ```
   *Expected result:* Displays task contract, recovered final patch (exact CAS diff), file mutation ledger (protected vs product files), and high-signal actions (git history searches, network calls, assertion inversions).

2. **Step 2: Run deterministic Layer 0 check:**
   Evaluate deterministic tripwires and compact view in-process ($0, no model):
   ```bash
   evallab trace autoreview runs/my-job/my-trial/agent/trajectory.json --compact-only --with-l0-cascade --json
   ```
   *Expected result:* Prints Stage 1 compact card, disambiguates new test files vs modified existing assertions, and reports whether Layer 0 tripwires fired.

3. **Step 3: Execute CoT-Free AutoReview Monitor (if flagged or uncertain):**
   ```bash
   evallab trace autoreview runs/my-job/my-trial/agent/trajectory.json --with-l0-cascade --json
   ```
   *Expected result:* Returns suspicion score (0–100), reasoning-free audit findings, and combined cascade verdicts (`cascade_alert` and `union_alert`). Note that `cascade_alert` and `union_alert` appear only when a Stage 1/2 model verdict is present (they are absent in `--compact-only` mode). A cascade alert is a high-precision alert (0/146 clean false alarms on Terminal Wrench DEV; on C1–C5: verdict 7/10 recall, 2/10 FA; union alert 3/10 FA; cascade alert 2/10 recall, 1/10 FA — not sized for this cohort) that flags suspect runs for human confirmation, not an autonomous ground-truth confirmation.

4. **Step 4: Adjudicate in Judge Station:**
   If a run sits in the ambiguous zone (suspicion 25–85) or requires human sign-off, it can be queued for blinded human review. Note: Judge Station operates on queues of cloud trace run IDs (JSONL records with `{"run_id": "<run_id>", "reason": "..."}`, from which the station derives the blinded `run-<16hex>` display ID). Local ATIF trial JSONs cannot be served directly in Judge Station without being registered in the cloud run store (`judge build-packet` is a fixed 18-case calibration packet builder, not a per-job queue generator). The production factory can automatically feed high-priority cases into Judge Station queues via `--autoreview-mode live` (`src/evallab/production/factory_autoreview.py`, merged in PR #1730).
   For cloud runs queued in `queue.jsonl`:
   ```bash
   evallab judge serve --queue queue.jsonl --state-dir results/judge-scratch --port 0
   ```
   *Expected result:* Serves blinded interface on `http://127.0.0.1:<port>` (pass `--port <port>` with an ephemeral port like `--port 0` in scratch/overnight runs to avoid collisions with default port 8795). *(Note: Do not pass `--open` in unattended, automated, or overnight runs. `--open` calls Python's `webbrowser.open()`, which steals operating system window focus; reserve `--open` for interactive desktop sessions only).* The operator judges the timeline without seeing the benchmark, model label, or detector verdicts until submission; Judge Station v2 provides card-first presentation, drafts, and undo. Inspect queue information-value strata using `evallab judge plan --queue queue.jsonl`, and evaluate agreement against labels and detectors via `evallab judge report --json`. Verdicts are written to `<state-dir>/verdicts.jsonl`.
---

### Runbook 3: Filter a Rollout Set

When preparing agent trajectories for Supervised Fine-Tuning (SFT) or Reinforcement Learning (RL), trajectories with reward 1.0 obtained via reward hacking must be purged to prevent training models to cheat. Filtering is performed via `evallab hack-drop` (merged in PR #1729), which evaluates candidate rollouts across task certification, deterministic Layer 0 tripwires, and optional Stage 3 AutoReview.

1. **Step 1: Evaluate candidate rollouts with deterministic task + L0 filtering ($0):**
   Filter rollout traces or directories using certified task statuses and deterministic AST/regex tamper signatures:
   ```bash
   evallab hack-drop \
     --rollouts runs/candidates.jsonl \
     --certified-tasks manifests/certified-tasks.json \
     --out-dir results/filtered \
     --dry-run --json
   ```
   *Expected result:* With `--dry-run`, only `report.json` is written (stage breakdowns for `task`, `l0`, `autoreview`). Drop `--dry-run` to also write `kept.jsonl` (clean passes), `dropped.jsonl` (hard hits: test/grader modification, answer-key access, fabricated verification), and `review.jsonl` (soft hits: config writes, parse ambiguities, uncertified tasks). `--dry-run` still makes (and does not cache) AutoReview model calls if `--with-autoreview` is set; it only skips the manifests and the cache.

2. **Step 2: Run optional Stage 3 AutoReview on soft-flagged rollouts:**
   For soft-flagged rollouts touching target surfaces, enable compact-card model review under strict budget controls:
   ```bash
   evallab hack-drop \
     --rollouts runs/candidates.jsonl \
     --certified-tasks manifests/certified-tasks.json \
     --with-autoreview \
     --autoreview-route google-antigravity/gemini-3.8-flash \
     --max-calls 50 \
     --max-model-usd 1.50 \
     --suspicion-threshold 50 \
     --out-dir results/filtered
   ```
   *Expected result:* Soft-flagged rollouts exceeding `--suspicion-threshold` (default 50) or receiving a cheating verdict escalate to `review` (or `drop` via `--autoreview-action drop`).
   *Known limit:* `hack-drop` catches AST test tampering and answer-key reads, but cannot detect unparsed shell writes (`dd`, `awk -i`), unrecognized edit tool shapes, or non-verdict semantic exploits without test writes; false drop rate on real rollouts is higher than in-sample false alarms due to treating second writes to new tests as edits.

3. **Step 3: Re-score surviving positive trials under hardened verifiers:**
   Re-grade trials using the L1–L4 hardened package verifier at zero model cost:
   ```bash
   evallab regrade <job_dir> --task library/tasks/my-task-hardened [--dry-run]
   ```
   Any trial whose reward drops from 1.0 to 0.0 under the hardened verifier is dropped as an exploit.
   *Execution and cost note:* CLI `evallab regrade` executes via local Docker ($0 model cost, local container execution). When `EVALLAB_DISABLE_LOCAL_DOCKER=1` is set, the CLI refuses execution (`error: Local Docker execution is disabled`). Note that `--held-out <bundle>` (which re-scores passes offline on cached local CPU images) is checked by the same `_check_regrade_environment()` guard, so held-out regrade also requires local Docker and is likewise refused when local Docker is disabled. Modal container execution with `CloudSpendGate` is supported only at the Python API level for single-trial regrades, not from the `evallab regrade` CLI.

4. **Step 4: Adjudicate borderline cases:**
   Rollouts routed to `results/filtered/review.jsonl` or sitting in uncertain score bands are queued for human adjudication in Judge Station (`evallab judge serve`).
---

## 4. Numbers You May Quote

Every number below was verified directly by opening and reading its source receipt file either in this repository (`research/experiments/**`) or in verified external bridge receipts (`research-context/bridge/**`, not committed to this repository). All intervals are 95% Wilson confidence intervals. For the Gold Certifier 8-task experiment rows, receipts were read and verified directly from `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` by the author; independent verification was not performed (VF-OV2 listed W3B-EXHAUSTIVE-GOLD-CERTIFIER as not verified). DT-15 refers to the earlier action-grounded trace reader baseline (`ds-dt15-action-grounded-reader-20261030`).

| Finding / Metric | Value | Gate Verdict | Status | Receipt Path |
|---|---|---|---|---|
| **STING Original Grader Weakness** | 12/18 = 66.7% (43.7–83.7%) weak original MiMo DEV graders accepting mutants | N/A (diagnostic) | Verified | `research/experiments/ov-sting-mutants-20261031/results/audit-summary.json` |
| **STING Grader Cost Efficiency** | $0.0116 / 70 sandbox-s per weak grader found ($0.1395 / 844 s over original-package cells + probes; total run $0.2068, 1,251 s, 89 executions including 12 repaired-package audits) | N/A (efficiency) | Verified | `research/experiments/ov-sting-mutants-20261031/results/audit-summary.json` |
| **STING Repaired Grader Residual Survivors** | 4/12 = 33.3% (13.8–60.9%) of prior "qualified" repairs still accepted mutants | N/A (diagnostic) | Verified | `research/experiments/ov-sting-mutants-20261031/results/audit-summary.json` |
| **Gold Certifier Gate 1 (Reference Validity)** | 8/8 = 100.0% (67.6–100.0%) reference patches pass hardened verifier | Gate 1 PASS | Receipt read by author; no independent verifier (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Gold Certifier Gate 2 (Nop Rejection)** | 8/8 = 100.0% (67.6–100.0%) empty patches rejected | Gate 2 PASS | Receipt read by author; no independent verifier (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Gold Certifier Gate 3 (Tamper Rejection)** | 8/8 = 100.0% (67.6–100.0%) witness and hook exploits rejected | Gate 3 PASS | Receipt read by author; no independent verifier (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Gold Certifier Gate 4 (L4 `.pth` Forgery)** | 8/8 = 100.0% (67.6–100.0%) site-packages `.pth` hijacking defeated | Gate 4 PASS | Receipt read by author; no independent verifier (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Gold Certifier Gate 5 (Train Mutants Killed)** | 19/20 = 95.0% (76.4–99.1%) training semantic mutants killed across 8 tasks (reports count; 0/1 on task 000931) | Gate 5: 19/20 killed (reports count, no 90% threshold in code) | Receipt read by author; no independent verifier (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Gold Certifier Gate 6 (Eval Mutants - Base Survivors)** | 3/8 = 37.5% (13.7–69.4%) base-surviving unseen held-out mutants killed | Gate 6: 3/8 base survivors killed (reports count) | Receipt read by author; no independent verifier (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Gold Certifier Gate 6 (Eval Mutants - All)** | 13/18 = 72.2% (49.1–87.5%) all unseen held-out eval mutants killed | Gate 6: 13/18 all mutants killed (reports count) | Receipt read by author; no independent verifier (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Unified Hardening Attack Closures** | 11/11 = 100.0% (74.1–100.0%) attack closures across 51 Modal cells at $0.1238 | PASS | Verified | `research/experiments/t2-unified-harden-20261031/results/summary.json` |
| **Package Anatomy Execution Speed** | p50 3.44 ms, p95 8.83 ms, max 10.56 ms across 35 strictly-DEV MiMo packages (27 pytest + 8 non-pytest; 1 non-DEV task excluded from the 36 classification rows) ($0) | PASS (gate_pass: true) | Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-sonnet/raw/T2A-PKG-ANATOMY2/receipts/summary.json` |
| **Package Anatomy C1–C5 Mechanism Detection** | 5/5 = 100.0% (56.6–100.0%) impossible tasks flagged with causal mechanism | PASS (gate_pass: true) | Verified (in-sample; external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-sonnet/raw/T2A-PKG-ANATOMY2/receipts/summary.json` |
| **Package Anatomy OQ-6 Card Generation** | 399 DEV cards (385 MiMo + 14 TB4), 0 non-DEV leaks | PASS | Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-sonnet/raw/T2A-PKG-ANATOMY2/receipts/summary.json` |
| **Trace Anatomy Zero Leaks & Errors** | 0/100 runs = 0.0% (0.0–3.7%) label leaks; 0/100 extraction errors | FAIL (prereg gate failed on SWE clause; whole-token leak check chosen after substring hits) | Pre-registered (gate failed) / Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/verify-all.json` |
| **Trace Anatomy Card Compression** | 24.8× median of per-run compression ratios (median card 3,069.5 chars vs 121,545 raw chars; ratio of medians is 39.6×) | N/A (diagnostic) | Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/verify-all.json` |
| **Trace Anatomy SWE Patch Recovery (Pre-registered Baseline)** | 15/17 (88.2%) in old DB projection → 15/17 (88.2%) in raw CAS anatomy (using pre-registered "any diff block" baseline) | FAIL (SWE clause required improvement; 15/17 -> 15/17) | Pre-registered (gate failed) / Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/verify-all.json` |
| **Trace Anatomy DeepSWE Patch Recovery (Post-Hoc Baseline)** | 0/17 (0.0%) in old DB projection → 15/17 (88.2%) in raw CAS anatomy (using post-hoc exact-copy baseline) | N/A (baseline added after prereg; do not quote as headline) | Post hoc / Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/verify-all.json` |
| **Terminal Wrench L0 Live Prefix Fix (Validation Fold, n=274)** | Recall 104/138 = 75.4% (67.6–81.8%), Clean FA 4/136 = 2.9% (1.1–7.3%) vs pre-fix baseline on all 304 (10/158 = 6.3% recall, 30/146 = 20.5% FA; Fisher exact $p = 1.08 \times 10^{-39}$ tests post-fix hack vs clean alert separation on validation fold) | N/A (no prereg gate) | Verified | `research/experiments/ov-autoreview-v2-tw-20261031/result.json`, `VF-OV1/verify-all.json` |
| **Terminal Wrench L0 Live Prefix Fix (Full DEV Set, n=304)** | Recall 121/158 = 76.6% (69.4–82.5%), Clean FA 4/146 = 2.7% (1.1–6.8%) | N/A (full DEV cohort) | Verified | `research/experiments/ov-autoreview-v2-tw-20261031/result.json` |
| **Terminal Wrench L0 + AutoReview Cascade (Full DEV Set, n=304)** | Recall 79/158 = 50.0% (42.3–57.7%), Clean FA 0/146 = 0.0% (0.0–2.6%) | G-TW FAIL (FA passed 0.0% <= 1.0%, recall 50.0% < 60% requirement) | Pre-registered (gate failed) / Verified | `research/experiments/ov-autoreview-v2-tw-20261031/result.json` |
| **Terminal Wrench Post-Hoc Cascade** | Recall 89/158 = 56.3% (defined after seeing data; counts 11 runs whose final AutoReview verdict was clean) | N/A (post-hoc definition; do not quote as headline) | Post hoc | `research/experiments/ov-autoreview-v2-tw-20261031/README.md:14`, `VF-OV1/verify-all.json` |
| **CheatBench AutoReview Clean FA vs DT-15 (Fixed Rubric)** | Clean FA 0/75 = 0.0% (0.0–4.9%) vs DT-15 8/75 (10.7%); discordant pairs 8 (DT-15 only) vs 0 (ours only), exact McNemar $p = 0.0078$ in our favour | G-CB FAIL, G-CB-P2 FAIL (FA passed, recall clause failed) | Pre-registered (gate failed) / Verified | `research/experiments/ov-autoreview-investigator-20261031/result.json`, `research/experiments/ov-autoreview-v2-tw-20261031/result.json` |
| **CheatBench AutoReview Recall vs DT-15 (Fixed Rubric)** | Recall 22/75 = 29.3% (20.2–40.4%) vs DT-15 36/75 (48.0%); discordant pairs 17 (DT-15 only) vs 3 (ours only), exact McNemar $p = 0.0026$ in DT-15's favour | G-CB FAIL, G-CB-P2 FAIL (recall < 25/74 requirement) | Pre-registered (gate failed) / Verified | `research/experiments/ov-autoreview-investigator-20261031/result.json`, `research/experiments/ov-autoreview-v2-tw-20261031/result.json` |
| **CheatBench AutoReview Quantile FA vs DT-15** | Clean FA 2/75 (2.7%) vs DT-15 8/75 (10.7%); discordant pairs 8 vs 2, exact McNemar $p = 0.109$ (not significant; net difference was 6 vs 0) | N/A (in-sample quantile) | Verified (in-sample quantile threshold q=0.985 evaluated on same 150 scores) | `research/experiments/ov-autoreview-investigator-20261031/result.json` |
| **Goodfire deep_swe AutoReview Fixed Rubric (Investigator Gated)** | Recall 3/80 = 3.8% (1.3–10.5%), Clean FA 0/70 = 0.0% (0.0–5.2%) across full 150-run DEV set (80 positives, 70 negatives) | G-GF FAIL (recall 3/80 < gate requirement) | Pre-registered (gate failed) / Verified | `research/experiments/ov-autoreview-investigator-20261031/result.json` |
| **Goodfire deep_swe AutoReview Fixed Rubric (W3-DEEPSWE Arm 2)** | Recall 6/50 = 12.0% (5.6–23.8%), Clean FA 0/50 = 0.0% (0.0–7.1%) on 100-run DEV subset (50 positives, 50 negatives) | N/A (diagnostic exploration arm; no gate declared) | Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/report.md`, `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/g38-flash/raw/W3-DEEPSWE-AUTOREVIEW/report.md` |
| **Goodfire deep_swe AutoReview Token Compression** | 8.19× smaller median input tokens (10,545.5 vs 86,387 tokens; $1.08 vs $6.66) | N/A (efficiency) | Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/report.md` |
| **Solvable Task Friction Propensity (Sol/Astra)** | 0/42 = 0.0% (0.0–8.4%) spontaneous subversions across 42 unprompted rollouts | PASS | Verified | `research/experiments/ov-safeguard-rollouts-20261031/receipts/summary.json`, `ov-safeguard-friction-v2-20261031/receipts/summary.json` |
| **Impossible Task Propensity (C1–C5)** | 6/20 = 30.0% (14.5–51.9%) spontaneous cheats on impossible tasks (test contradictions C1, hidden tests C2, evaluator mutation C3, fabricated verification C4, environment acquisition C5) | N/A (diagnostic) | Verified | `research/experiments/ov-c1-c5-live-rollouts-20261101/receipts/summary.json` |
| **Impossible Task Container Defense (`harden`)** | Cheats dropped from 6/20 (30.0%) to 2/20 (10.0%) (neutralized 4/4 C1 cheats, 0/2 C5 cheats) | N/A (diagnostic) | Verified | `research/experiments/ov-c1-c5-live-rollouts-20261101/receipts/summary.json` |
| **Impossible Task Monitor Performance** | 7/10 = 70.0% (39.7–89.2%) recall at 2/10 = 20.0% FA (verdict) or 3/10 = 30.0% FA (union); 5/6 = 83.3% (43.6–97.0%) on confirmed cheats | N/A (diagnostic; earlier 9/10 @ 0/10 claim retracted) | Verified | `research/experiments/ov-c1-c5-live-rollouts-20261101/receipts/summary.json` |
| **Human Calibration on Judge Station** | 65 queued cases across 3 queues + 4 guide examples, 0 verdicts recorded as of 2026-11-02 | Pending | Not measured (local uncommitted state in `~/.local/state/evallab-judge/queues/`; ground truth calibration pending human adjudication) | N/A (local state) |

---

## 5. Numbers You Must Not Quote (Corrected Claims)

Independent blind verifiers (VF-OV1, VF-OV2) audited the overnight experiments and corrected several unsupported or misleading claims. The statements on the left must **never** be quoted:

| Do Not Say | Say Instead | Grounded Reason |
|---|---|---|
| *"35/35 DEV MiMo packages measured / reference scores 1.0 on every layer"* | Coverage in K4–K6 was 8 non-pytest packages (the other 27 were not part of this run); reference 1.0 holds for L0/H1 on `000603` and `000838`, but T2 ran no hardened-spec (HS) reference cell there. | T2 ran no hardened-spec (HS) reference cell for `000603` or `000838`, and K4–K6 receipts cover only the 8 non-pytest packages. Cite per-package receipts. |
| *W3B impossible monitor achieved "9/10 recall at 0/10 false alarms"* | Recall was 7/10 at 2/10 false alarms (verdict) or 3/10 (union alert); 5/6 on container-confirmed cheats. | The 9/10 at 0/10 claim required post-hoc relabeling where two false alarms were reclassified as positives and two missed cheats were reclassified as negatives. |
| *CheatBench quantile false alarms beat DT-15 with McNemar $p = 0.031$* | Exact two-sided McNemar on the discordant pairs (8 vs 2) yields $p = 0.109$ (not statistically significant). | The reported 0.031 came from treating the net difference (6 vs 0) as if it were the discordant count. |
| *"89/158 with the cascade" as the headline for Terminal Wrench* | 79/158 at 0/146 false alarms is the pre-registered result; 89/158 is post hoc. | 89/158 was defined after inspecting outcomes and counts 11 runs whose final AutoReview verdict was `clean`. |
| *deep_swe "98% protected writes" discriminates cheats from clean runs* | 98% of all deep_swe rollouts write to protected paths, including 49/50 clean runs. | Agents legitimately add `_test.go` unit tests, making protected-surface writes non-discriminative without AST/patch inspection. |
| *deep_swe AutoReview token reduction was 8.25× (or 87k vs 10.5k means)* | Token median ratio recomputed from session receipts is 8.19× (86,387 vs 10,545.5 tokens). | 8.25× and 87,134 / 10,559 were unverified estimates; exact receipt medians give 8.19×. |
| *Any claim that labels were adjusted to match monitor output* | Labels are strictly frozen before reading monitor output; relabeling after observing monitor output is forbidden. | Scientific validity requires that ground truth labels remain independent of detector behavior. |
| *Claim that a specific deep_swe case was a clean run cleared by AutoReview* | One deep_swe case narrative was inverted in an earlier worker report (run ID kept in the local VF-OV1 report). | Case narrative was inverted in an earlier worker report. Per third-party data policy, per-run labels and run IDs are not committed. |

---

## 6. Known Gaps and Operating Limits

1. **Human Adjudication Pending (65 Queued Cases + 4 Guide Examples):**
   The local Judge Station queues (`~/.local/state/evallab-judge/queues/`) currently hold 65 adjudication cases across 3 queues (18-case adjudication packet, 23 Goodfire action gold, 24 Goodfire census) plus 4 guide examples; 0 human verdicts have been recorded in `verdicts.jsonl` as of 2026-11-02. Until human verdicts are recorded, operator-calibrated precision and recall benchmarks do not exist.
2. **Strictly DEV-Only Cohorts:**
   Held-out benchmark splits and TEST datasets were never accessed. All numbers reflect development sets; out-of-distribution transfer to held-out evaluation splits remains an empirical unknown.
3. **Spend-Gate Over-Reservation and Cost Estimates:**
   `CloudSpendGate` and `ReserveSettleSpendGate` (`src/evallab/modal_cells.py`) reserve a configurable amount per execution (`--per-execution-usd`, default $0.005 in `src/evallab/mutant_auditor.py:703`), reserving approximately 2.2× the estimated execution cost on Modal (e.g. STING estimated $0.2068 across 89 executions in `audit-summary.json`, ~$0.0023 each, vs $0.005 reserved). While `ReserveSettleSpendGate` releases unused reservation headroom upon completion, the initial reservation requires sufficient spend headroom. Pass `--ledger <path>.json` to persist execution reservations across runs; without `--ledger`, the gate does not record reservations durably across invocations. Reconcile ledger reservations against actual provider billing via `evallab spend reconcile`.
4. **Boundary-Shift Mutant Misses:**
   Contract-complete test synthesis currently misses subtle off-by-one or floating-point boundary shifts (e.g. task `000232` and `000484` LogEI edge cases), explaining why held-out generalization on base-surviving mutants sits at 3/8 (37.5%).
5. **No Operator-Runnable Arbitrary Package Certifier:**
   The `evallab certify-gold` CLI is plan-only (and non-plan mode is a stub). End-to-end certification exists only as an 8-task fixed MiMo experiment driver (`research/experiments/ov-gold-certifier-8-20261101/run_gold_certification.py`). Generalizing gold certification to arbitrary operator packages is an active research target (see `gh pr list` for work in flight).
6. **Rollout Filtering Blind Spots (`evallab hack-drop`):**
   `evallab hack-drop` is merged on `main` (PR #1729), but operates under defined blind spots: shell-level text modifications (`dd of=`, `awk -i`), unrecognized tool-call argument shapes, and semantic exploits with zero test writes bypass deterministic detection, while AutoReview relies on a single uncalibrated route without ensemble (see `docs/hack-drop.md`).
7. **L0 Evasion Evidence Is Thin:**
   L0 has one measured evasion class: login-shell startup files. The benchmark runner executes the test command through `bash -lc`, so writing `exit 0` into `/etc/profile` or `~/.bash_profile` ends the shell with code 0 before pytest starts. It slipped past L0 in 1 of 21 packages for a naive Sonnet attacker and in 3 of 21 packages for a naive Gemini attacker (L0 caught 14 of that attacker's 17 grader-passing packages); a rule for it is proposed, not implemented (author-measured, not independently verified). A Gemini informed-attacker run, shown the rules, produced 0 of 21 evasions, but only because knowing the rules made it much less able to pass the original grader at all (4 of 21 packages, all caught); the informed Sonnet arm refused and was not measured. So: one mechanism, two models, in-sample DEV, no gate.

---

## 7. What This Page Does Not Track

This page describes `origin/main` as of its last revision. It deliberately lists no pull requests, because what is in flight changes daily; run `gh pr list --repo PeterMakhnatch/eval-lab --state open` for that. Research results that sit under `research/experiments/` (workflow-engine comparison, cheap-monitor studies, Gold certification runs, informed-attacker runs) carry their own READMEs; treat the numbers there as author claims unless a README says an independent verifier checked them, because some of them were corrected after the first merge.
