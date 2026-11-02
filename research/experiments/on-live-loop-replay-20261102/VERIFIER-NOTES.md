# Verifier Notes: B2 Live Loop Replay Proof

**Verifier report:** `/Users/petermakhnatch/Developer/research-context/bridge/2026-11-02-overnight/ver/raw/VF-B-LOOP/report.md`  
**Verifier report SHA256[:12]:** `af6aba70ddae`  
**Status:** Crash recovery and guard mechanisms CONFIRMED; throughput, cycle latency, and queue priority claims CORRECTED

## Key Corrections and Guidance

| Metric / Claim | Author Value | Verified Value (Receipts) | Status | What Reader Should Use Instead |
|---|---|---|---|---|
| Crash Recovery & Idempotency | 5/5 crash trials clean | CONFIRMED: re-run across 20 Terminal Wrench runs with 5 random `kill -9` trials showed 0 duplicate serves, 0 lost requests, identical queue entries. | CONFIRMED | Crash recovery is robust and idempotent. |
| Replay Throughput (off) | 121.3 runs/min | 125.59 runs/min (`measure-off.json`) | CORRECTED | Use 125.59 runs/min. |
| Events processed (cached) | 89,522 events | 92,568 events (`events_total`) | CORRECTED | Use 92,568 events. |
| Cycle Latency Slowdown | +8.6% slowdown (0.058s vs 0.063s) | Baseline off: 0.0575s; cached active replay: 0.0718s (**+24.9% slowdown**) | CORRECTED | Cycle latency slowdown during active replay is +24.9%. |
| Queue Priority Ordering | "Priority-banded: high priority surfaces first" | `Judge Station` orders packets by **shuffled deterministic seed**, NOT by priority band. Priority bands are recorded in metadata but do not alter presentation order. | CORRECTED | Packets are presented uniformly shuffled, not priority-ordered. |
| Queue priority breakdown | 86 High, 75 Medium, 85 Normal (cached smoke: 7/2/1) | Queue file has 246 unique runs: Priority 2 (High): 79, Priority 1 (Med): 48, Priority 0 (Normal): 119. Cached smoke was 6/3/1. | CORRECTED | Use verified distribution: 79 High, 48 Medium, 119 Normal. |
| Cost per 1,000 runs | $2.27 (metered) | Assumes **L0-only** triggers (403.5 cards x $0.005615); with full stage-2 cascade or unthrottled reviews, see `on-review-load-20261102`. | QUALIFIED | Valid for L0 triggers only; see review-load simulations for cascade costs. |
