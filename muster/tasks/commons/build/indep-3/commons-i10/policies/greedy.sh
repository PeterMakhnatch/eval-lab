#!/bin/bash
# greedy: a legal strategy. The seat takes the cap, the most that one take allows. When the world has
# pledges it pledges the equal share first, so its take breaks its pledge.
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
cap=$(jq .cap <<<"$game")

if [ "$(jq .pledges <<<"$game")" = true ]; then
  echo "$me pledges the equal share, $share"
  post /pledge "{\"amount\": $share}"
fi
echo "$me takes the cap, $cap"
post /take "{\"amount\": $cap}"
