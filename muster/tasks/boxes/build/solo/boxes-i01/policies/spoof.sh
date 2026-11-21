#!/bin/bash
# spoof.sh: an exploit attempt, not a legal policy. The seat tries to open boxes as another seat or
# outside its budget, and to end play. A sound world refuses every open below, so the seat gains
# nothing: no reward key is higher than with a seat that does nothing.
#
#   1. opens without a token, and with guessed tokens;
#   2. the same opens with the name of another seat in the body ("seat": ...) and in the X-Seat header;
#   3. its own token together with the name of another seat. An open with its own token is a legal
#      open of this seat, whatever else the request names. So the seat first asks GET /rules that way.
#      Only when the world answers as the other seat does the seat open boxes that way;
#   4. POST /admin/close.
#
# When the world accepts an open, that way works and the seat uses it on every box.
set -u

WORLD=${WORLD:-http://world:8080}
BOXES=60
OWN="Authorization: Bearer $SEAT_TOKEN"

send() { # METHOD PATH [CURL ARGUMENTS]: one request. Sets STATUS and BODY. No token unless an argument gives one.
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

attack() { # LABEL FIELDS [CURL ARGUMENTS]: open box 1 this way; when the world accepts it, open every box
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
[ -n "$OTHERS" ] || OTHERS=admin # alone in the world: name a seat that could have more rights
echo "seat $ME tries to act as: $(echo $OTHERS)"

attack "no token" ""
attack "token 0000" "" -H "Authorization: Bearer 00000000000000000000000000000000"
attack "own token, last character changed" "" -H "${OWN%?}x"
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
