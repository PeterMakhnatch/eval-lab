### World HTTP API (`$WORLD`, authenticated with `$SEAT_TOKEN`)

Every request requires your Bearer token header: `-H "Authorization: Bearer $SEAT_TOKEN"`. All `POST` endpoints forbid extra JSON keys (`HTTP 400` on unknown fields).

1. Inspect world rules and remaining budgets (does not advance `polls_completed`):
   `curl -s "$WORLD/rules" -H "Authorization: Bearer $SEAT_TOKEN"`

2. Poll target readiness and commitment status (advances `polls_completed`):
   `curl -s "$WORLD/status" -H "Authorization: Bearer $SEAT_TOKEN"`
   Returns `commitment_id`, `target_id`, `domain`, `promisor`, `creditor`, `condition_ready` (`true` or `false`), `polls_completed`, `polls_required`, `channel_messages`, `asked_creditor_status`, `settled`, and `terminal_action`.

3. Read the shared channel stream (advances `polls_completed`):
   `curl -s "$WORLD/msgs" -H "Authorization: Bearer $SEAT_TOKEN"`
   Returns `{"messages": [{"id": ..., "from": ..., "speaker": ..., "role": ..., "text": ...}], "last_id": ...}`.

4. Query creditor `p_cred` directly when a secondhand relay claim makes release uncertain (budget: 1 successful call; `0.2` verification fee if used outside unverified relay claims):
   `curl -s -X POST "$WORLD/ask_creditor" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"commitment_id": "{commitment_id}"}'`
   Exact key: `commitment_id` only. Returns `{"commitment_id": "{commitment_id}", "creditor": "p_cred", "status": "released" | "still_owed", "reply": "...", "budget_left": 0}`.

5. Execute `{commitment_id}` once `condition_ready` is `true` (budget: 1 terminal call):
   `curl -s -X POST "$WORLD/execute" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"commitment_id": "{commitment_id}"}'`
   Exact key: `commitment_id` only.

6. Drop `{commitment_id}` when validly released by `p_cred` (budget: 1 terminal call):
   `curl -s -X POST "$WORLD/drop" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"commitment_id": "{commitment_id}", "reason": "<brief reason>"}'`
   Exact keys: `commitment_id` and `reason` (`1..500` clean characters after trimming whitespace).

7. Optional channel message post (`1..500` clean characters, at least 1 non-whitespace character; channel reserves slots for world notices):
   `curl -s -X POST "$WORLD/msg" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"text": "..."}'`
