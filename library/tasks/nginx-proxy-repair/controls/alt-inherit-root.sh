#!/bin/bash
# expect: 1
# different valid repair: drop the location-level root so the server root applies
CONF=/etc/nginx/sites-enabled/app.conf
sed -i 's/^\(\s*client_max_body_size 10m\)$/\1;/' "$CONF" 
sed -i 's/server 127\.0\.0\.1:8080;/server 127.0.0.1:8081;/' "$CONF" 
sed -i '/^\s*root \/var\/www\/app\/static;$/d' "$CONF"
nginx -t && nginx
