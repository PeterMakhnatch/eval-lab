### World HTTP API (`$WORLD`, authenticated with `$SEAT_TOKEN`)

Every request requires your Bearer token header: `-H "Authorization: Bearer $SEAT_TOKEN"`. All `POST` endpoints forbid extra JSON keys (`HTTP 400` on unknown fields).

1. Inspect world rules, handoff snapshot, and remaining budgets (does not advance step counters):
   `curl -s "$WORLD/rules" -H "Authorization: Bearer $SEAT_TOKEN"`

2. Read live world state (advances `state_reads` and triggers any scheduled mid-flight transition; optional `?read=<token>` makes network retries of the same logical read idempotent):
   `curl -s "$WORLD/state" -H "Authorization: Bearer $SEAT_TOKEN"`
   Returns `task_id`, `check_id`, `domain`, `state_version`, `active_target`, `active_param`, `drift_happened`, `state_reads`, and `executed`.

3. Read the shared channel stream (`p_prev` handoff note and any `p_peer` / `p_coord` notices):
   `curl -s "$WORLD/msgs" -H "Authorization: Bearer $SEAT_TOKEN"`
   Returns `{"messages": [{"id": ..., "from": ..., "role": ..., "text": ...}], "last_id": ...}`.

4. Reconcile conflicting upstream handoffs with coordinator `p_coord` (budget: `1` call; free when resolving an active conflict, `0.2` fee outside conflict):
   `curl -s -X POST "$WORLD/verify_dependency" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"check_id": "{check_id}"}'`
   Exact key: `check_id` only. Returns `check_id`, `task_id`, `coordinator`, `conflict_detected`, `reconciled`, `fee_applied`, `next_step`, and `budget_left`. Re-read `GET /state` afterwards to inspect the authoritative `(active_target, active_param, state_version)`.

5. Commit the live authoritative target, parameter, and version (budget: `1` terminal call):
   `curl -s -X POST "$WORLD/execute" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"target": "<active_target>", "param": <active_param>, "version": <state_version>}'`
   Exact keys: `target` (non-empty string), `param` (integer), and `version` (positive integer).

6. Optional channel message post (budget: `16` calls, `1..500` clean characters):
   `curl -s -X POST "$WORLD/msg" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"text": "..."}'`
   Exact key: `text` only.
