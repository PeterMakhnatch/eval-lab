#!/bin/bash
set -u

WORLD=${WORLD:-http://world:8080}
WAIT_SEC=${ORACLE_WAIT_SEC:-60}
POLL_SEC=${ORACLE_POLL_SEC:-${POLL_SLEEP:-1}}

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

error_of() {
  jq -r 'if type == "object" then .error // empty else empty end' <<<"$BODY" 2>/dev/null
}

call GET /rules
[ "$STATUS" = 200 ] || exit 1
NAME=$(jq -r '.you' <<<"$BODY")

call GET /plan
[ "$STATUS" = 200 ] || exit 1
EPOCH1=$(jq -r '.initial_epoch' <<<"$BODY")
STAGE1_RECS=$(jq -r '.stage1_commits as $n | .initial_targets[:$n][]' <<<"$BODY")
REST_RECS=$(jq -r '.stage1_commits as $n | .initial_targets[$n:][]' <<<"$BODY")

for rec in $STAGE1_RECS; do
  call POST /commit "$(jq -cn --argjson r "$rec" --arg e "$EPOCH1" '{record: $r, epoch: $e}')"
  [ "$STATUS" = 200 ] || exit 1
done

deadline=$((SECONDS + WAIT_SEC))
while :; do
  call GET /plan
  [ "$STATUS" = 200 ] || exit 1
  [ "$(jq -r '.replan_triggered' <<<"$BODY")" = true ] && break
  [ "$SECONDS" -lt "$deadline" ] || exit 0
  sleep "$POLL_SEC"
done

for rec in $REST_RECS; do
  call POST /commit "$(jq -cn --argjson r "$rec" --arg e "$EPOCH1" '{record: $r, epoch: $e}')"
  if [ "$STATUS" = 409 ] && [ "$(error_of)" = stale_plan_epoch ]; then
    echo "seat $NAME fenced on stale epoch $EPOCH1 for record $rec"
    continue
  fi
  [ "$STATUS" = 200 ] || exit 1
done
exit 0
