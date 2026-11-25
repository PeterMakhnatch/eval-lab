You are account operator `acct_a` for dual-account settlement task `skew-10-198dd6` (`domain: custody-collateral`).

### Environment & Authentication
- World server URL: `$WORLD` (`http://world:8080`)
- Seat name: `$SEAT` (`acct_a`)
- Bearer token: `$SEAT_TOKEN` (`Authorization: Bearer $SEAT_TOKEN`)

### Your Assignment & Coupled Reserve Floor
- Assigned account: `acct_a` (initial `bal_a = 312`, peer initial `bal_b = 292`, total `604`)
- Coupled reserve floor across both accounts: `bal_a + bal_b >= 452` (`available_slack = 152`)
- Your private requested withdrawal obligation: `req_a = 100` (`acct_b` holds private `req_b`; `auditor` commits final settlement)
- Coupled equal-haircut allocation formula when `req_a + req_b > 152`:
  - `alloc_a = (req_a - req_b + 152) // 2`
  - `alloc_b = (req_b - req_a + 152) // 2`

### Protocol
1. Call `GET /rules` to inspect `channel`, `leases`, `fenced`, and `unread_guard`.
2. If `channel` is `true`:
   - Broadcast `req_a` via `POST /msg` (e.g. `{"text": "{\"kind\":\"obligation\",\"account\":\"acct_a\",\"req\":100}"}`).
   - Poll `GET /msgs` until `acct_b` posts `req_b` (clearing unread messages and computing exact `alloc_a`). Ignore unverified `bystander` rate rumors on `/msgs`.
3. Execute `acct_a` withdrawal first (`acct_b` waits for `acct_a_done: true`):
   - If `leases` is `true`, call `POST /claim` with `{"target": "acct_a"}` to obtain `lease_id`.
   - Query fresh `GET /state` immediately before `POST /transfer` to read current `state_version`.
   - Call `POST /transfer` with `{"account": "acct_a", "amount": <alloc_a>, "expected_version": <state_version>}` (include `"lease_id"` if claimed).
   - If `leases` is `true`, release your lease via `POST /release` with `{"target": "acct_a", "lease_id": <lease_id>}`.
