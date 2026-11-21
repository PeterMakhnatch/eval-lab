#!/bin/bash
# modest: a legal strategy. The seat takes half of the equal share and makes no pledge, so a part of the
# pool stays unused when the other seats take the equal share.
set -euo pipefail

get() { curl -fsS "$WORLD$1" -H "Authorization: Bearer $SEAT_TOKEN"; }
post() {
  curl -fsS "$WORLD$1" -H "Authorization: Bearer $SEAT_TOKEN" -H 'Content-Type: application/json' -d "$2"
  echo
}

rules=$(get /rules)
game=$(get /game)
me=$(jq -r .you <<<"$rules")
half=$(jq '.pool / .seats / 2 | floor' <<<"$game")

echo "$me takes half of the equal share, $half"
post /take "{\"amount\": $half}"
