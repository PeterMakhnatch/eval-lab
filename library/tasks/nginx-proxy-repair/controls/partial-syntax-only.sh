#!/bin/bash
# expect: 0
# fixes only the fault nginx -t reports; /api still 502, /static still 404
CONF=/etc/nginx/sites-enabled/app.conf
sed -i 's/^\(\s*client_max_body_size 10m\)$/\1;/' "$CONF" 
nginx -t && nginx
