# HAR-126: Run Telemetry (Sampler & Extractor)

Provides per-round live telemetry sampling and post-hoc extraction for overnight model evaluation rounds (G2, G5, etc.), supporting Infra's throughput and concurrency analysis (HAR-129).

## 1. Available Metrics & Observability

### SGLang Prometheus `/metrics`
- **Endpoint**: `https://p-makhnatch--evallab-mimo-v26-9b-mimoserver.us-east.modal.direct/metrics`
- **Gauges captured by sampler**:
  - `sglang:num_running_reqs`: Currently active requests inside the engine.
  - `sglang:num_queue_reqs`: Requests waiting in engine scheduler queue (live queue wait).
  - `sglang:gen_throughput`: Output token generation rate (tokens/second).
  - `sglang:token_usage`: KV cache memory utilization (0.0 to 1.0).
  - `sglang:cache_hit_rate`: Prefix cache hit rate.
  - `sglang:num_used_tokens`: Total used tokens in KV cache.
- **Current status on deployed server**:
  - Probing the deployed server currently returns `HTTP 503` (when scaled down) or `HTTP 404` for `/metrics` because SGLang was launched without `--enable-metrics` in `tools/modal-mimo-serve/serve.py`.
  - **Recommendation**: Adding `"--enable-metrics",` to `sglang.launch_server` arguments in `serve.py` enables this Prometheus surface with negligible CPU overhead (<0.5%). Redeployment is token-free and takes ~2 minutes if the volume is mounted. *Note: Per policy, we do not redeploy; parent decides when to apply.*

### Modal Container & GPU Statistics
- **Container Counts**: Monitored via `modal container list --json` (or `--app-id evallab-mimo-v26-9b`). Modal exposes active container IDs, state, and creation time.
- **GPU Stats**: Modal has no public Prometheus push gateway or native GPU metric stream in CLI/SDK without web dashboard scraping. However, when a container is running, `modal container exec --no-pty <container_id> nvidia-smi --query-gpu=...` returns instantaneous GPU utilization %, memory utilization %, and VRAM usage. The sampler automatically probes this when containers are active.

### Lab Trial & Queue State
- **Runs directory**: Counts active running trials (directories with `lab-metadata.json` lacking `finished_at`) vs completed trials.
- **Queue directory**: Counts specs in `queue/running`, `queue/approved`, `queue/waiting`, and `queue/pending`.

### Post-Hoc Job Directory Extractor
- Reads local Harbor job directories without network.
- **Per-call latency**: Derived from consecutive trajectory step timestamps ending at agent (LLM) steps.
  - *Note*: Captures inference latency + harness execution + tool playback overhead (~1s sleep between actions in Terminus). It serves as a reliable upper bound on end-to-end round-trip latency.
- **Concurrency over time**:
  - Per-call concurrency computed at the midpoint of each LLM call across all trials in the round.
  - Peak call concurrency and time-weighted mean call concurrency.
  - Peak trial concurrency across job execution windows (`started_at` to `finished_at`).
  - Per-concurrency latency stratification (p50/p90 latency broken down by concurrency level).
- **Trial startup latency**: Delay from `started_at` to completion of first LLM step.
- **Queue wait**: **Not derivable post-hoc**. Neither `lab-metadata.json` nor proxy ledger `provider_usage.calls` records server-side queuing timestamps. Live queue wait must be captured via the sampler's `sglang:num_queue_reqs`.
- **Reconciliation**: Verifies count of trajectory agent steps against proxy ledger `provider_usage.calls`.

---

## 2. Live Sampler: Start & Stop

The sampler polls every 10–15 s and writes append-only JSONL.

### Starting Before a Round

Start in background before launching trials:
```bash
# Output destination in today's results home or next to runs
ROUND_RESULTS_DIR="$HOME/Developer/eval-lab-results/$(date +%F)"
mkdir -p "$ROUND_RESULTS_DIR"
TELEMETRY_OUT="$ROUND_RESULTS_DIR/g2-telemetry.jsonl"

# Using evallab CLI:
uv run --no-sync evallab telemetry sample \
  --out "$TELEMETRY_OUT" \
  --runs-dir runs \
  --interval 15.0 &
SAMPLER_PID=$!
echo "Sampler running (PID $SAMPLER_PID) -> $TELEMETRY_OUT"
```

Or using the standalone script:
```bash
python3 scripts/telemetry_sampler.py \
  --out "$TELEMETRY_OUT" \
  --runs-dir runs \
  --interval 15.0 &
SAMPLER_PID=$!
```

### Stopping After a Round
```bash
kill -TERM "$SAMPLER_PID"
wait "$SAMPLER_PID" 2>/dev/null
echo "Sampler stopped. Telemetry recorded at $TELEMETRY_OUT"
```

### Sampler Output Format (`*.jsonl`)
Each line contains:
```json
{
  "ts": "2026-10-01T05:30:15.123456+00:00",
  "interval_s": 15.0,
  "sglang": {
    "ok": true,
    "num_running_reqs": 12.0,
    "num_queue_reqs": 4.0,
    "gen_throughput_tok_s": 94.2,
    "token_usage": 0.35,
    "cache_hit_rate": 0.012,
    "num_used_tokens": 142050.0
  },
  "modal": {
    "ok": true,
    "container_count": 1,
    "containers": ["ta-01M3TV58TZK7CN22DQGQJM4MZR"]
  },
  "gpu": {
    "ok": true,
    "gpu_util_pct": 82.0,
    "mem_util_pct": 51.0,
    "mem_used_mib": 41200.0,
    "mem_total_mib": 81920.0
  },
  "lab": {
    "runs_running": 16,
    "runs_finished": 4,
    "queue": {
      "running": 16,
      "approved": 40,
      "waiting": 0,
      "pending": 0
    }
  }
}
```

---

## 3. Post-Hoc Extractor

Run the extractor on any completed job directories or glob pattern:

```bash
uv run --no-sync evallab telemetry extract \
  .worktrees/har116-live/runs/har116-a-*-loopfix-r2 \
  .worktrees/har116-live/runs/har116-b-* \
  --out ~/Developer/eval-lab-results/2026-10-01/har116-r2-summary.json
```

---

## 4. HAR-116 Round 2 Baseline Results

Extracted from `.worktrees/har116-live/runs/har116-a-*-loopfix-r2` and `har116-b-*` (20 trials, 01:50–02:23Z):

- **Trials**: 20 (all 20 have valid trajectories)
- **Total LLM calls**: 1,530
- **Execution window**: 2026-10-01 01:49:57Z to 02:22:30Z (~32.5 minutes)
- **Overall Per-Call Latency**:
  - `p50`: **5.29s**
  - `p90`: **21.96s**
  - `mean`: **10.51s**
  - `min`: 1.54s
  - `max`: 104.75s
- **Peak Concurrency**:
  - Peak active call concurrency: **20**
  - Peak active trial concurrency: **20**
  - Time-weighted mean call concurrency: **8.49**
- **Trial Startup Duration** (dispatch to first LLM response):
  - `p50`: **30.78s**
  - `p90`: **67.21s**
- **Latency by Concurrency Level**:
  | Concurrency | Calls ($n$) | p50 Latency (s) | p90 Latency (s) |
  |-------------|-------------|-----------------|-----------------|
  | 1           | 5           | 11.20           | 64.38           |
  | 2           | 79          | 7.81            | 14.35           |
  | 3           | 27          | 7.59            | 63.69           |
  | 4           | 37          | 2.82            | 3.17            |
  | 5           | 1           | 2.88            | 2.88            |
  | 6           | 28          | 5.00            | 63.18           |
  | 7           | 17          | 5.33            | 31.67           |
  | 8           | 53          | 3.76            | 7.05            |
  | 9           | 65          | 5.08            | 7.66            |
  | 10          | 107         | 5.76            | 63.19           |
  | 11          | 22          | 3.84            | 5.69            |
  | 12          | 16          | 4.87            | 63.32           |
  | 13          | 88          | 6.13            | 23.03           |
  | 14          | 98          | 5.86            | 9.42            |
  | 15          | 68          | 4.43            | 8.86            |
  | 16          | 50          | 4.68            | 8.77            |
  | 17          | 135         | 5.41            | 9.35            |
  | 18          | 36          | 4.88            | 13.90           |
  | 19          | 56          | 5.47            | 12.33           |
  | 20          | 542         | 5.20            | 31.47           |
