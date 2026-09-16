# What DSPy actually sends (GEPA-compiled judge, doc 11-subtly-wrong-cause-tls, replayed from cache)


## system (9217 chars)

```text
Your input fields are:
1. `family` (str): calibration family identifier
2. `rubric_json` (str): JSON with reference_facts, criteria (dimension -> criterion -> question) and verdict_convention
3. `document` (str): the postmortem under judgment
Your output fields are:
1. `reasoning` (str): 
2. `judgments` (dict[str, dict[str, JudgeCriterionVerdict]]): dimension -> criterion -> {verdict, rationale}; include every criterion from rubric_json exactly once
All interactions will be structured in the following way, with the appropriate values filled in.

[[ ## family ## ]]
{family}

[[ ## rubric_json ## ]]
{rubric_json}

[[ ## document ## ]]
{document}

[[ ## reasoning ## ]]
{reasoning}

[[ ## judgments ## ]]
{judgments}        # note: the value you produce must adhere to the JSON schema: {"type": "object", "$defs": {"JudgeCriterionVerdict": {"type": "object", "additionalProperties": false, "properties": {"rationale": {"type": "string", "minLength": 1, "title": "Rationale"}, "verdict": {"type": "string", "enum": ["yes", "no"], "title": "Verdict"}}, "required": ["verdict", "rationale"], "title": "JudgeCriterionVerdict"}}, "additionalProperties": {"type": "object", "additionalProperties": {"$ref": "#/$defs/JudgeCriterionVerdict"}}}

[[ ## completed ## ]]
In adhering to this structure, your objective is: 
        Judge an incident postmortem against every criterion in rubric_json.criteria.
        
        For each dimension and criterion, answer the criterion's question about the
        document with a raw yes/no. Negated criteria (listed in rubric_json.negated)
        ask whether the named flaw is PRESENT; answer yes when the flaw is present
        and do not invert. Base evidence decisions ONLY on rubric_json.reference_facts
        and the document text; never assume facts that are not supplied.
        
        Output a "reasoning" summary plus a judgments mapping of dimension -> criterion
        -> CriterionVerdict(verdict, rationale), exactly like the schema shown in the
        examples.
        
        Domain context (checkout-pool-exhaustion family):
        - The true mechanism per reference facts: v4.19.0 raised HTTP workers from 8
          to 32 at 14:02 UTC against a per-instance pool of max_connections=10 with a
          5000ms acquire timeout; pool active connections pinned at 10 with rising
          waiters/500s; rollback to 8 workers (v4.18.3) at 15:31 restored service
          within minutes (mitigated 15:38). The payments-vendor advisory is a decoy:
          it began later (~14:20), vendor latency stayed flat ~130ms, and every
          observed vendor call returned 200. A search-api TLS notice and a later
          ledger-db CPU warning were unrelated signals. No pool-saturation or
          acquire-wait alert existed; the 5xx alert fired customer-late (14:09).
        
        Calibration rules learned from reviewer feedback — apply these exactly:
        
        1. causal_reasoning.grounded_in_evidence: This asks whether the document
           grounds ITS OWN causal mechanism in multiple specific facts (times, counts,
           config values), even if that mechanism is WRONG. A wrong vendor story tied
           to advisory timing and the 5xx curve is still "grounded" — answer yes.
           Reserve "no" for vague assertion with no specific citations (e.g., "a
           recent change interacted with an existing limit" with no times/counts).
        
        2. causal_reasoning.identifies_the_mechanism: Answer yes only if the document
           explicitly names the 32-workers vs 10-connection contention with 5000ms
           acquire timeouts producing checkout 500s. Generic "a change hit a limit"
           wording is a no.
        
        3. causal_reasoning.rules_out_the_decoy: Answer yes only if the document
           explicitly rejects the payments vendor using timing, flat ~130ms latency,
           or successful (200) vendor calls. Never mentioning the vendor is a no.
        
        4. caus
... [truncated]
```


## user (5523 chars)

```text
[[ ## family ## ]]
checkout-pool-exhaustion

[[ ## rubric_json ## ]]
{"criteria":{"action_quality":{"actions_are_actionable":"Does the corrective-action section name concrete changes with owners, targets, or objectively checkable completion conditions?","actions_trace_to_findings":"Does each substantive corrective action address a cause or contributing factor established earlier in the postmortem?","closes_the_detection_gap":"Does an action add detection for pool saturation or connection-acquire wait?","fixes_the_capacity_coupling":"Does an action couple pool sizing to worker count or fail startup on an unsafe mismatch?","proposes_unsupported_work":"Does the document propose material work based on a cause or risk that the supplied reference facts do not support?"},"causal_reasoning":{"grounded_in_evidence":"Does it ground that mechanism in multiple specific supplied facts rather than assertion or fluent generalities?","identifies_the_mechanism":"Does the document identify contention between 32 workers and a 10-connection pool, leading to 5000ms acquire timeouts and checkout 500s?","rules_out_the_decoy":"Does it explicitly reject the payments-vendor advisory using timing, flat vendor latency, or successful vendor calls?","separates_contributing_factors":"Does it distinguish alerting, canary, review, or response gaps from the worker/pool causal mechanism?","uncertainty_is_genuine":"Are open questions limited to matters not settled by the supplied facts?"},"evidence_fidelity":{"asserts_unsupported_cause":"Does the document state a causal mechanism contradicted by, or absent from, the supplied reference facts?","blames_payments_vendor":"Does the document treat the payments vendor as a cause rather than a rejected hypothesis?","invents_evidence":"Does the document cite a dashboard, ticket, log, metric, meeting, or observation not present in the supplied reference facts?","misstates_a_fact":"Does the document materially misstate a time, count, version, configuration value, or other supplied reference fact?"}},"family":"checkout-pool-exhaustion","negated":["action_quality.proposes_unsupported_work","evidence_fidelity.blames_payments_vendor","evidence_fidelity.asserts_unsupported_cause","evidence_fidelity.misstates_a_fact","evidence_fidelity.invents_evidence"],"reference_facts":["checkout-api v4.19.0 raised HTTP workers from 8 to 32 at 14:02 UTC.","The per-instance database pool remained max_connections=10 with a 5000ms timeout.","Pool active connections pinned at 10; waiters and 500 responses rose after deploy.","The payments-vendor advisory began later, latency stayed about 130ms, and every observed vendor call returned 200.","Rolling back to 8 workers at 15:31 restored pool wait and errors within minutes.","No pool-saturation or acquire-wait alert existed; the 5xx alert was customer-late.","A search-api TLS notice and a later ledger-db CPU warning were unrelated signals."],"schema_version":1,"verdict_convention":"Return the raw pre-inversion yes/no answer. A negated criterion asks whether the named flaw is present; do not invert its answer."}

[[ ## document ## ]]
# Postmortem: INC-2291 — checkout-api elevated 5xx

## Summary

On 2026-07-14, `search-api v2.2.9` deployed a TLS bundle at 14:05. Checkout
began returning 500s in the same window. We paged at 14:09, spent time on a
payments-vendor advisory, and recovered when we rolled `checkout-api` back to
v4.18.3 at 15:31. Mitigated 15:38.

## Impact

Checkout degraded 94 minutes. About 7,000 POSTs failed, peak 38.1% 5xx, window
rate about 29.5%. Customers complained from 14:18.

## Timeline

- **14:02** — checkout-api v4.19.0 shipped as a routine worker-count change.
- **14:05** — search-api TLS bundle deploy.
- **14:07:30** — cert-expiry notice on search-api.
- **14:09** — checkout 5xx page at 18.4%.
- **14:20** — payments vendor advisory (red herring).
- **15:31** — checkout rollback; errors collapse.
- **15:38** — mitigated.

## Root Cause

The search-api TLS bundle deploy at 14:0
... [truncated]
```


## assistant response (5264 chars)

```text
{"text": "[[ ## reasoning ## ]]\nThe document attributes the incident to a search-api TLS bundle, contradicting the reference facts (rollback to 8 workers fixed it). Mechanism is wrong, though \"grounded\" in some cited specifics. Vendor is treated as unrelated (decoy rejected with flat 130ms), so blames_payments_vendor = no, but asserts_unsupported_cause = yes (TLS mechanism contradicted/absent). Contributing factors are substantive. Actions trace to the (wrong) TLS findings but do not address pool saturation/acquire wait or pool-worker coupling \u2014 closes_the_detection_gap and fixes_the_capacity_coupling are no. Actions are concrete/checkable. No clearly fabricated artifacts cited. Times match reference facts except minor window details not contradicted materially. Open questions are partly genuine but the second presumes vendor relevance the facts refute; still, first is genuine \u2014 mixed, lean yes given questions concern matters facts don't settle (cert-error counts), though second presumes settled matter; judge no because one presumes the vendor/certificate link the facts refute? Facts don't explicitly settle whether vendor was affected \u2014 but the certificate-family premise is unsupported. I'll answer no for genuine uncertainty being limited to unsettled matters since the question is premised on the fabricated mechanism.\n\n[[ ## judgments ## ]]\n{\"action_quality\": {\"actions_are_actionable\": {\"rationale\": \"Concrete, checkable actions: 'Roll back the search-api v2.2.9 TLS bundle and re-issue certificates', 'Add a dedicated cert-expiry page 14 days before expiry', 'Put a circuit breaker on checkout's search-api client', 'Require canaries for TLS material deploys'.\", \"verdict\": \"yes\"}, \"actions_trace_to_findings\": {\"rationale\": \"Each action maps to contributing factors the document itself established (no canary, no timeout isolation, alerting not naming cert errors, time on advisory).\", \"verdict\": \"yes\"}, \"closes_the_detection_gap\": {\"rationale\": \"No action addresses pool saturation or connection-acquire wait; the cert-expiry page and circuit breaker target the wrong (TLS) mechanism.\", \"verdict\": \"no\"}, \"fixes_the_capacity_coupling\": {\"rationale\": \"No action couples pool sizing to worker count or fails startup on a workers > max_connections mismatch; the worker change is dismissed as 'unrelated'.\", \"verdict\": \"no\"}, \"proposes_unsupported_work\": {\"rationale\": \"Material work (cert re-issue, circuit breaker on search-api, canaries for TLS deploys) is premised on the TLS mechanism, which the supplied facts do not support; facts say the search-api TLS notice was an unrelated signal.\", \"verdict\": \"yes\"}}, \"causal_reasoning\": {\"grounded_in_evidence\": {\"rationale\": \"It grounds its own (wrong) mechanism in specific citations: 'search-api v2.2.9 deployed a TLS bundle at 14:05', 'The 14:07:30 cert-expiry notice is the smoking gun', 'vendor_payments_p99_ms stayed near 130ms'.\", \"verdict\": \"yes\"}, \"identifies_the_mechanism\": {\"rationale\": \"Never names 32 workers vs 10-connection pool contention with 5000ms acquire timeouts; explicitly says 'v4.19.0's worker change is unrelated'.\", \"verdict\": \"no\"}, \"rules_out_the_decoy\": {\"rationale\": \"Explicitly rejects the vendor: 'The vendor advisory is also unrelated: `vendor_payments_p99_ms` stayed near 130ms' and calls it a 'red herring'.\", \"verdict\": \"yes\"}, \"separates_contributing_factors\": {\"rationale\": \"Contributing Factors list is substantive and distinct from the stated mechanism: no canary on TLS rollouts, no timeout isolation, alerting did not name cert errors, time spent on the vendor advisory.\", \"verdict\": \"yes\"}, \"uncertainty_is_genuine\": {\"rationale\": \"The second question ('Was the payments vendor also affected by the same certificate family?') presumes the unsupported certificate mechanism linking vendor and search-api; the first interprets TimeoutError text to fit the wrong sto
```
