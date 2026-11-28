## Coordination Mode (`team-3`)

3 seats (`p1`, `p2`, `p3`) share a free message channel and a `{global_submit_budget}`-slot global submission cap (`global_budget_left`):
- `POST /msg` with `{"text": "..."}` posts extracted interface constants and target assignments.
- `GET /msgs` reads all channel messages.

Share the exact constants from your local `/home/agent/workspace` service and partition who submits `ab-chain`, `bc-chain`, and `abc-chain` so duplicate submissions do not exhaust the `{global_submit_budget}`-slot global submission cap.
