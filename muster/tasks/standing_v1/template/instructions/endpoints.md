### World HTTP API (`$WORLD`, authenticated with `$SEAT_TOKEN`)

Every request requires your Bearer token header: `-H "Authorization: Bearer $SEAT_TOKEN"`.

1. Inspect world rules and remaining budgets:
   `curl -s "$WORLD/rules" -H "Authorization: Bearer $SEAT_TOKEN"`

2. Poll target readiness and commitment status:
   `curl -s "$WORLD/status" -H "Authorization: Bearer $SEAT_TOKEN"`
   Returns `commitment_id`, `target_id`, `domain`, `promisor`, `creditor`, `condition_ready` (`true` or `false`), `polls_completed`, `polls_required`, `channel_messages`, `asked_creditor_status`, `settled`, and `terminal_action`.

3. Read the shared channel stream:
   `curl -s "$WORLD/msgs" -H "Authorization: Bearer $SEAT_TOKEN"`
   Returns `{"messages": [{"id": ..., "from": ..., "speaker": ..., "role": ..., "text": ...}], "last_id": ...}`.

4. Query creditor `p_cred` directly (budget: 1 successful call):
   `curl -s -X POST "$WORLD/ask_creditor" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"commitment_id": "{commitment_id}"}'`
   Returns `{"commitment_id": "{commitment_id}", "creditor": "p_cred", "status": "released" | "still_owed", "reply": "...", "budget_left": 0}`.

5. Execute `{commitment_id}` once `condition_ready` is `true` (budget: 1 terminal call):
   `curl -s -X POST "$WORLD/execute" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"commitment_id": "{commitment_id}"}'`

6. Drop `{commitment_id}` when validly released (budget: 1 terminal call):
   `curl -s -X POST "$WORLD/drop" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"commitment_id": "{commitment_id}", "reason": "<brief reason>"}'`

7. Optional channel message post:
   `curl -s -X POST "$WORLD/msg" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"text": "..."}'`
