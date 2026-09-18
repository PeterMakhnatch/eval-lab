# Judge input — checkout-pool-exhaustion / 10-correct-timeline-dense

- evidence digest: `sha256:2614343c64ec72bd4e5096489abffd16e1ecff3bfaae4722274557e4753ba908`
- evidence source: https://github.com/PeterMakhnatch/harbor-practice@a3bedf451187952f79f30c817cdcf5738ee6c24e `datasets/judged-output/checkout-pool-exhaustion/environment/evidence`
- evidence files: alerts.log, checkout-api.log, deploys.csv, metrics.csv, oncall-chat.txt, service-config.yaml, ticket.md
- files this document cites by name, and whether each is in the judge input: {'deploys.csv': True, 'metrics.csv': True, 'service-config.yaml': True}
- message sizes: [2159, 15192] characters (['system', 'user'])

## system

````text
Your input fields are:
1. `family` (str): calibration family identifier
2. `rubric_json` (str): JSON with reference_facts, criteria (dimension -> criterion -> question), negated criteria and verdict_convention
3. `evidence` (str): every file of the incident's evidence directory, verbatim; the only artifacts that exist besides the reference facts
4. `document` (str): the postmortem under judgment
Your output fields are:
1. `reasoning` (str): 
2. `judgments` (dict[str, dict[str, JudgeCriterionVerdict]]): dimension -> criterion -> {verdict, rationale}; include every criterion from rubric_json exactly once
All interactions will be structured in the following way, with the appropriate values filled in.

[[ ## family ## ]]
{family}

[[ ## rubric_json ## ]]
{rubric_json}

[[ ## evidence ## ]]
{evidence}

[[ ## document ## ]]
{document}

[[ ## reasoning ## ]]
{reasoning}

[[ ## judgments ## ]]
{judgments}        # note: the value you produce must adhere to the JSON schema: {"type": "object", "$defs": {"JudgeCriterionVerdict": {"type": "object", "additionalProperties": false, "properties": {"rationale": {"type": "string", "minLength": 1, "title": "Rationale"}, "verdict": {"type": "string", "enum": ["yes", "no"], "title": "Verdict"}}, "required": ["verdict", "rationale"], "title": "JudgeCriterionVerdict"}}, "additionalProperties": {"type": "object", "additionalProperties": {"$ref": "#/$defs/JudgeCriterionVerdict"}}}

[[ ## completed ## ]]
In adhering to this structure, your objective is: 
        Judge an incident postmortem against every criterion in the rubric.
        
        For each dimension and criterion in rubric_json.criteria, answer the criterion's question about the document with a raw yes/no. Negated criteria ask whether a flaw is PRESENT; answer yes when the flaw is present and do not invert. Decide evidence questions against the supplied evidence files and rubric_json.reference_facts: a cited artifact, value, or event that appears in neither does not exist, and a value that disagrees with those files is misstated. Do not assume facts that are not supplied. Include every criterion in rubric_json.criteria exactly once.
````

## user

````text
[[ ## family ## ]]
checkout-pool-exhaustion

[[ ## rubric_json ## ]]
{"criteria":{"action_quality":{"actions_are_actionable":"Does the corrective-action section name concrete changes with owners, targets, or objectively checkable completion conditions?","actions_trace_to_findings":"Does each substantive corrective action address a cause or contributing factor established earlier in the postmortem?","closes_the_detection_gap":"Does an action add detection for pool saturation or connection-acquire wait?","fixes_the_capacity_coupling":"Does an action couple pool sizing to worker count or fail startup on an unsafe mismatch?","proposes_unsupported_work":"Does the document propose material work based on a cause or risk that the supplied evidence files and reference facts do not support?"},"causal_reasoning":{"grounded_in_evidence":"Does it ground that mechanism in multiple specific items from the supplied evidence files or reference facts rather than assertion or fluent generalities?","identifies_the_mechanism":"Does the document identify contention between 32 workers and a 10-connection pool, leading to 5000ms acquire timeouts and checkout 500s?","rules_out_the_decoy":"Does it explicitly reject the payments-vendor advisory using timing, flat vendor latency, or successful vendor calls?","separates_contributing_factors":"Does it distinguish alerting, canary, review, or response gaps from the worker/pool causal mechanism?","uncertainty_is_genuine":"Are open questions limited to matters not settled by the supplied evidence files or reference facts?"},"evidence_fidelity":{"asserts_unsupported_cause":"Does the document state a causal mechanism contradicted by, or absent from, the supplied evidence files and reference facts?","blames_payments_vendor":"Does the document treat the payments vendor as a cause rather than a rejected hypothesis?","invents_evidence":"Does the document cite a dashboard, ticket, log line, metric, meeting, or observation that appears in neither the supplied evidence files nor the reference facts?","misstates_a_fact":"Does the document materially misstate a time, count, version, configuration value, or other fact established by the supplied evidence files or reference facts?"}},"family":"checkout-pool-exhaustion","negated":["action_quality.proposes_unsupported_work","evidence_fidelity.blames_payments_vendor","evidence_fidelity.asserts_unsupported_cause","evidence_fidelity.misstates_a_fact","evidence_fidelity.invents_evidence"],"reference_facts":["checkout-api v4.19.0 raised HTTP workers from 8 to 32 at 14:02 UTC.","The per-instance database pool remained max_connections=10 with a 5000ms timeout.","Pool active connections pinned at 10; waiters and 500 responses rose after deploy.","The payments-vendor advisory began later, latency stayed about 130ms, and every observed vendor call returned 200.","Rolling back to 8 workers at 15:31 restored pool wait and errors within minutes.","No pool-saturation or acquire-wait alert existed; the 5xx alert was customer-late.","A search-api TLS notice and a later ledger-db CPU warning were unrelated signals."],"schema_version":1,"verdict_convention":"Return the raw pre-inversion yes/no answer. A negated criterion asks whether the named flaw is present; do not invert its answer."}

[[ ## evidence ## ]]
Evidence directory /app/evidence (7 files; source https://github.com/PeterMakhnatch/harbor-practice@a3bedf451187 datasets/judged-output/checkout-pool-exhaustion/environment/evidence). These are the only files the postmortem author had. A cited artifact that is not in this directory and not in the reference facts does not exist.

### /app/evidence/alerts.log (sha256:c4ff0761241b)
```
# Alert manager history, 2026-07-14 (all services).
# PAGE = woke someone. WARN = dashboard + email only. INFO = dashboard only.

2026-07-14T09:02:11Z INFO  backup-lag: nightly ledger-db backup finished 11m late
2026-07-14T14:07:30Z INFO  search-api-cert-expiry: TLS certificate for search-api expires in 21 days
2026-07-14T14:09:10Z PAGE  checkout-api-5xx-rate: 5xx rate 18.4% > 5% for 5m
2026-07-14T14:24:00Z WARN  ledger-db-cpu: ledger-db CPU 70% > 70% for 10m (page threshold is 90%)
2026-07-14T15:39:40Z INFO  checkout-api-5xx-rate: resolved
2026-07-14T15:44:00Z INFO  ledger-db-cpu: resolved

# Configured alerts for checkout-api, for reference:
#   checkout-api-5xx-rate       PAGE   5xx rate > 5% for 5m
#   checkout-api-latency-p99    WARN   p99 > 2000ms for 10m
#   checkout-api-instance-down  PAGE   healthy instances < 2
# No alert is configured on database connection pool saturation or acquire wait.
```

### /app/evidence/checkout-api.log (sha256:8720bd086018)
```
# Filtered sample of checkout-api application logs, 2026-07-14.
# Filter: level >= WARN, plus INFO lines matching 'startup' or 'vendor.payments'.
# Repeated identical ERROR lines are collapsed; see metrics.csv for full counts.

2026-07-14T13:47:02Z INFO  startup: checkout-api v4.18.3 workers=8 db_pool_max=10 acquire_timeout_ms=5000
2026-07-14T13:52:41Z INFO  vendor.payments: POST /charge 200 in 126ms
2026-07-14T13:58:19Z INFO  vendor.payments: POST /charge 200 in 133ms
2026-07-14T14:02:11Z INFO  startup: checkout-api v4.19.0 workers=32 db_pool_max=10 acquire_timeout_ms=5000
2026-07-14T14:02:44Z INFO  vendor.payments: POST /charge 200 in 129ms
2026-07-14T14:03:58Z WARN  db.pool: acquire slow wait_ms=812 active=10 max=10 waiters=6
2026-07-14T14:04:07Z WARN  db.pool: acquire slow wait_ms=1904 active=10 max=10 waiters=23
2026-07-14T14:04:12Z ERROR http: POST /v1/checkout 500 TimeoutError: connection pool exhausted (max=10, waiters=41) after 5000ms
2026-07-14T14:06:33Z WARN  db.pool: acquire slow wait_ms=2210 active=10 max=10 waiters=58
2026-07-14T14:08:50Z ERROR http: POST /v1/checkout 500 TimeoutError: connection pool exhausted (max=10, waiters=76) after 5000ms
2026-07-14T14:11:05Z INFO  vendor.payments: POST /charge 200 in 131ms
2026-07-14T14:17:22Z ERROR http: POST /v1/checkout 500 TimeoutError: connection pool exhausted (max=10, waiters=88) after 5000ms
2026-07-14T14:22:03Z INFO  vendor.payments: POST /charge 200 in 128ms
2026-07-14T14:31:47Z WARN  db.pool: acquire slow wait_ms=3050 active=10 max=10 waiters=91
2026-07-14T14:38:14Z INFO  vendor.payments: POST /charge 200 in 134ms
2026-07-14T14:45:29Z ERROR http: POST /v1/checkout 500 TimeoutError: connection pool exhausted (max=10, waiters=97) after 5000ms
2026-07-14T14:59:01Z INFO  vendor.payments: POST /charge 200 in 130ms
2026-07-14T15:08:36Z ERROR http: POST /v1/checkout 500 TimeoutError: connection pool exhausted (max=10, waiters=94) after 5000ms
2026-07-14T15:19:52Z WARN  db.pool: acquire slow wait_ms=2871 active=10 max=10 waiters=85
2026-07-14T15:24:40Z INFO  vendor.payments: POST /charge 200 in 127ms
2026-07-14T15:31:44Z INFO  startup: checkout-api v4.18.3 workers=8 db_pool_max=10 acquire_timeout_ms=5000
2026-07-14T15:33:02Z INFO  db.pool: acquire ok wait_ms=3 active=4 max=10 waiters=0
2026-07-14T15:36:18Z INFO  vendor.payments: POST /charge 200 in 132ms
2026-07-14T15:41:55Z INFO  db.pool: acquire ok wait_ms=2 active=4 max=10 waiters=0
```

### /app/evidence/deploys.csv (sha256:af4a54de6448)
```
deployed_at_utc,service,release,change_summary,deployed_by
2026-07-10T11:20:00Z,checkout-api,v4.18.1,add structured request logging,ci-bot
2026-07-13T09:41:00Z,checkout-api,v4.18.3,dependency bumps only; no configuration change,ci-bot
2026-07-14T14:02:00Z,checkout-api,v4.19.0,raise HTTP server worker count from 8 to 32 for throughput,ci-bot
2026-07-14T14:05:00Z,search-api,v2.2.9,refresh bundled TLS certificate authorities,ci-bot
2026-07-14T15:31:00Z,checkout-api,v4.18.3,rollback of v4.19.0,r.okafor
2026-07-15T08:15:00Z,ledger-api,v9.4.0,scheduled index maintenance job,ci-bot
```

### /app/evidence/metrics.csv (sha256:33cdee49f365)
```
bucket_end_utc,checkout_requests,checkout_5xx,http_5xx_rate_pct,http_p99_ms,db_pool_wait_p99_ms,db_pool_active,db_pool_max,vendor_payments_p99_ms,ledger_db_cpu_pct
2026-07-14T13:45:00Z,1180,2,0.2,238,3,4,10,131,41
2026-07-14T13:50:00Z,1204,3,0.2,241,3,4,10,129,42
2026-07-14T13:55:00Z,1191,2,0.2,236,4,5,10,133,41
2026-07-14T14:00:00Z,1213,3,0.2,244,4,5,10,130,43
2026-07-14T14:05:00Z,1248,76,6.1,3812,1450,10,10,129,63
2026-07-14T14:10:00Z,1236,227,18.4,5001,2104,10,10,133,68
2026-07-14T14:15:00Z,1229,331,26.9,5002,2388,10,10,132,69
2026-07-14T14:20:00Z,1241,410,33.0,5003,2596,10,10,128,70
2026-07-14T14:25:00Z,1218,429,35.2,5001,2733,10,10,131,70
2026-07-14T14:30:00Z,1225,441,36.0,5002,3050,10,10,134,71
2026-07-14T14:35:00Z,1232,451,36.6,5004,2988,10,10,130,71
2026-07-14T14:40:00Z,1209,452,37.4,5001,3011,10,10,129,72
2026-07-14T14:45:00Z,1214,463,38.1,5003,3104,10,10,133,72
2026-07-14T14:50:00Z,1197,451,37.7,5002,2967,10,10,127,71
2026-07-14T14:55:00Z,1188,442,37.2,5001,2901,10,10,130,71
2026-07-14T15:00:00Z,1176,432,36.7,5002,2874,10,10,132,70
2026-07-14T15:05:00Z,1163,421,36.2,5003,2842,10,10,128,70
2026-07-14T15:10:00Z,1155,410,35.5,5001,2810,10,10,131,70
2026-07-14T15:15:00Z,1147,399,34.8,5002,2788,10,10,129,69
2026-07-14T15:20:00Z,1139,389,34.2,5001,2871,10,10,127,69
2026-07-14T15:25:00Z,1131,378,33.4,5002,2755,10,10,130,69
2026-07-14T15:30:00Z,1128,367,32.5,5001,2702,10,10,133,68
2026-07-14T15:35:00Z,1142,48,4.2,486,210,7,10,131,52
2026-07-14T15:40:00Z,1168,4,0.3,251,4,4,10,132,44
2026-07-14T15:45:00Z,1183,2,0.2,239,3,4,10,130,42
2026-07-14T15:50:00Z,1196,3,0.2,242,3,4,10,128,41
2026-07-14T15:55:00Z,1201,2,0.2,240,4,5,10,131,41
```

### /app/evidence/oncall-chat.txt (sha256:6be22c8ca642)
```
#checkout-incidents — 2026-07-14 (all times UTC)

14:12 <r.okafor> paged on checkout 5xx, anyone else seeing it
14:13 <s.lindqvist> yep, dashboard is red
14:15 <s.lindqvist> roughly 18% of POST /v1/checkout failing, all with 500s
14:18 <r.okafor> customers are complaining in #support too
14:20 <r.okafor> payments vendor status page just posted "elevated latency" — probably them
14:21 <r.okafor> opening a ticket with the vendor
14:23 <s.lindqvist> hmm, our vendor call p99 is 130ms and flat all afternoon, doesn't look like them
14:26 <r.okafor> keeping the vendor theory for now, it's the only thing that changed externally
14:29 <r.okafor> anything deployed today?
14:31 <s.lindqvist> checkout-api v4.19.0 went out at 14:02. search-api also shipped at 14:05
14:33 <r.okafor> search-api isn't in the checkout path, ignore that one
14:36 <s.lindqvist> the 500s are all TimeoutError, not vendor errors
14:41 <r.okafor> vendor advisory cleared and we're still erroring, so it isn't them
14:44 <s.lindqvist> ledger-db cpu warning fired too, but it's at 70 and pages at 90
14:52 <r.okafor> trying a restart of two instances, no change
15:12 <s.lindqvist> restart didn't help, errors identical after
15:26 <r.okafor> going to roll back v4.19.0, we're out of other ideas
15:31 <r.okafor> rollback started
15:38 <r.okafor> errors are gone, calling it mitigated
15:40 <s.lindqvist> we should figure out what in v4.19.0 did this, the diff is tiny
```

### /app/evidence/service-config.yaml (sha256:132d18c0b18b)
```
# checkout-api runtime configuration.
# This is the configuration currently in effect, i.e. v4.18.3 after the
# rollback at 2026-07-14T15:31:00Z.

service: checkout-api
release: v4.18.3

server:
  # v4.19.0 changed this value to 32. The rollback restored it to 8.
  workers: 8
  request_timeout_ms: 8000

database:
  host: ledger-db.internal
  pool:
    # Per-instance connection pool. Last changed 2025-11-04, when the service
    # ran 8 workers. Not touched by v4.19.0.
    max_connections: 10
    acquire_timeout_ms: 5000
    idle_timeout_ms: 30000

vendor:
  payments:
    base_url: https://api.payments-vendor.example
    timeout_ms: 3000
    retries: 2

deployment:
  strategy: rolling
  canary: false
  instances: 6
```

### /app/evidence/ticket.md (sha256:1e15c280c628)
```
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
```


[[ ## document ## ]]
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

The timeline of INC-2291 is the mechanism: the 14:02 worker-count change
immediately saturates a 10-connection pool, 500s begin at 14:04, a vendor
advisory at 14:20 does not move vendor latency, and rolling back workers at
15:31 restores the pool. Mitigated 15:38.

## Impact

- 14:04 to 15:38: 94 minutes.
- 7,021 `checkout_5xx` vs a 2–3 per-bucket baseline.
- Peak 38.1% at 14:45; ~29.5% across the window.
- `#support` complaints from 14:18.

## Timeline

- **14:02** — `deploys.csv` v4.19.0; log `workers=32 db_pool_max=10`.
- **14:03:58** — `db.pool` wait_ms=812 active=10 max=10 waiters=6.
- **14:04:12** — first `connection pool exhausted (max=10, waiters=41) after 5000ms`.
- **14:05** — 6.1% 5xx, wait p99 1450.
- **14:09** — page 18.4%.
- **14:20** — vendor advisory; on-call adopts it.
- **14:23** — vendor p99 still ~130ms.
- **14:41** — advisory clears; our errors do not.
- **14:45** — 38.1% 5xx, wait p99 3104, waiters=97.
- **14:52** — restarts; no change.
- **15:31** — v4.18.3 workers=8.
- **15:33** — wait_ms=3 active=4.
- **15:38** — mitigated.

## Root Cause

32 workers / 10 connections / 5000ms acquire timeout. The deploy triggered a
latent pool-sizing defect. Evidence: startup line, pool warnings, TimeoutError,
`metrics.csv`, `service-config.yaml`. Not the cause: payments vendor (flat
~130ms, 200s, wrong timing), search-api TLS, ledger-db CPU 72%.

## Contributing Factors

No pool alert; uncoupled config; canary off; vendor hypothesis delayed rollback;
restarts tried before rollback.

## Corrective Actions

1. Startup assertion `workers <= max_connections`.
2. Raise `max_connections` and load-test 32 workers.
3. Alert on pool saturation and acquire wait.
4. Enable canary deploys.
5. Add "what did we deploy?" to the first five minutes of the runbook.

## Open Questions

Reconciliation report missing; reason for 32 workers missing; other services
unexamined; vendor's own 14:20 advisory unexplained (not needed here).


Respond with the corresponding output fields, starting with the field `[[ ## reasoning ## ]]`, then `[[ ## judgments ## ]]` (must be formatted as a valid Python dict[str, dict[str, JudgeCriterionVerdict]]), and then ending with the marker for `[[ ## completed ## ]]`.
````
