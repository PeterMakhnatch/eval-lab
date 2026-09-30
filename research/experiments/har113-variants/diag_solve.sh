#!/bin/bash
# HAR-113 environment diagnosis, run by Harbor's oracle agent in place of a
# model: which interpreters exist, which of them has pytest and the project
# module, and what the test command's PATH resolves. Read-only; the verifier
# then grades the untouched repo exactly as a nop would.
MODULE="__MODULE__"
echo "== cwd $(pwd); PATH=$PATH"
for tool in python python3 pytest pip pip3 conda uv; do
  echo "== which -a $tool: $(which -a "$tool" 2>/dev/null | tr '\n' ' ')"
done
echo "== login-shell PATH: $(bash -lc 'echo $PATH' 2>/dev/null)"
CANDIDATES=$( (ls -d /usr/bin/python3* /usr/local/bin/python3* /usr/bin/python /usr/local/bin/python \
  /root/.venv/bin/python /testbed/.venv/bin/python /testbed/venv/bin/python /opt/*/bin/python \
  /opt/conda/bin/python /opt/conda/envs/*/bin/python /root/.pyenv/versions/*/bin/python \
  /root/miniconda3/bin/python /root/miniconda3/envs/*/bin/python /root/.cache/pypoetry/virtualenvs/*/bin/python \
  /usr/local/python*/bin/python3 2>/dev/null; find / -xdev -path /proc -prune -o -type f -name 'python3.[0-9]*' -perm -u+x -print 2>/dev/null) \
  | grep -v -E '(-config|m-config)$' | sort -u | head -n 40)
for py in $CANDIDATES; do
  [ -x "$py" ] || continue
  "$py" - "$MODULE" <<'EOF' 2>&1 | sed "s|^|== $py: |"
import importlib.util
import sys

found = {}
for name in ("pytest", sys.argv[1], "setuptools", "Cython"):
    try:
        spec = importlib.util.find_spec(name)
        found[name] = spec.origin if spec else None
    except Exception as exc:  # noqa: BLE001 - the probe reports any failure
        found[name] = f"error {type(exc).__name__}"
print(sys.version.split()[0], found)
EOF
done
echo "== pytest executables:"
find / -xdev -path /proc -prune -o -type f -path '*/bin/pytest' -print 2>/dev/null | head -n 20
echo "== git status --ignored (top 30):"
git status --ignored --porcelain 2>/dev/null | head -n 30
echo "== extra:"
( __EXTRA__ ) 2>&1 | head -n 60
exit 0
