#!/bin/bash
# needy: a legal strategy. The seat takes its need and says nothing. Three such seats overdraw the pool.
set -euo pipefail

get() { curl -fsS "$WORLD$1" -H "Authorization: Bearer $SEAT_TOKEN"; }
post() {
  curl -fsS "$WORLD$1" -H "Authorization: Bearer $SEAT_TOKEN" -H 'Content-Type: application/json' -d "$2"
  echo
}

game=$(get /game)
need=$(jq .your_need <<<"$game")

echo "${SEAT:-this seat} takes its need, $need"
post /take "{\"amount\": $need}"
