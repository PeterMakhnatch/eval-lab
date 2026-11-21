#!/bin/bash
# random.sh: a legal baseline without a plan. The seat opens distinct boxes in a pseudo-random order
# until its budget is used. The order is fixed by the seat token, and the token is fixed by the seed
# of the family, the instance and the seat name. So every seat of every instance has its own order,
# and a second run opens the same boxes.
set -u

WORLD=${WORLD:-http://world:8080}
BOXES=60

call() { # METHOD PATH [JSON]: one request to the world. Sets STATUS and BODY.
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
    # 6 and 7: the request did not reach the world, so a second try cannot open a box twice
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
ORDER=$(python3 -c '
import hashlib, sys
token, boxes = sys.argv[1], int(sys.argv[2])
order = sorted(range(1, boxes + 1), key=lambda box: hashlib.sha256(f"{token}/{box}".encode()).hexdigest())
print(*order)' "$SEAT_TOKEN" "$BOXES")
for box in $ORDER; do
  [ "$LEFT" -gt 0 ] || break
  call POST /open "{\"box\": $box}"
  if [ "$STATUS" != 200 ]; then
    echo "open $box refused: $STATUS $BODY"
    exit 1
  fi
  LEFT=$(jq -r '.budget_left' <<<"$BODY")
  echo "box $box: flag $(jq -r '.flag' <<<"$BODY"), $LEFT opens left"
done
