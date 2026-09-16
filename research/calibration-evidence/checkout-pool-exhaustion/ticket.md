# INC-2291 — checkout-api elevated 5xx

- Severity: SEV-2
- Opened: 2026-07-14 14:09 UTC (auto-paged by `checkout-api-5xx-rate`)
- Mitigated: 2026-07-14 15:38 UTC
- Status: mitigated, postmortem not yet written
- Incident commander: r.okafor
- Responders: s.lindqvist, r.okafor
- Affected service: checkout-api (POST /v1/checkout)

## Raw notes captured during the incident

These are the commander's live notes. They were typed under time pressure and
have not been reviewed.

```
14:12  paged, confirming elevated 5xx on checkout-api
14:20  payments vendor status page posted "elevated latency" advisory
14:29  asked whether anything shipped today
14:41  vendor advisory cleared; our error rate unchanged
15:26  decision: roll back checkout-api
15:31  rollback started
15:38  error rate back to baseline, declaring mitigated
```

## Not yet gathered

- Payment reconciliation report for the affected window (finance owns it; not
  available until the next business day).
- Whether any customer was charged without receiving an order confirmation.
