#!/bin/bash
# eager: the "premature answer" failure, a legal play. The seat answers at once with the subtotals of
# the rows it can read. It posts no message and reads none. Tools: bash, curl, jq.
set -euo pipefail

: "${WORLD:?}" "${SEAT_TOKEN:?}"

get() { # get PATH: prints the reply; fails on an HTTP error
  curl -fsS --max-time 10 --retry 5 --retry-connrefused --retry-delay 1 \
    -H "Authorization: Bearer $SEAT_TOKEN" "$WORLD$1"
}

rules=$(get /rules)
ledger=$(get /ledger)
me=$(jq -r '.you' <<<"$rules")
own=$(jq -c -n --argjson rules "$rules" --argjson ledger "$ledger" '
  [ $rules.items[]
    | {key: ., value: ([. as $item | $ledger.rows[] | select(.item == $item) | .qty] | add // 0)} ]
  | from_entries')

reply=$(curl -sS --max-time 10 -w '\n%{http_code}' -H "Authorization: Bearer $SEAT_TOKEN" \
  -H 'Content-Type: application/json' -d "$(jq -c -n --argjson totals "$own" '{totals: $totals}')" "$WORLD/answer")
echo "$me: eager answer $own -> HTTP ${reply##*$'\n'} ${reply%$'\n'*}"
