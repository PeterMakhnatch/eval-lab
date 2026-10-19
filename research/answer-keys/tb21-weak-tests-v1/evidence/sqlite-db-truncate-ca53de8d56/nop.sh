#!/usr/bin/env bash
# envcheck: capture-base
# expect: 0
set -e
mkdir -p /logs/artifacts/envcheck-capture/.ref-subset-states
if [ -L / ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -L /app ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -L /app/recover.json ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -e /app/recover.json ] && [ ! -f /app/recover.json ]; then echo 'ref-subset: target is not a regular file' >&2; exit 97; fi
if [ -f /app/recover.json ]; then printf 'present\n'; else printf 'absent\n'; fi > /logs/artifacts/envcheck-capture/.ref-subset-states/0
if [ -f /app/recover.json ]; then stat -c '%a' -- /app/recover.json > /logs/artifacts/envcheck-capture/.ref-subset-states/0.mode; fi
#!/usr/bin/env bash
# envcheck: capture-base
# expect: 0
if [ -e /app/recover.json ]; then mkdir -p /logs/artifacts/envcheck-capture/app && cp -a /app/recover.json /logs/artifacts/envcheck-capture/app/recover.json; fi
printf 'complete\n' > /logs/artifacts/envcheck-capture/.ref-subset-states/complete
