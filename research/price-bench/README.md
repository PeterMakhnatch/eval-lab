---
status: living
audience: [builder, analyst, operator]
---

# PRICE BENCH — HAR-201

A reproducible, $0 comparison of eval windows and training sequence-token costs.
No provider client, resource creation or model inference is used. Prices dated
2026-10-08 are sourced observations, not historical invoice tariffs.

```sh
env -u VIRTUAL_ENV uv run --no-sync evallab price bench
env -u VIRTUAL_ENV uv run --no-sync evallab price bench --json
# --catalog, --profile and --scenarios accept alternate local input paths.
```

## Inputs and calibration

- `prices.yaml`: dated list prices, billing granularity, sources and GPU/host specs.
- `profile-mimo9b.json`: mined cohorts, billed totals, statistics and source hashes.
- `scenarios.yaml`: comparable N=200 windows and separate N=20/200/1000 sweeps.
- `policy/daytona-limits.yaml`: quota safety clamp; one extra sandbox reserve.

Expected window seconds are **cold + (N/C) × mean wall + max(0, p95 − mean) + idle**.
GPU billing includes the full window and Modal's 4 CPU / 16 GiB overhead. Cold
start is unknown, so ~$0.23 HAR-129 cold-start cost implies ~294s; this is an
explicit inferred free parameter, not a fit to HAR-116/G2. HAR-116 calibrates
as two independent 20-trial windows. HAR-168's ~$0.654/~$0.320 are mixed-window
measured-time upper bounds, not settled server invoices or a C20 forecast.
Sandbox per-trial costs use mean wall. Missing declarations use verified
2 CPU / 8 GiB task defaults; unknown disk uses 10 GiB with Daytona's first 5 GiB
free tier. Alternate sandbox backends (E2B, Modal sandbox) aggregate provider
component CPU and memory tariffs.
## Reading the output

Six parts: calibration, eval ranking, excluded/flagged rows, training/GRPO,
ranking flips and proposed capped probes. Eval prices are **projected** even
when timing/rates are measured. p50/p90 are displayed; mean is used for spend.
The workload denominator includes failed historical trials, not accepted passes.

Only LLM time scales with HBM bandwidth. KV-pool scaling gates concurrency;
active-stream saturation and Daytona quota mismatches remain explicit flags.
Hosts reserve 2 CPU / 2 GiB, bill `ceil(C / slots)` whole host windows plus
600s prep, and honor hourly rounding. Pack fractions are reservation hypotheses,
not permission to reduce task limits. IPv4, taxes and unpriced transfers/storage
can add costs; resource availability and backend qualification are unproved.

Own training uses measured sequence tok/s or projected dense-BF16 FLOPS ratios,
with GPU-only rates; GPUs below measured peak RAM and unqualified multi-GPU
nodes cannot win the trainer selection. Tinker is labelled **different base
model (Qwen3.5-9B)** and excludes sampling/prefill. Reference GRPO is 128 rollouts:
prefix-consistent tokens are `2 × median_input / median_requests`; per-call is
`median_input`. LoRA/SFT throughput is not measured GRPO throughput.

Flip thresholds hold other inputs fixed. Probe caps use a full wave and
`max(mean,p90)` wall with cold/drain/idle and host prep/rounding. They are screens,
not statistical qualifications. **No probe ran; separate paid approval is required.**
