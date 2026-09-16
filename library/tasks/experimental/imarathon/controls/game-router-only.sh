#!/bin/bash
# expect: 0.15
# Partial-work control: repairs only the router, produces no deliverables.
# Passes exactly the 3 router gates; hygiene stays uncredited because no
# deliverables were submitted. Container-only; never run on host.
set -euo pipefail
cd /opt/platform

GOOD_PORT=$(awk '/upstream_port:/ {print $2}' conf/manifest.yaml)
sed "s/^\( *client_max_body_size [0-9]\+k\)$/\1;/" router/nginx.conf > router/nginx.conf.new
mv router/nginx.conf.new router/nginx.conf
sed "s/^\( *server 127\.0\.0\.1:\)[0-9]\+;/\1${GOOD_PORT};/" router/nginx.conf > router/nginx.conf.new
mv router/nginx.conf.new router/nginx.conf
sed "s|^\( *root \)/opt/platform/router/html/static/;|alias /opt/platform/router/html/static/;|" router/nginx.conf > router/nginx.conf.new
mv router/nginx.conf.new router/nginx.conf
printf 'IMARATHON_CONTROL router-only setup complete\n'
