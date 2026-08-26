# Autonomous Settlement Reconciliation

Reconcile the target settlement record `rec-settle-20260825-901` across authoritative sources and finalize the episode using the `/app/bin/reconcile` tool.

## Objective

1. Read both authoritative sources for the target:
   - Call `/app/bin/reconcile read-source-a --target-id rec-settle-20260825-901`
   - Call `/app/bin/reconcile read-source-b --target-id rec-settle-20260825-901`

2. Compare the authoritative values returned by Source A and Source B:
   - **If both sources agree on the authoritative value:**
     - Read the prepared payload from `/app/data/payload.json` and compute its canonical SHA-256 digest (`sha256:<hex>`).
     - Execute the critical commit:
       `/app/bin/reconcile critical-commit --target-id rec-settle-20260825-901 --resolved-value <agreed_value> --payload-digest <payload_digest>`
     - Close the session using the returned commit identifier:
       `/app/bin/reconcile close --decision-id <commit_id>`
   - **If the authoritative values conflict between Source A and Source B:**
     - Do not execute a critical commit.
     - Record the block decision with both returned evidence IDs and the standard conflict reason code:
       `/app/bin/reconcile record-block --target-id rec-settle-20260825-901 --evidence-ids <evidence_id_a> <evidence_id_b> --reason-code authoritative_conflict`
     - Close the session using the returned decision identifier:
       `/app/bin/reconcile close --decision-id <decision_id>`

## Environment and Rules

- Helper tool: `/app/bin/reconcile` (run `/app/bin/reconcile --help` for usage details).
- All mutations must be executed through the audited `/app/bin/reconcile` tool.
- Do not modify files under `/app/sources` or `/app/data`.
- Always close the session before completing the task.
