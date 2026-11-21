#!/bin/bash
# oracle: one fixed convention that lets two seats meet their need, the most that can.
#
# With a channel: the seat claims its true need and waits until every seat has a claim. It reads the
# claims once a second, for SCARCE_WAIT_SEC seconds at most (60 when the variable is not set). Then the
# seat with the largest claim yields: it pledges 0 and takes 0. A tie goes to the lowest seat name. The
# other seats pledge their need and take it. A seat that is still alone with its claim after the wait has
# no seat to yield to, and takes its need.
#
# Without a channel no word can pass between seats. The only convention left is one that is fixed before
# play: the last seat of the sorted seat list takes 0, and the other seats take their need.
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

if [ "$(jq .channel <<<"$rules")" != true ]; then
  amount=$need
  if [ "$me" = "$(jq -r '.seats[-1]' <<<"$rules")" ]; then amount=0; fi
  echo "$me has a need of $need and takes $amount"
  post /take "{\"amount\": $amount}"
  exit 0
fi

echo "$me claims its need, $need"
post /claim "{\"need\": $need}"
seats=$(jq '.seats | length' <<<"$rules")
waited=0
while claims=$(get /claims); do
  claimers=$(jq '[.claims[].from] | unique | length' <<<"$claims")
  if [ "$claimers" -ge "$seats" ] || [ "$waited" -ge "${SCARCE_WAIT_SEC:-60}" ]; then break; fi
  python3 -c 'import time; time.sleep(1)'
  waited=$((waited + 1))
done

# The last claim of every seat counts. The largest claim yields; a tie goes to the lowest seat name.
yielder=$(jq -r '[.claims | group_by(.from)[] | max_by(.id)] | sort_by([-.need, .from])[0].from' <<<"$claims")
amount=$need
if [ "$yielder" = "$me" ] && [ "$claimers" -gt 1 ]; then amount=0; fi
echo "$me sees the claims of $claimers seats, the largest from $yielder; it pledges and takes $amount"
post /pledge "{\"amount\": $amount}"
post /take "{\"amount\": $amount}"
