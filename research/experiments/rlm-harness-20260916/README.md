# RLM harness study, 2026-09-16 overnight (Harness lane, HAR-55)

Peter (2026-09-16 ~05:50 UTC, AFK): "run overnight ... take dspy.rlm harness and
improve it in some non-trivial way ... hill climb relevant benchmarks measurably and
produce a lot of output that we can analyze later on ... 5-6 hours." Branch
`harness/rlm-overnight-20260916`, worktree `.worktrees/rlm-harness-20260916`, base
`origin/main` 04d04499. Raw runs stay under the worktree `runs/` (not committed);
this packet holds the reviewed summaries, the specs that ran, and the decision.

## TL;DR

1. The registered Harbor `dspy-rlm` agent cannot run: Harbor 0.21.0 (and upstream
   `main` on 2026-09-16) builds `dspy.RLM(..., max_iterations=...)`, but dspy 3.3.1
   takes `max_iters=` and raises `TypeError` before the first model call; dspy and
   deno were also absent from the shared harbor tool. A lab-owned import-path agent
   (`evallab.harbor_rlm:LabRlmAgent`, agent id `rlm`) now runs through the normal
   executor with a named, content-addressed harness policy per trial.
2. With GLM-5.3-Flash the dominant harness defect is **format drift**: the model
   mirrors dspy's rendered REPL history (`Reasoning: ... Code: ```python`) instead of
   the `[[ ## field ## ]]` markers the ChatAdapter parses. Stock dspy then retries
   with its JSONAdapter, which GLM answers with a bare `{}`, and the trial dies.
   43 drifted turns were observed across 37 runs (19 in 12 stock bench tasks,
   24 in 11 Harbor trials).
3. Two harness interventions fix that class without touching the model or the
   task: in-loop recovery (drifted turn becomes a recoverable observation) and,
   under `lenient` policies, **salvage** of the fenced code plus its preamble as the
   action. Paired on 12 identical tasks: accuracy 12/12 vs 12/12, mean cost
   0.051 → 0.034 USD-eq (ratio 0.69; per-task 7 cheaper / 5 dearer, sign-test
   p = 0.77), iterations 5.5 → 4.0, wall 143 s → 99 s; on the `ledger-agg` family
   iterations 6.17 → 3.33 and cost −53 %. The mean effect is real, the per-task
   effect is noisy because drift is stochastic; n must grow (see post-reset).
4. On Harbor the stock harness has a second, structural defect: the REPL is a
   host-side Pyodide sandbox, so `open("/app/output/result.json", "w")` succeeds in
   the sandbox and never reaches the task container. `syn-funcdag-easy` stock:
   reward 0.0 (13 steps, 8 drifted turns, file written to the sandbox);
   `bridge`, `orchestrator-bridge`, `compact-bridge`: reward 1.0 each via
   `write_file` + `exec_command` verification.
5. At 120 k characters the synthetic suite does not exercise recursion at all:
   0 `llm_query` calls in 26 bench runs; GLM classified merchants from world
   knowledge in Python dicts, followed chains with regex, and replayed state
   streams in code. The RLM behaved as a CodeAct harness. Next iteration must use
   inputs whose semantics cannot be shortcut by name knowledge.
6. The Z.ai Pro 5-hour window (12 000 credits) is the binding shared constraint:
   14 % at 06:19 Z, 61 % at 06:50 Z, 75 % at 06:58 Z with this lane stopped
   (HAR-54 GEPA confirmation runs alone burned ~160 credits/min). This lane
   paused all billable work at 06:50 Z and resumes only after the 10:39 Z reset
   under a 70 % self-cap (`runs/rlm-overnight-20260916/post-reset.sh`).

## What was built (all on the branch)

| Piece | Path | Notes |
|---|---|---|
| Harbor agent | `src/evallab/harbor_rlm.py` | `LabRlmAgent` (host-side dspy.RLM + Harbor `EnvironmentToolBridge`), optional `run_python` tool executing inside the container (`ContainerPythonBridge`), writes `agent/rlm/{policy,trajectory,usage}.json` |
| Runner lane | `execution_contracts.py`, `runner.py`, `queue.py`, `profiles.py`, `credentials.py`, `schemas/__init__.py` | agent id `rlm`, profile `rlm-glm-5.3-flash`, `harness_policy` on `RunRequest`/`ExperimentSpec`, `--agent-kwarg policy=…,cost_limit_usd=…`, host 0400 secret-file transport (`EVALLAB_ZAI_SECRET_FILE`), no proxy URLs; additive only |
| Policies | `src/evallab/rlm/policies.py` | content-addressed `RlmPolicy`; `stock` reproduces Harbor defaults; `orchestrator` (decomposition nudge adapted from alexzhang13/rlm `ORCHESTRATOR_ADDENDUM`, MIT), `*-mask4`/`compact` (last-N observation masking of REPL outputs, HAR-50 semantics), `*-remind`, `*-verify`, `stock-lenient`/`orchestrator-lenient` (salvage), `stock-markers` (history rendered with dspy field markers), `bridge`/`orchestrator-bridge`/`compact-bridge` (sandbox↔container guidance), `tools-bridge`/`orchestrator-tools` (`run_python`) |
| Harness | `src/evallab/rlm/harness.py` | `LabRlm(dspy.RLM)`: policy addenda, history masking / marker rendering, iteration reminders, API-equivalent cost ceiling, no JSONAdapter fallback, in-loop parse recovery, salvage; `build_lms`, `lm_usage`, `run_rlm` |
| Synthetic suite | `src/evallab/rlm/bench/` | deterministic `ledger-agg`, `chain-lookup`, `state-tracking` families with structured ground truth and format-tolerant scorer |
| Runners | `src/evallab/rlm/bench_runner.py`, `bench_report.py`, `gepa_rlm.py` | per-task JSONL + trajectories, paired report with exact sign tests on accuracy/cost/iterations, GEPA driver over the action instructions (written, not yet run: quota) |
| Tests | `tests/test_rlm_lane.py`, `tests/test_rlm_policies_bench.py`, `tests/test_rlm_harness.py` | 14 + 14 + 8 focused behavioural tests (`test_rlm_harness.py` skips without dspy) |

Runtime: isolated `runs/.harbor-dspy` venv (untracked, under the scanner-ignored runs/) (harbor 0.21.0 + dspy 3.3.1, deno 2.9.6) put on
`PATH` only for this lane's executor process; the shared `~/.local/bin/harbor` tool is
untouched. Model: `zai-coding-plan/glm-5.3-flash` via the Z.ai coding endpoint, key
read host-side from the runner's 0400 file, never in the container or logs.

## Track 1: registered Harbor tasks through the normal executor

Specs in [specs/](specs/); each trial ran serially via `evallab submit` →
`evallab approve --actor harness (…HAR-55)` → `evallab tick --max-specs 1`.
Rewards are the task verifiers'; `pf` = drifted turns recovered in-loop.

| Task | Policy | Reward | Steps | pf | USD-eq | Decisive event |
|---|---|---|---|---|---|---|
| event-summary | stock (smoke) | 1.0 | 4 | 0 | 0.032 | `open()` in sandbox failed on step 2, recovered with `read_file` |
| event-summary | stock | 1.0 | 7 | 3 | 0.056 | recovered from 3 drifted turns |
| event-summary | bridge / compact-bridge / orchestrator-bridge (r1) | died | 0 | – | 0.03 total | provider 429s + dspy JSON fallback `{}` (pre-fix runtime); queued again as r2 |
| syn-funcdag-easy | stock | 0.0 | 13 | 8 | 0.117 | wrote `/app/output/result.json` with sandbox `open()`; never reached container |
| syn-funcdag-easy | bridge | 1.0 | 7 | 2 | 0.063 | `write_file` + `cat` verification |
| syn-funcdag-easy | compact-bridge | 1.0 | 6 | 2 | 0.060 | same |
| syn-funcdag-easy | orchestrator-bridge | 1.0 | 7 | 2 | 0.057 | same |
| travel-lisbon-002 | bridge | 0.0 | 7 | 2 | 0.051 | correct infeasibility reasoning; omitted required `acquired_sources` field |
| travel-lisbon-002 | compact-bridge | 0.0 | 9 | 5 | 0.081 | same omission |

Three consecutive rate-limit deaths tripped the lab's `quiet_failure_rule`
quarantine; it was cleared by one free `oracle` control (`rlm-reset-oracle-20260916`,
reward 1.0), which is the intended reset path. 11 approved specs (r1 lisbon stock /
orchestrator-bridge, r2 event-summary bridge family, `tools-bridge` and
`orchestrator-tools` on all three tasks) are parked behind `evallab stop` until the
window resets.

## Track 2: synthetic long-context suite (host-only dspy.RLM)

Seed 1, 6 tasks per family, ~120 k characters per context, GLM-5.3-Flash root, no
separate sub-LM (Harbor default), cost ceiling 0.6 USD-eq per task. Every task was
solved by both policies; the measurable effect is efficiency.

| policy | n | acc | ledger | chain | state | drifted turns | salvaged | iters | in tok | out tok | reasoning tok | USD-eq/task | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| stock | 12 | 1.000 | 1.00 | 1.00 | – | 19 | – | 5.5 | 17 910 | 5 922 | 3 678 | 0.051 | 143 |
| stock-lenient | 14 | 1.000 | 1.00 | 1.00 | 1.00 | 0 | 19 | 4.0 | 11 593 | 4 006 | 2 314 | 0.034 | 99 |

Paired on the 12 shared task ids (exact two-sided sign test): accuracy 0 wins / 0
losses / 12 ties; cost ratio 0.69 (7 cheaper, 5 dearer, p = 0.77); iterations ratio
0.74; wall ratio 0.72. All 19 salvaged actions executed valid code; one REPL error in
56 lenient iterations (a regex typo the model fixed next turn); zero in stock.

Drift taxonomy over all 43 drifted turns (bench + Harbor): 14 fully mirrored
`Reasoning:/Code:` history format; 10 `[[ ## reasoning ## ]]` then a `Code:` label;
8 bare preamble + fenced code; 6 pure prose with no code; 3 malformed marker
placement; 2 bare preamble + `[[ ## code ## ]]`. Every drifted turn re-sends the
whole history, which is why the two most expensive stock trajectories
(`ledger-agg-s1-03`: 10 steps, 5 drifted, 0.133; `ledger-agg-s1-04`: 8 steps,
4 drifted, 0.138) cost 4× the median.

An earlier stock run on the pre-fix runtime (dspy JSON fallback on, 2 LM retries,
3 workers under provider 429s) is retained under
`runs/…/bench-s1-c120000/invalid-oldruntime/` and excluded: 7/18 of its rows are
infrastructure errors, not model behaviour.

## Post-reset batch (10:39 Z → session end)

Scheduled by `runs/rlm-overnight-20260916/post-reset.sh` (waits for the window to
read < 25 %, re-checks before every step, stops at 70 %): finish `state-tracking`
for stock and stock-lenient (completes the 18-task pairing), then `stock-markers`,
`orchestrator-lenient`, `stock-markers-lenient` over the full suite, while Track 1
drains the 11 parked specs serially. Results land in
`results/report.md` / `results/track1.json` when this packet is refreshed.

## Limits

- n is small everywhere; the only statistically solid statement is the
  existence and cost of the drift/sandbox defects, not a ranking of policies.
- The suite saturates accuracy for GLM-5.3-Flash and never triggered sub-LM use;
  it measures CodeAct-style REPL competence and harness overhead, not recursion.
- "Cost" is API-list-price equivalent from token counts (Z.ai coding-plan billing
  is a subscription window); the provider's window credits are reported separately
  in `results/quota-readings.jsonl`.
- GEPA over the action instructions (`gepa_rlm.py`) is implemented but was not run:
  a 36-metric-call search costs roughly a third of a window that HAR-54 also needs.
- Single model. Format drift is a GLM-5.3-Flash × dspy-ChatAdapter interaction; a
  different root model may not show it.

## Next decision (for Research-Harbor / Peter)

Adopt `stock-lenient` (or `stock-markers-lenient` if the post-reset batch confirms
it removes drift at the source) as the lab's default RLM policy for GLM-5.3-Flash,
and `bridge` guidance (or `tools-bridge`) for Harbor tasks; then rebuild the
synthetic suite so semantic classification cannot be shortcut (free-text records,
no recognisable entity names) to measure recursion proper before spending a window
on GEPA.

## Reproduce

```bash
cd .worktrees/rlm-harness-20260916
uv run pytest tests/test_rlm_lane.py tests/test_rlm_policies_bench.py -q
PYTHONPATH=src runs/.harbor-dspy/bin/python -m pytest tests/test_rlm_harness.py -q
# bench (needs ZAI_API_KEY in the environment; never write it to disk)
PYTHONPATH=src runs/.harbor-dspy/bin/python -m evallab.rlm.bench_runner --policy stock-lenient \
  --seed 1 --n-per-family 6 --context-chars 120000 --workers 2 --out runs/x
PYTHONPATH=src runs/.harbor-dspy/bin/python -m evallab.rlm.bench_report --dir runs/x --baseline stock
# Harbor: submit specs/rlm-<task>-<policy>-r<k>.json, approve, then
PATH="$PWD/runs/.harbor-dspy/bin:$PATH" uv run evallab tick --max-specs 1
```
