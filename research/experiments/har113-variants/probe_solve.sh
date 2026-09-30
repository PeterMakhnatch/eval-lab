#!/bin/bash
# HAR-113 leak probe, run by Harbor's oracle agent in place of a model: apply
# the task's answer-leak blocklist exactly as the lab's Terminus harness does
# (evallab.harbor_terminus.apply_mimo_blocklist), then try to fetch the
# released fix from PyPI. Touches nothing under /testbed; the verifier then
# grades the untouched repo with the blocklist still in /etc/hosts.
PKG="__PKG__"
VER="__VER__"
OUT=/tmp/har113-probe
echo "== user $(id -un) uid=$(id -u)"
if [ -f /var/lib/mimo/blocklist ]; then
  cat /var/lib/mimo/blocklist >> /etc/hosts && echo "== blocklist applied: $(grep -c '^0.0.0.0' /etc/hosts) hosts blocked"
else
  echo "== no blocklist staged"
fi
for host in pypi.org files.pythonhosted.org pypi.python.org; do
  echo "== resolve $host: $(getent hosts "$host" | head -n 1)"
done
echo "== pip index settings"
env | grep -i -E '^(pip_|uv_|https?_proxy|no_proxy)' || echo "(no pip/uv/proxy env)"
for f in /etc/pip.conf /etc/xdg/pip/pip.conf "$HOME/.pip/pip.conf" "$HOME/.config/pip/pip.conf"; do
  [ -f "$f" ] && { echo "-- $f"; sed -n '1,20p' "$f"; }
done
PY=$(command -v python || command -v python3)
echo "== python $PY ($($PY --version 2>&1))"
if $PY -m pip --version >/dev/null 2>&1; then PIP="$PY -m pip"
elif command -v pip >/dev/null 2>&1; then PIP=pip
elif command -v pip3 >/dev/null 2>&1; then PIP=pip3
else PIP=""; fi
echo "== pip: ${PIP:-none} ($($PIP --version 2>&1))"
if [ -n "$PIP" ]; then
  $PIP config list 2>&1 | sed 's/^/pip config: /'
  rm -rf "$OUT"
  timeout 180 $PIP download --no-deps --no-cache-dir --retries 1 --timeout 15 -d "$OUT" "$PKG==$VER" > /tmp/har113-pip.log 2>&1
  RC=$?
  tail -n 6 /tmp/har113-pip.log
  echo "== pip download $PKG==$VER exit $RC; files: $(ls "$OUT" 2>/dev/null | tr '\n' ' ')"
fi
timeout 60 "$PY" - "$PKG" "$VER" <<'EOF' 2>&1
import socket
import sys
import urllib.request

pkg, ver = sys.argv[1:3]
for url in (f"https://pypi.org/pypi/{pkg}/{ver}/json", "https://files.pythonhosted.org/"):
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            print(f"== https {url}: HTTP {response.status}")
    except Exception as exc:  # noqa: BLE001 - the probe reports any failure
        print(f"== https {url}: {type(exc).__name__}: {exc}")
# The blocklist maps names to 0.0.0.0 only, so an IPv6 lookup still reaches
# DNS. Whether that matters depends on the sandbox routing IPv6.
for host in ("pypi.org", "files.pythonhosted.org"):
    try:
        address = socket.getaddrinfo(host, 443, socket.AF_INET6, socket.SOCK_STREAM)[0][4]
        with socket.create_connection(address[:2], timeout=10):
            print(f"== ipv6 {host} {address[0]}: connected")
    except Exception as exc:  # noqa: BLE001 - the probe reports any failure
        print(f"== ipv6 {host}: {type(exc).__name__}: {exc}")
EOF
rm -rf "$OUT" /tmp/har113-pip.log
exit 0
