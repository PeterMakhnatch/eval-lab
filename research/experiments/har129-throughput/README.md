# HAR-129 item 5: throughput and cost per run (G2/G5 telemetry only)

Tool + HAR-116 baseline for sizing Modal concurrency and GPU type for
self-hosted MiMo (Terminus-2) evals. Built and validated on HAR-116 now so
it can be re-run unchanged the moment G2/G5 land.

## Method

`study.py` has two subcommands (stdlib only, plus `evallab` rate helpers):

- `collect`: job dirs + `modal app logs` captures + `modal billing
  report --resolution h --json` output → `trials.csv`, `calls.csv`,
  `server_batches.csv`, `concurrency.csv`, `summary.json`.
- `report`: `summary.json` → text report with the batch-throughput fit,
  the cost-optimal concurrency model, and the GPU comparison.

Rates are reused, never re-derived: `MIMO_SELFHOSTED_SERVER_USD_PER_HOUR`
/ `mimo_selfhosted_trial_cost_usd` (`evallab.execution_contracts`) and
`estimate_cost_usd` / `DAYTONA_RATE_CARD` (`evallab.task_qualification`).

Telemetry mapping (verified on HAR-116, one job end to end):

- Trial wall + agent/verifier/setup split: `result.json` timing blocks.
- LLM time per call: `result.json`
  `agent_result.metadata.api_request_times_msec` (per-call durations).
  `lab-metadata.json` `provider_usage.calls[]` has tokens/status but **no
  timestamps**, and ATIF `trajectory.json` steps have no per-call LLM
  timestamps either — so per-call LLM time comes only from
  `api_request_times_msec`, and per-call wall time is not derivable.
- Tokens: provider calls reconcile exactly with `agent_result`
  (checked: 97 calls, 2,438,052 in / 32,707 out on the sample job), so
  calls are attributed to the trial when a job holds exactly one trial.
- Server batch size, gen/prefill throughput, queue depth, KV usage:
  SGLang `Decode batch` / `Prefill batch` lines in `modal app logs`.
  Log timestamps carry no zone; the container runs UTC (tunnel lines in
  the same capture use `+0000`).
- Concurrent trials over time: trial `[started_at, finished_at)` overlap,
  sampled per minute.
- Cost per run: billed Modal $ for the serving app(s) over the session
  window ÷ runs. Daytona per-trial: `estimate_cost_usd` with
  `sandbox_seconds` = trial wall; sandbox CPU/RAM is **not recorded** in
  the trial dir (only the 10 GiB storage override), so anything above the
  storage floor is a stated [INFERENCE] scenario.

## HAR-116 baseline (measured 2026-10-01)

40 jobs (`har116-*`, all `--n-concurrent 1`) × 1 trial = 40 runs in two
waves (00:06Z and 01:50Z) against one server deployment per wave
(`ap-i1jDXiUmYYzX3k11Tpww83` 00:02–00:34Z, `ap-yXxcAQPhR4WRVkUd4thsUc`
01:47–02:22Z). Server: 1× A100-80GB + 4 CPU + 16 GiB, $2.814912/h,
context 64K, `--cuda-graph-max-bs-decode 16`.

- Observed concurrency: peak **20** concurrent trials, time-weighted mean
  3.0 (idle gap between waves).
- Wall/run: mean **623 s**, median 617 s, max 1,944 s. Split: LLM time
  mean 234 s, tool/sandbox time mean 349 s (agent_exec minus
  Σ api_request_times). Full-length runs
  (`TrialBudgetExhaustedError`, n=22): wall mean 920 s, LLM mean 368 s,
  mean out-tokens 21,303. (10 fast `ModuleNotFoundError` failures, wall
  mean 49 s, pull the overall mean down.)
- Decode: 4,185 batches, gen tok/s overall mean 236; batch size 1–18
  observed (bs=1: 78 tok/s mean, n=1555 … bs=8: 537, bs=15: 833;
  bs=16/18 thin: n=5/2). Fit `G(bs)=bs/(a+b·bs)` over graph batches
  (bs≤16): a=0.0122 s, b=0.00046 s → ~79 tok/s per request at bs=1.
  Prefill median 735 tok/s in (n=2387).
- Queue wait: **zero** batches with `#queue-req > 0` at C≤20.
- Memory pressure: KV `full token usage` max **0.42**.
- Cost: billed $3.2398 ($1.5338 wave 1 + $1.7060 wave 2; matches
  $2.814912/h × uptime) → server **$0.0810/run**. Daytona: storage floor
  $0.0001/trial; [INFERENCE] 2vCPU/4GiB ≈ $0.0287/trial at mean wall
  ($0.0425 at full-length 920 s wall), 4vCPU/8GiB ≈ 2× that.

Full tables: `data/har116-baseline/report.txt` (also `summary.json` +
CSVs). Raw `modal app logs` captures (~9.5k lines) were parsed to
`server_batches.csv` and **not** committed; refetch with the commands
below.

## Model: cost-optimal concurrency (A100-80GB)

Trials decode only a fraction of wall time (LLM 234 s of 623 s wall), so
the mean decode batch is bs_mean(C) = 0.17 + 0.39·C from the measured
per-minute running-req regression — NOT C itself. At C=16 trials the mean
batch is ≈6.4; the graph limit (bs 16) binds on average only near C≈40.
A decoding request sees a size-biased batch, so r_req(C) = E[G(B)/B] over
Poisson(bs_mean(C)) and W(C) = tool + out/r_req(C). The Poisson tail
calibrates almost exactly at the observed peak: empirical P(bs>16|conc≥18)
= 0.0032 (617 batches, 7 min) vs Poisson prediction 0.0033.
Eager-regime numbers stay [INFERENCE], and the 51→29 tok/s per-request
drop from bs=16 to bs=18 is suggested by 2 batches (n=5 at bs=16, n=2 at
bs=18), not a measured curve:

| C | bs_mean | P(bs>16) | r_req | W(s) | server $/run | regime |
|---|---------|----------|-------|------|--------------|--------|
| 1 | 0.6 | 0.0000 | 77 | 523 | 0.4086 | graph |
| 4 | 1.7 | 0.0000 | 74 | 530 | 0.1035 | graph |
| 8 | 3.3 | 0.0000 | 71 | 539 | 0.0527 | graph |
| 12 | 4.8 | 0.0000 | 67 | 548 | 0.0357 | graph |
| 16 | 6.4 | 0.0003 | 64 | 558 | 0.0273 | graph |
| 20 | 7.9 | 0.0033 | 61 | 568 | 0.0222 | graph |
| 24 | 9.4 | 0.0169 | 58 | 579 | 0.0188 | graph+eager-mix [INFERENCE] |
| 32 | 12.5 | 0.1331 | 51 | 612 | 0.0149 | graph+eager-mix [INFERENCE] |
| 40 | 15.6 | 0.3974 | 42 | 672 | 0.0131 | graph+eager-mix [INFERENCE] |
| 48 | 18.7 | 0.6861 | 32 | 764 | 0.0124 | eager [INFERENCE] |

Sanity gates (printed by `report`, measured not modelled): r_req(1)=77 vs
measured bs=1 per-request 78 (ratio 0.99, PASS ±10%); W at experienced
concurrency vs wall mean by outcome — TrialBudgetExhaustedError 828 vs
920 s (0.90, PASS), LoopBreakStop 512 vs 566 s (0.91, PASS), clean 448 vs
495 s (0.91, PASS). ModuleNotFoundError FAILs (4 vs 49 s): those are
setup-failure trials (tool≈3 s, ~101 out-tokens), not decode-bound — the
model only predicts decode-bound walls, which is exactly the G2 regime.
Server $/run keeps falling to C≈48 while W rises gently (523 → 764 s);
KV (ceiling C≈80) never binds first. Cost alone says 48, wall time and
eager uncertainty say lower — the reported default is the largest C with
P(bs>16) < 0.01, computed from the table itself.

GPU comparison (Modal list, modal.com/pricing, retrieved 2026-10-01;
server total = GPU + 4 CPU + 16 GiB; throughput scaling [INFERENCE]):

| GPU | GPU $/h | server $/h |
|-----|---------|------------|
| A100-80GB | 2.4984 | 2.8149 |
| A100-40GB | 2.0988 | 2.4153 (−14%) |
| L40S | 1.9512 | 2.2677 (−19%) |
| H100 | 3.9492 | 4.2657 (+52%) |

## G2 wave 1, measured (2026-10-01 08:02–09:02Z)

**Inputs:**
- 20 `har120-*` jobs, all started at 08:01:59Z against app `ap-6MVKRrBnozowbUzD1yL5NE`, from the `.worktrees/har126-live/runs` checkout.
- Server logs fetched without `--timestamps`, for 07:20–09:10Z.
- Hourly billing for 2026-10-01.

The output is in `data/g2-wave1/`, with the full table in `report.txt`. The 22 tick-2 jobs, which failed with 503 on a new app, are excluded.

| measure | value |
|---|---|
| peak / time-weighted mean concurrent trials | 19 / **4.5** |
| makespan | 3,616 s. 10 trials were done by 622 s, 18 by 1,122 s, 19 by 1,927 s; the last was an `AgentTimeoutError` at 3,616 s |
| server queueing | none (queue>0 batch share 0.0000) |
| KV usage, max | 0.37 |
| batch size at ≥ 17 concurrent | P(bs>16) = 0 over 351 batches |
| gen tok/s by batch size | 73 at bs=1, 262 at bs=4, 484 at bs=8, 603 at bs=12, 652 at bs=13. Nearly linear, with no saturation in range |
| Modal billed, this app | $0.76 (07h: Engineering's chain smoke, not G2) + $2.82 (08h) + $0.21 (09h tail) |
| server $/run, G2-attributable (08h + 09h) | **$0.15** |
| model at sustained C=20 | $0.033/run, W≈834 s (re-fit on this mix) |

**Reading:**
- The server was never the bottleneck at C=20: there was no queueing, KV peaked at 37%, and no batch exceeded the CUDA-graph range.
- The measured cost per run is about 5× the sustained-C model because of **utilization, not throughput**. A single wave of 20 at a fixed start leaves the A100 serving 1–2 trials for the last ~28 minutes, waiting on one trial that ran to the 3,600 s agent timeout.
- The bs=1 sanity gate fails here: 73 tok/s measured vs 98 modelled. The likely cause is slower long-context decode at bs=1 in this mix, whose longest prompt was about 56K tokens [INFERENCE]. Walls by outcome still match the model within 8% (ratios 0.92–0.98).

## Recommended defaults

1. **Keep the A100-80GB at C=20; C=24 is safe if a lane needs it.** At C=20 the measured headroom is large (KV 0.37, no queueing). The model gives P(bs>16)=0.006 and $0.028/run at C=24. Values above 24 still rest on the eager-mix [INFERENCE].
2. **The bigger lever is keeping the slots full.**
   - **Rolling dispatch:** keep at least C runs queued and start a new run as each one ends, rather than waves of exactly C.
   - **Scale down as soon as the last run ends:** stop the app on the run's teardown instead of the idle tail.
   - At the 4.5 mean measured here, cost per run is about $0.15. At a sustained 20 it would be about $0.03.
   - For G5, the per-task serial arm order still allows concurrent task groups, so rolling dispatch over the task groups applies.
3. **Budget long-tail trials explicitly.** One run at the agent timeout holds the whole server; at a 60-run scale that is about $1.40 for a single straggler wave. The agent timeout is frozen eval policy and is not changed here.
4. **GPU type:** keep the A100-80GB. L40S and H100 win only if their measured tok/s beats the price delta, and that remains [INFERENCE].

## Rerun on G2/G5

```bash
cd ~/Developer/eval-lab/.worktrees/<worktree>
# 1. Fetch server logs + billing (read-only). No --timestamps: the parser
#    expects SGLang's own "[YYYY-MM-DD HH:MM:SS]" at line start.
env -u VIRTUAL_ENV uv run --project tools/modal-mimo-serve --locked \
  modal app logs <APP_ID> --since <start ISO> --until <end ISO> > /tmp/g2-logs.txt
env -u VIRTUAL_ENV uv run --project tools/modal-mimo-serve --locked \
  modal billing report --start <YYYY-MM-DD> --end <YYYY-MM-DD> \
  --resolution h --json > /tmp/g2-billing.json
# 2. Collect + report (job glob + app id are the only per-session inputs):
env -u VIRTUAL_ENV uv run --no-sync python \
  research/experiments/har129-throughput/study.py collect \
  --job "<runs>/g2-*" \
  --server-log "<APP_ID>=/tmp/g2-logs.txt" \
  --billing /tmp/g2-billing.json --app <APP_ID> \
  --out research/experiments/har129-throughput/data/g2
env -u VIRTUAL_ENV uv run --no-sync python \
  research/experiments/har129-throughput/study.py report \
  --data research/experiments/har129-throughput/data/g2 \
  --out research/experiments/har129-throughput/data/g2/report.txt
```

HAR-116 exact rerun: `--job
"/Users/petermakhnatch/Developer/eval-lab/.worktrees/har116-live/runs/har116-*"`
with `--server-log ap-yXxcAQPhR4WRVkUd4thsUc=/tmp/har129-logs-a.txt
--server-log ap-i1jDXiUmYYzX3k11Tpww83=/tmp/har129-logs-b.txt`,
`--billing data/modal-billing-2026-10-01.json`, `--app` both ids
(reproduces `data/har116-baseline/`).

## Gaps / [INFERENCE] list

- Poisson batch-size distribution around bs_mean(C): [INFERENCE] above
  C=20, calibrated at C=20 (empirical 0.0032 vs predicted 0.0033).
  Eager-regime batch rates rest on 2 batches at bs=18 (flat hold past 18
  is optimistic). G2 (60 trials at parallelism 20) measures bs_mean and
  P(bs>16) at C=20 directly. KV ceiling C≈80 is [INFERENCE] (linear
  running-req(C), same context mix).
- Daytona sandbox shape (vCPU/RAM): not in telemetry; cost scenarios
  above are [INFERENCE] except the storage floor.
- Server log retention is short (both HAR-116 apps only kept their
  recent windows; a redeploy starts a new app id): fetch logs + billing
  promptly after G2/G5, and record the app id(s) with the run.
- Per-call LLM wall time: only per-call durations
  (`api_request_times_msec`) exist; no per-call timestamps anywhere.
- SGLang log timestamps assumed UTC (no zone printed).
