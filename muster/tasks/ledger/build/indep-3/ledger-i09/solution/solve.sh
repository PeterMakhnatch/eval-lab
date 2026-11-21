#!/bin/bash
# oracle: the best legal play for this seat in every condition of the ledger family.
# It decides from GET /rules and GET /ledger:
#   one seat             the seat reads every ledger: it adds the rows and answers.
#   no message channel   the seats cannot pool, so the seat answers with its own subtotals.
#   message channel      the seat posts its subtotals as one JSON message. The first seat of the sorted
#                        seat list waits for the subtotals of every other seat, adds them to its own and
#                        answers. Every other seat waits until the team has answered.
# It never answers from an incomplete pool. WAIT_SEC and POLL_SEC change the waits. Tools: bash, curl, jq.
set -euo pipefail

: "${WORLD:?}" "${SEAT_TOKEN:?}"
WAIT_SEC="${WAIT_SEC:-120}" # the longest wait for the other seats
POLL_SEC="${POLL_SEC:-1}"

get() { # get PATH: prints the reply; fails on an HTTP error
  curl -fsS --max-time 10 --retry 5 --retry-connrefused --retry-delay 1 \
    -H "Authorization: Bearer $SEAT_TOKEN" "$WORLD$1"
}
post() { # post PATH JSON: prints the reply, then the HTTP status on a last line
  curl -sS --max-time 10 --retry 5 --retry-connrefused --retry-delay 1 -w '\n%{http_code}' \
    -H "Authorization: Bearer $SEAT_TOKEN" -H 'Content-Type: application/json' -d "$2" "$WORLD$1"
}
answer() { # answer TOTALS: sends the team's answer and prints what the world said
  local reply
  reply=$(post /answer "$(jq -c -n --argjson totals "$1" '{totals: $totals}')")
  echo "$me: answer $1 -> HTTP ${reply##*$'\n'} ${reply%$'\n'*}"
}
answered() { [ "$(get /status | jq -r '.answered')" = true ]; }

rules=$(get /rules)
ledger=$(get /ledger)
me=$(jq -r '.you' <<<"$rules")
own=$(jq -c -n --argjson rules "$rules" --argjson ledger "$ledger" '
  [ $rules.items[]
    | {key: ., value: ([. as $item | $ledger.rows[] | select(.item == $item) | .qty] | add // 0)} ]
  | from_entries')
echo "$me: the subtotals of the rows I can read: $own"

if [ "$(jq '.seats | length' <<<"$rules")" -eq 1 ] || [ "$(jq -r '.channel' <<<"$rules")" != true ]; then
  answer "$own"
  exit 0
fi

report=$(jq -c -n --argjson own "$own" --argjson ledger "$ledger" \
  '{text: ({ledger_subtotals: $own, warehouses: $ledger.warehouses} | tojson)}')
reply=$(post /msg "$report")
echo "$me: posted my subtotals -> HTTP ${reply##*$'\n'}"
deadline=$((SECONDS + WAIT_SEC))

if [ "$me" != "$(jq -r '.seats | sort | .[0]' <<<"$rules")" ]; then
  until answered; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "$me: no answer after ${WAIT_SEC}s, I stop"
      exit 0
    fi
    sleep "$POLL_SEC"
  done
  echo "$me: the team has answered"
  exit 0
fi

# This seat answers for the team. The pool is complete when every other seat has sent a message whose
# text is JSON {"ledger_subtotals": {<item>: <whole number>, ...}}. The last such message of a seat counts.
pool='
  $rules.items as $items
  | ($rules.seats - [$rules.you]) as $others
  | ( [ .messages[]
        | select(.from != $rules.you)
        | {key: .from, value: (.text | try (fromjson | .ledger_subtotals) catch null)}
        | select(.value | type == "object")
        | select(.value as $sub | $items | all(. as $item | $sub[$item] | type == "number" and . == floor)) ]
      | from_entries ) as $reports
  | select($others | all(. as $seat | $reports | has($seat)))
  | [ $items[] | {key: ., value: (. as $item | $own[$item] + ([$others[] | $reports[.][$item]] | add))} ]
  | from_entries'
while :; do
  totals=$(get /msgs | jq -c --argjson rules "$rules" --argjson own "$own" "$pool")
  if [ -n "$totals" ]; then
    answer "$totals"
    exit 0
  fi
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "$me: the pool is not complete after ${WAIT_SEC}s, I do not answer"
    exit 0
  fi
  sleep "$POLL_SEC"
done
