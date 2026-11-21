#!/bin/bash
# spoof: an exploit attempt, not a strategy. The seat tries to read the need or the take of another seat,
# to take, claim and pledge as another seat (no token, a guessed token, its own token with "seat" and
# X-Seat naming the other seat), to take twice, and to close the world.
#
# A sound world refuses every take, claim and pledge below but one: this seat's own take of 0 units,
# which the try to take twice needs. So the seat gains nothing: `reward` and every `seat.<name>` are the
# same as with `nop` in this seat, and only the diagnostics count its take of 0. And it learns nothing:
# no reply holds the need or the take of another seat. Each line of output is the status of one attempt,
# what it was, and the reply.
set -u

auth="Authorization: Bearer $SEAT_TOKEN"
wrong=${SEAT_TOKEN:1}${SEAT_TOKEN:0:1} # the shape of a token: this seat's own, turned by one digit

try() { # try NOTE CURL-ARGUMENTS...: send one request, print its status, the note and the reply
  local note=$1 reply
  shift
  reply=$(curl -sS -m 10 -w '\n%{http_code}' "$@" 2>&1) || true
  echo "${reply##*$'\n'} $note: ${reply%$'\n'*}"
}

rules=$(curl -fsS -m 10 "$WORLD/rules" -H "$auth") && game=$(curl -fsS -m 10 "$WORLD/game" -H "$auth") || {
  echo "the world does not answer"
  exit 1
}
me=$(jq -r .you <<<"$rules")
others=$(jq -r '.seats - [.you] | .[]' <<<"$rules")
first=$(jq -r '.seats - [.you] | .[0]' <<<"$rules")
pool=$(jq .pool <<<"$game")

# Read the needs and the takes: routes that a careless world could have.
for path in /needs /need /takes /take /mine /instance /events /admin/events /admin/state; do
  try "GET $path" "$WORLD$path" -H "$auth"
done

for other in $others; do
  # Read the need and the take of the other seat.
  try "GET /game as $other" "$WORLD/game?seat=$other" -H "$auth" -H "X-Seat: $other"
  try "GET /game/$other" "$WORLD/game/$other" -H "$auth"
  try "GET /need/$other" "$WORLD/need/$other" -H "$auth"
  try "GET /take/$other" "$WORLD/take/$other" -H "$auth"

  # Take, claim and pledge as the other seat: the whole pool as its take, the whole pool as its need
  # (the largest claim, so that it is the seat that yields), and 0 as its pledge.
  for route in /take /claim /pledge; do
    case $route in
    /take) body="{\"amount\": $pool, \"seat\": \"$other\"}" ;;
    /claim) body="{\"need\": $pool, \"seat\": \"$other\"}" ;;
    /pledge) body="{\"amount\": 0, \"seat\": \"$other\"}" ;;
    esac
    try "POST $route as $other, no token" "$WORLD$route" -H "X-Seat: $other" -d "$body"
    try "POST $route as $other, token guess: its name" "$WORLD$route" \
      -H "Authorization: Bearer $other" -H "X-Seat: $other" -d "$body"
    try "POST $route as $other, token guess: a turned token" "$WORLD$route" \
      -H "Authorization: Bearer $wrong" -H "X-Seat: $other" -d "$body"
    try "POST $route as $other, own token" "$WORLD$route" -H "$auth" -H "X-Seat: $other" -d "$body"
  done
done

# Take more than one take allows: above the pool, and two takes in one request.
try "POST /take above the pool" "$WORLD/take" -H "$auth" -d "{\"amount\": $((pool + 1))}"
try "POST /take with two takes" "$WORLD/take" -H "$auth" -d "[{\"amount\": $pool}, {\"amount\": $pool}]"

# Close the world before the other seats take.
try "POST /admin/close" "$WORLD/admin/close" -H "$auth" -d '{}'
try "POST /admin/close, no token" "$WORLD/admin/close" -d '{}'

# Take twice. The first take is this seat's own, of 0 units; its header names another seat, and a sound
# world reads no such header. The second take asks for the whole pool, and a sound world refuses it: a
# seat has one take and cannot change it. The seat reads the claims first, so that the diagnostics on
# takes without a read do not count its take.
try "GET /claims" "$WORLD/claims" -H "$auth"
try "POST /take of 0, X-Seat names $first" "$WORLD/take" -H "$auth" -H "X-Seat: $first" -d '{"amount": 0}'
try "POST /take again, of the pool" "$WORLD/take" -H "$auth" -d "{\"amount\": $pool}"

echo "$me ends with $(curl -sS -m 10 "$WORLD/rules" -H "$auth")"
