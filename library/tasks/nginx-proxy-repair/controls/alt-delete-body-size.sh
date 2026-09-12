#!/bin/bash
# expect: 1
# Valid alternate repair: removes the erroneous client_max_body_size directive
# entirely (falling back to nginx default 1m), fixes the backend upstream port to
# 8081, and configures static asset alias.
CONF=/etc/nginx/sites-enabled/app.conf

# 1. Delete the erroneous client_max_body_size line instead of adding a semicolon
sed -i '/client_max_body_size/d' "$CONF"

# 2. Fix backend port from 8080 to 8081
sed -i 's/server 127\.0\.0\.1:8080;/server 127.0.0.1:8081;/' "$CONF"

# 3. Fix static routing using alias
sed -i 's#root /var/www/app/static;#alias /var/www/app/static/;#' "$CONF"

nginx -t
if pgrep -x nginx >/dev/null; then
  nginx -s reload
else
  nginx
fi
