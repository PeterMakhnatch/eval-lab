#!/bin/bash
python3 -m http.server 9000 --bind :: >/dev/null 2>&1 </dev/null &
server=$!
sleep 2

addrs=$(python3 - <<'PY'
import ipaddress, socket
out = []
for line in open("/proc/net/if_inet6"):
    hexaddr, _, _, scope, _, dev = line.split()
    if dev == "lo" or scope != "00":
        continue
    out.append(str(ipaddress.IPv6Address(bytes.fromhex(hexaddr))))
try:
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        out.append(info[4][0])
except OSError:
    pass
print(" ".join(sorted(set(out))))
PY
)
echo "SELF $SEAT addrs=[$addrs]"

auth=(-H "Authorization: Bearer $SEAT_TOKEN")
code=$(curl -sS -m 5 -o /dev/null -w '%{http_code}' "$WORLD/health" 2>/dev/null)
[ "$code" = 200 ] && echo "PASS world_reachable code=$code" || echo "FAIL world_reachable code=$code"

curl -sS -m 5 -o /dev/null "${auth[@]}" -H 'Content-Type: application/json' \
  -d "$(jq -cn --arg t "addr $SEAT $addrs" '{text: $t}')" "$WORLD/msg"
sleep 8
msgs=$(curl -sS -m 5 "${auth[@]}" "$WORLD/msgs")

probe() {
  if curl -sS -g -m 3 -o /dev/null "$2" 2>/dev/null; then echo "FAIL $1 reached $2"; else echo "PASS $1 blocked $2"; fi
}
for other in p1 p2 p3 p4 p5 p6 p7 p8; do
  [ "$other" = "$SEAT" ] && continue
  if getent hosts "$other" >/dev/null 2>&1; then echo "FAIL dns_$other resolves $(getent hosts "$other" | head -1)"; else echo "PASS dns_$other does_not_resolve"; fi
  probe "name_$other" "http://$other:9000/"
  for addr in $(jq -r --arg o "$other" '.messages[] | .text | select(startswith("addr " + $o + " ")) | split(" ")[2:][]' <<<"$msgs"); do
    case "$addr" in *:*) url="http://[$addr]:9000/" ;; *) url="http://$addr:9000/" ;; esac
    probe "addr_$other" "$url"
  done
done
for addr in $addrs; do
  case "$addr" in *:*) url="http://[$addr]:9000/" ;; *) url="http://$addr:9000/" ;; esac
  if curl -sS -g -m 3 -o /dev/null "$url" 2>/dev/null; then echo "PASS control_self reached $url"; else echo "FAIL control_self blocked $url"; fi
done
probe internet "https://example.com/"
probe internet_ip "http://1.1.1.1/"
kill "$server" 2>/dev/null
exit 0
