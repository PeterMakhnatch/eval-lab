#!/usr/bin/env bash
# envcheck: capture-base
# expect: 0
set -e
mkdir -p /logs/artifacts/envcheck-capture/.ref-subset-states
if [ -L / ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -L /app ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -L /app/merged_users.parquet ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -e /app/merged_users.parquet ] && [ ! -f /app/merged_users.parquet ]; then echo 'ref-subset: target is not a regular file' >&2; exit 97; fi
if [ -f /app/merged_users.parquet ]; then printf 'present\n'; else printf 'absent\n'; fi > /logs/artifacts/envcheck-capture/.ref-subset-states/0
if [ -f /app/merged_users.parquet ]; then stat -c '%a' -- /app/merged_users.parquet > /logs/artifacts/envcheck-capture/.ref-subset-states/0.mode; fi
if [ -L / ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -L /app ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -L /app/conflicts.json ]; then echo 'ref-subset: symlink target' >&2; exit 97; fi
if [ -e /app/conflicts.json ] && [ ! -f /app/conflicts.json ]; then echo 'ref-subset: target is not a regular file' >&2; exit 97; fi
if [ -f /app/conflicts.json ]; then printf 'present\n'; else printf 'absent\n'; fi > /logs/artifacts/envcheck-capture/.ref-subset-states/1
if [ -f /app/conflicts.json ]; then stat -c '%a' -- /app/conflicts.json > /logs/artifacts/envcheck-capture/.ref-subset-states/1.mode; fi
#!/usr/bin/env bash
# envcheck: capture-base
# expect: 0
if [ -e /app/merged_users.parquet ]; then mkdir -p /logs/artifacts/envcheck-capture/app && cp -a /app/merged_users.parquet /logs/artifacts/envcheck-capture/app/merged_users.parquet; fi
if [ -e /app/conflicts.json ]; then mkdir -p /logs/artifacts/envcheck-capture/app && cp -a /app/conflicts.json /logs/artifacts/envcheck-capture/app/conflicts.json; fi
printf 'complete\n' > /logs/artifacts/envcheck-capture/.ref-subset-states/complete
