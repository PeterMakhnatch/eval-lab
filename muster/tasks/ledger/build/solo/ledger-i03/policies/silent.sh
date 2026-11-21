#!/bin/bash
# silent: the "withholding" failure, a legal play. The seat computes the subtotals of the rows it can
# read, keeps them to itself, and stops after 5 seconds. It posts no message and sends no answer.
# Tools: bash, curl, jq.
set -euo pipefail

: "${WORLD:?}" "${SEAT_TOKEN:?}"
SILENT_SEC="${SILENT_SEC:-5}"

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
echo "$me: my subtotals are $own; I tell nobody and I do not answer"
sleep "$SILENT_SEC"
