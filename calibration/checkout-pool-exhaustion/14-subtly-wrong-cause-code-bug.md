<!-- calibration-variant: subtly-wrong-cause -->
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

v4.19.0 introduced a connection-leak in the checkout handler. Workers were
raised 8→32 in the same release, which multiplied the leak. We paged at 14:09
and recovered by rolling back to v4.18.3 at 15:31. Mitigated 15:38.

## Impact

94 minutes, ~7,000 failed checkouts, 38.1% peak 5xx, ~29.5% across the window.

## Timeline

- **14:02** — v4.19.0 deploy (worker change plus the leaking handler).
- **14:04** — pool exhausted; first TimeoutError.
- **14:09** — page.
- **14:20** — vendor advisory considered and later dropped (latency ~130ms).
- **14:52** — restarts; leak immediately refilled the pool.
- **15:31** — rollback removes the leak.
- **15:38** — mitigated.

## Root Cause

A connection leak in the v4.19.0 handler. The pool sitting at `active=10
max=10` with a growing waiter count is the signature of connections that are
checked out and never returned. Raising workers from 8 to 32 only made the
leak faster. The 10-connection pool size is not itself the defect; it had
been stable since 2025-11-04.

We rule out the payments vendor (flat p99, 200s). We rule out search-api TLS.

## Contributing Factors

- No integration test that asserts connections return to the pool.
- Restarts were tried before rollback, which cannot clear a leak that
  reproduces on the new code.
- Canary was off, so all six instances leaked at once.

## Corrective Actions

1. Revert the v4.19.0 handler diff and add a regression test that checks
   `db_pool_active` after 1k requests.
2. Add a runtime guard that pages if `db_pool_active` stays at max for 60s.
3. Enable canary deploys.
4. Require a connection-pool soak test on any checkout-api change.

## Open Questions

- Which function leaked? The supplied evidence has no stack of an unreturned
  checkout, only the TimeoutError.
- Was the leak in new worker-pool code or in the payment client?
