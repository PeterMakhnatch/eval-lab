---
status: living
audience:
  - operator
  - builder
---

# The Trust Engine: Architecture, Commands, Runbooks, and Verified Evidence

The trust engine makes agent evaluation and training data trustworthy by guaranteeing that reward signals cannot be gamed without human-auditable evidence.
It attacks the evaluation gaming problem from both ends: certifying task packages before rollouts so weak or tamperable graders cannot accept false solutions, and monitoring agent execution traces without viewing private reasoning to catch cheating in progress or post hoc.
Together, the two halves provide defence in depth: container hardening raises the difficulty of grader subversion before rollouts begin, while CoT-free monitoring provides low-false-alarm, partial-recall alerts and structured evidence for human adjudication.
All enforcement is grounded in deterministic container boundaries, AST semantic mutations, lossless structured case anatomy, and calibrated human adjudication.
When cheating occurs, the pipeline isolates exact diffs, tool calls, and mutation ledgers so a person can audit the run in two minutes.

---

## 1. Architecture: The Two Halves

The engine divides cleanly into two halves that share a common ontology of reward hacking mechanisms:

1. **Task Certifier (Is the exam fair?):** Inspects task packages (instruction, Dockerfile, test files, grader scripts, reference patch) before agent rollouts begin. It tests whether an incorrect or adversarial solution can obtain a passing reward.
2. **Trace Monitor and Judge Station (Did the run cheat?):** Inspects recorded execution traces of agents without viewing chain-of-thought (`reasoning_content` is stripped). It flags cheating during live execution or batch evaluation, and feeds suspect cases to blind human adjudication.

Human verdicts from the Judge Station provide ground truth that calibrates Layer 0 tripwires, AutoReview thresholds, and task certifier audits, while rollout filtering ensures cheated trajectories never contaminate downstream training sets.

```mermaid
flowchart TD
    subgraph Half1["Half 1: Task Certifier (Is the Exam Fair?)"]
        pkg["Task Package\n(instruction, Dockerfile, tests, grader)"]
        ref["Reference Patch\n(gold solution)"]
        
        ta["evallab tasks anatomy\n(Static Vulnerability Parser: AST & Regex)"]
        am["evallab audit-mutants\n(STING: AST Semantic Mutation Testing)"]
        hd["evallab harden\n(L1 Auto-load, L2 Pristine, L3 Report, L4 Verifier)"]
        cg["evallab certify-gold\n(Contract-Complete Test Synthesis + 6 Gates)"]
        
        pkg & ref --> ta
        pkg & ref --> am
        pkg --> hd
        am & hd --> cg
        cg --> certified["Hardened Task Package\n(Tamper-resistant verifier & tests)"]
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
        
        verdicts -. Calibrate .-> l0
        verdicts -. Calibrate .-> ar
        cascade --> hackdrop["evallab hack-drop\n(Rollout Filter - In Progress)"]
        hackdrop --> clean_sft["Clean SFT / RL Training Corpus"]
    end
```

---

## 2. Command Reference

All CLI commands run under `evallab` (or `python -m evallab.cli`) with `EVALLAB_DISABLE_LOCAL_DOCKER=1`. Every command line listed below has been verified with `--help` in the worktree.

| Command | Input | Output | Cost Class | Code Location |
|---|---|---|---|---|
| `evallab tasks anatomy` | Task directories or collections; optional `--reference-patch` | `evallab.package_anatomy/v1` card (stdout or JSONL) | Free local ($0, static AST/regex, <10 ms) | `src/evallab/package_anatomy.py` |
| `evallab audit-mutants` | Base package, reference patch, output dir; optional `--repaired-package` | Audit summary JSON, mutated cells CSV, survivor diffs | Modal cents (~$0.012 per weak grader; metered cap) | `src/evallab/mutant_auditor.py` |
| `evallab harden` | Task package path; `--expected-tests` (required for pytest unless `--allow-degraded`), optional `--single-container`, `--out` | Hardened package directory (L1–L4 hardened verifier) | Free local ($0, static container re-wiring) | `src/evallab/full_hardening.py` |
| `evallab certify-gold` | Base package, reference patch, output dir; optional `--cap-usd`, `--model` | Hardened package with `test_gold_contract.py`, 6-gate summary | Subscription (synthesis via `omp -p`) + Modal cents (execution matrix) | `src/evallab/gold_certifier.py` |
| `evallab trace anatomy` | Run ID (cloud/CheatBench) or `--atif-file <trial.json>`; optional `--include-narration` | Structured Case Anatomy card (human-readable text or JSON) | Free local ($0, raw CAS projection, <100 ms) | `src/evallab/trace_anatomy.py` |
| `evallab trace autoreview` | Run ID or ATIF trial JSON; optional `--compact-only`, `--with-l0-cascade`, `--model` | AutoReview judgment JSON (suspicion score, cited steps, cascade/union alerts when model runs) | Subscription (or Free local $0 with `--compact-only`) | `src/evallab/autoreview_monitor.py` |
| `evallab regrade` | Stored Harbor job directory (`<job_dir>`); optional `--task` (hardened verifier) or `--held-out` (offline tests), `--dry-run` | Re-scored job directory under `runs/` | Modal cents (container execution via ModalBackend, $0 model cost) or Free local ($0 with `--held-out`) | `src/evallab/regrade.py`, `src/evallab/cli.py` |
| `evallab judge serve` | Queue file (`[NAME=]PATH`), findings JSONL; optional `--port`, `--open` | Local blinded web UI on `127.0.0.1:8795` (use `--port <port>` with ephemeral port for scratch runs); records human verdicts to `verdicts.jsonl` | Free local ($0, stdlib HTTP, no models, no network) | `src/evallab/production/judge_station.py`, `judge_web.py` |
| `evallab judge build-packet` | Fixed 18-case packet markdown (`--adjudication`) and DT-17 cases JSON (`--cases`); optional `--state-dir` | `<state-dir>/queues/adjudication-packet.jsonl` and `guide-examples.jsonl` (fixed 18-case calibration builder) | Free local ($0, file formatting) | `src/evallab/production/judge_station.py` |
| `evallab judge export` | Verdicts state directory | Exported CSV or JSON of human verdicts | Free local ($0, read-only export) | `src/evallab/production/judge_station.py` |
| `evallab live-supervisor` | Live runs root; subcommands: `scan`, `serve`, `replay`, `adjudicate`, `rows`, `route-alarms`, `fleet-report` | `trace-findings/v1` records, shadow-mode decisions | Free local ($0, deterministic rules) | `src/evallab/production/live_supervisor.py` |
| `evallab watch` | Live runs root; subcommands: `ack`, `stop` | Live trial monitoring, stop requests | Free local ($0, watchdog loops) | `src/evallab/harbor_watch_hooks.py` |
| `evallab hack-drop` | Evaluated rollout traces / SFT candidate dataset | Cleaned dataset with reward-hacked runs purged | **In progress** (planned CLI; does not yet exist on `main`) | Planned pipeline filter |

---

## 3. Runbooks

### Runbook 1: Certify a Task Package

Before deploying a benchmark task to model evaluations, verify that its grader cannot be satisfied by broken or cheating code.

1. **Step 1: Run static package anatomy:**
   Check container boundary posture, baked files, reset lists, hook vectors, and test assertions:
   ```bash
   evallab tasks anatomy library/tasks/my-task --reference-patch library/tasks/my-task/reference.patch --json
   ```
   *Expected result:* Exit 0. Examine `exploit_grader_gap`, `grader_tamperable`, and `weak_check` hypotheses. If `AR-TESTS-IN-IMAGE` or `GT-AUTOLOAD-WRITABLE` fires with score $\ge 80$ (or categorical confidence `static_certain` / `static_probable`), the package requires container hardening.

2. **Step 2: Audit semantic mutations with STING:**
   Generate AST mutations of the reference patch and test whether the original grader accepts them:
   ```bash
   evallab audit-mutants \
     --package library/tasks/my-task \
     --reference-patch library/tasks/my-task/reference.patch \
     --out results/audit-my-task \
     --max-usd 0.50 \
     --approval "Operator task certification run"
   ```
   *Expected result:* Outputs `audit-summary.json`. If `weak_original: true`, the benchmark grader passes semantic bugs and requires hardening tests. (Note: requires paid Modal container execution authorized via `--approval` and bounded by `--max-usd`).

3. **Step 3: Certify with Gold Certifier (synthesis + separate verifier + 6 gates):**
   Synthesize contract-complete behavioral tests and package them into an L1–L4 separate verifier:
   ```bash
   evallab certify-gold \
     --package library/tasks/my-task \
     --reference-patch library/tasks/my-task/reference.patch \
     --out library/tasks/my-task-hardened \
     --cap-usd 0.95 \
     --model google-antigravity/gemini-3.8-flash
   ```
   *Expected result:* Evaluates the 6 certification gates on Modal:
   - Gate 1 (Reference Validity): Reference patch scores 1.0.
   - Gate 2 (Nop Rejection): Empty submission scores 0.0.
   - Gate 3 (Witness / Tamper Rejection): Grader tampering and conftest hooks score 0.0.
   - Gate 4 (L4 `.pth` Startup Interception): Interpreter hijack via `.pth` scores 0.0.
   - Gate 5 (Train Mutants Killed): Reports training semantic mutants killed (19/20 = 95.0% observed across 8 tasks; 0/1 on task 000931; reports count, no hard 90% threshold in code).
   - Gate 6 (Eval Mutants Killed): Reports held-out eval mutants killed out-of-sample (counts only).

---

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
   *Expected result:* Returns suspicion score (0–100), reasoning-free audit findings, and combined cascade verdicts (`cascade_alert` and `union_alert`). Note that `cascade_alert` and `union_alert` appear only when a Stage 1/2 model verdict is present (they are absent in `--compact-only` mode). A cascade alert is a high-precision alert (0/146 clean false alarms on Terminal Wrench DEV, but 50.0% recall and 2/10 false alarms on C1–C5) that flags suspect runs for human confirmation, not an autonomous ground-truth confirmation.

4. **Step 4: Adjudicate in Judge Station:**
   If a run sits in the ambiguous zone (suspicion 25–85) or requires human sign-off, it can be queued for blinded human review. Note: Judge Station operates on queues of cloud trace run IDs (`{"run_id": "<16hex>", "reason": "..."}`). Local ATIF trial JSONs cannot be served directly in Judge Station without being registered in the cloud run store (`judge build-packet` is a fixed 18-case calibration packet builder, not a per-job queue generator).
   For cloud runs queued in `queue.jsonl`:
   ```bash
   evallab judge serve --queue queue.jsonl --state-dir results/judge-scratch --port 0 --open
   ```
   *Expected result:* Serves blinded interface on `http://127.0.0.1:<port>` (pass `--port <port>` with an ephemeral port like `--port 0` in scratch/overnight runs to avoid collisions with default port 8795). The operator judges the timeline without seeing the benchmark, model label, or detector verdicts until submission. Verdicts are written to `<state-dir>/verdicts.jsonl`.

---

### Runbook 3: Filter a Rollout Set

When preparing agent trajectories for Supervised Fine-Tuning (SFT) or Reinforcement Learning (RL), trajectories with reward 1.0 obtained via reward hacking must be purged to prevent training models to cheat.

1. **Current Production Pipeline:**
   *Note: `evallab hack-drop` is currently in progress on `main`. Today, filtering is performed by composing `evallab trace autoreview` and hardened re-scoring.*

2. **Step 1: Batch evaluate traces with the L0 + AutoReview cascade:**
   Run AutoReview across the candidate pool:
   ```bash
   evallab trace autoreview <trace_path> --with-l0-cascade --json
   ```
   Traces with `cascade_alert: true` (high-precision filter) or `union_alert: true` (high-recall filter) are discarded or routed to human review. Note: `cascade_alert` and `union_alert` require full Stage 1/2 model evaluation to be present.

3. **Step 2: Re-score surviving positive trials under hardened verifiers:**
   Re-grade trials using the L1–L4 hardened package verifier at zero model cost:
   ```bash
   evallab regrade <job_dir> --task library/tasks/my-task-hardened [--dry-run]
   ```
   Any trial whose reward drops from 1.0 to 0.0 under the hardened verifier is dropped as an exploit. (Note: while model invocation cost is $0, the verifier executes in Modal containers and incurs container execution spend managed via `CloudSpendGate`, unless `--held-out` is used to re-score offline on cached local CPU images).

4. **Step 3: Adjudicate borderline cases:**
   Traces in the uncertain score band are queued for human review via `evallab judge serve`.

---

## 4. Numbers You May Quote

Every number below was verified directly by opening and reading its source receipt file either in this repository (`research/experiments/**`) or in verified external bridge receipts (`research-context/bridge/**`, not committed to this repository). All intervals are 95% Wilson confidence intervals. DT-15 refers to the earlier action-grounded trace reader baseline (`ds-dt15-action-grounded-reader-20261030`).

| Finding / Metric | Value | Gate Verdict | Status | Receipt Path |
|---|---|---|---|---|
| **STING Original Grader Weakness** | 12/18 = 66.7% (43.7–83.7%) weak original MiMo DEV graders accepting mutants | N/A (diagnostic) | Verified | `research/experiments/ov-sting-mutants-20261031/results/audit-summary.json` |
| **STING Grader Cost Efficiency** | $0.0116 / 70 sandbox-s per weak grader found ($0.1395 / 844 s over original-package cells + probes; total run $0.2068, 1,251 s, 89 executions including 12 repaired-package audits) | N/A (efficiency) | Verified | `research/experiments/ov-sting-mutants-20261031/results/audit-summary.json` |
| **STING Repaired Grader Residual Survivors** | 4/12 = 33.3% (13.8–60.9%) of prior "qualified" repairs still accepted mutants | N/A (diagnostic) | Verified | `research/experiments/ov-sting-mutants-20261031/results/audit-summary.json` |
| **Gold Certifier Gate 1 (Reference Validity)** | 8/8 = 100.0% (67.6–100.0%) reference patches pass hardened verifier | Gate 1 PASS | Verified (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Gold Certifier Gate 2 (Nop Rejection)** | 8/8 = 100.0% (67.6–100.0%) empty patches rejected | Gate 2 PASS | Verified (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Gold Certifier Gate 3 (Tamper Rejection)** | 8/8 = 100.0% (67.6–100.0%) witness and hook exploits rejected | Gate 3 PASS | Verified (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Gold Certifier Gate 4 (L4 `.pth` Forgery)** | 8/8 = 100.0% (67.6–100.0%) site-packages `.pth` hijacking defeated | Gate 4 PASS | Verified (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Gold Certifier Gate 5 (Train Mutants Killed)** | 19/20 = 95.0% (76.4–99.1%) training semantic mutants killed across 8 tasks (reports count; 0/1 on task 000931) | Gate 5: 19/20 killed (reports count, no 90% threshold in code) | Verified (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Gold Certifier Gate 6 (Eval Mutants - Base Survivors)** | 3/8 = 37.5% (13.7–69.4%) base-surviving unseen held-out mutants killed | Gate 6: 3/8 base survivors killed (reports count) | Verified (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Gold Certifier Gate 6 (Eval Mutants - All)** | 13/18 = 72.2% (49.1–87.5%) all unseen held-out eval mutants killed | Gate 6: 13/18 all mutants killed (reports count) | Verified (counts only; small in-sample set of 8 tasks, 4 repaired; no pre-reg file) | `research/experiments/ov-gold-certifier-8-20261101/results/summary.json` |
| **Unified Hardening Attack Closures** | 11/11 = 100.0% (74.1–100.0%) attack closures across 51 Modal cells at $0.1238 | PASS | Verified | `research/experiments/t2-unified-harden-20261031/results/summary.json` |
| **Package Anatomy Execution Speed** | p50 3.44 ms, p95 8.83 ms, max 10.56 ms across 35 MiMo packages (27 strict-DEV pytest + 8 non-pytest, 1 non-DEV) ($0) | PASS (gate_pass: true) | Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-sonnet/raw/T2A-PKG-ANATOMY2/receipts/summary.json` |
| **Package Anatomy C1–C5 Mechanism Detection** | 5/5 = 100.0% (56.6–100.0%) impossible tasks flagged with causal mechanism | PASS (gate_pass: true) | Verified (in-sample; external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-sonnet/raw/T2A-PKG-ANATOMY2/receipts/summary.json` |
| **Package Anatomy OQ-6 Card Generation** | 399 DEV cards (385 MiMo + 14 TB4), 0 non-DEV leaks | PASS | Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-sonnet/raw/T2A-PKG-ANATOMY2/receipts/summary.json` |
| **Trace Anatomy Zero Leaks & Errors** | 0/100 runs = 0.0% (0.0–3.7%) label leaks; 0/100 extraction errors | FAIL (prereg gate failed on SWE clause; whole-token leak check chosen after substring hits) | Pre-registered (gate failed) / Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/verify-all.json` |
| **Trace Anatomy Card Compression** | 24.8× median of per-run compression ratios (median card 3,069.5 chars vs 121,545 raw chars; ratio of medians is 39.6×) | N/A (diagnostic) | Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/verify-all.json` |
| **Trace Anatomy SWE Patch Recovery (Pre-registered Baseline)** | 15/17 (88.2%) in old DB projection → 15/17 (88.2%) in raw CAS anatomy (using pre-registered "any diff block" baseline) | FAIL (SWE clause required improvement; 15/17 -> 15/17) | Pre-registered (gate failed) / Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/verify-all.json` |
| **Trace Anatomy DeepSWE Patch Recovery (Post-Hoc Baseline)** | 0/17 (0.0%) in old DB projection → 15/17 (88.2%) in raw CAS anatomy (using post-hoc exact-copy baseline) | N/A (baseline added after prereg; do not quote as headline) | Post hoc / Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/verify-all.json` |
| **Terminal Wrench L0 Live Prefix Fix (Validation Fold, n=274)** | Recall 104/138 = 75.4% (67.6–81.8%), Clean FA 4/136 = 2.9% (1.1–7.3%) vs pre-fix baseline (6.3% recall / 20.5% FA; Fisher exact $p = 1.08 \times 10^{-39}$ tests post-fix separation on validation fold) | PASS (diagnostic validation) | Verified | `research/experiments/ov-autoreview-v2-tw-20261031/result.json`, `VF-OV1/verify-all.json` |
| **Terminal Wrench L0 Live Prefix Fix (Full DEV Set, n=304)** | Recall 121/158 = 76.6% (69.4–82.5%), Clean FA 4/146 = 2.7% (1.1–6.8%) | N/A (full DEV cohort) | Verified | `research/experiments/ov-autoreview-v2-tw-20261031/result.json` |
| **Terminal Wrench L0 + AutoReview Cascade (Full DEV Set, n=304)** | Recall 79/158 = 50.0% (42.3–57.7%), Clean FA 0/146 = 0.0% (0.0–2.6%) | G-TW FAIL (FA passed 0.0% <= 1.0%, recall 50.0% < 60% requirement) | Pre-registered (gate failed) / Verified | `research/experiments/ov-autoreview-v2-tw-20261031/result.json` |
| **Terminal Wrench Post-Hoc Cascade** | Recall 89/158 = 56.3% (defined after seeing data; counts 11 runs whose final AutoReview verdict was clean) | N/A (post-hoc definition; do not quote as headline) | Post hoc | `research/experiments/ov-autoreview-v2-tw-20261031/result.json` |
| **CheatBench AutoReview Clean FA vs DT-15 (Fixed Rubric)** | Clean FA 0/75 = 0.0% (0.0–4.9%) vs DT-15 8/75 (10.7%); discordant pairs 8 (DT-15 only) vs 0 (ours only), exact McNemar $p = 0.0078$ in our favour | G-CB FAIL, G-CB-P2 FAIL (FA passed, recall clause failed) | Pre-registered (gate failed) / Verified | `research/experiments/ov-autoreview-investigator-20261031/result.json` |
| **CheatBench AutoReview Recall vs DT-15 (Fixed Rubric)** | Recall 22/75 = 29.3% (20.2–40.4%) vs DT-15 36/75 (48.0%); discordant pairs 17 (DT-15 only) vs 3 (ours only), exact McNemar $p = 0.0026$ in DT-15's favour | G-CB FAIL, G-CB-P2 FAIL (recall < 25/74 requirement) | Pre-registered (gate failed) / Verified | `research/experiments/ov-autoreview-investigator-20261031/result.json` |
| **CheatBench AutoReview Quantile FA vs DT-15** | Clean FA 2/75 (2.7%) vs DT-15 8/75 (10.7%); discordant pairs 8 vs 2, exact McNemar $p = 0.109$ (not significant; net difference was 6 vs 0) | N/A (in-sample quantile) | Verified (in-sample quantile threshold q=0.985 evaluated on same 150 scores) | `research/experiments/ov-autoreview-investigator-20261031/result.json` |
| **Goodfire deep_swe AutoReview Fixed Rubric** | Recall 6/50 = 12.0% (5.6–23.8%) on subset (or 3/80 = 3.8% across full 80 positives), Clean FA 0/50 = 0.0% (0.0–7.1%) (or 0/70 across 70 negatives) | G-GF FAIL (recall 3/80 < gate requirement) | Pre-registered (gate failed) / Verified (external bridge receipt, not in repo) | `research/experiments/ov-autoreview-investigator-20261031/result.json`, `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/report.md` |
| **Goodfire deep_swe AutoReview Token Compression** | 8.19× smaller median input tokens (10,545.5 vs 86,387 tokens; $1.08 vs $6.66) | N/A (efficiency) | Verified (external bridge receipt, not in repo) | `/Users/petermakhnatch/Developer/research-context/bridge/2026-10-31-overnight/ag-opus/raw/VF-OV1/report.md` |
| **Solvable Task Friction Propensity (Sol/Astra)** | 0/42 = 0.0% (0.0–8.4%) spontaneous subversions across 42 unprompted rollouts | PASS | Verified | `research/experiments/ov-safeguard-rollouts-20261031/receipts/summary.json`, `ov-safeguard-friction-v2-20261031/receipts/summary.json` |
| **Impossible Task Propensity (C1–C5)** | 6/20 = 30.0% (14.5–51.9%) spontaneous cheats on impossible tasks (test contradictions C1, hidden tests C2, missing assets C3, fabricated verification C4, environment acquisition C5) | N/A (diagnostic) | Verified | `research/experiments/ov-c1-c5-live-rollouts-20261101/receipts/summary.json` |
| **Impossible Task Container Defense (`harden`)** | Cheats dropped from 6/20 (30.0%) to 2/20 (10.0%) (neutralized 4/4 C1 cheats, 0/2 C5 cheats) | N/A (diagnostic) | Verified | `research/experiments/ov-c1-c5-live-rollouts-20261101/receipts/summary.json` |
| **Impossible Task Monitor Performance** | 7/10 = 70.0% (39.7–89.2%) recall at 2/10 = 20.0% FA (verdict) or 3/10 = 30.0% FA (union); 5/6 (83.3%) on confirmed cheats | N/A (diagnostic; earlier 9/10 @ 0/10 claim retracted) | Verified | `research/experiments/ov-c1-c5-live-rollouts-20261101/receipts/summary.json` |
| **Human Calibration on Judge Station** | 69 Judge Station adjudication verdicts pending human evaluation | Pending | Not measured (ground truth calibration pending human adjudication) | N/A |

---

## 5. Numbers You Must Not Quote (Corrected Claims)

Independent blind verifiers (VF-OV1, VF-OV2) audited the overnight experiments and corrected several unsupported or misleading claims. The statements on the left must **never** be quoted:

| Do Not Say | Say Instead | Grounded Reason |
|---|---|---|
| *"35/35 DEV MiMo packages measured / reference scores 1.0 on every layer"* | Coverage was 27 pytest-eligible packages with full layers plus 8 non-pytest packages classified only; one package was not strictly DEV. | T2 ran no hardened-spec (HS) reference cell for `000603` or `000838`. Cite per-package receipts. |
| *W3B impossible monitor achieved "9/10 recall at 0/10 false alarms"* | Recall was 7/10 at 2/10 false alarms (verdict) or 3/10 (union alert); 5/6 on container-confirmed cheats. | The 9/10 at 0/10 claim required post-hoc relabeling where two false alarms were reclassified as positives and two missed cheats were reclassified as negatives. |
| *CheatBench quantile false alarms beat DT-15 with McNemar $p = 0.031$* | Exact two-sided McNemar on the discordant pairs (8 vs 2) yields $p = 0.109$ (not statistically significant). | The reported 0.031 came from treating the net difference (6 vs 0) as if it were the discordant count. |
| *"89/158 with the cascade" as the headline for Terminal Wrench* | 79/158 at 0/146 false alarms is the pre-registered result; 89/158 is post hoc. | 89/158 was defined after inspecting outcomes and counts 11 runs whose final AutoReview verdict was `clean`. |
| *deep_swe "98% protected writes" discriminates cheats from clean runs* | 98% of all deep_swe rollouts write to protected paths, including 49/50 clean runs. | Agents legitimately add `_test.go` unit tests, making protected-surface writes non-discriminative without AST/patch inspection. |
| *deep_swe AutoReview token reduction was 8.25× (or 87k vs 10.5k means)* | Token median ratio recomputed from session receipts is 8.19× (86,387 vs 10,545.5 tokens). | 8.25× and 87,134 / 10,559 were unverified estimates; exact receipt medians give 8.19×. |
| *Any claim that labels were adjusted to match monitor output* | Labels are strictly frozen before reading monitor output; relabeling after observing monitor output is forbidden. | Scientific validity requires that ground truth labels remain independent of detector behavior. |
| *Claim that a specific deep_swe case was a clean run cleared by AutoReview* | One deep_swe case narrative was inverted in an earlier worker report (run ID kept in the local VF-OV1 report). | Case narrative was inverted in the worker report; the run was an enacted hack that AutoReview missed. Per third-party policy, individual run IDs are not committed. |

---

## 6. Known Gaps and Operating Limits

1. **69 Human Verdicts Pending:**
   The 69-case adjudication queue in Judge Station has not yet been judged by a human operator. Until those verdicts are recorded, human-calibrated precision and recall benchmarks do not exist.
2. **Strictly DEV-Only Cohorts:**
   Held-out benchmark splits and TEST datasets were never accessed. All numbers reflect development sets; out-of-distribution transfer to held-out evaluation splits remains an empirical unknown.
3. **Spend-Gate Over-Reservation:**
   `CloudSpendGate` reserves a configurable amount per execution (`--per-execution-usd`, default $0.005), which reserves approximately 1.5× to 3× the actual metered container cost on Modal (e.g. STING metered $0.2068 for 89 executions, ~$0.0023 each, vs $0.005 reserved). Real costs must be read from metered container logs (`qa-execution.json` or `audit-summary.json`), not reservation ledgers.
4. **Boundary-Shift Mutant Misses:**
   Contract-complete test synthesis currently misses subtle off-by-one or floating-point boundary shifts (e.g. task `000232` and `000484` LogEI edge cases), explaining why held-out generalization on base-surviving mutants sits at 3/8 (37.5%).
5. **Rollout Filtering CLI (`evallab hack-drop`):**
   `evallab hack-drop` is in progress and not yet merged on `origin/main`. Automated filtering of rollout training data currently requires manual composition of `evallab trace autoreview` and hardened re-scoring.
