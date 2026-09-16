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
   Stock drifted in 10 of 18 bench runs (24 turns) and in 3 of 4 Harbor trials
   (17 turns).
3. The drift is caused by the history rendering, and two harness changes remove
   it without touching the model or the task. Rendering the REPL history with the
   same field markers the adapter expects (`stock-markers`) cuts drift from 24 to
   4 turns; adding **salvage** of the fenced code plus its preamble on the residual
   drift (`*-lenient`) brings it to 0. Paired on the 18 identical tasks, every arm
   keeps 18/18 accuracy; cost ratio vs stock: `stock-lenient` 0.58 (12 cheaper /
   6 dearer, sign-test p = 0.24), `stock-markers-lenient` 0.62 (12/6),
   `stock-markers` 0.64 (10/8), `orchestrator-lenient` 0.73 (9/9); iterations
   5.4 → 3.8–4.2. Cheaper because a drifted turn re-sends the whole history.
4. On Harbor the stock harness has a second, structural defect: the REPL is a
   host-side Pyodide sandbox, so `open("/app/output/result.json", "w")` succeeds in
   the sandbox and never reaches the task container. `syn-funcdag-easy` stock:
   reward 0.0 (13 steps, 8 drifted turns, file written to the sandbox); all seven
   bridge/tools arms (`bridge`, `compact-bridge`, `orchestrator-bridge`,
   `tools-bridge`, `orchestrator-tools`, `tools-lenient`): reward 1.0.
   `travel-lisbon-002` scored 0.0 under all six policies for the same reason
   (answer omits `acquired_sources`, which the task never names) — a task-schema
   failure, not a harness one. `event-summary` is 1.0 under every policy.
5. At 120 k characters the three original synthetic families do not exercise
   recursion: GLM solved them as a CodeAct harness (0–7 `llm_query` steps in
   100 runs, classifying merchants from world knowledge, regex for chains, code
   replay for state). A fourth family, `memo-classify` (slot-filled free-text
   memos, no entity or category words), does force it: 3.5–5.2 sub-LM calls per
   task, 4/4 correct under both `stock-lenient` and `orchestrator-lenient`,
   0.104 vs 0.119 USD-eq per task (orchestrator dearer, 1 cheaper / 3 dearer).
6. The Z.ai Pro 5-hour window (12 000 credits) is the binding shared constraint:
   14 % at 06:19 Z, 100 % by ~07:15 Z with this lane stopped since 06:50 Z (HAR-54
   GEPA runs burned ~160 credits/min). After the 10:39 Z reset the whole
   post-reset batch here (16 Harbor trials + 108 bench tasks) used 7 % of the
   window.
7. Confound: the host Mac slept repeatedly between 10:44 Z and ~13:00 Z and
   briefly at 13:46–13:49 Z and 14:12–14:14 Z (`pmset -g log`), so **wall-clock
   numbers from the post-reset batch are unreliable** (memo-classify
   `stock-lenient`, `stock-markers-lenient`, state-tracking `stock-lenient`);
   tokens, cost, iterations and rewards are unaffected. The runner's 900 s trial
   watchdog counts monotonic time, which stops during sleep, so one trial spanned
   110 minutes before it was killed, and killed trials had no trajectory on disk
   (fixed on the branch: the agent now writes `trajectory.partial.json` after every
   step).

## What was built (all on the branch)

| Piece | Path | Notes |
|---|---|---|
| Harbor agent | `src/evallab/harbor_rlm.py` | `LabRlmAgent` (host-side dspy.RLM + Harbor `EnvironmentToolBridge`), optional `run_python` tool executing inside the container (`ContainerPythonBridge`), writes `agent/rlm/{policy,trajectory,usage}.json` and `trajectory.partial.json` after every step |
| Runner lane | `execution_contracts.py`, `runner.py`, `queue.py`, `profiles.py`, `credentials.py`, `schemas/__init__.py` | agent id `rlm`, profile `rlm-glm-5.3-flash`, `harness_policy` on `RunRequest`/`ExperimentSpec`, `--agent-kwarg policy=…,cost_limit_usd=…`, host 0400 secret-file transport (`EVALLAB_ZAI_SECRET_FILE`), no proxy URLs; additive only |
| Policies | `src/evallab/rlm/policies.py` | content-addressed `RlmPolicy`; `stock` reproduces Harbor defaults; `orchestrator` (decomposition nudge adapted from alexzhang13/rlm `ORCHESTRATOR_ADDENDUM`, MIT), `*-mask4`/`compact` (last-N observation masking of REPL outputs, HAR-50 semantics), `*-remind`, `*-verify`, `*-lenient` (salvage of drifted actions), `stock-markers`/`stock-markers-lenient` (history rendered with dspy field markers), `bridge`/`orchestrator-bridge`/`compact-bridge`/`bridge-lenient` (sandbox↔container guidance), `tools-bridge`/`orchestrator-tools`/`tools-lenient` (`run_python`) |
| Harness | `src/evallab/rlm/harness.py` | `LabRlm(dspy.RLM)`: policy addenda, history masking / marker rendering, iteration reminders, API-equivalent cost ceiling, no JSONAdapter fallback, in-loop parse recovery, salvage; `build_lms`, `lm_usage`, `run_rlm` |
| Synthetic suite | `src/evallab/rlm/bench/` | deterministic `ledger-agg`, `chain-lookup`, `state-tracking`, `memo-classify` families with structured ground truth and format-tolerant scorer |
| Runners / analysis | `src/evallab/rlm/bench_runner.py`, `bench_report.py`, `traj_report.py`, `gepa_rlm.py` | per-task JSONL + trajectories, paired report with exact sign tests on accuracy/cost/iterations, mechanical trajectory analysis (drift taxonomy, tool usage, cost anatomy), GEPA driver over the action instructions (written, not run: quota) |
| Tests | `tests/test_rlm_lane.py`, `tests/test_rlm_policies_bench.py`, `tests/test_rlm_harness.py`, `tests/test_rlm_traj_report.py` | 14 + 15 + 8 + 6 focused behavioural tests (`test_rlm_harness.py` skips without dspy) |

Runtime: isolated `runs/.harbor-dspy` venv (untracked, under the scanner-ignored runs/) (harbor 0.21.0 + dspy 3.3.1, deno 2.9.6) put on
`PATH` only for this lane's executor process; the shared `~/.local/bin/harbor` tool is
untouched. Model: `zai-coding-plan/glm-5.3-flash` via the Z.ai coding endpoint, key
read host-side from the runner's 0400 file, never in the container or logs.

## Track 1: registered Harbor tasks through the normal executor

Specs in [specs/](specs/); each trial ran serially via `evallab submit` →
`evallab approve --actor harness (…HAR-55)` → `evallab tick --max-specs 1`.
Rewards are the task verifiers'; `pf` = drifted turns recovered in-loop, `sv` =
salvaged actions (lenient policies only). Full rows incl. token counts and policy
digests: [results/track1.json](results/track1.json).

| Task | Policy | Reward | Steps | pf | sv | USD-eq | Decisive event |
|---|---|---|---|---|---|---|---|
| event-summary | stock (smoke) | 1.0 | 4 | 0 | – | 0.032 | `open()` in sandbox failed on step 2, recovered with `read_file` |
| event-summary | stock | 1.0 | 7 | 3 | – | 0.056 | recovered from 3 drifted turns |
| event-summary | bridge | 1.0 | 8 | 4 | 0 | 0.055 | |
| event-summary | compact-bridge | 1.0 | 4 | 1 | 0 | 0.033 | |
| event-summary | orchestrator-bridge | 1.0 | 5 | 2 | 0 | 0.045 | |
| event-summary | tools-bridge | 1.0 | 7 | 3 | 0 | 0.050 | |
| event-summary | orchestrator-tools | 1.0 | 3 | 0 | 0 | 0.024 | cheapest: `run_python` did the whole job in one container call |
| syn-funcdag-easy | stock | 0.0 | 13 | 8 | 0 | 0.116 | wrote `/app/output/result.json` with sandbox `open()`; never reached container |
| syn-funcdag-easy | bridge | 1.0 | 7 | 2 | – | 0.063 | `write_file` + `cat` verification |
| syn-funcdag-easy | compact-bridge | 1.0 | 6 | 2 | – | 0.060 | same |
| syn-funcdag-easy | orchestrator-bridge | 1.0 | 7 | 2 | 0 | 0.056 | same |
| syn-funcdag-easy | tools-bridge | 1.0 | 8 | 3 | 0 | 0.070 | `run_python` in container |
| syn-funcdag-easy | orchestrator-tools | 1.0 | 5 | 2 | 0 | 0.048 | same |
| syn-funcdag-easy | tools-lenient | 1.0 | 5 | 0 | 5 | 0.049 | every drifted turn salvaged; 5 steps vs 13 for stock |
| travel-lisbon-002 | stock | 0.0 | 14 | 6 | 0 | 0.150 | verifier: `missing required sources` (answer lacks `acquired_sources`) |
| travel-lisbon-002 | bridge | 0.0 | 7 | 2 | 0 | 0.051 | same |
| travel-lisbon-002 | compact-bridge | 0.0 | 9 | 5 | 0 | 0.081 | same |
| travel-lisbon-002 | orchestrator-bridge | 0.0 | 6 | 2 | 0 | 0.051 | same |
| travel-lisbon-002 | orchestrator-tools | 0.0 | 13 | 7 | 0 | 0.149 | same |
| travel-lisbon-002 | tools-lenient | 0.0 | 7 | 0 | 3 | 0.077 | same |

Not in the table: the three r1 event-summary bridge-family trials that died on the
pre-fix runtime (provider 429s + dspy JSON fallback `{}`; 0 steps, reran as r2
above), and three `trial_wall_clock_timeout` kills (`syn-funcdag-easy`
bridge-lenient, `travel-lisbon-002` bridge-lenient and tools-bridge). The first two
of those ran across the host-sleep period (TL;DR 7) and left no trajectory; the
third is a genuine 900 s cap on lisbon. Three consecutive rate-limit deaths tripped
the lab's `quiet_failure_rule` quarantine; it was cleared by one free `oracle`
control (`rlm-reset-oracle-20260916`, reward 1.0), the intended reset path.

Reading: every policy that tells the model about the sandbox/container split solves
funcdag; the `run_python` tool does not add reward over `write_file` guidance but
produced the cheapest event-summary trial. Lisbon is lost identically by all six
policies: each trajectory read `task.json`, reasoned the budget is infeasible, and
wrote a refusal without the `acquired_sources` list the verifier requires and the
instruction never names. A policy addendum that fixes that would be task-specific,
so it was not tried.

## Track 2: synthetic long-context suite (host-only dspy.RLM)

Seed 1, 6 tasks per family, ~120 k characters per context, GLM-5.3-Flash root, no
separate sub-LM (Harbor default), cost ceiling 0.6–0.8 USD-eq per task, 3 workers.
Every task was solved by every policy; the measurable effect is efficiency and
drift. Rows: [results/](results/) `*-s1-r*.jsonl`; report:
[results/bench-s1-c120000.report.md](results/bench-s1-c120000.report.md).

| policy | n | acc | drifted turns (runs) | salvaged | iters | in tok | out tok | reasoning tok | USD-eq/task |
|---|---|---|---|---|---|---|---|---|---|
| stock | 18 | 1.000 | 24 (10) | – | 5.4 | 18 730 | 7 719 | 5 133 | 0.060 |
| stock-markers | 18 | 1.000 | 4 (4) | – | 4.2 | 11 944 | 4 888 | 3 016 | 0.038 |
| stock-markers-lenient | 18 | 1.000 | 0 | 1 | 3.8 | 10 728 | 5 097 | 3 177 | 0.037 |
| stock-lenient | 18 (+2 repeats) | 1.000 | 0 | 32 | 3.9 | 11 392 | 4 344 | 2 583 | 0.035 |
| orchestrator-lenient | 18 | 1.000 | 1 (1) | 13 | 3.8 | 12 717 | 5 977 | 4 075 | 0.044 |

Paired on the 18 shared task ids against stock (exact two-sided sign test on
per-task cost; accuracy ties everywhere):

| candidate | cost ratio | cheaper / dearer | p | iterations ratio |
|---|---|---|---|---|
| stock-lenient | 0.58 | 12 / 6 | 0.24 | 0.72 |
| stock-markers-lenient | 0.62 | 12 / 6 | 0.24 | 0.71 |
| stock-markers | 0.64 | 10 / 8 | 0.82 | 0.77 |
| orchestrator-lenient | 0.73 | 9 / 9 | 1.00 | 0.70 |

Wall-clock ratios are omitted: the post-reset arms ran across host sleep (TL;DR 7).
The pre-reset 12-task pairing (stock vs stock-lenient, both awake) gave wall ratio
0.72.

The cleanest result is the drift ablation. `stock-markers` changes one thing, the
history is rendered to the model with the same `[[ ## reasoning ## ]] / [[ ## code ## ]]`
markers the adapter parses instead of `Reasoning:/Code:` labels, and drift falls
from 24 turns to 4 with no salvage at all. That pins the cause on the rendering:
the model copies the format it is shown. Salvage handles the residual (1 action in
18 markers-lenient runs). The remaining cost gap between `stock-markers` and
`stock-lenient` is within the per-task noise (10/8 vs 12/6).

Drift taxonomy (mechanical, `traj_report.py`, over the 24 stock bench turns): 8
`[[ ## reasoning ## ]]` then a `Code:` label, 8 prose with no fence within the
recorded head, 4 fully mirrored `Reasoning:/Code:` history format, 4 preamble +
fence. Harbor stock (17 turns): 9 preamble + fence, 4 `[[ ## code ## ]]` without
the reasoning marker, 3 prose, 1 reasoning marker + `Code:`. Every drifted turn
re-sends the whole history, which is why the two most expensive stock trajectories
(`ledger-agg-s1-03`: 10 steps, 5 drifted, 0.133; `ledger-agg-s1-04`: 8 steps,
4 drifted, 0.138) cost 4× the median. Full table incl. tool usage per policy:
[results/traj-report.md](results/traj-report.md).

An earlier stock run on the pre-fix runtime (dspy JSON fallback on, 2 LM retries,
3 workers under provider 429s) is retained under
`runs/…/bench-s1-c120000/invalid-oldruntime/` and excluded: 7/18 of its rows are
infrastructure errors, not model behaviour.

### memo-classify: forcing recursion

60 k characters, 4 tasks, seed 1, cost ceiling 0.8. Memos are slot-filled free text
with no entity or category words, so the only route to the labels is to read them
(the model cannot classify by merchant name as it did on `ledger-agg`). Report:
[results/bench-s1-c60000-memo.report.md](results/bench-s1-c60000-memo.report.md).

| policy | n | acc | iters | sub-LM calls/task | in tok | out tok | USD-eq/task |
|---|---|---|---|---|---|---|---|
| stock-lenient | 4 | 1.000 | 4.8 | 5.2 | 25 976 | 15 264 | 0.104 |
| orchestrator-lenient | 4 | 1.000 | 5.0 | 3.5 | 24 308 | 19 291 | 0.119 |

Both arms used `llm_query` / `llm_query_batched` on every task (per-task sub-calls
1–10) and both were 4/4; the orchestrator nudge is 15 % dearer (1 cheaper / 3
dearer, p = 0.63) because its reasoning tokens go up, not because it recurses more.
Wall times for `stock-lenient` here (251–2644 s) are host-sleep artefacts.

## Post-reset batch (10:39 Z → 15:07 Z)

`runs/rlm-overnight-20260916/post-reset.sh` waited for the window to read < 25 %
(observed 0 % at 10:43 Z), re-checked before every step under a 70 % cap, and ran:
Track 1 (submit + approve 4 lenient specs, drain 12 approved serially) and Track 2
(memo-classify both arms, state-tracking completion, then `stock-markers-lenient`,
`orchestrator-lenient`, `stock-markers` over the three original families). Window
at the end: 7 % (922 credits). Readings every step:
[results/quota-readings.jsonl](results/quota-readings.jsonl).

## Limits

- n is small everywhere (18 paired synthetic tasks, 1 trial per Harbor task ×
  policy); the solid statements are the existence, cause and cost of the drift and
  sandbox defects, not a ranking among the lenient/markers arms.
- The three original families saturate accuracy for GLM-5.3-Flash and barely
  trigger sub-LM use; they measure CodeAct-style REPL competence and harness
  overhead. `memo-classify` does force recursion but has n = 4 per arm.
- Wall-clock numbers from the post-reset batch are contaminated by host sleep;
  tokens, cost, iterations and rewards are not.
- "Cost" is API-list-price equivalent from token counts (Z.ai coding-plan billing
  is a subscription window); the provider's window credits are reported separately
  in `results/quota-readings.jsonl`.
- GEPA over the action instructions (`gepa_rlm.py`) is implemented but was not run:
  a 36-metric-call search costs roughly a third of a window that HAR-54 also needs,
  and the drift result says the first thing to optimise is the rendering, not the
  instructions.
- Single model. Format drift is a GLM-5.3-Flash × dspy-ChatAdapter interaction; a
  different root model may not show it. The `stock-markers` ablation would be the
  first thing to repeat on a second model.
- The runner's trial watchdog counts monotonic time, which stops while the host
  sleeps; a wall-clock cap is only a cap while the machine is awake.

## Next decision (for Research-Harbor / Peter)

Adopt `stock-markers-lenient` as the lab's default RLM policy for GLM-5.3-Flash
(rendering fix removes the cause, salvage covers the residual; 0 drift in 18 runs,
cost ratio 0.62 at equal accuracy) with `bridge` guidance for Harbor tasks
(`tools-bridge` when a task needs in-container Python). Report the
`max_iterations`/`max_iters` break upstream to Harbor. Next measurement: repeat
the `stock` vs `stock-markers` ablation on a second root model and grow
`memo-classify` to 6/family before spending a window on GEPA.

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
