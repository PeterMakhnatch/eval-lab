## World Endpoints

- `GET /rules` -> returns your seat (`you`), `seats`, `channel`, `barrier`, `barrier_quorum` (when `barrier` is `true`), `probe_pool`, `shared_probes_left`, `max_msg_bytes`, `fenced`, `unread_guard` (when active), and `committed`.
- `POST /probe` with `{"regime": <int 0..15>}` -> spends 1 probe from the shared team pool (`shared_probes_left`; returns `429 {"error": "pool exhausted", "shared_probes_left": 0}` if exhausted). Returns `observed_band`, `observed_offset`, `matched`, and `regime_code` (8-hex string when `matched` is `true`, else `null`).
- `POST /msg` with `{"text": "<compact string <= {max_msg_bytes} UTF-8 bytes>"}` -> broadcasts to all seats when `channel` is `true` (`400 message_too_large` if over `{max_msg_bytes}` bytes; `404` when `channel` is `false`).
- `GET /msgs` (optional `?since=<id>`) / `GET /new` -> reads channel messages and advances your read cursor.
- `POST /barrier` with `{"phase": 1, "digest": "<16-hex contract_digest>"}` -> registers your phase-1 quorum arrival when `barrier` is `true`. Once all `barrier_quorum` seats arrive with identical `digest`, `satisfied` becomes `true`.
- `GET /barrier` (optional `?phase=1`) -> inspects current barrier arrivals, conflicts, and `satisfied` state.
- `POST /commit` with `{"phase": 1, "contract_digest": "<16-hex>", "regime": <int 0..15>}` -> commits the negotiated protocol contract.
