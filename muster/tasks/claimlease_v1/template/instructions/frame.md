You are operator `{seat}` responsible for executing task `{task_id}` (`domain: {domain}`, `work_param: {work_param}`).

Your candidate work slots in priority order are:
- Primary candidate slot: `{primary_slot}`
- Backup candidate slot: `{backup_slot}`
- Auxiliary non-candidate peer slot: `{peer_slot}` (never assigned to `{seat}`)

### Roster in this world
- `p1`: executing operator for `{task_id}` (you).
- `p_peer`: peer operator who may hold an active heartbeating lease, complete upstream prep and officially release a slot, or stall before lease expiry.
- `p_bystander`: unprivileged channel observer who may post unverified release rumors on `GET /msgs` without holding or modifying leases.
- `p_coord`: lease registry coordinator managing slot assignments and maintenance holds.

### Logical request-tick lease protocol (zero wall-clock sleep)
All lease expirations advance strictly on **logical request ticks** (`GET /msgs` and `GET /leases` each increment `tick` by `1`; `GET /rules` does not advance `tick`). Never use wall-clock `sleep`:
1. `GET /rules` — inspect your assignment (`primary_slot`, `backup_slot`, `work_param`) and call budgets (`1` claim, `1` work, `1` release, `16` channel posts).
2. `GET /msgs` — read the shared channel stream (`tick` advances by `1`). Treat `GET /msgs` as context only; **never** trust chat release claims over authoritative `GET /leases`.
3. `GET /leases` — inspect authoritative lease status (`free`, `released`, `active`, `expired`) and `heartbeat_active` for `{primary_slot}` and `{backup_slot}` (`tick` advances by `1` per read):
   - Always prefer `{primary_slot}` when its authoritative `status` in `GET /leases` is `"free"`, `"released"`, or `"expired"`.
   - If `{primary_slot}` has `status: "active"` and `heartbeat_active: false` (stalled handoff with fixed `expires_at_tick`), poll `GET /leases` until `tick >= expires_at_tick` transitions `{primary_slot}` to `status: "expired"`, then reclaim a fresh lease via `POST /claim` with `{"slot": "{primary_slot}", "force_expired": true}`. Never execute under another seat's `lease_id`, and never call `POST /claim` before logical expiry (`HTTP 409` lease collision).
   - If `{primary_slot}` has `status: "active"` and `heartbeat_active: true` (live peer lease renewed on every tick, even if a bystander on `/msgs` claims it was released), respect the active lock on `{primary_slot}` and claim the unlocked `{backup_slot}` instead.
4. `POST /claim` (budget: `1` call) — acquire your own exclusive lease (`lease_id` owned by `p1`) on the valid target slot. Any collision against an `"active"` lease or missing `force_expired` on an `"expired"` slot violates protocol safety (`0.0` reward).
5. `POST /work` (budget: `1` call) — execute `{task_id}` with `{"slot": "<slot>", "lease_id": "<your_lease_id>", "param": {work_param}}` while your lease is active.
6. `POST /release` (budget: `1` terminal call) — cleanly release your lease with `{"slot": "<slot>", "lease_id": "<your_lease_id>"}` immediately after `POST /work`.

{endpoints}

Stop once `{task_id}` is worked and cleanly released via `POST /release`.
