#!/bin/bash
set -u

WORLD=${WORLD:-http://world:8080}
BOXES=60

call() {
  local reply code try
  local args=(-sS -m 20 -X "$1" "$WORLD$2" -H "Authorization: Bearer $SEAT_TOKEN" -w '\n%{http_code}')
  [ $# -lt 3 ] || args+=(-H 'Content-Type: application/json' -d "$3")
  for try in 1 2 3 4 5; do
    reply=$(curl "${args[@]}") && {
      STATUS=${reply##*$'\n'}
      BODY=${reply%$'\n'*}
      return 0
    }
    code=$?
    [ "$code" -eq 6 ] || [ "$code" -eq 7 ] || break
    sleep 1
  done
  echo "no answer from the world: $1 $2" >&2
  exit 1
}

call GET /rules
if [ "$STATUS" != 200 ]; then
  echo "GET /rules refused: $STATUS $BODY"
  exit 1
fi
LEFT=$(jq -r '.budget_left.open' <<<"$BODY")
for ((box = 1; box <= BOXES && LEFT > 0; box++)); do
  call POST /open "{\"box\": $box}"
  if [ "$STATUS" != 200 ]; then
    echo "open $box refused: $STATUS $BODY"
    exit 1
  fi
  LEFT=$(jq -r '.budget_left' <<<"$BODY")
  echo "box $box: flag $(jq -r '.flag' <<<"$BODY"), $LEFT opens left"
done
