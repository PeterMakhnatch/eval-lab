### World HTTP API (`$WORLD`, authenticated with `$SEAT_TOKEN`)

Every request requires your Bearer token header: `-H "Authorization: Bearer $SEAT_TOKEN"`. All `POST` endpoints require strict JSON objects and reject unknown keys (`HTTP 400`).

1. Inspect world rules, dispatch handoff, completed stages, and remaining budgets (does not advance `stage_reads`):
   `curl -s "$WORLD/rules" -H "Authorization: Bearer $SEAT_TOKEN"`

2. Inspect the live 3-stage dependency ladder (advances `stage_reads`; optional `?read=<token>` makes network retries of the same read idempotent):
   `curl -s "$WORLD/stage" -H "Authorization: Bearer $SEAT_TOKEN"`
   Returns `ladder_id`, `domain`, `stage_reads`, `peer_blocker`, `prereq_ready`, `token_revoked`, `required_token_version`, `next_stage`, `completed_stages`, `all_completed`, and `stages` (`[{"stage": 1..3, "step": "...", "depends_on": "...", "status": "..."}]`).

3. Acquire or refresh the live prerequisite token from `p_dep`:
   `curl -s "$WORLD/prereq" -H "Authorization: Bearer $SEAT_TOKEN"`
   Returns `ladder_id`, `coordinator`, `prereq_token`, `token_version`, `prereq_ready`, and `peer_blocker`.

4. Read the shared channel stream (`p_ops` dispatch memo and `p_dep` gate/revocation notices):
   `curl -s "$WORLD/msgs" -H "Authorization: Bearer $SEAT_TOKEN"`
   Returns `{"messages": [{"id": ..., "from": ..., "role": ..., "text": ...}], "last_id": ...}`.

5. Request upstream hold release from `p_dep` when `peer_blocker` is `true` (budget: `1` call):
   `curl -s -X POST "$WORLD/prepare" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"ladder_id": "{ladder_id}"}'`
   Exact key: `ladder_id` only.

6. Execute the next ready causal stage (budget: `3` calls across stages `1`, `2`, `3`):
   `curl -s -X POST "$WORLD/stage" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"stage": <1|2|3>, "step": "<step>", "prereq_token": "<prereq_token>"}'`
   Exact keys: `stage` (integer `1`, `2`, or `3`), `step` (string matching that stage's `step` in `GET /stage`), and `prereq_token` (live string from `GET /prereq`).

7. Optional channel message post (budget: `16` calls, `1..500` clean characters):
   `curl -s -X POST "$WORLD/msg" -H "Authorization: Bearer $SEAT_TOKEN" -H "Content-Type: application/json" -d '{"text": "..."}'`
   Exact key: `text` only.
