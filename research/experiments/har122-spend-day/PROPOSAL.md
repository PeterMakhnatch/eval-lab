# Proposal: Pre-Launch Day-Cap Enforcement Gate

## 1. Context & Motivation

Card HAR-122 requires establishing durable spend visibility and budgeting controls for Eval Lab.
Currently, standing policy defines `daily_cost_ceiling_usd: 20.0` in `policy/standing-approvals.yaml`, and `evallab.queue.PolicyEngine` checks spec estimates against this limit. However, the check historically read only recorded spec estimates from the queue without integrating real-world infrastructure usage (Modal container hours and Daytona sandboxes) or actual settled proxy ledgers.

This proposal specifies a pre-launch day-cap check that executes before any billable job launches.

---

## 2. Where It Hooks

The pre-launch check hooks at two distinct enforcement boundaries:

1. **Queue Admission Boundary (`evallab submit` / `evallab approve`)**:
   In `evallab.queue.PolicyEngine.evaluate()`, before admitting a spec into `queue/pending` or transitioning it to `queue/approved`.
2. **Execution Dispatch Boundary (`evallab tick` / `evallab run`)**:
   Immediately before spawning the Harbor subprocess or launching a container/sandbox in `evallab.runner.dispatch_attempt()` and `evallab.preflight.build_preflight_report()`.

Checking at dispatch is critical: queue admission can occur hours or days before execution. A spec admitted in the morning when $2 remained under the cap could exceed the cap if dispatched at night after intervening jobs spent money.

---

## 3. What It Reads

The check evaluates total committed spend for the current UTC calendar day ($D = \text{date.today(UTC)}$):

$$\text{Committed}(D) = \text{Settled}(D) + \text{InFlight}(D) + \text{Candidate}.\text{est\_cost\_usd}$$

### A. Settled Spend ($\text{Settled}(D)$)
Reads from the spend-day accounting surface (`evallab.spend_day.build_day_ledger`):
- **Modal Billed Usage**: Query `modal_billing_rows` for today's reported cost. If the latest report is stale (>4 hours), attempt a read-only fetch via the reconcile path if credentials are available; otherwise use the catalog records.
- **Daytona Estimated Usage**: Query catalog `trials` and `jobs` for all trials running or completed during day $D$, pricing wall seconds using `DAYTONA_RATE_CARD`.
- **Model API Settled Dollars**: Sum settled `cost_usd` from `jobs.lab_metadata.provider_usage` (excluding zero-priced self-hosted routes) plus any completed exploration `spend.jsonl` rows timestamped today.

### B. In-Flight Spend ($\text{InFlight}(D)$)
Reads the live queue directory (`queue/`):
- All specs currently in `running` or `approved` state scheduled for today.
- For each in-flight spec $i$, committed cost is:
  $$\text{cost}(i) = \max(i.\text{cost\_limit\_usd},\ i.\text{est\_cost\_usd})$$
  This reserves the authorized upper ceiling while the job executes, preventing concurrent jobs from oversubscribing the daily headroom.

---

## 4. How It Treats Estimates & Reservations

1. **Upper-Bound Accounting**:
   - For in-flight proxy-metered model trials, in-flight reservations (`attempted_cost_usd` or `cost_limit_usd`) count against the cap until the job transitions to `done` or `failed` and reports settled actuals.
   - For Daytona sandboxes, in-flight jobs are sized by `(timeout_seconds + TRIAL_PHASE_ALLOWANCE_SECONDS) × hourly_rate` as a conservative upper bound.
2. **Zero-Priced Route Separation**:
   - Self-hosted MiMo models are counted under Modal GPU hours, never as model token dollars. In-flight self-hosted trials reserve their estimated Modal container hours ($2.8149/h \times \text{hours} / \text{concurrency}$) if the server is active.
3. **Basis Flagging**:
   - The ledger maintains the `basis` attribute (`billed` vs `estimate` vs `ledger`). In any operator warning or refusal message, the components are listed explicitly (e.g. `Settled: $14.20 ($12.00 billed, $2.20 estimate) + In-flight: $4.50`).

---

## 5. Fail-Closed Behaviour

The check strictly fails closed under any of the following conditions:

1. **Catalog / Database Unreachable**:
   If PostgreSQL is unavailable or fails connection queries, the dispatch check refuses launch with exit code 1 and error `REFUSAL: daily_cap_unverified (database unreachable)`. It never assumes zero historical spend.
2. **Unratable In-Flight or Candidate Spec**:
   If a billable spec specifies a cloud environment (`daytona` or remote Modal) without a valid `est_cost_usd` or required resource specifications (`cpus`, `memory_mb`), dispatch refuses with `REFUSAL: unratable_cost_spec`.
3. **Cap Exceeded**:
   If $\text{Committed}(D) > \text{daily\_cost\_ceiling\_usd}$, dispatch refuses immediately with:
   ```
   REFUSAL: daily_cost_ceiling_exceeded
   Current committed spend $21.45 exceeds daily ceiling $20.00:
     Settled: $16.60 ($13.68 billed Modal, $2.92 est Daytona, $0.00 model)
     In-flight: $3.50 (2 jobs running)
     Candidate: $1.35
     Headroom: -$1.45
   ```
4. **No Silent Overrides**:
   Bypassing the check requires an explicit operator override flag (`--force-cap-override` or a signed human approval recorded in the spec), which writes an audit event to `queue/events.jsonl`.
