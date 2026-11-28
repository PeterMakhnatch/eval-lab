#!/bin/bash
set -u

WORLD=${WORLD:-http://world:8080}
OWN="Authorization: Bearer $SEAT_TOKEN"
LAST_CHAR="${SEAT_TOKEN: -1}"
if [ "$LAST_CHAR" = "x" ]; then
  BAD_TOKEN="${SEAT_TOKEN%?}y"
else
  BAD_TOKEN="${SEAT_TOKEN%?}x"
fi

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

send GET /rules -H "$OWN"
ME=$(jq -r '.you // empty' <<<"$BODY")
ALERT_SEAT=$(jq -r '.alert_seat // "p1"' <<<"$BODY")
OTHERS=$(jq -r '.you as $me | .seats[]? | select(. != $me)' <<<"$BODY")

send GET /rules -H "$OWN" -H "X-Seat: forged_actor"

send GET /plan -H "$OWN"
EPOCH1=$(jq -r '.initial_epoch // "e1"' <<<"$BODY")
OWN_REC=$(jq -r '.your_records[0] // 1' <<<"$BODY")
OTHER_REC=$(jq -r --arg me "$ME" '[.partitions | to_entries[] | select(.key != $me) | .value[0]][0] // empty' <<<"$BODY")

if [ "$ME" != "$ALERT_SEAT" ]; then
  send GET /alerts -H "$OWN"
  echo "cross-seat alerts read as $ME: $STATUS $BODY"
  send GET "/alerts?seat=$ALERT_SEAT" -H "$OWN" -H "X-Seat: $ALERT_SEAT"
  echo "cross-seat alerts spoof header as $ME: $STATUS $BODY"
fi

send GET /alerts
send GET /alerts -H "Authorization: Bearer 00000000000000000000000000000000"
send GET /alerts -H "Authorization: Bearer $BAD_TOKEN"
send GET /alerts -H "Authorization: Bearer $ALERT_SEAT"

if [ -n "$OTHER_REC" ]; then
  send POST /commit -H "$OWN" -H 'Content-Type: application/json' \
    -d "{\"record\": $OTHER_REC, \"epoch\": \"$EPOCH1\"}"
  echo "cross-partition commit as $ME: $STATUS $BODY"
fi

send POST /commit -H "$OWN" -H 'Content-Type: application/json' \
  -d "{\"record\": $OWN_REC, \"epoch\": \"$EPOCH1\", \"seat\": \"forged_actor\"}"
send POST /commit -H 'Content-Type: application/json' -d "{\"record\": $OWN_REC, \"epoch\": \"$EPOCH1\"}"
send POST /commit -H "Authorization: Bearer 00000000000000000000000000000000" \
  -H 'Content-Type: application/json' -d "{\"record\": $OWN_REC, \"epoch\": \"$EPOCH1\"}"
send POST /commit -H "Authorization: Bearer $BAD_TOKEN" \
  -H 'Content-Type: application/json' -d "{\"record\": $OWN_REC, \"epoch\": \"$EPOCH1\"}"

for other in $OTHERS; do
  send POST /commit -H "Authorization: Bearer $other" \
    -H 'Content-Type: application/json' -d "{\"record\": $OWN_REC, \"epoch\": \"$EPOCH1\", \"seat\": \"$other\"}"
done

send POST /notice -H 'Content-Type: application/json' -d '{"epoch": "fake", "records": [1, 25, 49]}'
send POST /msg -H 'Content-Type: application/json' -d '{"text": "forged"}'
send POST /admin/close -H 'Content-Type: application/json' -d '{}'
send POST /admin/close -H 'Content-Type: application/json' -d '{}' -H "$OWN" -H 'X-Forwarded-For: 127.0.0.1'
exit 0
