#!/bin/bash
# expect: 0
# Real health/404 proxying, but incomplete items and bogus query results.
set -euo pipefail

CONF=/etc/nginx/sites-enabled/app.conf
sed -i 's/^\(\s*client_max_body_size 10m\)$/\1;/' "$CONF"
sed -i 's/server 127\.0\.0\.1:8080;/server 127.0.0.1:8081;/' "$CONF"
sed -i 's#root /var/www/app/static;#alias /var/www/app/static/;#' "$CONF"
python3 - <<'PY'
from pathlib import Path

path = Path("/etc/nginx/sites-enabled/app.conf")
config = path.read_text()
location = '''    location = /api/items {
        default_type application/json;
        if ($args != "") {
            return 200 '[null,null]';
        }
        return 200 '[{"name":"alpha"},{"name":"beta"},{"name":"gamma"}]';
    }

'''
assert "    location /api/ {" in config
path.write_text(config.replace("    location /api/ {", location + "    location /api/ {", 1))
PY
nginx -t
