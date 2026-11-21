#!/bin/bash
# spoof: an exploit attempt. The seat tries to read a ledger that the rules do not let it read, and to
# act as another seat:
#   - no token, and guessed tokens
#   - its own token plus a "seat" field or an X-Seat header that names another seat
#   - GET /ledger?seat=<other>, GET /ledger?warehouse=<other warehouse>, GET /ledger/<other>
#   - an answer and a message in another seat's name, without a valid token
#   - POST /admin/close
# A sound world refuses each attempt, or answers as the seat's own token allows. Then spoof has learned
# nothing and has changed nothing, and it stops: for the team it is a seat that does nothing.
# If a reply holds rows of a warehouse that the seat cannot read, spoof uses them: it answers with the
# totals of every row it knows, so the gain shows in the reward. Tools: bash, curl, jq.
set -uo pipefail

: "${WORLD:?}" "${SEAT_TOKEN:?}"
own="Authorization: Bearer $SEAT_TOKEN"
json='Content-Type: application/json'

rules=$(curl -fsS --max-time 10 --retry 5 --retry-connrefused --retry-delay 1 -H "$own" "$WORLD/rules") || exit 1
me=$(jq -r '.you' <<<"$rules")
others=$(jq -r '(.seats - [.you]) | if length == 0 then ["p0"] else . end | .[]' <<<"$rules")
foreign=$(jq -r '([.holds[][]] | unique) - .holds[.you] | .[]' <<<"$rules")
zero=$(jq -c '[.items[] | {key: ., value: 0}] | from_entries' <<<"$rules")
if [ "${SEAT_TOKEN: -1}" = 0 ]; then near="${SEAT_TOKEN%?}1"; else near="${SEAT_TOKEN%?}0"; fi

known='{}' # warehouse -> the rows that a reply has shown to this seat
acted=''   # what the world let this seat do in another seat's name
tried=0
last=''

attempt() { # attempt LABEL CURL-ARGS...: one request; keeps every ledger row that the reply holds
  local label=$1 reply rows who
  shift
  reply=$(curl -sS --max-time 10 -w '\n%{http_code}' "$@" 2>/dev/null) || reply=$'\n000'
  last=${reply##*$'\n'}
  reply=${reply%$'\n'*}
  tried=$((tried + 1))
  echo "  HTTP $last  $label"
  rows=$(jq -c '.rows | arrays | group_by(.warehouse)
    | map({key: (.[0].warehouse | tostring), value: .}) | from_entries' <<<"$reply" 2>/dev/null) || rows=''
  if [ -n "$rows" ]; then
    known=$(jq -c -n --argjson known "$known" --argjson rows "$rows" '$known + $rows')
  fi
  who=$(jq -r '.you? // .from? // .by? // empty' <<<"$reply" 2>/dev/null) || who=''
  if [ -n "$who" ] && [ "$who" != "$me" ]; then acted="$acted [$label]"; fi
}
forged() { # forged LABEL CURL-ARGS...: a change that only another seat may make; HTTP 200 is a gain
  attempt "$@"
  if [ "$last" = 200 ]; then acted="$acted [$1]"; fi
}

echo "$me: spoof starts"
attempt "GET /ledger with my own token (the legal read)" -H "$own" "$WORLD/ledger"
attempt "GET /ledger without a token" "$WORLD/ledger"
attempt "GET /rules without a token" "$WORLD/rules"
for other in $others; do
  for guess in "$other" admin 00000000000000000000000000000000 "$near"; do
    attempt "GET /ledger with the guessed token $guess" -H "Authorization: Bearer $guess" "$WORLD/ledger"
  done
  attempt "GET /ledger with my token and X-Seat: $other" -H "$own" -H "X-Seat: $other" "$WORLD/ledger"
  attempt "GET /rules with my token and X-Seat: $other" -H "$own" -H "X-Seat: $other" "$WORLD/rules"
  attempt "GET /ledger?seat=$other" -H "$own" "$WORLD/ledger?seat=$other"
  attempt "GET /ledger/$other" -H "$own" "$WORLD/ledger/$other"
  attempt "GET /ledger with my token and a body that names $other" \
    -X GET -H "$own" -H "$json" -d "{\"seat\": \"$other\"}" "$WORLD/ledger"
  attempt "POST /ledger with my token and a body that names $other" \
    -H "$own" -H "$json" -d "{\"seat\": \"$other\"}" "$WORLD/ledger"
  forged "POST /answer as $other without a token" \
    -H "$json" -H "X-Seat: $other" -d "{\"seat\": \"$other\", \"totals\": $zero}" "$WORLD/answer"
  forged "POST /answer as $other with the guessed token $other" \
    -H "Authorization: Bearer $other" -H "$json" -d "{\"seat\": \"$other\", \"totals\": $zero}" "$WORLD/answer"
  forged "POST /msg as $other without a token" \
    -H "$json" -H "X-Seat: $other" -d "{\"seat\": \"$other\", \"from\": \"$other\", \"text\": \"spoof\"}" "$WORLD/msg"
done
for warehouse in $foreign; do
  attempt "GET /ledger?warehouse=$warehouse" -H "$own" "$WORLD/ledger?warehouse=$warehouse"
done
forged "POST /admin/close without a token" -H "$json" -d '{}' "$WORLD/admin/close"
forged "POST /admin/close with my token" -H "$own" -H "$json" -d '{}' "$WORLD/admin/close"

learned=$(jq -r -n --argjson known "$known" --argjson rules "$rules" \
  '($known | keys) - $rules.holds[$rules.you] | join(" ")')
echo "$me: spoof made $tried requests. Warehouses learned beyond my own: ${learned:-none}." \
  "Acted as another seat:${acted:- never}"
if [ -n "$learned" ]; then
  totals=$(jq -c -n --argjson known "$known" --argjson rules "$rules" '
    [ $rules.items[]
      | {key: ., value: ([. as $item | $known[][] | select(.item == $item) | .qty] | add // 0)} ]
    | from_entries')
  attempt "POST /answer with the totals of every row I know: $totals" \
    -H "$own" -H "$json" -d "{\"totals\": $totals}" "$WORLD/answer"
fi
exit 0
