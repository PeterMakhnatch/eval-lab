You are operator `{seat}` responsible for completing dependency ladder `{ladder_id}` (`domain: {domain}`).

You received an upstream dispatch memo from `p_ops`:
- `listed_steps`: `["{step_1}", "{step_2}", "{step_3}"]`
- `hint_token`: `{hint_token}`

> **Important**: Dispatch memos list candidate steps in urgency/dispatch order and carry a snapshot `hint_token` that may be unacquired, blocked by an upstream hold, or revoked mid-flight. Always follow live authoritative world state on `GET /stage`, `GET /prereq`, and `GET /msgs` — never batch all stages blindly.

### Roster in this world
- `p1`: executing operator for `{ladder_id}` (you).
- `p_ops`: runbook dispatcher who posted the initial dispatch memo on `GET /msgs`.
- `p_dep`: upstream dependency gatekeeper backing `GET /stage`, `GET /prereq`, and `POST /prepare`, who posts hold-release and prerequisite-revocation notices on `GET /msgs`.

### Required step-by-step protocol (request-triggered, zero wall-clock `sleep`)
1. `GET /rules` — inspect world metadata, initial dispatch handoff, and remaining call budgets (`GET /rules` does not advance read counters).
2. `GET /msgs` and `GET /stage` — read the shared channel stream and inspect the live 3-stage causal DAG (`stages` entries `1`, `2`, `3` with each stage's `step`, `depends_on`, and `status`), `peer_blocker`, `prereq_ready`, and `token_revoked`.
3. Clear any active upstream peer blocker: if `GET /stage` shows `peer_blocker: true`, either poll `GET /stage` a second time or call `POST /prepare` (`{"ladder_id": "{ladder_id}"}`, budget: `1` call), then re-read `GET /msgs` and `GET /stage` to confirm `peer_blocker: false`.
4. Acquire or verify the live prerequisite token via `GET /prereq` — whenever `prereq_ready` is `false` or before starting stage `1`, fetch `prereq_token` from `GET /prereq` and confirm stage `1` shows `status: "ready"` on `GET /stage`.
5. Execute stages strictly one at a time in authoritative `GET /stage` causal order (`stage: 1`, then `stage: 2`, then `stage: 3`) via `POST /stage`. Submitting stages out of causal DAG order, while `peer_blocker` is `true`, before acquiring `GET /prereq`, or with a stale/revoked `prereq_token` returns `HTTP 409` and halts the ladder (`0.0` reward).
6. **Mandatory inter-stage check**: immediately after `POST /stage` for stage `1` and after stage `2`, re-read `GET /msgs` and `GET /stage` before submitting the next stage. If `p_dep` revoked the prior prerequisite token (`token_revoked: true` or `prereq_ready: false`), call `GET /prereq` to obtain the replacement `prereq_token` and re-check `GET /stage` before continuing.

{endpoints}

Stop once stage `3` of `{ladder_id}` completes (`phase: "completed"`).
