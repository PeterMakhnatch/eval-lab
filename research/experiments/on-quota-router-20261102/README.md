# Quota-Aware Route Selector (`on-quota-router-20261102`)

## Motivation
During the 2026-11-02 overnight shift, two Devin workers burned 6% of the daily allowance and 3% of the weekly allowance within 3 minutes, forcing manual intervention to re-route squads. On prior runs, an Anthropic 7-day rolling ceiling abruptly stalled worker squads mid-execution.

This experiment implements a pure Python standard library quota router that:
1. Parses live `omp usage` text output (including ANSI escape sequences, percent utilization, duration to reset, and dollar balances).
2. Uses empirical measured burn rates per route (percent per worker-hour) derived from tonight's logs to project whether planned work will exceed quota gates.
3. Implements dual hysteresis:
   - **Threshold Hysteresis (Schmitt Trigger)**: Once a route trips its gate, its re-admission threshold is lowered by a configurable hysteresis band (default 5%) to prevent flapping at the boundary.
   - **Affinity Hysteresis (Route Stickiness)**: An active route is retained across invocations unless an alternative candidate offers a decisive headroom advantage (default 10%).
4. Provides a Temporal-compatible activity `route_quota_ok(route, worker_hours) -> (bool, reason)` matching F3's orchestrator interface.
5. Provides a full CLI interface (`python -m quota_router pick ...`).

---

## Empirical Burn Rate Measurements (Tonight's Evidence)

| Route / Pool | Metric | Gate | Measured Burn (%/h) | Sample Size | Observed Delta | Confidence |
|---|---|---|---|---|---|---|
| `devin/*` | `devin_daily` | 80.0% | **60.0% / h** | 0.1h (2 workers, 3 min) | +6.0% in 3 min | LOW (small sample, high burn) |
| `devin/*` | `devin_wk` | 50.0% | **30.0% / h** | 0.1h (2 workers, 3 min) | +3.0% in 3 min | LOW |
| `anthropic/*` | `claude_7d` | 30.0% | **0.96% / h** | 6.28h (16 items/lanes) | +6.0% (1.0% -> 7.0%) | MEDIUM-HIGH |
| `google-antigravity/gemini-*` | `gemini_5h` | 60.0% | **1.09% / h** | 15.63h (29 tasks) | +17.0% (0.0% -> 17.0%) | HIGH |
| `google-antigravity/gemini-*` | `gemini_wk` | 40.0% | **0.59% / h** | 15.63h (29 tasks) | +9.2% (0.0% -> 9.2%) | HIGH |
| `google-antigravity/claude-*` | `ag_shared_wk` | 85.0% | 8.00% / h | 0.0h (unmeasured tonight) | baseline at 88.1% (above gate) | LOW |
| `openai-codex/*` | `codex_7d` | 80.0% | 5.00% / h | 0.0h (unmeasured tonight) | baseline at 87.0% (above gate) | LOW |

---

## Usage

### CLI
```bash
# Pick best route for a 1.5-hour task
python -m quota_router pick anthropic/claude-sonnet-5-5 devin/claude-sonnet-5-5 google-antigravity/gemini-3.8-flash --hours 1.5

# Check quota for a single route
python -m quota_router check devin/claude-sonnet-5-5 --hours 2.0

# Parse live or cached usage output to JSON
python -m quota_router parse --json

# Scrub usage output to create sanitized fixtures
python -m quota_router scrub -o fixtures/scrubbed.txt
```

### Python API
```python
from quota_router import pick, route_quota_ok

# Select best candidate
result = pick(
    candidates=["devin/claude-sonnet-5-5", "google-antigravity/gemini-3.8-flash"],
    need_worker_hours=2.0,
)
if result.refused:
    print(f"Refusal reason: {result.reason}")
else:
    print(f"Selected route: {result.route}")

# Activity check
ok, reason = route_quota_ok("devin/claude-sonnet-5-5", worker_hours=2.0)
```
