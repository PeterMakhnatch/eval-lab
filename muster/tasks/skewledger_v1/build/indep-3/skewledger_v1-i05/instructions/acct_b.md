You are account operator `acct_b` for dual-account settlement task `skew-05-09b2d7` (`domain: omnibus-reserve`).

### Environment & Authentication
- World server URL: `$WORLD` (`http://world:8080`)
- Seat name: `$SEAT` (`acct_b`)
- Bearer token: `$SEAT_TOKEN` (`Authorization: Bearer $SEAT_TOKEN`)

### Your Assignment & Coupled Reserve Floor
- Assigned account: `acct_b` (peer initial `bal_a = 332`, initial `bal_b = 332`, total `664`)
- Coupled reserve floor across both accounts: `bal_a + bal_b >= 520` (`available_slack = 144`)
- Your private requested withdrawal obligation: `req_b = 116` (`acct_a` holds private `req_a`; `auditor` commits final settlement)
- Coupled equal-haircut allocation formula when `req_a + req_b > 144`:
  - `alloc_a = (req_a - req_b + 144) // 2`
  - `alloc_b = (req_b - req_a + 144) // 2`

### Protocol
1. Call `GET /rules` to inspect `channel`, `leases`, `fenced`, and `unread_guard`.
2. If `channel` is `true`:
   - Broadcast `req_b` via `POST /msg` (e.g. `{"text": "{\"kind\":\"obligation\",\"account\":\"acct_b\",\"req\":116}"}`).
   - Poll `GET /msgs` until `acct_a` posts `req_a` and compute `alloc_b`. Ignore unverified `bystander` rate rumors on `/msgs`.
3. Wait for `acct_a` to complete its transfer (`acct_a_done: true` on `GET /state`), then refresh state so you never commit a stale `expected_version`:
   - If `channel` is `true`, call `GET /msgs` so `unread_count` is `0` when `unread_guard` is active.
   - If `leases` is `true`, call `POST /claim` with `{"target": "acct_b"}` to obtain `lease_id`.
   - Query fresh `GET /state` **after** `acct_a_done` is `true` to obtain the updated `state_version`.
   - Call `POST /transfer` with `{"account": "acct_b", "amount": <alloc_b>, "expected_version": <state_version>}` (include `"lease_id"` if claimed).
   - If `leases` is `true`, release your lease via `POST /release` with `{"target": "acct_b", "lease_id": <lease_id>}`.
