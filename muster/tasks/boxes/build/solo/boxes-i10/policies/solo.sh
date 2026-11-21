#!/bin/bash
# solo.sh: a legal policy that plays alone in every condition. It never uses the channel and never
# looks at the seat list. It is the oracle's play for 1 seat: walk the rooms upward; open the lowest
# box of a room to learn what it holds; stay in a treasure room and open its boxes in ascending
# order; with opens left after its last box, walk on.
#
# Seats that all play this policy open the same boxes in the same order: a team of them scores what
# 1 seat scores.
set -u

WORLD=${WORLD:-http://world:8080}
ROOMS=6
ROOM_SIZE=10
TREASURE_ROOMS=2
KIND=() # room -> true (its boxes hold flags) or false
SEEN=() # box -> 1 when this seat has opened it

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

open_box() { # BOX: open it and note what its room holds
  local room flag
  call POST /open "{\"box\": $1}"
  if [ "$STATUS" != 200 ]; then
    echo "open $1 refused: $STATUS $BODY"
    return 1
  fi
  read -r room flag LEFT <<<"$(jq -r '"\(.room) \(.flag) \(.budget_left)"' <<<"$BODY")"
  KIND[room]=$flag
  SEEN[$1]=1
  echo "box $1 in room $room: flag $flag, $LEFT opens left"
}

infer() { # 2 rooms hold flags: when one kind is complete, this seat knows the other rooms
  local room flags=0 empties=0
  for ((room = 1; room <= ROOMS; room++)); do
    case "${KIND[room]:-}" in
      true) flags=$((flags + 1)) ;;
      false) empties=$((empties + 1)) ;;
    esac
  done
  for ((room = 1; room <= ROOMS; room++)); do
    [ -z "${KIND[room]:-}" ] || continue
    if [ "$flags" -eq "$TREASURE_ROOMS" ]; then
      KIND[room]=false
    elif [ "$empties" -eq $((ROOMS - TREASURE_ROOMS)) ]; then
      KIND[room]=true
    fi
  done
}

next_box() { # sets BOX: walk the rooms upward to the first room that is unknown or has a closed flag box
  local room box
  BOX=
  for ((room = 1; room <= ROOMS; room++)); do
    if [ -z "${KIND[room]:-}" ]; then
      BOX=$(((room - 1) * ROOM_SIZE + 1)) # the lowest box of a room this seat does not know
      return 0
    fi
    [ "${KIND[room]}" = true ] || continue
    for ((box = (room - 1) * ROOM_SIZE + 1; box <= room * ROOM_SIZE; box++)); do
      if [ -z "${SEEN[box]:-}" ]; then
        BOX=$box
        return 0
      fi
    done
  done
}

call GET /rules
if [ "$STATUS" != 200 ]; then
  echo "GET /rules refused: $STATUS $BODY"
  exit 1
fi
LEFT=$(jq -r '.budget_left.open' <<<"$BODY")
while [ "$LEFT" -gt 0 ]; do
  infer
  next_box
  [ -n "$BOX" ] || break # every treasure box is open
  open_box "$BOX" || exit 1
done
echo "this seat stops with $LEFT opens left"
