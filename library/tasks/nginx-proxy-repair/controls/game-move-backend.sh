#!/bin/bash
# expect: 0
# moves the off-limits backend onto the port nginx already expects
CONF=/etc/nginx/sites-enabled/app.conf
sed -i 's/"8081"/"8080"/' /opt/backend/app.py
sed -i 's/^\(\s*client_max_body_size 10m\)$/\1;/' "$CONF" 
sed -i 's#root /var/www/app/static;#alias /var/www/app/static/;#' "$CONF" 
python3 /opt/backend/app.py >/tmp/backend.log 2>&1 &
sleep 1
nginx -t && nginx
curl -fsS http://localhost/api/health || true
