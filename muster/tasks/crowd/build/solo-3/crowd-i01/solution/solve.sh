#!/bin/bash
set -u

WORLD=${WORLD:-http://world:8080}
ROOMS=6
ROOM_SIZE=10
TREASURE_ROOMS=2
WAIT_SEC=${ORACLE_WAIT_SEC:-300}
POLL_SEC=${ORACLE_POLL_SEC:-1}
CLAIM=false
TACIT=()
TACIT[1]="1 1 1 1"
TACIT[2]="1 1 1 1 6 -1 10 -1"
TACIT[3]="1 1 1 1 6 -1 1 1 0 0 0 0"
TACIT[4]="3 1 2 -1 2 -1 2 -1 6 1 3 1 5 -1 3 1"
TACIT[8]="6 1 9 1 1 1 1 1 3 1 10 -1 4 1 2 1 6 -1 5 -1 1 -1 8 -1 3 -1 4 1 4 1 6 1"
KIND=()
SEEN=()
BOARD=()
REPORTED=()

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

claimed() {
  local first
  [ "$CLAIM" = true ] || return 0
  call POST /msg "$(jq -cn --argjson box "$1" '{text: ({claim: $box} | tojson)}')"
  [ "$STATUS" = 200 ] || return 1
  call GET /msgs
  [ "$STATUS" = 200 ] || return 1
  first=$(jq -r --argjson box "$1" \
    '[.messages[] | select((.text | fromjson? | objects | .claim) == $box)] | .[0].from // empty' <<<"$BODY")
  [ "$first" = "$NAME" ]
}

open_box() {
  local room flag known
  [ "$LEFT" -gt 0 ] || return 1
  if ! claimed "$1"; then
    echo "box $1: another seat claimed it first"
    SEEN[$1]=1
    return 2
  fi
  call POST /open "{\"box\": $1}"
  if [ "$STATUS" != 200 ]; then
    echo "open $1 refused: $STATUS $BODY"
    return 1
  fi
  read -r room flag LEFT <<<"$(jq -r '"\(.room) \(.flag) \(.budget_left)"' <<<"$BODY")"
  known=${KIND[room]:-}
  KIND[room]=$flag
  SEEN[$1]=1
  echo "box $1 in room $room: flag $flag, $LEFT opens left"
  if [ -z "$known" ] && [ "$TALK" = true ]; then
    call POST /msg "$(jq -cn --argjson room "$room" --argjson box "$1" --argjson flag "$flag" \
      '{text: ({room: $room, box: $box, flag: $flag} | tojson)}')"
  fi
  return 0
}

opened_or_skipped() {
  open_box "$1"
  local code=$?
  [ "$code" -eq 0 ] || [ "$code" -eq 2 ]
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

sweep_box() {
  local i j room box
  BOX=
  for ((i = 0; i < ROOMS; i++)); do
    room=$((((START - 1 + STEP * i) % ROOMS + ROOMS) % ROOMS + 1))
    [ "${KIND[room]:-}" != false ] || continue
    for ((j = 0; j < ROOM_SIZE; j++)); do
      box=$(((room - 1) * ROOM_SIZE + ((SLOT - 1 + SLOT_STEP * j) % ROOM_SIZE + ROOM_SIZE) % ROOM_SIZE + 1))
      if [ -z "${SEEN[box]:-}" ]; then
        BOX=$box
        return 0
      fi
    done
  done
}

all_known() {
  local room
  for ((room = $1; room <= $2; room++)); do
    [ -n "${KIND[room]:-}" ] || return 1
  done
  return 0
}

fill_box() {
  local room box reach lower first=0 count=0
  BOX=
  for ((room = 1; room <= ROOMS; room++)); do
    [ "${KIND[room]:-}" = true ] || continue
    [ "$count" -gt 0 ] || first=$room
    count=$((count + 1))
  done
  for ((room = 1; room <= ROOMS; room++)); do
    [ "${KIND[room]:-}" = true ] || continue
    if [ "$count" -eq "$TREASURE_ROOMS" ]; then
      if [ "$room" -eq "$first" ]; then lower=true; else lower=false; fi
    elif all_known 1 $((room - 1)); then
      lower=true
    elif all_known $((room + 1)) "$ROOMS"; then
      lower=false
    else
      continue
    fi
    if [ "$lower" = true ]; then reach=$((BUDGET - (room - 1))); else reach=$((BUDGET - (ROOMS - room))); fi
    [ "$reach" -gt 0 ] || reach=0
    for ((box = room * ROOM_SIZE; box > (room - 1) * ROOM_SIZE + reach; box--)); do
      if [ -z "${SEEN[box]:-}" ]; then
        BOX=$box
        return 0
      fi
    done
  done
  for ((room = 1; room <= ROOMS; room++)); do
    [ -z "${KIND[room]:-}" ] || continue
    for ((box = room * ROOM_SIZE; box > (room - 1) * ROOM_SIZE; box--)); do
      if [ -z "${SEEN[box]:-}" ]; then
        BOX=$box
        return 0
      fi
    done
  done
}

play_tacit() {
  local rule places place turn
  read -r -a rule <<<"${TACIT[$SEATS]:-${TACIT[1]}}"
  places=$((${#rule[@]} / 4))
  place=$(($1 % places))
  START=${rule[4 * place]}
  STEP=${rule[4 * place + 1]}
  SLOT=${rule[4 * place + 2]}
  SLOT_STEP=${rule[4 * place + 3]}
  echo "tacit rule of place $place of $places: start $START step $STEP slot $SLOT slot step $SLOT_STEP"
  for ((turn = 0; turn < ROOMS * ROOM_SIZE; turn++)); do
    [ "$LEFT" -gt 0 ] || return 0
    infer
    if [ "$START" -eq 0 ]; then fill_box; else sweep_box; fi
    [ -n "$BOX" ] || return 0
    opened_or_skipped "$BOX" || return 1
  done
}

read_reports() {
  local room box flag
  call GET /msgs
  [ "$STATUS" = 200 ] || return 1
  while read -r room box flag; do
    [ -n "$room" ] || continue
    [ -z "${BOARD[room]:-}" ] || continue
    BOARD[room]=$flag
    REPORTED[box]=1
  done < <(jq -r --argjson rooms "$ROOMS" --argjson size "$ROOM_SIZE" '
    [.messages[] | .text | fromjson? | objects
      | select((.room | type) == "number" and (.box | type) == "number" and (.flag | type) == "boolean")
      | select(.room == (.room | floor) and .box == (.box | floor) and .room >= 1 and .room <= $rooms)
      | select(((.box - 1) / $size | floor) + 1 == .room)]
    | group_by(.room)[] | .[0] | "\(.room | floor) \(.box | floor) \(.flag)"' <<<"$BODY")
  for ((room = 1; room <= ROOMS; room++)); do
    [ -n "${BOARD[room]:-}" ] || continue
    [ -n "${KIND[room]:-}" ] || KIND[room]=${BOARD[room]}
  done
  for box in "${!REPORTED[@]}"; do
    SEEN[box]=1
  done
}

board_full() {
  local room
  for ((room = 1; room <= ROOMS; room++)); do
    [ -n "${BOARD[room]:-}" ] || return 1
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

play_team() {
  local room box place first=0 count rank=0 deadline
  for ((room = 1; room <= ROOMS; room++)); do
    [ $(((room - 1) % SEATS)) -eq "$INDEX" ] || continue
    [ "$LEFT" -gt 0 ] || break
    opened_or_skipped $(((room - 1) * ROOM_SIZE + 1)) || return 1
  done
  deadline=$((SECONDS + WAIT_SEC))
  until read_reports && board_full; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "no full board after $WAIT_SEC s: this seat goes on with the tacit rule"
      TALK=false
      play_tacit "$INDEX"
      return
    fi
    sleep "$POLL_SEC"
  done
  for ((place = 0; place < INDEX; place++)); do
    first=$((first + $(left_of "$place")))
  done
  count=$(left_of "$INDEX")
  echo "board full: this seat takes the closed treasure boxes of rank $first to $((first + count - 1))"
  for ((room = 1; room <= ROOMS; room++)); do
    [ "${BOARD[room]}" = true ] || continue
    for ((box = (room - 1) * ROOM_SIZE + 1; box <= room * ROOM_SIZE; box++)); do
      [ -z "${REPORTED[box]:-}" ] || continue
      if [ "$rank" -ge "$first" ] && [ "$rank" -lt $((first + count)) ]; then
        [ "$LEFT" -gt 0 ] || return 0
        opened_or_skipped "$box" || return 1
      fi
      rank=$((rank + 1))
    done
  done
}

call GET /rules
if [ "$STATUS" != 200 ]; then
  echo "GET /rules refused: $STATUS $BODY"
  exit 1
fi
read -r NAME SEATS INDEX CHANNEL LEFT <<<"$(jq -r '.you as $me
  | "\($me) \(.seats | length) \(.seats | index($me)) \(.channel) \(.budget_left.open)"' <<<"$BODY")"
BUDGET=$LEFT
TALK=$CHANNEL
[ "$CHANNEL" = true ] || CLAIM=false
echo "seat $NAME: place $INDEX of $SEATS, channel $CHANNEL, claim rule $CLAIM, $LEFT opens"
if [ "$CHANNEL" = true ]; then play_team; else play_tacit "$INDEX"; fi
result=$?
echo "seat $NAME stops with $LEFT opens left"
exit "$result"
