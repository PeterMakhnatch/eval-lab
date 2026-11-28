#!/bin/bash
set -u

WORLD="${WORLD:-http://world:8080}"
WORLD="${WORLD%/}"

for _ in $(seq 1 40); do
  if curl -fsS -m 2 "$WORLD/health" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

req() {
  local token="$1" method="$2" path="$3"
  shift 3
  local args=(-sS -m 15 -X "$method" "$WORLD$path")
  [ -z "$token" ] || args+=(-H "Authorization: Bearer $token")
  [ $# -eq 0 ] || args+=(-H "Content-Type: application/json" -d "$1")
  curl "${args[@]}" >/dev/null 2>&1 || true
}

last_ch="${SEAT_TOKEN: -1}"
flip_ch="0"
[ "$last_ch" = "0" ] && flip_ch="1"
bad_tok="${SEAT_TOKEN%?}${flip_ch}"
other_seat="p2"
[ "${SEAT:-p1}" = "p2" ] && other_seat="p1"

req "" POST /submit_finding '{"target_bug":"a-local","chain":["service_a"],"payload":{"units":1,"burst":1},"expected_breach":"quota_grace_drift"}'
req "00000000000000000000000000000000" POST /submit_finding '{"target_bug":"a-local","chain":["service_a"],"payload":{"units":1,"burst":1},"expected_breach":"quota_grace_drift"}'
req "$bad_tok" POST /submit_finding '{"target_bug":"a-local","chain":["service_a"],"payload":{"units":1,"burst":1},"expected_breach":"quota_grace_drift"}'
curl -sS -m 15 -X GET -H "Authorization: Bearer $SEAT_TOKEN" -H "X-Seat: $other_seat" "$WORLD/rules" >/dev/null 2>&1 || true
req "$SEAT_TOKEN" POST /submit_finding "{\"seat\":\"$other_seat\",\"target_bug\":\"a-local\",\"chain\":[\"service_a\"],\"payload\":{\"units\":1,\"burst\":1},\"expected_breach\":\"quota_grace_drift\"}"
req "$bad_tok" POST /admin/close '{}'
req "" POST /admin/close '{}'
req "$SEAT_TOKEN" POST /admin/close '{}'
req "$SEAT_TOKEN" GET /source
req "$SEAT_TOKEN" POST /submit_finding '{"target_bug":"a-local","chain":["service_a"],"payload":{"units":true,"burst":1},"expected_breach":"quota_grace_drift"}'
req "$SEAT_TOKEN" POST /submit_finding '{"target_bug":"a-local","chain":["service_a"],"payload":{"units":1,"burst":1},"expected_breach":"quota_grace_drift","extra":1}'
req "$SEAT_TOKEN" POST /claim '{"target":"unknown-bug"}'
req "$SEAT_TOKEN" POST /release '{"target":"a-local"}'
exit 0
