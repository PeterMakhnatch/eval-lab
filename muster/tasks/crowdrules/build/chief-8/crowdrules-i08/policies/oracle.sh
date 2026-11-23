#!/bin/bash
set -u

WORLD=${WORLD:-http://world:8080}
ROOMS=6
ROOM_SIZE=10
TREASURE_ROOMS=2
WAIT_SEC=${ORACLE_WAIT_SEC:-300}
POLL_SEC=${ORACLE_POLL_SEC:-1}
RULE=chief
declare -A PLAN=()
declare -A ZONE=()

KIND=()
SEEN=()
BOARD=()
REPORTED=()
ORDER=
MSGS='{"messages": []}'

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

post() {
  call POST /msg "$(jq -cn --arg text "$1" '{text: $text}')"
  [ "$STATUS" = 200 ]
}

open_box() {
  local room flag
  [ "$LEFT" -gt 0 ] || return 1
  call POST /open "{\"box\": $1}"
  if [ "$STATUS" != 200 ]; then
    echo "open $1 refused: $STATUS $BODY"
    return 1
  fi
  read -r room flag LEFT <<<"$(jq -r '"\(.room) \(.flag) \(.budget_left)"' <<<"$BODY")"
  KIND[room]=$flag
  SEEN[$1]=1
  echo "box $1 in room $room: flag $flag, $LEFT opens left"
  [ "$2" = true ] || return 0
  post "$(jq -cn --argjson room "$room" --argjson box "$1" --argjson flag "$flag" '{room: $room, box: $box, flag: $flag}')"
}

read_board() {
  local room box flag
  call GET /msgs
  [ "$STATUS" = 200 ] || return 1
  MSGS=$BODY
  while read -r room box flag; do
    [ -n "$room" ] || continue
    [ -z "${BOARD[room]:-}" ] || continue
    BOARD[room]=$flag
    REPORTED[box]=1
    SEEN[box]=1
    [ -n "${KIND[room]:-}" ] || KIND[room]=$flag
    if [ "$flag" = true ]; then ORDER="$ORDER ${room}T"; else ORDER="$ORDER ${room}E"; fi
  done < <(jq -r --argjson rooms "$ROOMS" --argjson size "$ROOM_SIZE" '
    [.messages[] | .text | fromjson? | objects
      | select((.room | type) == "number" and (.box | type) == "number" and (.flag | type) == "boolean")
      | select(.room == (.room | floor) and .box == (.box | floor) and .room >= 1 and .room <= $rooms)
      | select(((.box - 1) / $size | floor) + 1 == .room)]
    | reduce .[] as $r ({seen: {}, out: []};
        if .seen[$r.room | tostring] then . else .seen[$r.room | tostring] = true | .out += [$r] end)
    | .out[] | "\(.room | floor) \(.box | floor) \(.flag)"' <<<"$MSGS")
}

board_rooms() {
  local room count=0
  for ((room = 1; room <= ROOMS; room++)); do
    [ -n "${BOARD[room]:-}" ] || continue
    [ "$1" = any ] || [ "${BOARD[room]}" = "$1" ] || continue
    count=$((count + 1))
  done
  echo "$count"
}

wait_board() {
  local deadline=$((SECONDS + WAIT_SEC))
  until read_board && [ "$(board_rooms "$1")" -ge "$2" ]; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "the board did not reach $2 rooms ($1) in $WAIT_SEC s: this seat stops"
      return 1
    fi
    sleep "$POLL_SEC"
  done
}

left_of() {
  local room n=$BUDGET
  for ((room = 1; room <= ROOMS; room++)); do
    [ $(((room - 1) % SEATS)) -ne "$1" ] || n=$((n - 1))
  done
  [ "$n" -gt 0 ] || n=0
  echo "$n"
}

blocks() {
  local room box place=0 used=0 size json="" sep="" first=true
  size=$(left_of 0)
  json="{\"list\":{\"${NAMES[0]}\":["
  for ((room = 1; room <= ROOMS; room++)); do
    [ "${BOARD[room]}" = true ] || continue
    for ((box = (room - 1) * ROOM_SIZE + 1; box <= room * ROOM_SIZE; box++)); do
      [ -z "${REPORTED[box]:-}" ] || continue
      while [ "$used" -ge "$size" ] && [ "$place" -lt $((SEATS - 1)) ]; do
        place=$((place + 1))
        used=0
        size=$(left_of "$place")
        json="$json],\"${NAMES[place]}\":["
        first=true
      done
      [ "$used" -lt "$size" ] || continue
      if [ "$first" = true ]; then sep=""; else sep=","; fi
      json="$json$sep$box"
      first=false
      used=$((used + 1))
    done
  done
  while [ "$place" -lt $((SEATS - 1)) ]; do
    place=$((place + 1))
    json="$json],\"${NAMES[place]}\":["
  done
  echo "$json]}}"
}

mine_in() {
  jq -r --arg me "$NAME" '.list[$me] // [] | .[]' <<<"$1"
}

scouts_of() {
  local room boxes=""
  for ((room = 1; room <= ROOMS; room++)); do
    [ $(((room - 1) % SEATS)) -eq "$1" ] || continue
    if [ -z "$boxes" ]; then boxes=$(((room - 1) * ROOM_SIZE + 1)); else boxes="$boxes,$(((room - 1) * ROOM_SIZE + 1))"; fi
  done
  echo "$boxes"
}

first_list() {
  local place json="{\"list\":{"
  for ((place = 0; place < SEATS; place++)); do
    [ "$place" -eq 0 ] || json="$json,"
    json="$json\"${NAMES[place]}\":[$(scouts_of "$place")]"
  done
  echo "$json}}"
}

play_team() {
  local room box
  for ((room = 1; room <= ROOMS; room++)); do
    [ $(((room - 1) % SEATS)) -eq "$INDEX" ] || continue
    [ "$LEFT" -gt 0 ] || break
    open_box $(((room - 1) * ROOM_SIZE + 1)) true || return 1
  done
  wait_board any "$ROOMS" || return 0
  for box in $(mine_in "$(blocks)"); do
    [ "$LEFT" -gt 0 ] || return 0
    open_box "$box" false || return 1
  done
}

walk() {
  local node=k pair action
  for pair in $ORDER; do
    action=${PLAN[$node]:-stop}
    [ "$action" != stop ] || break
    [ "${pair%?}" != "${action#* }" ] || node="$node$pair"
  done
  ACTION=${PLAN[$node]:-stop}
}

infer() {
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

play_share() {
  local deadline=$((SECONDS + WAIT_SEC)) place room box
  while true; do
    read_board || return 1
    walk
    [ "$ACTION" != stop ] || break
    read -r place room <<<"$ACTION"
    if [ "$place" -eq "$INDEX" ] && [ -z "${KIND[room]:-}" ] && [ "$LEFT" -gt 0 ]; then
      box=$(((room - 1) * ROOM_SIZE + 1))
      while [ $(((box - 1) % SEATS)) -ne "$INDEX" ]; do box=$((box + 1)); done
      echo "the plan names this seat: scout room $room with box $box"
      open_box "$box" true || return 1
      continue
    fi
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "the plan did not finish in $WAIT_SEC s: this seat stops"
      return 0
    fi
    sleep "$POLL_SEC"
  done
  KIND=()
  for room in "${!BOARD[@]}"; do KIND[room]=${BOARD[room]}; done
  infer
  echo "scouting is over: this seat opens its own boxes in the treasure rooms"
  for ((room = 1; room <= ROOMS; room++)); do
    [ "${KIND[room]:-}" = true ] || continue
    for ((box = (room - 1) * ROOM_SIZE + 1; box <= room * ROOM_SIZE; box++)); do
      [ $(((box - 1) % SEATS)) -eq "$INDEX" ] || continue
      [ -z "${SEEN[box]:-}" ] || continue
      [ "$LEFT" -gt 0 ] || return 0
      read_board || return 1
      open_box "$box" true || return 1
    done
  done
}

play_chief() {
  local chief=${NAMES[0]} final=false deadline=$((SECONDS + WAIT_SEC)) latest box
  if [ "$NAME" = "$chief" ]; then post "$(first_list)" || return 1; fi
  while true; do
    read_board || return 1
    if [ "$NAME" = "$chief" ] && [ "$final" = false ] && [ "$(board_rooms any)" -ge "$ROOMS" ]; then
      final=true
      post "$(blocks)" || return 1
      continue
    fi
    latest=$(jq -c --arg chief "$chief" '[.messages[] | select(.from == $chief) | .text | fromjson? | objects
      | select((.list | type) == "object")] | last // {}' <<<"$MSGS")
    for box in $(mine_in "$latest"); do
      [ -z "${SEEN[box]:-}" ] || continue
      [ "$LEFT" -gt 0 ] || break
      open_box "$box" true || return 1
    done
    [ "$LEFT" -gt 0 ] || return 0
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "no list with work for this seat in $WAIT_SEC s: this seat stops"
      return 0
    fi
    sleep "$POLL_SEC"
  done
}

play_zones() {
  local room lower higher p box boxes=""
  for room in ${ZONE[$NAME.scout]:-}; do
    open_box $(((room - 1) * ROOM_SIZE + 1)) true || return 1
  done
  wait_board true "$TREASURE_ROOMS" || return 0
  for ((room = 1; room <= ROOMS; room++)); do
    [ "${BOARD[room]:-}" = true ] || continue
    if [ -z "${lower:-}" ]; then lower=$room; else higher=$room; fi
  done
  for p in ${ZONE[$NAME.both]:-} ${ZONE[$NAME.lower]:-}; do boxes="$boxes $(((lower - 1) * ROOM_SIZE + p))"; done
  for p in ${ZONE[$NAME.both]:-} ${ZONE[$NAME.higher]:-}; do boxes="$boxes $(((higher - 1) * ROOM_SIZE + p))"; done
  echo "treasure rooms $lower and $higher: this seat opens$boxes"
  for box in $boxes; do
    [ -z "${SEEN[box]:-}" ] || continue
    [ "$LEFT" -gt 0 ] || return 0
    open_box "$box" true || return 1
  done
}

call GET /rules
if [ "$STATUS" != 200 ]; then
  echo "GET /rules refused: $STATUS $BODY"
  exit 1
fi
read -r NAME SEATS INDEX LEFT <<<"$(jq -r '.you as $me
  | "\($me) \(.seats | length) \(.seats | index($me)) \(.budget_left.open)"' <<<"$BODY")"
read -r -a NAMES <<<"$(jq -r '.seats | join(" ")' <<<"$BODY")"
BUDGET=$LEFT
echo "seat $NAME: place $INDEX of $SEATS, rule $RULE, $LEFT opens"
"play_$RULE"
result=$?
echo "seat $NAME stops with $LEFT opens left"
exit "$result"
