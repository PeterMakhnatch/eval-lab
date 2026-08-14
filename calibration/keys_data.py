"""Gold per-criterion verdicts for every labeled document.

Verdicts are the judge's pre-inversion yes/no against the family's CR/AQ/EF
rubrics. Rationales are one line each.
"""

# ruff: noqa: E501
from __future__ import annotations

from collections.abc import Callable

Cell = tuple[str, str]
Dim = dict[str, Cell]
Key = dict[str, Dim]


def _co(**cells: Cell) -> Dim:
    return dict(cells)


def checkout_correct() -> Key:
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "yes",
                "Root cause names workers=32 versus pool max=10 and the 5000ms acquire timeout.",
            ),
            grounded_in_evidence=(
                "yes",
                "Causal claim cites at least two of startup line, TimeoutError, metrics.csv, and service-config.yaml.",
            ),
            rules_out_the_decoy=(
                "yes",
                "Payments-vendor advisory is rejected with flat ~130ms latency and wrong timing.",
            ),
            separates_contributing_factors=(
                "yes",
                "Contributing factors are detection, canary, and review gaps, not a restatement of the pool mismatch.",
            ),
            uncertainty_is_genuine=(
                "yes",
                "Open questions include reconciliation or why workers were raised, which the pack cannot settle.",
            ),
        ),
        "action_quality": _co(
            fixes_the_capacity_coupling=(
                "yes",
                "An action couples pool size to workers or fails startup on mismatch.",
            ),
            closes_the_detection_gap=(
                "yes",
                "An action adds a pool-saturation or acquire-wait alert tied to alerts.log.",
            ),
            actions_are_actionable=(
                "yes",
                "Each action names a concrete config, alert, or process change with a done-when.",
            ),
            actions_trace_to_findings=(
                "yes",
                "Actions follow the pool mismatch, detection gap, or canary finding stated earlier.",
            ),
            proposes_unsupported_work=(
                "no",
                "No vendor replacement, ledger-db resize, TLS work, or service rewrite is proposed.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_payments_vendor=(
                "no",
                "Vendor advisory is treated as a rejected hypothesis, not a cause.",
            ),
            asserts_unsupported_cause=(
                "no",
                "Cause is the worker/pool mismatch established by the evidence.",
            ),
            misstates_a_fact=(
                "no",
                "Times, versions, pool size 10, and worker counts 8/32 match the evidence.",
            ),
            invents_evidence=(
                "no",
                "Cited files and numbers appear in /app/evidence.",
            ),
        ),
    }


def checkout_absent() -> Key:
    """Empty, generic, or copied text that never answers the judge questions."""
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "no",
                "Root cause does not state the 32-worker / 10-connection acquire-timeout mechanism.",
            ),
            grounded_in_evidence=(
                "no",
                "No causal claim is tied to two distinct files from /app/evidence.",
            ),
            rules_out_the_decoy=(
                "no",
                "Payments-vendor advisory is not explicitly ruled out with evidence.",
            ),
            separates_contributing_factors=(
                "no",
                "Contributing factors are missing, generic, or restated cause.",
            ),
            uncertainty_is_genuine=(
                "no",
                "Open questions are empty, filler, or already answered by the pack.",
            ),
        ),
        "action_quality": _co(
            fixes_the_capacity_coupling=(
                "no",
                "No action makes worker count and pool capacity consistent.",
            ),
            closes_the_detection_gap=(
                "no",
                "No action adds a pool-saturation or acquire-wait alert from the evidence.",
            ),
            actions_are_actionable=(
                "no",
                "Actions are absent, TBD, or generic advice without a concrete change.",
            ),
            actions_trace_to_findings=(
                "no",
                "Actions are missing or disconnected from findings in this document.",
            ),
            proposes_unsupported_work=(
                "no",
                "The document does not propose vendor, TLS, ledger-db, or rewrite work.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_payments_vendor=(
                "no",
                "Vendor is not treated as a cause or contributing factor.",
            ),
            asserts_unsupported_cause=(
                "no",
                "No unsupported cause is asserted as established fact.",
            ),
            misstates_a_fact=(
                "no",
                "No specific time, version, or count disagrees with the evidence files.",
            ),
            invents_evidence=(
                "no",
                "No people, tickets, dashboards, or metrics absent from /app/evidence are cited.",
            ),
        ),
    }


def checkout_wrong_vendor() -> Key:
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "no",
                "Root cause blames vendor latency rather than workers=32 versus pool=10.",
            ),
            grounded_in_evidence=(
                "yes",
                "The wrong claim is still tied to the vendor advisory and the 5xx curve.",
            ),
            rules_out_the_decoy=(
                "no",
                "The payments-vendor advisory is presented as the cause.",
            ),
            separates_contributing_factors=(
                "yes",
                "Listed factors (single vendor, timeouts, no breaker) are distinct from that cause.",
            ),
            uncertainty_is_genuine=(
                "yes",
                "Asks for the vendor's own RCA and reconciliation, which the pack cannot settle.",
            ),
        ),
        "action_quality": _co(
            fixes_the_capacity_coupling=(
                "no",
                "Actions target the vendor path, not pool-versus-worker coupling.",
            ),
            closes_the_detection_gap=(
                "no",
                "Vendor-latency alerting is not the pool-saturation gap in alerts.log.",
            ),
            actions_are_actionable=(
                "yes",
                "Vendor review, failover, breaker, and timeout changes are specific enough to execute.",
            ),
            actions_trace_to_findings=(
                "yes",
                "Each action follows the document's (wrong) vendor findings.",
            ),
            proposes_unsupported_work=(
                "yes",
                "Proposes a vendor reliability review and a secondary payments provider.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_payments_vendor=(
                "yes",
                "Vendor elevated-latency advisory is stated as the root cause.",
            ),
            asserts_unsupported_cause=(
                "yes",
                "Vendor capacity/latency is asserted as the established cause.",
            ),
            misstates_a_fact=(
                "yes",
                "Treats the 14:20 advisory as aligning with onset even though errors start at 14:04.",
            ),
            invents_evidence=(
                "no",
                "Uses the advisory and metrics that exist in the pack; no invented artifacts.",
            ),
        ),
    }


def checkout_wrong_tls() -> Key:
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "no",
                "Root cause is search-api TLS, not the worker/pool mismatch.",
            ),
            grounded_in_evidence=(
                "yes",
                "Ties the claim to the 14:05 TLS deploy and 14:07:30 cert-expiry notice.",
            ),
            rules_out_the_decoy=(
                "yes",
                "Payments vendor is rejected using flat ~130ms latency.",
            ),
            separates_contributing_factors=(
                "yes",
                "Canary, timeout isolation, and vendor time-loss are distinct from the TLS claim.",
            ),
            uncertainty_is_genuine=(
                "yes",
                "Asks how many 500s were certificate errors, which the pack cannot settle.",
            ),
        ),
        "action_quality": _co(
            fixes_the_capacity_coupling=(
                "no",
                "Actions roll back TLS and add search-api isolation, not pool-to-worker coupling.",
            ),
            closes_the_detection_gap=(
                "no",
                "Cert-expiry paging is not the missing pool-saturation alert.",
            ),
            actions_are_actionable=(
                "yes",
                "TLS rollback, cert page, breaker, and TLS canary are concrete.",
            ),
            actions_trace_to_findings=(
                "yes",
                "Actions follow the document's TLS-dependency findings.",
            ),
            proposes_unsupported_work=(
                "yes",
                "Proposes remediating the search-api TLS bundle, which the evidence does not establish.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_payments_vendor=(
                "no",
                "Vendor is explicitly called unrelated.",
            ),
            asserts_unsupported_cause=(
                "yes",
                "search-api TLS is asserted as the cause of checkout 500s.",
            ),
            misstates_a_fact=(
                "no",
                "Deploy times and vendor latency figures that appear match the files.",
            ),
            invents_evidence=(
                "no",
                "TLS deploy and cert notice exist in the evidence; no extra artifacts.",
            ),
        ),
    }


def checkout_wrong_ledger() -> Key:
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "no",
                "Root cause is ledger-db CPU, not 32 workers versus 10 connections.",
            ),
            grounded_in_evidence=(
                "yes",
                "Cites the 14:24 ledger-db CPU warning and the worker-count increase.",
            ),
            rules_out_the_decoy=(
                "yes",
                "Payments vendor is rejected on ~130ms latency.",
            ),
            separates_contributing_factors=(
                "yes",
                "Autoscaling, review, and page-threshold notes are distinct from the CPU claim.",
            ),
            uncertainty_is_genuine=(
                "yes",
                "Asks which query burned CPU; pg_stat_statements is not in the pack.",
            ),
        ),
        "action_quality": _co(
            fixes_the_capacity_coupling=(
                "no",
                "Actions scale the database and freeze workers rather than couple pool to workers.",
            ),
            closes_the_detection_gap=(
                "no",
                "Lowering ledger-db CPU page is not the missing pool-saturation alert.",
            ),
            actions_are_actionable=(
                "yes",
                "Scale ledger-db, freeze workers, DBA gate, and 70% page are specific.",
            ),
            actions_trace_to_findings=(
                "yes",
                "Actions follow the document's database-CPU findings.",
            ),
            proposes_unsupported_work=(
                "yes",
                "Proposes scaling ledger-db because of the CPU warning.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_payments_vendor=("no", "Vendor is stated as uninvolved."),
            asserts_unsupported_cause=(
                "yes",
                "ledger-db CPU is asserted as the established cause.",
            ),
            misstates_a_fact=(
                "no",
                "72% CPU and 90% page threshold match alerts.log.",
            ),
            invents_evidence=("no", "No artifacts beyond the supplied files are cited."),
        ),
    }


def checkout_wrong_traffic() -> Key:
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "no",
                "Root cause is a traffic spike, not the pool-versus-worker mismatch.",
            ),
            grounded_in_evidence=(
                "yes",
                "Points at metrics.csv request counts and the 14:02 deploy as a failed throughput fix.",
            ),
            rules_out_the_decoy=(
                "yes",
                "Vendor advisory is called unrelated because latency stayed flat.",
            ),
            separates_contributing_factors=(
                "yes",
                "Load-shed, planning, and autoscaling gaps are distinct from the spike claim.",
            ),
            uncertainty_is_genuine=(
                "yes",
                "Asks which campaign drove the spike; no marketing ticket is supplied.",
            ),
        ),
        "action_quality": _co(
            fixes_the_capacity_coupling=(
                "no",
                "Autoscaling and load-shed do not couple pool size to workers.",
            ),
            closes_the_detection_gap=(
                "no",
                "No pool-saturation or acquire-wait alert is added.",
            ),
            actions_are_actionable=(
                "yes",
                "Autoscale, load-shed 503, capacity ticket, and standby region are concrete.",
            ),
            actions_trace_to_findings=(
                "yes",
                "Actions follow the document's spike and admission-control findings.",
            ),
            proposes_unsupported_work=(
                "yes",
                "Treats a traffic-spike capacity program as remediation for this incident.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_payments_vendor=("no", "Vendor is called unrelated."),
            asserts_unsupported_cause=(
                "yes",
                "An unforecasted traffic spike is asserted as the cause.",
            ),
            misstates_a_fact=(
                "yes",
                "Reads ~1200 req/5min as a surge even though that is the pre-incident baseline in metrics.csv.",
            ),
            invents_evidence=("no", "Uses metrics.csv; no invented dashboards or people."),
        ),
    }


def checkout_wrong_leak() -> Key:
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "no",
                "Root cause is a handler leak, not uncoupled pool sizing for 32 workers.",
            ),
            grounded_in_evidence=(
                "yes",
                "Reads pool active=10 and growing waiters as a leak signature in the logs.",
            ),
            rules_out_the_decoy=(
                "yes",
                "Vendor is ruled out with flat p99 and HTTP 200s.",
            ),
            separates_contributing_factors=(
                "yes",
                "Missing pool tests, restarts-before-rollback, and canary-off are distinct.",
            ),
            uncertainty_is_genuine=(
                "yes",
                "Asks which function leaked; the pack has no unreturned-checkout stack.",
            ),
        ),
        "action_quality": _co(
            fixes_the_capacity_coupling=(
                "no",
                "Actions revert a supposed leak rather than couple pool size to workers.",
            ),
            closes_the_detection_gap=(
                "yes",
                "Adds a page when db_pool_active stays at max, which is the missing saturation signal.",
            ),
            actions_are_actionable=(
                "yes",
                "Revert, regression test, pool page, canary, and soak test are specific.",
            ),
            actions_trace_to_findings=(
                "yes",
                "Actions follow the document's leak and canary findings.",
            ),
            proposes_unsupported_work=(
                "yes",
                "Proposes remediating a code defect in the v4.19.0 diff other than the worker-count change.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_payments_vendor=("no", "Vendor is ruled out."),
            asserts_unsupported_cause=(
                "yes",
                "A connection leak is asserted as the established cause.",
            ),
            misstates_a_fact=("no", "Pool size, times, and vendor latency match the files."),
            invents_evidence=("no", "No extra people or dashboards are introduced."),
        ),
    }


def checkout_useless_tbd() -> Key:
    key = checkout_correct()
    key["action_quality"] = _co(
        fixes_the_capacity_coupling=("no", "Corrective actions are only 'TBD.'"),
        closes_the_detection_gap=("no", "No detection-gap action is specified."),
        actions_are_actionable=("no", "TBD is not a named change to a named system."),
        actions_trace_to_findings=("no", "TBD does not trace any action to a finding."),
        proposes_unsupported_work=("no", "TBD does not propose unsupported vendor or TLS work."),
    )
    return key


def checkout_useless_generic() -> Key:
    key = checkout_correct()
    key["action_quality"] = _co(
        fixes_the_capacity_coupling=(
            "no",
            "Actions are platform-wide monitoring and process advice, not pool-to-worker coupling.",
        ),
        closes_the_detection_gap=(
            "no",
            "'Improve monitoring' does not add the missing pool-saturation alert.",
        ),
        actions_are_actionable=(
            "no",
            "List is generic advice without a named config key or done-when.",
        ),
        actions_trace_to_findings=(
            "no",
            "Generic process actions are disconnected from the pool-mismatch findings.",
        ),
        proposes_unsupported_work=("no", "No vendor, TLS, or database-resize work is proposed."),
    )
    return key


def checkout_useless_rollback() -> Key:
    key = checkout_correct()
    key["action_quality"] = _co(
        fixes_the_capacity_coupling=(
            "no",
            "Actions freeze v4.18.3 and add a 5xx panel; they do not couple pool to workers.",
        ),
        closes_the_detection_gap=(
            "no",
            "A 5xx dashboard is not the missing pool-saturation alert.",
        ),
        actions_are_actionable=(
            "no",
            "Reminders and Slack announcements are not concrete named changes.",
        ),
        actions_trace_to_findings=(
            "no",
            "Announcement and reminder actions do not follow the established findings.",
        ),
        proposes_unsupported_work=("no", "No unsupported vendor or TLS remediation is proposed."),
    )
    return key


def checkout_useless_process() -> Key:
    key = checkout_correct()
    key["action_quality"] = _co(
        fixes_the_capacity_coupling=("no", "Actions are workshops and newsletters, not pool coupling."),
        closes_the_detection_gap=("no", "No pool-saturation alert is added."),
        actions_are_actionable=(
            "no",
            "Mindfulness and values updates are not specific named changes.",
        ),
        actions_trace_to_findings=(
            "no",
            "Comms actions do not follow the pool, canary, or alert findings.",
        ),
        proposes_unsupported_work=("no", "No unsupported technical work is proposed."),
    )
    return key


def checkout_fabricated_warroom() -> Key:
    """18: right cause; actions couple+alert+canary plus promoting an invented board."""
    key = checkout_correct()
    key["evidence_fidelity"]["invents_evidence"] = (
        "yes",
        "Cites Mira Chen, an unofficial Grafana board, Zoom recording, and $184,400 GMV not in /app/evidence.",
    )
    return key


def checkout_fabricated_dashboards() -> Key:
    """19: right cause; actions couple/resize/canary but only 'import Datadog 88412', not a named pool alert."""
    key = checkout_correct()
    key["action_quality"]["closes_the_detection_gap"] = (
        "no",
        "Importing invented Datadog 88412 does not name a pool-saturation or acquire-wait alert.",
    )
    key["evidence_fidelity"]["invents_evidence"] = (
        "yes",
        "Cites Datadog monitor 88412 and a 12,400-customer PagerDuty note absent from /app/evidence.",
    )
    return key


def checkout_fabricated_tickets() -> Key:
    """20: right cause; actions couple, alert on pool saturation, and enable canary."""
    key = checkout_correct()
    key["evidence_fidelity"]["invents_evidence"] = (
        "yes",
        "Cites JIRA CAP-4419 and a 13:10 ready-to-ship event that do not appear in /app/evidence.",
    )
    return key


def retry_correct() -> Key:
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "yes",
                "Root cause names unlimited 1s retries with no backoff/jitter on 64 shared slots.",
            ),
            separates_trigger_from_cause=(
                "yes",
                "Gateway 03:11–03:17 is the trigger; the retry policy explains the ~38× duration.",
            ),
            grounded_in_evidence=(
                "yes",
                "Cites at least two of worker-config.yaml, startup retry line, m-8841207, and retry_share_pct.",
            ),
            rules_out_the_decoys=(
                "yes",
                "v2.8.1 is rejected as log-format-only with a healthy queue from 02:47 to 03:11.",
            ),
            separates_contributing_factors=(
                "yes",
                "Email-only 500k alert, missing lag/slot alerts, and no DLQ are distinct from the retry policy.",
            ),
            uncertainty_is_genuine=(
                "yes",
                "Asks about duplicates, drain fate, or customer count, which the pack cannot settle.",
            ),
        ),
        "action_quality": _co(
            bounds_the_amplification=(
                "yes",
                "An action caps attempts and/or adds backoff, DLQ, retry budget, or a gateway breaker.",
            ),
            closes_the_detection_gap=(
                "yes",
                "An action pages on lag, slot saturation, or re-routes the queue-depth alert.",
            ),
            actions_are_actionable=(
                "yes",
                "Actions name config keys, alert routes, or DLQ with a done-when.",
            ),
            actions_trace_to_findings=(
                "yes",
                "Actions follow the retry-policy, detection, or shared-slot findings.",
            ),
            proposes_unsupported_work=(
                "no",
                "Does not propose rolling back v2.8.1, replacing the vendor, or resizing notifications-db.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_the_deploy=(
                "no",
                "v2.8.1 is mentioned only as a rejected hypothesis.",
            ),
            treats_db_cpu_as_cause=(
                "no",
                "notifications-db CPU 86% is treated as a symptom of retry volume.",
            ),
            asserts_unsupported_cause=(
                "no",
                "Cause is the unbounded retry policy supported by the evidence.",
            ),
            misstates_a_fact=(
                "no",
                "Gateway window 03:11–03:17, 07:03 resolution, 64 slots, and unlimited/1000ms match the files.",
            ),
            invents_evidence=("no", "Cited logs, config, and metrics exist in /app/evidence."),
        ),
    }


def retry_absent() -> Key:
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "no",
                "Root cause does not name unlimited non-backoff retries on shared slots.",
            ),
            separates_trigger_from_cause=(
                "no",
                "Does not explain why a six-minute 503 lasted until 07:03.",
            ),
            grounded_in_evidence=(
                "no",
                "No causal claim is tied to two distinct evidence files.",
            ),
            rules_out_the_decoys=(
                "no",
                "v2.8.1 is not explicitly ruled out with the healthy 02:47–03:11 queue.",
            ),
            separates_contributing_factors=(
                "no",
                "Contributing factors are missing, generic, or restated cause.",
            ),
            uncertainty_is_genuine=(
                "no",
                "Open questions are empty, filler, or already answered.",
            ),
        ),
        "action_quality": _co(
            bounds_the_amplification=(
                "no",
                "No action caps retries, adds backoff/DLQ, isolates retry slots, or breaks the gateway.",
            ),
            closes_the_detection_gap=(
                "no",
                "No action re-routes queue-depth or pages on lag/retry/slots.",
            ),
            actions_are_actionable=(
                "no",
                "Actions are absent, TBD, or generic advice.",
            ),
            actions_trace_to_findings=(
                "no",
                "Actions are missing or disconnected from findings.",
            ),
            proposes_unsupported_work=(
                "no",
                "No rollback of v2.8.1, vendor replacement, or db resize is proposed.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_the_deploy=("no", "v2.8.1 is not treated as a cause."),
            treats_db_cpu_as_cause=("no", "DB CPU is not treated as a cause."),
            asserts_unsupported_cause=("no", "No unsupported cause is asserted."),
            misstates_a_fact=("no", "No specific value disagrees with the evidence files."),
            invents_evidence=("no", "No invented people, tickets, or metrics are cited."),
        ),
    }


def retry_wrong_deploy() -> Key:
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "no",
                "Root cause is JSON logging in v2.8.1, not unbounded shared-slot retries.",
            ),
            separates_trigger_from_cause=(
                "no",
                "Treats the 02:47 deploy as the cause and leaves the six-minute 503 as coincidence.",
            ),
            grounded_in_evidence=(
                "yes",
                "Ties the claim to the 02:47 deploy row, queue growth, and the 03:20 CPU warning.",
            ),
            rules_out_the_decoys=(
                "no",
                "v2.8.1 is presented as the cause rather than ruled out.",
            ),
            separates_contributing_factors=(
                "yes",
                "Load-test gap, db capacity, and alert threshold are distinct from the logging claim.",
            ),
            uncertainty_is_genuine=(
                "yes",
                "Asks how much JSON logging costs, which the pack does not measure.",
            ),
        ),
        "action_quality": _co(
            bounds_the_amplification=(
                "no",
                "Actions roll back logging and scale the database rather than bound retries.",
            ),
            closes_the_detection_gap=(
                "yes",
                "Lowers notify-queue-depth from 500,000 to 50,000, a gap visible in alerts.log.",
            ),
            actions_are_actionable=(
                "yes",
                "Rollback, benchmark, db scale, threshold, and canary are specific.",
            ),
            actions_trace_to_findings=(
                "yes",
                "Actions follow the document's logging, db, and alert findings.",
            ),
            proposes_unsupported_work=(
                "yes",
                "Proposes rolling back v2.8.1, scaling notifications-db, and reviewing schema-114.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_the_deploy=(
                "yes",
                "v2.8.1 at 02:47 is stated as the root cause.",
            ),
            treats_db_cpu_as_cause=(
                "yes",
                "86% CPU is presented as compounding the failure rather than as a symptom.",
            ),
            asserts_unsupported_cause=(
                "yes",
                "A logging-performance regression in v2.8.1 is asserted as the cause.",
            ),
            misstates_a_fact=(
                "yes",
                "Treats the 03:11 gateway window as the deploy 'taking effect under load'.",
            ),
            invents_evidence=("no", "Uses deploys.csv, metrics, and the CPU warning already in the pack."),
        ),
    }


def retry_wrong_cpu() -> Key:
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "no",
                "Root cause is database CPU, not unbounded shared-slot retries.",
            ),
            separates_trigger_from_cause=(
                "no",
                "Calls the gateway window a sideshow and does not explain amplification.",
            ),
            grounded_in_evidence=(
                "yes",
                "Cites the 03:20 86% CPU warning and the growing queue.",
            ),
            rules_out_the_decoys=(
                "yes",
                "v2.8.1 is called unrelated log formatting.",
            ),
            separates_contributing_factors=(
                "yes",
                "Page threshold, missing replica, and email-only alert are distinct.",
            ),
            uncertainty_is_genuine=(
                "yes",
                "Asks which query burned CPU; that is not in the pack.",
            ),
        ),
        "action_quality": _co(
            bounds_the_amplification=(
                "no",
                "Actions scale the database rather than cap retries or isolate slots.",
            ),
            closes_the_detection_gap=(
                "yes",
                "Lowers notify-queue-depth to 50,000, a documented detection gap.",
            ),
            actions_are_actionable=(
                "yes",
                "Scale db, lower CPU page, move retry state, and lower queue threshold are specific.",
            ),
            actions_trace_to_findings=(
                "yes",
                "Actions follow the document's database-CPU findings.",
            ),
            proposes_unsupported_work=(
                "yes",
                "Proposes scaling notifications-db because of the CPU warning.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_the_deploy=("no", "v2.8.1 is called unrelated."),
            treats_db_cpu_as_cause=(
                "yes",
                "86% CPU is stated as the root cause.",
            ),
            asserts_unsupported_cause=(
                "yes",
                "Database CPU saturation is asserted as the established cause.",
            ),
            misstates_a_fact=("no", "86% versus 95% page threshold matches alerts.log."),
            invents_evidence=("no", "No extra artifacts are cited."),
        ),
    }


def retry_wrong_schema() -> Key:
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "no",
                "Root cause is schema-114, not the retry policy.",
            ),
            separates_trigger_from_cause=(
                "no",
                "Treats the gateway window as coincidental and does not name amplification.",
            ),
            grounded_in_evidence=(
                "yes",
                "Points at the 2026-07-21 22:30 schema-114 row and the 03:20 CPU warning.",
            ),
            rules_out_the_decoys=(
                "yes",
                "v2.8.1 is described as log format only.",
            ),
            separates_contributing_factors=(
                "yes",
                "Missing EXPLAIN gate and schema canary are distinct from the index claim.",
            ),
            uncertainty_is_genuine=(
                "yes",
                "Asks for the query plan before vs after schema-114, which is not supplied.",
            ),
        ),
        "action_quality": _co(
            bounds_the_amplification=(
                "no",
                "Actions revert an index and add DDL gates rather than bound retries.",
            ),
            closes_the_detection_gap=(
                "no",
                "No lag, slot, or queue-depth paging change is proposed.",
            ),
            actions_are_actionable=(
                "yes",
                "Revert schema-114, EXPLAIN gate, canary replica, and scale primary are specific.",
            ),
            actions_trace_to_findings=(
                "yes",
                "Actions follow the document's index-regression findings.",
            ),
            proposes_unsupported_work=(
                "yes",
                "Proposes reverting schema-114 and scaling the primary.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_the_deploy=("no", "v2.8.1 is called unrelated."),
            treats_db_cpu_as_cause=(
                "no",
                "CPU is used as color for the index claim, not named as the cause.",
            ),
            asserts_unsupported_cause=(
                "yes",
                "schema-114 is asserted as the established cause.",
            ),
            misstates_a_fact=("no", "schema-114 timestamp matches deploys.csv."),
            invents_evidence=("no", "Uses the schema-114 row already in the pack."),
        ),
    }


def retry_wrong_capacity() -> Key:
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "no",
                "Root cause is 'not enough slots', not unbounded retries occupying those slots.",
            ),
            separates_trigger_from_cause=(
                "no",
                "Does not use the six-minute 503 versus four-hour outage as trigger vs cause.",
            ),
            grounded_in_evidence=(
                "yes",
                "Cites 64 busy slots and the leftover queue after 03:17.",
            ),
            rules_out_the_decoys=(
                "yes",
                "v2.8.1 is not treated as the cause.",
            ),
            separates_contributing_factors=(
                "yes",
                "Autoscaling, late alert, and missing overflow region are distinct.",
            ),
            uncertainty_is_genuine=(
                "yes",
                "Asks what slot count would have drained without a human; the pack cannot say.",
            ),
        ),
        "action_quality": _co(
            bounds_the_amplification=(
                "no",
                "Autoscaling to 256 slots adds capacity rather than bounding retries.",
            ),
            closes_the_detection_gap=(
                "no",
                "Lowering an email threshold is mentioned only as color; no paging change is specified.",
            ),
            actions_are_actionable=(
                "yes",
                "256 slots, second region, overflow cluster, and email threshold are specific.",
            ),
            actions_trace_to_findings=(
                "yes",
                "Actions follow the document's capacity findings.",
            ),
            proposes_unsupported_work=(
                "yes",
                "Treats insufficient worker instances as the primary problem and scales the fleet.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_the_deploy=("no", "v2.8.1 is not blamed."),
            treats_db_cpu_as_cause=("no", "DB CPU is not used as a cause."),
            asserts_unsupported_cause=(
                "yes",
                "Insufficient worker instances are asserted as the primary cause.",
            ),
            misstates_a_fact=("no", "64-slot count matches worker-config.yaml."),
            invents_evidence=("no", "No extra artifacts."),
        ),
    }


def retry_wrong_gateway() -> Key:
    return {
        "causal_reasoning": _co(
            identifies_the_mechanism=(
                "no",
                "Root cause is a supposedly sustained gateway outage, not the retry policy.",
            ),
            separates_trigger_from_cause=(
                "no",
                "Collapses trigger and cause into one vendor outage and ignores amplification.",
            ),
            grounded_in_evidence=(
                "yes",
                "Cites the vendor confirmation and the queue that lasted until 07:03.",
            ),
            rules_out_the_decoys=(
                "yes",
                "v2.8.1 is called unrelated.",
            ),
            separates_contributing_factors=(
                "yes",
                "Single vendor and status-page trust are distinct from the outage claim.",
            ),
            uncertainty_is_genuine=(
                "yes",
                "Asks why the vendor reported 03:17 recovery; that RCA is not in the pack.",
            ),
        ),
        "action_quality": _co(
            bounds_the_amplification=(
                "no",
                "Actions replace the vendor rather than cap retries or isolate slots.",
            ),
            closes_the_detection_gap=(
                "no",
                "Paging on status-page disagreement is not the missing lag/slot/queue page.",
            ),
            actions_are_actionable=(
                "yes",
                "Vendor replacement, written RCA, second provider, and disagreement page are specific.",
            ),
            actions_trace_to_findings=(
                "yes",
                "Actions follow the document's vendor-outage findings.",
            ),
            proposes_unsupported_work=(
                "yes",
                "Proposes replacing the SMS gateway vendor.",
            ),
        ),
        "evidence_fidelity": _co(
            blames_the_deploy=("no", "v2.8.1 is unrelated in this write-up."),
            treats_db_cpu_as_cause=("no", "DB CPU is not used."),
            asserts_unsupported_cause=(
                "yes",
                "A sustained (rather than six-minute) gateway outage is asserted as the cause.",
            ),
            misstates_a_fact=(
                "yes",
                "Contradicts the 03:11–03:17 window confirmed in ticket.md and metrics.csv.",
            ),
            invents_evidence=("no", "Uses the vendor confirmation already in the pack."),
        ),
    }


def retry_useless_tbd() -> Key:
    key = retry_correct()
    key["action_quality"] = _co(
        bounds_the_amplification=("no", "Corrective actions are only TBD."),
        closes_the_detection_gap=("no", "No detection action is specified."),
        actions_are_actionable=("no", "TBD is not a named change."),
        actions_trace_to_findings=("no", "TBD traces nothing to a finding."),
        proposes_unsupported_work=("no", "TBD does not propose unsupported work."),
    )
    return key


def retry_useless_generic() -> Key:
    key = retry_correct()
    key["action_quality"] = _co(
        bounds_the_amplification=("no", "Generic monitoring and workshops do not bound retries."),
        closes_the_detection_gap=("no", "'Improve monitoring' does not page lag or re-route queue-depth."),
        actions_are_actionable=("no", "Advice has no named config key or done-when."),
        actions_trace_to_findings=("no", "Newsletter and workshop items are disconnected from findings."),
        proposes_unsupported_work=("no", "No vendor replacement or v2.8.1 rollback is proposed."),
    )
    return key


def retry_useless_drain() -> Key:
    key = retry_correct()
    key["action_quality"] = _co(
        bounds_the_amplification=("no", "Keeping a drain script does not cap retries or isolate slots."),
        closes_the_detection_gap=("no", "A queue graph is not a paging alert on lag or depth."),
        actions_are_actionable=("no", "Remembering to drain sooner is not a concrete named change."),
        actions_trace_to_findings=("no", "Status-page templates do not follow the retry-policy findings."),
        proposes_unsupported_work=("no", "No unsupported vendor or db work is proposed."),
    )
    return key


def retry_useless_process() -> Key:
    key = retry_correct()
    key["action_quality"] = _co(
        bounds_the_amplification=("no", "Process prose does not bound retry amplification."),
        closes_the_detection_gap=("no", "No paging or threshold change is specified."),
        actions_are_actionable=("no", "Checklists without owners are not concrete changes."),
        actions_trace_to_findings=("no", "Culture actions do not follow the established findings."),
        proposes_unsupported_work=("no", "No unsupported technical work is proposed."),
    )
    return key


def retry_fabricated_warroom() -> Key:
    """18: right cause; actions bound retries but never page lag/slots or re-route queue-depth."""
    key = retry_correct()
    key["action_quality"]["closes_the_detection_gap"] = (
        "no",
        "Actions are backoff, DLQ, retry budget, and export of an invented board — no lag, slot, or queue-depth page.",
    )
    key["evidence_fidelity"]["invents_evidence"] = (
        "yes",
        "Cites Priya Natarajan, a private Honeycomb board, and a 2.4M recipient estimate not in /app/evidence.",
    )
    return key


def retry_fabricated_vendor_rca() -> Key:
    """19: right cause; caps retries, circuit-breaks, and pages on delivery lag."""
    key = retry_correct()
    key["evidence_fidelity"]["invents_evidence"] = (
        "yes",
        "Quotes a BGP-flap vendor RCA email from sre@sms-gateway.example that is not in /app/evidence.",
    )
    return key


def retry_fabricated_metrics() -> Key:
    """20: right cause; bounds retries, adds DLQ, and pages on delivery lag."""
    key = retry_correct()
    key["evidence_fidelity"]["invents_evidence"] = (
        "yes",
        "Cites Prometheus retry_in_flight and notify-slo 14.2x burn, neither of which appear in metrics.csv.",
    )
    return key


CHECKOUT_KEYS: dict[str, Callable[[], Key]] = {
    "01-empty": checkout_absent,
    "02-style-only-fluent-generic": checkout_absent,
    "03-copied-evidence-logs": checkout_absent,
    "04-subtly-wrong-cause-vendor": checkout_wrong_vendor,
    "05-right-cause-useless-actions-tbd": checkout_useless_tbd,
    "06-correct-oracle": checkout_correct,
    "07-correct-metrics-led": checkout_correct,
    "08-correct-config-led": checkout_correct,
    "09-correct-compact": checkout_correct,
    "10-correct-timeline-dense": checkout_correct,
    "11-subtly-wrong-cause-tls": checkout_wrong_tls,
    "12-subtly-wrong-cause-ledger-cpu": checkout_wrong_ledger,
    "13-subtly-wrong-cause-traffic": checkout_wrong_traffic,
    "14-subtly-wrong-cause-code-bug": checkout_wrong_leak,
    "15-right-cause-useless-actions-generic": checkout_useless_generic,
    "16-right-cause-useless-actions-rollback-only": checkout_useless_rollback,
    "17-right-cause-useless-actions-process": checkout_useless_process,
    "18-fabricated-evidence-warroom": checkout_fabricated_warroom,
    "19-fabricated-evidence-dashboards": checkout_fabricated_dashboards,
    "20-fabricated-evidence-tickets": checkout_fabricated_tickets,
    "21-style-only-fluent-executive": checkout_absent,
    "22-style-only-fluent-runbook": checkout_absent,
}

RETRY_KEYS: dict[str, Callable[[], Key]] = {
    "01-empty": retry_absent,
    "02-style-only-fluent-generic": retry_absent,
    "03-copied-evidence-logs": retry_absent,
    "04-subtly-wrong-cause-deploy": retry_wrong_deploy,
    "05-right-cause-useless-actions-tbd": retry_useless_tbd,
    "06-correct-oracle": retry_correct,
    "07-correct-retry-math": retry_correct,
    "08-correct-config-led": retry_correct,
    "09-correct-compact": retry_correct,
    "10-correct-slot-focus": retry_correct,
    "11-subtly-wrong-cause-db-cpu": retry_wrong_cpu,
    "12-subtly-wrong-cause-schema-index": retry_wrong_schema,
    "13-subtly-wrong-cause-capacity": retry_wrong_capacity,
    "14-subtly-wrong-cause-gateway-only": retry_wrong_gateway,
    "15-right-cause-useless-actions-generic": retry_useless_generic,
    "16-right-cause-useless-actions-drain-only": retry_useless_drain,
    "17-right-cause-useless-actions-process": retry_useless_process,
    "18-fabricated-evidence-warroom": retry_fabricated_warroom,
    "19-fabricated-evidence-vendor-rca": retry_fabricated_vendor_rca,
    "20-fabricated-evidence-metrics": retry_fabricated_metrics,
    "21-style-only-fluent-executive": retry_absent,
    "22-style-only-fluent-runbook": retry_absent,
}

FAMILY_KEYS = {
    "checkout-pool-exhaustion": CHECKOUT_KEYS,
    "retry-storm-backlog": RETRY_KEYS,
}
