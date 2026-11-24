### World HTTP API (`$WORLD`, authenticated with `$SEAT_TOKEN`)

Every request requires your Bearer token header: `-H "Authorization: Bearer $SEAT_TOKEN"`. All `POST` endpoints forbid extra JSON keys (`HTTP 400` on unknown fields).

1. Inspect world rules, candidate slots, `work_param`, and budgets (does not advance `tick`):
   `curl -s "$WORLD/rules" -H "Authorization: Bearer $SEAT_TOKEN"`

2. Read the shared channel stream (advances `tick` from `0` to `1` on initial sync only; poll `GET /leases` to advance lease ticks):
   `curl -s "$WORLD/msgs" -H "Authorization: Bearer $SEAT_TOKEN"`
   Returns `{"tick": ..., "messages": [{"id": ..., "from": ..., "role": ..., "text": ...}], "last_id": ...}`.

3. Inspect authoritative slot leases (advances `tick` by `1` and updates any logical expirations):
   `curl -s "$WORLD/leases" -H "Authorization: Bearer $SEAT_TOKEN"`
   Returns `task_id`, `tick`, `lease_reads`, `collisions_recorded`, and `leases` (each with `slot`, `priority`, `candidate_for_p1`, `status`, `holder`, `lease_id`, `acquired_at_tick`, `expires_at_tick`, `heartbeat_active`, `claimable`, `requires_force_expired`, `released_by`, `released_at_tick`, `expired_at_tick`, `work_completed`).

4. Acquire an exclusive lease on a free, released, or logically expired candidate slot (budget: `1` call):
   `curl -s -X POST "$WORLD/claim" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"slot": "<slot>"}'`
   When (and only when) reclaiming a slot whose `GET /leases` `status` is `"expired"`, pass `"force_expired": true` (`HTTP 409` on non-expired slots):
   `curl -s -X POST "$WORLD/claim" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"slot": "<slot>", "force_expired": true}'`

5. Execute the assigned work under your active owned `lease_id` (budget: `1` call):
   `curl -s -X POST "$WORLD/work" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"slot": "<slot>", "lease_id": "<lease_id>", "param": {work_param}}'`
   Exact keys: `slot` (string), `lease_id` (string), and `param` (integer `{work_param}`).

6. Release your lease cleanly after `POST /work` completes (budget: `1` terminal call):
   `curl -s -X POST "$WORLD/release" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"slot": "<slot>", "lease_id": "<lease_id>"}'`
   Exact keys: `slot` (string) and `lease_id` (string).

7. Optional channel message post (budget: `16` calls, `1..500` clean characters):
   `curl -s -X POST "$WORLD/msg" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"text": "..."}'`
   Exact key: `text` only.
