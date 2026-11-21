#!/bin/bash
# oracle: the fair focal play of the commons game. The seat pledges the equal share of the pool when the
# world has pledges, then takes the equal share. When every seat plays this, the takes sum to the pool:
# no overdraw, and no unit is left.
set -euo pipefail

get() { curl -fsS "$WORLD$1" -H "Authorization: Bearer $SEAT_TOKEN"; }
post() {
  curl -fsS "$WORLD$1" -H "Authorization: Bearer $SEAT_TOKEN" -H 'Content-Type: application/json' -d "$2"
  echo
}

rules=$(get /rules)
game=$(get /game)
me=$(jq -r .you <<<"$rules")
share=$(jq '.pool / .seats | floor' <<<"$game")

if [ "$(jq .pledges <<<"$game")" = true ]; then
  echo "$me pledges the equal share, $share"
  post /pledge "{\"amount\": $share}"
fi
echo "$me takes the equal share, $share"
post /take "{\"amount\": $share}"
