#!/bin/bash
# Oracle solution: the three faults in /etc/nginx/sites-enabled/app.conf.
set -euo pipefail

CONF=/etc/nginx/sites-enabled/app.conf

# 1. `nginx -t` fails: client_max_body_size is missing its semicolon.
sed -i 's/^\(\s*client_max_body_size 10m\)$/\1;/' "$CONF"

# 2. /api/* returns 502: upstream points at 8080, the backend listens on 8081.
sed -i 's/server 127\.0\.0\.1:8080;/server 127.0.0.1:8081;/' "$CONF"

# 3. /static/* returns 404: `root` inside `location /static/` appends the
#    location path again (/var/www/app/static/static/...). Use `alias`.
sed -i 's#root /var/www/app/static;#alias /var/www/app/static/;#' "$CONF"

nginx -t
if pgrep -x nginx >/dev/null; then
  nginx -s reload
else
  nginx
fi
