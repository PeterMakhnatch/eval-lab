#!/usr/bin/env bash
# envcheck: capture-base
# expect: 0
set -e
mkdir -p /logs/artifacts/envcheck-capture/.ref-subset-states
if [ -L / ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -L /app ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -L /app/program.py ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -e /app/program.py ] && [ ! -f /app/program.py ]; then echo 'ref-subset: target is not a regular file' >&2; exit 97; fi
if [ -f /app/program.py ]; then printf 'present\n'; else printf 'absent\n'; fi > /logs/artifacts/envcheck-capture/.ref-subset-states/0
if [ -f /app/program.py ]; then stat -c '%a' -- /app/program.py > /logs/artifacts/envcheck-capture/.ref-subset-states/0.mode; fi
#!/usr/bin/env bash
# envcheck: capture-base
# expect: 0
if [ -e /app/program.py ]; then mkdir -p /logs/artifacts/envcheck-capture/app && cp -a /app/program.py /logs/artifacts/envcheck-capture/app/program.py; fi
printf 'complete\n' > /logs/artifacts/envcheck-capture/.ref-subset-states/complete
