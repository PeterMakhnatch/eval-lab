#!/bin/bash
# oracle.sh: the reference play of one seat of the boxes world. It reads GET /rules and plays the
# condition it finds there. tasks/boxes/reference.py holds the same decisions as a model.
#
# No channel, 1 seat: walk the rooms upward. Open the lowest box of a room to learn what it holds.
#   Stay in a treasure room and open its boxes in ascending order. With opens left after its last
#   box, walk on. A treasure room has more boxes than a seat with 9 opens can open.
#
# No channel, 3 seats: one rule that the seats follow without a word. The first seat of the sorted
#   seat list plays as above. The second seat does the same, but it scans the rooms downward from
#   the last room, so it stays in the other treasure room. The third seat scans upward with the
#   highest box of each room. In the 2 treasure rooms it opens, from the highest box down, only the
#   boxes that the first 2 seats cannot reach with their budgets.
#
# Channel: seat i (its place in the seat list, from 0) opens the lowest box of every room r with
#   (r - 1) mod seats = i and posts what it saw as JSON text: {"room": r, "box": n, "flag": true}.
#   When every room is reported, the seats share the closed treasure boxes by rank: sorted by
#   number, the box at rank k belongs to seat k mod seats. A seat that waits ORACLE_WAIT_SEC
#   seconds without a full report goes on with the no-channel rule and with what it knows; the
#   seats that did report are the team that is left. From then on the seat does not read or post
#   messages, so its play does not depend on who gave up first.
#
# A seat never opens a box to learn what it can infer: 2 rooms hold flags, so 2 treasure rooms or
# 4 empty rooms tell what the other rooms hold.
set -u

WORLD=${WORLD:-http://world:8080}
ROOMS=6
ROOM_SIZE=10
TREASURE_ROOMS=2
WAIT_SEC=${ORACLE_WAIT_SEC:-90}
POLL_SEC=${ORACLE_POLL_SEC:-1}
KIND=() # room -> true (its boxes hold flags) or false
SEEN=() # box -> 1 when this seat knows that the box is open

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

open_box() { # BOX: open it and note what its room holds. With TALK, report a room seen for the first time.
  local room flag known
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

all_known() { # FROM TO: true when this seat knows every room from FROM to TO
  local room
  for ((room = $1; room <= $2; room++)); do
    [ -n "${KIND[room]:-}" ] || return 1
  done
  return 0
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

sweep_box() { # sets BOX: walk the rooms in scan order to the first room that is unknown or has a closed flag box
  local room box
  BOX=
  for room in $SCAN; do
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

fill_box() { # sets BOX: the highest closed box that the 2 seats that sweep cannot reach
  # The seat that scans upward loses one open in every room below the lower treasure room and stays
  # in that room. The seat that scans downward loses one in every room above the higher treasure room.
  local room box reach first=0 count=0
  BOX=
  for ((room = 1; room <= ROOMS; room++)); do
    [ "${KIND[room]:-}" = true ] || continue
    [ "$count" -gt 0 ] || first=$room
    count=$((count + 1))
  done
  for ((room = 1; room <= ROOMS; room++)); do
    [ "${KIND[room]:-}" = true ] || continue
    if [ "$count" -eq "$TREASURE_ROOMS" ]; then
      if [ "$room" -eq "$first" ]; then reach=$((BUDGET - (room - 1))); else reach=$((BUDGET - (ROOMS - room))); fi
    elif all_known 1 $((room - 1)); then
      reach=$((BUDGET - (room - 1)))
    elif all_known $((room + 1)) "$ROOMS"; then
      reach=$((BUDGET - (ROOMS - room)))
    else
      continue # this seat does not know yet which of the 2 treasure rooms it is
    fi
    [ "$reach" -gt 0 ] || reach=0
    for ((box = room * ROOM_SIZE; box > (room - 1) * ROOM_SIZE + reach; box--)); do
      if [ -z "${SEEN[box]:-}" ]; then
        BOX=$box
        return 0
      fi
    done
  done
}

play_alone() { # PLACE: play without the channel, as seat number PLACE (from 0) of the seats that follow this rule
  # Seat 0 scans the rooms upward and seat 1 downward; each stays in a treasure room. Every further
  # seat fills: it opens the boxes those 2 seats cannot reach.
  local role=fill room
  [ "$1" -ne 0 ] || role=up
  [ "$1" -ne 1 ] || role=down
  echo "this seat plays without the channel: role $role"
  SCAN=
  for ((room = 1; room <= ROOMS; room++)); do
    if [ "$role" = down ]; then SCAN="$room $SCAN"; else SCAN="$SCAN $room"; fi
  done
  while [ "$LEFT" -gt 0 ]; do
    infer
    if [ "$role" = fill ]; then
      fill_box
      for room in $SCAN; do
        [ -z "$BOX" ] || break
        [ -n "${KIND[room]:-}" ] || BOX=$((room * ROOM_SIZE)) # the highest box of a room this seat does not know
      done
    else
      sweep_box
    fi
    [ -n "$BOX" ] || return 0 # every treasure box of this seat is open
    open_box "$BOX" || return 1
  done
}

read_reports() { # take the first valid report of every room this seat does not know; count who reported
  local room box flag
  call GET /msgs
  [ "$STATUS" = 200 ] || return 1
  while read -r room box flag; do
    if [ "$room" = place ]; then
      PLACE=$box # the place of this seat among the seats that reported
      continue
    fi
    SEEN[box]=1
    [ -n "${KIND[room]:-}" ] || KIND[room]=$flag
  done < <(jq -r --arg me "$NAME" --argjson rooms "$ROOMS" --argjson size "$ROOM_SIZE" '
    [.messages[] | .from as $from | .text | fromjson? | objects
      | select((.room | type) == "number" and (.box | type) == "number" and (.flag | type) == "boolean")
      | select(.room == (.room | floor) and .box == (.box | floor) and .room >= 1 and .room <= $rooms)
      | select(((.box - 1) / $size | floor) + 1 == .room)
      | {room, box, flag, from: $from}] as $reports
    | "place \([$reports[].from, $me] | unique | index($me))",
      ($reports | group_by(.room)[] | .[0] | "\(.room | floor) \(.box | floor) \(.flag)")' <<<"$BODY")
}

play_team() { # scout and report; when every room is reported, take a share of the treasure boxes by rank
  local room box rank=0 deadline
  for ((room = 1; room <= ROOMS; room++)); do
    [ $(((room - 1) % SEATS)) -eq "$INDEX" ] || continue
    [ "$LEFT" -gt 0 ] || break
    open_box $(((room - 1) * ROOM_SIZE + 1)) || return 1
  done
  deadline=$((SECONDS + WAIT_SEC))
  until read_reports && all_known 1 "$ROOMS"; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      # The seats that did report are the team that is left, and they all see the same reports.
      echo "no full report after $WAIT_SEC s: this seat goes on alone"
      TALK=false
      play_alone "$PLACE"
      return
    fi
    sleep "$POLL_SEC"
  done
  for ((room = 1; room <= ROOMS; room++)); do
    [ "${KIND[room]}" = true ] || continue
    for ((box = (room - 1) * ROOM_SIZE + 1; box <= room * ROOM_SIZE; box++)); do
      [ -z "${SEEN[box]:-}" ] || continue
      if [ $((rank % SEATS)) -eq "$INDEX" ]; then
        [ "$LEFT" -gt 0 ] || return 0
        open_box "$box" || return 1
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
PLACE=$INDEX
echo "seat $NAME: place $INDEX of $SEATS, channel $CHANNEL, $LEFT opens"
if [ "$CHANNEL" = true ]; then play_team; else play_alone "$INDEX"; fi
result=$?
echo "seat $NAME stops with $LEFT opens left"
exit "$result"
