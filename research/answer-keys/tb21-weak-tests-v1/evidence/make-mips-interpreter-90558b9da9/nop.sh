#!/usr/bin/env bash
# envcheck: capture-base
# expect: 0
set -e
mkdir -p /logs/artifacts/envcheck-capture/.ref-subset-states
if [ -L / ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -L /app ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -L /app/vm.js ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -e /app/vm.js ] && [ ! -f /app/vm.js ]; then echo 'ref-subset: target is not a regular file' >&2; exit 97; fi
if [ -f /app/vm.js ]; then printf 'present\n'; else printf 'absent\n'; fi > /logs/artifacts/envcheck-capture/.ref-subset-states/0
if [ -f /app/vm.js ]; then stat -c '%a' -- /app/vm.js > /logs/artifacts/envcheck-capture/.ref-subset-states/0.mode; fi
if [ -L / ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -L /app ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -L /app/memory.js ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -e /app/memory.js ] && [ ! -f /app/memory.js ]; then echo 'ref-subset: target is not a regular file' >&2; exit 97; fi
if [ -f /app/memory.js ]; then printf 'present\n'; else printf 'absent\n'; fi > /logs/artifacts/envcheck-capture/.ref-subset-states/1
if [ -f /app/memory.js ]; then stat -c '%a' -- /app/memory.js > /logs/artifacts/envcheck-capture/.ref-subset-states/1.mode; fi
#!/usr/bin/env bash
# envcheck: capture-base
# expect: 0
if [ -e /app/vm.js ]; then mkdir -p /logs/artifacts/envcheck-capture/app && cp -a /app/vm.js /logs/artifacts/envcheck-capture/app/vm.js; fi
if [ -e /app/memory.js ]; then mkdir -p /logs/artifacts/envcheck-capture/app && cp -a /app/memory.js /logs/artifacts/envcheck-capture/app/memory.js; fi
printf 'complete\n' > /logs/artifacts/envcheck-capture/.ref-subset-states/complete
