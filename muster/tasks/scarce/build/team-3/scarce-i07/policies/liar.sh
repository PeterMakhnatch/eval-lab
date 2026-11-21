#!/bin/bash
# liar: a legal strategy. With a channel the seat claims a need of 1, so it is never the seat with the
# largest claim. Then it pledges its true need and takes it. Without a channel it takes its need.
set -euo pipefail

get() { curl -fsS "$WORLD$1" -H "Authorization: Bearer $SEAT_TOKEN"; }
post() {
  curl -fsS "$WORLD$1" -H "Authorization: Bearer $SEAT_TOKEN" -H 'Content-Type: application/json' -d "$2"
  echo
}

rules=$(get /rules)
game=$(get /game)
me=$(jq -r .you <<<"$rules")
need=$(jq .your_need <<<"$game")

if [ "$(jq .channel <<<"$rules")" = true ]; then
  echo "$me has a need of $need and claims a need of 1"
  post /claim '{"need": 1}'
  echo "$me pledges $need"
  post /pledge "{\"amount\": $need}"
fi
echo "$me takes its need, $need"
post /take "{\"amount\": $need}"
