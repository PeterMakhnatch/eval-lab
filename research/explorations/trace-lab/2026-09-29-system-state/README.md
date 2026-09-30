---
type: reference
collection: trace-lab
updated: 2026-09-29
audience: Peter
status: snapshot
---

# Trace Lab on 2026-09-29: what we have, whether it works, what's missing

Snapshot at 17:10Z, by Research-Harbor. It was checked against code in the `har81-dispatch-531` worktree, the run folders, Linear receipts, and live reads of Modal's app list and billing report. Unverified claims are marked **[INFERENCE]** or **UNKNOWN**.

## Bottom line

1. **Running tasks works.** Overnight, 44 MiMo runs on the distill were all scored, and 8 passed (7 clean). Every run records its exact setup, every step at four layers, and every model call.
2. **Nothing costing money is running.** At 17:05Z Modal showed no apps and no containers. Daytona sandboxes delete themselves on stop and have a 70-minute lifetime cap. The Daytona list itself couldn't be re-checked from here.
3. **Cost tracking has three real holes.**
   - MiMo runs write no cost, so the $20/day gate only sees estimates.
   - Unresolved calls inflate token totals.
   - A job killed while saving vanishes from the catalog and from spend.

   All three have $0 fixes (section 3).
4. **Processing traces is all manual.** Every analysis is a script an agent runs by hand after the fact. Nothing runs when a job lands. Stitching continuation files alone is implemented four separate times.
5. **Choosing experiments has no fixed method.** The good experiment READMEs (HAR-81/90/85) pin the setup and budget, but none says why it runs that many trials. At 44 runs, a pass rate is only known to ±11 points. Section 5 proposes a one-page experiment card and a processing pipeline that runs on every job. Section 5e now holds Peter's 22:25Z reset: explore with a few runs, no SFT or RL.

## Posters

| Poster | What it shows |
|---|---|
| [01-system-today.png](01-system-today.png) | Every part involved in one MiMo run, left to right: tasks, experiment and approval, execution (controller, normaliser, proxy, Modal, Daytona, verifier), then records and analysis. The bottom band is the money-and-teardown health check. |
| [02-runs-to-decisions.png](02-runs-to-decisions.png) | The proposed system: question → experiment card → run → automatic processing → blind hand audit → findings to owners. It also carries the experiment ladder and the sample-size arithmetic. Each step is marked working, manual, or missing. |

Each poster also exists as `.svg` and `.excalidraw`. The scene code is in `src/` and renders with the managed skill `excalidraw-diagrams`.

## 1. The parts

| # | Part | What it does | Where | Operated by | Automatic? |
|---|---|---|---|---|---|
| 1 | FineEnvs task snapshots | Six HF repos pinned by revision, 7,780 tasks. We use code, cyber and terminal. | `derived/task-store/hf/…`, pins in `research/experiments/har81-mimo-sft/split.json` | Data | pull is manual |
| 2 | Sealed split | Held-out tasks are never trained or tuned on (salt `har81-sealed-20260928`). The train pool has 48 tasks. | `split.json`, `cohort.json` | Data | frozen once |
| 3 | Nop qualification | Runs every task with an agent that does nothing. A grader that errors or passes here is broken. HAR-88: 3 of 113 broken. HAR-95: 13/13 ok. | `research/experiments/mimo-daytona-nop/`, `task_qualification.py` | Data | manual batch |
| 4 | Experiment README + treatment key | The question, the pinned setup (hashed into a key), the tasks, costs and stop rules | `research/experiments/<name>/README.md`, `derived/har81/treatment-key.json` | Infra / Engineering | written by hand |
| 5 | Queue + policy gate | submit → `approve --actor peter` → tick. Caps are $3/job and $20/UTC day, with a breaker after 3 failures. | `src/evallab/queue.py:571-586`, `policy/standing-approvals.yaml` | Infra; Peter approves | tick is automatic |
| 6 | Terminus-2 controller (Harbor 0.21.0) | Runs the agent loop on the Mac: prompt, reply, keystrokes, screen. It summarises at 16,384 tokens. | `src/evallab/harbor_terminus.py` | — | yes |
| 7 | MiMo reply normaliser | Maps MiMo's own tool-call formats to Terminus commands | `src/evallab/mimo_tool_calls.py` (#521, #526, #533, #536) | Engineering | yes |
| 8 | Capture proxy + ledger | Logs every model call with tokens, marked reconciled or unresolved | `src/evallab/model_capture.py`, `containers/zai_openapi_secret_proxy.py` | — | yes |
| 9 | Modal SGLang server | Serves the distill: 1× A100-80GB at $2.81/h, scales to zero after 5 idle minutes, 64K context | `tools/modal-mimo-serve/serve.py` | Engineering / Infra | **stop is manual** |
| 10 | Daytona sandbox | One per trial. Tier 2, 70-minute lifetime, auto-stop after 5 idle minutes, deleted on stop | `src/evallab/harbor_daytona.py:113-136` | — | yes |
| 11 | Verifier | The task's own tests produce a reward of 1.0 or 0.0. The run is scored even when it hits a limit (#515/#528/#531). | per task `tests/` | — | yes |
| 12 | Trial records | `result.json`, `trajectory.json` (+ `.cont-N`, four layers per step), `recording.cast`, `verifier/`, `lab-metadata.json`, `job.log`, `lock.json` | `runs/<job>/<trial>/` | — | yes |
| 13 | Catalog tables | `trial_treatment` (setup key per trial), `trial_capture` (tokens, gaps), `model_calls`. The pool check refuses mixed setups. | `derived/parquet/external/task_catalog/` | Data | **manual collect** |
| 14 | Tagger (probe-03) | First failure, harness vs model, loops, stuck terminals, taint. Agrees with blind hand reads on 43/44. | `research/explorations/trace-lab/probe-03-capabilities/` | Traces | **manual** |
| 15 | SFT export | Passing runs only. It refuses held-out tasks and runs secret and taint checks. Latest output: 7 conversations. | `src/evallab/sft_terminus.py`, `/private/tmp/har81-wave/sft-waves-ab-v3-curated/` | Infra | **manual** |
| 16 | Coordination | Linear cards (the work queue and receipts), Herdr tabs, `MISSION.md` (decisions) | `lin`, `inbox/sft-overnight-20260918/MISSION.md` | Research-Harbor | — |

**Compared with the plan.** [`loop-system/`](../../loop-system/README.md) planned an always-on Linux VM, GLM as the agent, Reef as the improver and Tinker RL. None of that was built. What runs today is a Mac worktree, the self-hosted distill on Modal, and manual approve-then-tick.

## 2. One trial, end to end

1. Data freezes the task pool from the sealed split, minus broken graders.
2. An experiment README pins the setup, and `stage.py` hashes it into a treatment key.
3. The job goes into the queue. Peter approves it, and the policy gate checks the estimate against $3/job and $20/day.
4. `tick` starts Harbor. The controller opens a Daytona sandbox and calls the Modal server through the capture proxy.
5. Each MiMo reply is normalised into keystrokes. The screen text comes back, and the loop repeats until the model declares completion or hits a limit (200 calls, 2.5M input tokens, 900/3,600 s).
6. The verifier runs the task's tests and writes the reward. The sandbox is deleted.
7. Records land in `runs/<job>/<trial>/`. Someone then runs ingest, treatment-collect and pool-check by hand.
8. Agents run the tagger, the reports and the SFT export by hand, and post receipts to Linear.

## 3. Is the infrastructure correct?

| Check | Status | Evidence |
|---|---|---|
| Modal costs nothing when idle | **PASS** | `serve.py:110-122`: `min_containers=0`, 5-min scaledown. At 17:05Z, `modal app list` and `container list` were both empty. |
| Modal is stopped after each wave | **PARTIAL** | Stopped by hand every time. One stop came 2.5 min late, costing ≈$0.12 (HAR-81). |
| Daytona sandboxes can't leak | **PASS** | `harbor_daytona.py:116-136`: explicit TTL (70 min on HAR-81), auto-stop after 5 idle minutes, `stop(delete=True)` always. |
| Daytona state checkable now | **UNKNOWN** | `daytona list` is unauthorized from this shell. Infra's last check (11:27Z) showed 0 sandboxes. |
| $3/job and $20/day caps enforced in code | **PASS** | `queue.py:571-586` `PolicyGate.decide` |
| Caps see real MiMo spend | **FAIL** | The daily sum reads `trials.cost_usd` (`database.py:204, 349-361`), and `agent_result.cost_usd` is null on all 27 wave-B trials I checked. The gate sees only up-front estimates. |
| Every model call accounted for | **PASS** | Unresolved calls are flagged, not dropped (0758-c: 93 flagged, and the run was refused as `proxy_usage_unreconciled`). |
| Token totals honest | **FAIL** | Tokens reserved for unresolved calls are added to totals. 0758-c shows 26.1M input vs 5.5M actually sent (`runner.py:652-690`). |
| A killed job is still counted | **FAIL** | a4-000240 was killed while saving. It has no `lab-metadata.json` and no `finished_at`, and is absent from the catalog and from spend. |
| Bills reconciled | **PARTIAL** | Done by hand in receipts. Modal's billing report shows $13.68 for 2026-09-29, which matches the receipts. Daytona has no per-sandbox bill, so sandbox cost is a rate-card estimate (≈$2.4 today). |

**$0 fixes, ranked by what they protect:**

1. Write each MiMo trial's cost at finalize: server share plus sandbox share, with the formula that already exists (`mimo_selfhosted_trial_cost_usd`, $2.814912/h). That makes the $20 gate see real spend.
2. A repair path for jobs killed while saving, so they land in the catalog as unscored-with-cost instead of vanishing.
3. Keep reserved tokens of unresolved calls in a separate field (`attempted_input_tokens`), not in the totals.
4. A stop hook that runs `modal app stop` when a wave's stop rule fires and records the empty container list.
5. A daily reconcile that pulls `modal billing report` into the catalog and puts the gap next to our computed cost.
6. Re-authorise the Daytona CLI (read-only list) so every receipt can show the sandbox count.
7. Rotate the SGLang key before the next deploy. The local key file was deleted after the run.

## 4. What we do with traces today

| Asset | Question | Mode | Validation |
|---|---|---|---|
| Four-layer step records (HAR-92) | What did the model propose, what did the harness accept, what ran, what came back? | **automatic** at runtime | hand counts match on 4 trials; Reef found 1 wrong step in 5 trials (being fixed, HAR-100) |
| Catalog tables + pool check (HAR-93) | Which runs can be compared with which? | manual collect | 44/44 checked. 43 fall into one setup (split by terminal vs non-terminal timeouts); the 1 orphan has unknown fields |
| Tagger, probe-03 (HAR-91) | What failed first, harness or model, which steps? | manual | 43/44 agree with blind hand reads |
| Stuck-terminal rule (HAR-99) | Did the agent get trapped in a pager, REPL or prompt? | manual | 51/54 blind; 53/54 after 2 disclosed fixes |
| Replay census (HAR-100) | Would a parser change alter any past decision? | manual | exact match on 3,513 live turns |
| `evallab report run` | One trial, human-readable | manual | — |
| SFT export + curation | Which passes become training data? | manual | fidelity audit; 4 bugs fixed |
| Docent export, Scout scans | View and search in outside tools | manual, dry-run only | not uploaded |

These analyses were redone by hand more than once overnight, and are the automation candidates:

- continuation stitching (4 implementations)
- receipt tables
- token and coverage reconciliation
- parse-rate replay
- grader-suspect screening
- reading-sheet picks
- the SFT export → curate → audit chain
- spend accounting
- repair of killed jobs

## 5. The missing system

### 5a. An experiment card before every paid run

This is one page and a gate. Nothing is staged until it's filled in.

1. **Decision:** what we will do differently depending on the result.
2. **Change:** exactly one thing differs from the control, and both are pinned as treatment keys.
3. **Tasks:** from the sealed split, nop-qualified, suspect graders excluded, and whether they're paired across arms.
4. **Size and why:** the smallest effect worth detecting, and the number of runs that detects it (see 5d).
5. **Metrics, declared up front:**
   - pass rate and clean-pass rate
   - stop reasons
   - parse rate
   - loop tokens
   - cost per run
6. **Budget:** expected, worst case, and the stop rule.
7. **Acceptance:**
   - every run scored
   - ledger reconciled
   - cost written
   - teardown verified
   - pool check passed
   - blind hand audit of 5 runs
8. **Result → decision:** recorded on the card.

HAR-81's README already covers 2, 3, 6 and most of 7. The new parts are 1, 4 and 8.

### 5b. Processing every run automatically

When a job lands, one command (and later the tick itself) should:

1. check integrity: all scored, ledger reconciled, cost written, no killed job left behind;
2. collect the catalog rows and run the pool check;
3. stitch continuations once, with a shared library that also feeds Docent and Scout;
4. run the detectors: parse shapes, confirmation and repeat loops, stuck terminal, token ceiling, taint candidates, diff drift;
5. tag first failure and attribution (probe-03 moved into Eval Lab);
6. write one run report, the catalog flags and the SFT selection with reasons;
7. pick a stratified blind sample (pass, fail, oddity) for hand audit, and record the agreement numbers.

### 5c. Five uses of traces, and who owns each

| Use | Overnight example | Owner |
|---|---|---|
| Is the result real? | 2684's pass depended on a forbidden network install. Its 1.0 is kept, but it's excluded from training. | Research-Harbor gate |
| Harness bug | MiMo's reply formats. Parse rate 99.5% / 96.8% after three fixes. | Engineering |
| Broken task | Graders 1634, 1789 and 1702 (HAR-97) | Data |
| Training data | 7 clean passing conversations | Infra |
| What the model can't do | Cyber: only 1 of 6 runs reproduced the crash, and it never submitted | Traces → next question |

### 5d. Why the number of runs matters

- **Precision now:** at an 18% pass rate (8/44), the 95% interval is ±11 points, roughly 7%–29%. It shrinks to ±5 points at 200 runs.
- **Detecting a 10-point gain** (18% → 28%) at 95% confidence and 80% power takes about **277 runs per arm** if the arms use different tasks. Pairing the same held-out tasks across arms needs fewer; the baseline's per-task spread tells us how many.
- **Four attempts per task** separate "never" from "sometimes". They don't estimate a rate.
- **Cost:** HAR-81 cost ≈$0.21 per run all-in ($9.10 / 44) **[INFERENCE: includes server idle time]**. A 277-run arm is ≈$60, before the savings from the confirmation-loop fix.

### 5e. What we do next (reset, Peter 2026-09-29 ~22:25Z)

The SFT ladder first written here (held-out baseline, data collection, fine-tune) is withdrawn. Poster 2's ladder panel shows that old version. Peter's direction: no SFT or RL for now. Get a minimal setup right, run a few tasks, and learn to analyse them well; fix tasks where needed.

| Step | Work | Card | Cost |
|---|---|---|---|
| 1 | Exploration setup: Harbor's default trajectory settings (one file per run, real tool calls), the self-hosted XiaomiMiMo/MiMo-V2.6-Distill-Qwen-9B on Modal with 120-call / 2.5M-input-token limits, cost written per run (corrected 2026-09-30: the MiMo-Flash switch was Research-Harbor's, not Peter's; Peter kept the distill) | HAR-104 (Engineering) | $0 + one proof run |
| 2 | One run understood end to end: every view agrees on `arvo-18737`, with a short walkthrough; check the lower ceiling loses no past pass | HAR-106 (Traces) | $0 |
| 3 | About 10 Python code tasks (FineEnvs code set), nop-checked | HAR-105 (Data) | cents |
| 4 | One run per task on the new setup | HAR-104 | under $1 expected, $3 cap |
| 5 | Analyse the batch: Eval Lab first, then Scout and Docent compared against hand reads; fix Eval Lab gaps | HAR-106 | ≤ $2 for tool model calls |
| 6 | Task-fix loop: fix 1634 / 1789 / 1702 and the cyber submit instruction in local copies, nop before and after, then 1–2 model runs to check they are fixed and still useful | HAR-105 | cents |

**Why a model change was proposed (withdrawn).** The proposal to move to MiMo-V2.6-Flash was Research-Harbor's, not Peter's, and was withdrawn on 2026-09-30 ~03:40Z at Peter's instruction; the exploration model remains the distill on Modal. The cost facts that motivated the proposal: overnight Modal cost $13.68 for 58 runs, about 4.9 A100-hours. Runs read about 2.8M input tokens each (the median failing run hit the 2.5M ceiling) and wrote about 14K. The same tokens would cost about $3 on MiMo-V2.6-Flash or DeepSeek V4.1 Flash off-peak with 90% prefix-cache hits **[INFERENCE: hit rate assumed]**, and about $16.6 on Qwen3.5-9B via OpenRouter, which lists no cache discount. The bigger saving would have been the lower ceiling: 10 runs at 500K tokens is about 5M tokens.

Outcome: HAR-104 ran 10 Python tasks on the distill: 4/10 reward, 2 earned (002391, 002864), 2 copied the fix from PyPI (000226, 000927).

### 5f. Where professional tools fit

Traces' survey, [`LANDSCAPE.md`](../LANDSCAPE.md) (2026-09-29), puts Inspect Scout and Docent at the centre of the field. Both import Harbor runs, but neither handles our continuation files or MiMo's in-text commands. The shared stitching library in 5b is exactly the pre-processor they need. The tools can then serve as viewers and search layers on top of our records, which stay the source of truth.

## 6. Decisions for Peter

Superseded by the reset in 5e (Peter approved it at ~22:25Z). The held-out baseline and SFT `max_length` questions are withdrawn. The suspect graders get fixed and re-checked (HAR-105) instead of just being excluded. Still open: whether 5a's experiment card becomes a gate, and who builds 5b's automatic processing. Both come back after the exploration batch.

## Evidence

- Code (Eval Lab worktree `har81-dispatch-531`): `src/evallab/queue.py:571-586`, `database.py:204,349-361`, `harbor_daytona.py:113-136`, `runner.py:652-690`, `tools/modal-mimo-serve/serve.py:60-122`, `derived/har81/treatment-key.json`.
- Live reads (2026-09-29 17:05Z): `modal app list --json` returned `[]`, `modal container list` returned `[]`, and `modal billing report --for today` gave 13 rows totalling $13.68.
- Trial check: `agent_result.cost_usd` is null on all 27 `har81-*` trial results in `har81-dispatch-531/runs/`.
- Linear receipts: HAR-81, 88, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100.
- Working audits (transient agent outputs, summarised here): InfraAudit, SystemMap, AnalysisInventory.
