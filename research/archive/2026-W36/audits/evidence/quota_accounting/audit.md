# Audit Evidence: Quota Accounting vs `events.jsonl` Truth

## Subject
- **Handoffs**: `agents/handoffs/quota-accounting.md`, `agents/handoffs/quota-gate.md`, `agents/handoffs/quota-fallback.md`
- **Modules**: `src/evallab/quota.py`, `src/evallab/queue.py`, `src/evallab/preflight.py`
- **Tests**: `tests/test_quota.py`, `tests/test_quota_gate.py`
- **Event Log**: `queue/events.jsonl`

## Verified Claims & Analysis
1. **Account vs Lab Headroom Separation**: `quota.py` models `Headroom` (account-wide subscription remaining) and `ConsumptionLedger` (lab-attributable token counts) separately. Every metric carries `[observed]` or `[unavailable]`.
2. **Promoted Evidence Quota Fallback**: `quota.py` reads `agent/quota/*.rate-limits.json` sidecars in `research/evidence/runs/` when raw session rollouts are omitted. Harvesting yields 67 snapshots with `used_percent=70.0%`, weekly rolling window (`10080` min), `hard_stop=True`, and resets at `2026-08-20T18:32:49Z`.
3. **Consumption Truth vs Events Truth**:
   - `queue/events.jsonl` is the authoritative record of queue state transitions and gate admissions/refusals (144 events, 53 codex events).
   - `events.jsonl` records `dispatch_attempt_reserved` with a static heuristic (`estimated_cost_usd: 2.5`), which is a reservation upper-bound, not actual spend or token measurement.
   - `quota.py` extracts true consumption directly from trial artifacts: 9 trials consumed 113,176 uncached in, 1,176,832 cached in (91.2% cached), 40,375 output tokens, totaling $0.9462 list-price equivalent.
4. **PolicyGate Wiring**: `PolicyGate` reads `quota.py` headroom, blocks on provider-reported exhaustion (`reason_code="subscription_quota_exhausted"`), warns on stale readings, and admits overrides via `evallab approve --despite-quota` (which logs `reason_code="quota_override"` in `events.jsonl`).
5. **Test Suite**: 79 unit and contract tests in `tests/test_quota.py` and `tests/test_quota_gate.py` pass cleanly in 0.36s.

## Verdict
**CONFIRMED**. Quota accounting accurately extracts provider rate limits and artifact tokens from disk, correctly separating account-wide subscription limits from lab token totals. The relationship with `events.jsonl` holds: `events.jsonl` tracks lifecycle transitions and reservation bounds, while `quota.py` measures ground-truth usage from artifacts.
