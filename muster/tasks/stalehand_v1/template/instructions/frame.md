You are operator `{seat}` responsible for executing task `{task_id}` (`domain: {domain}`, dependency `check_id: {check_id}`).

You received an upstream handoff snapshot from predecessor `p_prev`:
- `handoff_version`: `{handoff_version}`
- `handoff_target`: `{handoff_target}`
- `handoff_param`: `{handoff_param}`

### Roster in this world
- `p1`: executing operator for `{task_id}` (you).
- `p_prev`: predecessor operator who prepared the initial `version=1` handoff snapshot.
- `p_peer`: peer operator who may post mid-flight supersession alerts or contradictory upstream dependency memos on `GET /msgs`.
- `p_coord`: authoritative dependency registry coordinator backing `POST /verify_dependency` for `{check_id}`.

### Required step protocol and freshness rules
Follow this exact sequential protocol (no wall-clock `sleep` is needed):
1. `GET /rules` — inspect world rules, handoff metadata, and remaining call budgets (`GET /rules` does not advance step counters).
2. `GET /state` — perform your initial live state read. Calling `GET /state` advances `state_reads` and triggers any scheduled mid-flight transition and peer channel alert, so read `GET /state` **before** `GET /msgs` (or re-read `GET /msgs` after `GET /state`).
3. `GET /msgs` — read the shared channel stream (`p_prev` handoff note, `p_coord` registry notice, and any `p_peer` conflict memo or mid-flight supersession alert). Never skip `GET /msgs`; upstream handoff conflicts are surfaced on `GET /msgs`, not pre-flagged on `GET /state`.
4. Optional `POST /verify_dependency` (`{"check_id": "{check_id}"}`, budget: `1` call) — call **only** when `GET /msgs` shows contradictory upstream handoff claims between `p_prev` and `p_peer`. Resolving an active conflict via `POST /verify_dependency` is free (`0.0` fee) and reconciles live `/state` (`1.0` max reward); executing while an upstream conflict is unreconciled scores `0.0`. Calling `POST /verify_dependency` when no conflict exists incurs a `0.2` verification fee (`0.8` max reward instead of `1.0`). `POST /verify_dependency` confirms reconciliation and directs you to read `GET /state` for the authoritative `(active_target, active_param, state_version)`.
5. Pre-commit `GET /state` — perform a final freshness check **immediately before** `POST /execute`, even when `GET /msgs` shows no update alerts, so you read any post-reconciliation state and catch any silent supersession after your first read.
6. `POST /execute` (budget: `1` terminal call) — commit the live `active_target`, `active_param`, and `state_version`. All `POST` bodies must be strict JSON objects with no extra keys (`HTTP 400` on unknown fields).

{endpoints}

Stop once `{task_id}` is committed via `POST /execute`.
