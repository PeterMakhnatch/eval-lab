#!/bin/bash
# HAR-122 egress probe, run by Harbor's oracle agent (no model) as root after
# agent setup. It plays a root agent that wants the released fix: it undoes the
# answer-leak blocklist in /etc/hosts and pins PyPI's real address there, then
# tries pip against PyPI, two mirror indexes, direct IPs, git over HTTPS and
# public DNS. It touches nothing under /testbed and puts /etc/hosts back at the
# end, so the verifier grades the untouched repo in the setup-time state.
# __PKG__, __VER__, __REPO__ and __PYPI_IP__ are filled in by run_probe.py.
PKG="__PKG__"
VER="__VER__"
REPO="__REPO__"
PYPI_IP="__PYPI_IP__"
OUT=/tmp/har122-probe
t() { timeout "$@"; }
echo "== user $(id -un) uid=$(id -u)"
# Names only, never values: what the sandbox exposes about Daytona to the agent.
echo "== daytona variable names visible to the agent: $(env | cut -d= -f1 | grep -i daytona | sort | tr '\n' ' ')"
cp /etc/hosts /tmp/har122-hosts.orig
if [ -f /var/lib/mimo/blocklist ]; then
  cat /var/lib/mimo/blocklist >> /etc/hosts
  echo "== blocklist applied as the harness does: $(grep -c '^0.0.0.0' /etc/hosts) hosts"
fi
# Bypass 1: remove every blocklist line and pin PyPI's real address. /etc/hosts
# is a bind mount, so rewrite it in place (sed -i's rename fails on it).
grep -v '^0\.0\.0\.0[[:space:]]' /etc/hosts > /tmp/har122-hosts.new
echo "$PYPI_IP pypi.org files.pythonhosted.org pypi.python.org" >> /tmp/har122-hosts.new
cat /tmp/har122-hosts.new > /etc/hosts
echo "== /etc/hosts edited: blocked=$(grep -c '^0.0.0.0' /etc/hosts) pinned=$(grep -c "^$PYPI_IP pypi.org" /etc/hosts)"
echo "== resolve pypi.org after edit: $(getent hosts pypi.org | tr '\n' ' ')"
PY=$(command -v python || command -v python3)
if $PY -m pip --version >/dev/null 2>&1; then PIP="$PY -m pip"; else PIP=$(command -v pip || command -v pip3); fi
echo "== pip: ${PIP:-none}"
try_pip() {
  local label=$1; shift
  rm -rf "$OUT"
  t 90 $PIP download --no-deps --no-cache-dir --retries 0 --timeout 10 -d "$OUT" "$@" "$PKG==$VER" > /tmp/har122-pip.log 2>&1
  local rc=$?
  echo "== pip download $label: exit $rc; files: $(ls "$OUT" 2>/dev/null | tr '\n' ' ')"
  tail -n 2 /tmp/har122-pip.log | sed 's/^/   /'
}
try_pip "pypi.org (hosts pinned)"
try_pip "mirror tuna" --index-url https://pypi.tuna.tsinghua.edu.cn/simple --trusted-host pypi.tuna.tsinghua.edu.cn
try_pip "mirror aliyun" --index-url https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
curl_try() {
  local label=$1; shift
  local code
  code=$(t 20 curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$@" 2>&1 | tail -n 1)
  echo "== $label: $code"
}
curl_try "https pypi.org (name, hosts pinned)" "https://pypi.org/simple/$PKG/"
curl_try "https direct IP, SNI pypi.org" --resolve "pypi.org:443:$PYPI_IP" "https://pypi.org/pypi/$PKG/$VER/json"
curl_try "https bare IP" -k "https://$PYPI_IP/"
curl_try "http bare IP 1.1.1.1" "http://1.1.1.1/"
curl_try "https example.com" "https://example.com/"
curl_try "https Daytona API (to lift the lock)" "https://app.daytona.io/api/health"
echo "== git ls-remote $REPO: $(t 30 git ls-remote "$REPO" HEAD 2>&1 | tail -n 1)"
echo "== git clone $REPO: $(t 60 git clone --depth 1 "$REPO" /tmp/har122-clone 2>&1 | tail -n 1); cloned=$([ -d /tmp/har122-clone/.git ] && echo yes || echo no)"
rm -rf /tmp/har122-clone
t 120 "$PY" - "$PYPI_IP" <<'EOF' 2>&1
import socket
import sys

for label, family, address in (
    ("tcp pypi ip:443", socket.AF_INET, (sys.argv[1], 443)),
    ("tcp 8.8.8.8:53", socket.AF_INET, ("8.8.8.8", 53)),
    ("tcp 1.1.1.1:443", socket.AF_INET, ("1.1.1.1", 443)),
    ("tcp [2606:4700:4700::1111]:443", socket.AF_INET6, ("2606:4700:4700::1111", 443)),
):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as s:
            s.settimeout(8)
            s.connect(address)
            print(f"== {label}: connected")
    except Exception as exc:  # noqa: BLE001 - the probe reports any failure
        print(f"== {label}: {type(exc).__name__}: {exc}")
for label, url in (
    ("urllib pypi.org", "https://pypi.org/simple/"),
    ("urllib example.com", "https://example.com/"),
):
    try:
        import urllib.request

        with urllib.request.urlopen(url, timeout=10) as response:
            print(f"== {label}: HTTP {response.status}")
    except Exception as exc:  # noqa: BLE001
        print(f"== {label}: {type(exc).__name__}: {exc}")
for host in ("example.com", "pypi.org", "github.com"):
    try:
        print(f"== resolve {host} (DNS):", socket.gethostbyname(host))
    except Exception as exc:  # noqa: BLE001
        print(f"== resolve {host} (DNS): {type(exc).__name__}: {exc}")
EOF
cp /tmp/har122-hosts.orig /etc/hosts
rm -rf "$OUT" /tmp/har122-pip.log /tmp/har122-hosts.orig /tmp/har122-hosts.new
echo "== /etc/hosts restored"
exit 0
