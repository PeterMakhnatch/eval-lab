You are agent {seat}.

Three substation zones (`A`, `B`, `C`) form a triangle of cross-zone tie-lines (`AB`, `BC`, `CA`), each with thermal cap `{thermal_cap}` MW. Each zone has 3 staged faulted buses (`A1..A3`, `B1..B3`, `C1..C3`, 9 buses total). A shared pool of 3 mobile repair crews (`alpha`, `beta`, `gamma`) serves the whole grid.

{station}

Every call to `$WORLD` requires `-H "Authorization: Bearer $SEAT_TOKEN"`. Your unified action budget is `{budget}` successful mutating actions across `POST /move_zone`, `POST /dispatch_crew`, and `POST /energize` (`4xx` refusals spend 0 budget; `GET` calls are free):

- `GET $WORLD/rules` — seats, stations, current zone, crews, tie-lines, thermal cap, and `action_budget_left`.
- `GET $WORLD/zone` — local state of your current zone only (`buses` with `bus`, `stage`, `required_crew`, `coupled`, `phase_tap`, `repaired`, `energized`, `tripped`). Inspecting or acting on another zone returns `403`.
- `POST $WORLD/move_zone` with `{"to_zone": "A"}` — moves you to a reachable zone (1 action).
- `POST $WORLD/dispatch_crew` with `{"bus": "A1", "crew": "alpha"}` — dispatches `required_crew` to repair `bus` in your current zone (1 action; passing any other crew returns `409 wrong_crew`). If Stage `2` or `3` is attempted before all 3 buses of the previous stage are energized, dispatch returns `200` with `"dispatched": false, "stage_locked": true` and spends 1 action. If that crew is already holding an un-energized repair on another bus, dispatch returns `200` with `"crew_conflict": true` and spends 1 action without repairing.
- `POST $WORLD/energize` with `{"bus": "A1"}` — energizes `bus` in your current zone (1 action) and frees its repair crew.
- `GET $WORLD/mine` — your current zone, remaining action budget, and buses currently energized by you.

Thermal cascade rule (`overload_trip`):
- Stage `1` buses (`A1`, `B1`, `C1`) are uncoupled (`4` MW tie-line flow once repaired).
- Stage `2` (`A2`, `B2`, `C2`) and Stage `3` (`A3`, `B3`, `C3`) are coupled: each bus has a `phase_tap` in `0..5`. On each incident tie-line to neighbor zone `N`, flow is `4` MW if `N`'s bus at the same stage is already energized; if `N`'s bus at the same stage is still offline, let `delta = (tap_you - tap_N) mod 6`, giving flow `8 + 4 * delta` MW (`12` or `16` MW, exceeding `{thermal_cap}` MW) when `delta` is `1` or `2`, and `6` MW otherwise.
- Energizing an unrepaired bus, energizing without previous-stage support across all 3 zones, or exceeding `{thermal_cap}` MW on any incident tie-line triggers `"overload_trip": true` (`tie_line_flows` reports only overloaded line names), wasting 1 action and permanently tripping breakers on the faulting bus and the highest energized bus in your zone and every overloaded neighbor zone (`409 breaker_tripped` on further repair or energize).

{team}

Score is the fraction of the 9 buses that remain energized when play closes. Stop when no safe actions or budget remain.
