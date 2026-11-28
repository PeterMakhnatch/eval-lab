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

commit_record() {
  local rec=$1 ep=$2
  call POST /commit "$(jq -cn --argjson r "$rec" --arg e "$ep" '{record: $r, epoch: $e}')"
  if [ "$STATUS" != 200 ]; then
    echo "commit $rec ($ep) refused: $STATUS $BODY" >&2
    return 1
  fi
  return 0
}

wait_for_replan() {
  local deadline=$((SECONDS + WAIT_SEC))
  while :; do
    call GET /plan
    [ "$STATUS" = 200 ] || return 1
    if [ "$(jq -r '.replan_triggered' <<<"$BODY")" = true ]; then
      return 0
    fi
    [ "$SECONDS" -lt "$deadline" ] || return 2
    sleep "$POLL_SEC"
  done
}

read_named_update() {
  local deadline=$((SECONDS + WAIT_SEC)) parsed
  while :; do
    call GET /notices
    [ "$STATUS" = 200 ] || return 1
    parsed=$(jq -r --arg alert "$ALERT_SEAT" --arg ep1 "$EPOCH1" --argjson mine "$MY_RECS_JSON" '
      [.notices[] | select(.from == $alert and .epoch != $ep1)] | last // empty
      | "\(.epoch) \([.records[] | select(. as $r | $mine | index($r))] | map(tostring) | join(" "))"
    ' <<<"$BODY")
    if [ -n "$parsed" ]; then
      EPOCH2=${parsed%% *}
      MY_E2=${parsed#* }
      [ "$MY_E2" != "$EPOCH2" ] || MY_E2=
      return 0
    fi
    [ "$SECONDS" -lt "$deadline" ] || return 2
    sleep "$POLL_SEC"
  done
}

read_chat_update() {
  local deadline=$((SECONDS + WAIT_SEC)) parsed
  while :; do
    call GET /msgs
    [ "$STATUS" = 200 ] || return 1
    parsed=$(jq -r --arg alert "$ALERT_SEAT" --arg ep1 "$EPOCH1" --argjson mine "$MY_RECS_JSON" '
      [.messages[] | select(.from == $alert) | .text | fromjson? | objects
        | select((.epoch | type) == "string" and .epoch != $ep1 and (.records | type) == "array")] | last // empty
      | "\(.epoch) \([.records[] | select(. as $r | $mine | index($r))] | map(tostring) | join(" "))"
    ' <<<"$BODY")
    if [ -n "$parsed" ]; then
      EPOCH2=${parsed%% *}
      MY_E2=${parsed#* }
      [ "$MY_E2" != "$EPOCH2" ] || MY_E2=
      return 0
    fi
    [ "$SECONDS" -lt "$deadline" ] || return 2
    sleep "$POLL_SEC"
  done
}

call GET /rules
[ "$STATUS" = 200 ] || exit 1
read -r NAME CHANNEL NOTICES FENCE ALERT_SEAT LEFT <<<"$(jq -r '
  "\(.you) \(.channel) \(.notices) \(.epoch_fence) \(.alert_seat) \(.budget_left.commit)"
' <<<"$BODY")"

call GET /plan
[ "$STATUS" = 200 ] || exit 1
EPOCH1=$(jq -r '.initial_epoch' <<<"$BODY")
STAGE1_RECS=$(jq -r '.stage1_commits as $n | .initial_targets[:$n][]' <<<"$BODY")
MY_RECS_JSON=$(jq -c '.your_records' <<<"$BODY")

echo "seat $NAME: channel=$CHANNEL notices=$NOTICES fence=$FENCE epoch1=$EPOCH1"
for rec in $STAGE1_RECS; do
  commit_record "$rec" "$EPOCH1" || exit 1
done

wait_for_replan
rc=$?
[ "$rc" -eq 1 ] && exit 1
[ "$rc" -eq 2 ] && exit 0

if [ "$NAME" = "$ALERT_SEAT" ]; then
  call GET /alerts
  [ "$STATUS" = 200 ] || exit 1
  EPOCH2=$(jq -r '.alerts[0].epoch' <<<"$BODY")
  ALL_RECS=$(jq -c '.alerts[0].records' <<<"$BODY")
  MY_E2=$(jq -r --arg me "$NAME" '.alerts[0].targets[$me][]' <<<"$BODY")
  if [ "$NOTICES" = true ]; then
    call POST /notice "$(jq -cn --arg ep "$EPOCH2" --argjson recs "$ALL_RECS" '{epoch: $ep, records: $recs}')"
    [ "$STATUS" = 200 ] || exit 1
  elif [ "$CHANNEL" = true ]; then
    call POST /msg "$(jq -cn --arg ep "$EPOCH2" --argjson recs "$ALL_RECS" '{text: ({epoch: $ep, records: $recs} | tojson)}')"
    [ "$STATUS" = 200 ] || exit 1
  fi
  for rec in $MY_E2; do
    commit_record "$rec" "$EPOCH2" || exit 1
  done
  exit 0
fi

if [ "$NOTICES" = true ]; then
  read_named_update
  rc=$?
  [ "$rc" -eq 1 ] && exit 1
  [ "$rc" -eq 2 ] && exit 0
  for rec in $MY_E2; do
    commit_record "$rec" "$EPOCH2" || exit 1
  done
elif [ "$CHANNEL" = true ]; then
  read_chat_update
  rc=$?
  [ "$rc" -eq 1 ] && exit 1
  [ "$rc" -eq 2 ] && exit 0
  for rec in $MY_E2; do
    commit_record "$rec" "$EPOCH2" || exit 1
  done
fi
exit 0
