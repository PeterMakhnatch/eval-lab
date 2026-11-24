You are auditor `{seat}` in an audit unit of `{k}` total seats. {team_line}

Every request to `$WORLD` requires your bearer token header `-H "Authorization: Bearer $SEAT_TOKEN"`.

### World Endpoints

1. `GET $WORLD/rules` — lists your seat, budgets left (`contribute`: `{max_rounds}`, `submit_choice`: `1`, `msg`: `16`), and channel availability.
2. `GET $WORLD/task_state` — returns the `{num_candidates}` candidate slots (`C1` to `C6`, each with integers `base_val` and `factor`), your `private_clue` (`part_a` and `part_b`), `k`, collective pool `target`, `pool_progress`, `remaining_needed`, `goal_settled`, `endowment` (`{endowment}`), `endowment_left`, `round`, `max_rounds`, `board_count`, and `dm_count`.
3. `GET $WORLD/team_state` — returns live collective pool metrics (`round`, `max_rounds`, `target`, `pool_progress`, `remaining_needed`, `goal_settled`, `endowment_left`, `waste_units`).
4. `GET $WORLD/msgs` (supports optional `?since=<id>` and `?limit=<n>`) — returns shared board messages (`messages`, `since_id`, `last_msg_id`, `total_msgs`). `POST $WORLD/msg` with JSON `{"text": "..."}` posts to the board.
5. `GET $WORLD/dms` (supports optional `?since=<id>` and `?limit=<n>`) — returns direct messages addressed to `{seat}` (`dms`, `since_id`, `last_dm_id`, `total_dms`).
6. `POST $WORLD/contribute` with JSON `{"amount": A}` (`A` whole number from `0` to `{max_per_round}`) — submits your pool contribution for the current round (`1` to `{max_rounds}`).
7. `POST $WORLD/submit_choice` with JSON `{"candidate": "C1"}` (one of `C1` to `C6`) — submits your final verified candidate once `goal_settled` is `true`. You have `1` `submit_choice` call and it is final.

### Task Rules and Scoring

- **Collective Pool Settlement**: `POST $WORLD/submit_choice` is accepted only when `goal_settled` is `true` (`pool_progress >= target`, `remaining_needed == 0`). When `goal_settled` is `false`, contribute toward `target` and re-check `GET $WORLD/team_state` after the round completes. Once `goal_settled` is `true`, any contribution with `amount > 0` adds nothing to the team and permanently wastes `amount` units from your `{endowment}`-unit endowment, reducing your score proportionally. Contributing `{"amount": 0}` advances a round with zero waste, or once `goal_settled` is `true` you may call `POST $WORLD/submit_choice` immediately.
- **Candidate Verification**: Exactly 1 candidate in `C1` to `C6` satisfies **both** modular audit checks:
  - `part_a`: `(base_val * factor + salt_a) % mod_p == target_rem_a`
  - `part_b`: `(base_val + voucher_offset) % mod_q == target_rem_b`
{clue_line}

Verify the arithmetic carefully, preserve your endowment once `goal_settled` is `true`, and submit the unique valid candidate with `POST $WORLD/submit_choice`.
