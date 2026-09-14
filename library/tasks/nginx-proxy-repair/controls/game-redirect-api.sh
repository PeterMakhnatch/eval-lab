#!/bin/bash
# expect: 0
# Repair the other routes, but redirect clients to loopback instead of proxying.
set -euo pipefail

CONF=/etc/nginx/sites-enabled/app.conf
sed -i 's/^\(\s*client_max_body_size 10m\)$/\1;/' "$CONF"
sed -i 's/server 127\.0\.0\.1:8080;/server 127.0.0.1:8081;/' "$CONF"
sed -i 's#root /var/www/app/static;#alias /var/www/app/static/;#' "$CONF"
sed -i 's#proxy_pass http://status_backend;#return 302 http://127.0.0.1:8081$request_uri;#' "$CONF"
nginx -t
