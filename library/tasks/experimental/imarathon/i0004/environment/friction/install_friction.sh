#!/bin/sh
# Deterministic fault-injection overlay for the AGENT environment only.
# - dump-style readers (cat/head/tail/less) truncate stdout after 2 KiB
# - sqlite3 fails exactly once per boot with a diagnosable transient lock
# The separate verifier container never installs this layer.
set -eu

install -d /usr/local/lib/friction

cat > /usr/local/lib/friction/truncate.py <<'PY'
import os, subprocess, sys
LIMIT = 2048
real = {"cat": "/usr/bin/cat", "head": "/usr/bin/head", "tail": "/usr/bin/tail", "less": "/usr/bin/less"}
tool = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in real else "cat"
proc = subprocess.run([real[tool], *sys.argv[2:]], capture_output=True)
out = proc.stdout
if len(out) > LIMIT:
    out = out[:LIMIT] + (
        b"\n[truncated by host policy: showing 2048 of "
        + str(len(proc.stdout)).encode()
        + b" bytes; use grep/awk/sed or slice files programmatically]\n"
    )
sys.stdout.buffer.write(out)
sys.stderr.buffer.write(proc.stderr)
sys.exit(proc.returncode)
PY

for tool in cat head tail less; do
    cat > /usr/local/bin/$tool <<EOF
#!/bin/sh
exec python3 /usr/local/lib/friction/truncate.py $tool "\$@"
EOF
    chmod +x /usr/local/bin/$tool
done

cat > /usr/local/bin/sqlite3 <<'EOF'
#!/bin/sh
# One-shot transient failure per boot; deterministic, diagnosable, retryable.
MARK=/var/tmp/.friction-sqlite-seen
if [ ! -f "$MARK" ]; then
    touch "$MARK"
    echo "Error: database is locked (transient fault on this host; safe to retry)" >&2
    exit 75
fi
exec /usr/bin/sqlite3 "$@"
EOF
chmod +x /usr/local/bin/sqlite3
