## Coordination Mode (`team-3-claimlock`)

3 seats (`p1`, `p2`, `p3`) share a message channel, a `{global_submit_budget}`-slot global submission cap, and server-side target claim locks (`max_claims = 2` active claims per seat):
- `POST /msg` with `{"text": "..."}` and `GET /msgs` exchange extracted service constants.
- `POST /claim` with `{"target": "<target_bug>"}`, `POST /release` with `{"target": "<target_bug>"}`, and `GET /claims` manage atomic target locks on `a-local`, `b-local`, `c-local`, `ab-chain`, `bc-chain`, and `abc-chain`.

Submitting or claiming a target already claimed by a peer or already found returns `409` (`already_claimed` or `already_found`) and spends `0` submission budget. Unused claims auto-release if a seat's per-seat submission budget reaches `0`. Claim your target before submitting.
